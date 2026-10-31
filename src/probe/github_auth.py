"""GitHub App authentication helpers: JWT generation and installation tokens.

Provides credentials for Probe when running as a registered GitHub App.
Also supports fallback to GITHUB_TOKEN for personal tokens, CI, and local dev.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
import jwt

from probe.github_client import API_URL, USER_AGENT, GitHubClient, GitHubError

_TOKEN_CACHE: dict[int, tuple[str, float]] = {}  # installation_id -> (token, expires_at_timestamp)


def create_app_jwt(app_id: str | int, private_key: str | bytes) -> str:
    """Create an RS256 JWT for GitHub App authentication (valid for 10 minutes)."""
    now = int(time.time())
    payload = {
        "iat": now - 60,          # 60s in the past to allow for clock drift
        "exp": now + (10 * 60),   # 10m is the GitHub API max
        "iss": str(app_id),
    }
    key_bytes = private_key.encode("utf-8") if isinstance(private_key, str) else private_key
    return jwt.encode(payload, key_bytes, algorithm="RS256")


def load_private_key() -> str | None:
    """Load the private key from GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH."""
    key = os.environ.get("GITHUB_PRIVATE_KEY")
    if key:
        return key.strip()

    key_path = os.environ.get("GITHUB_PRIVATE_KEY_PATH")
    if key_path:
        path = Path(key_path)
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()

    return None


def get_installation_token(
    app_id: str | int,
    private_key: str | bytes,
    installation_id: int,
    *,
    base_url: str = API_URL,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Obtain an installation access token for a GitHub App installation."""
    now = time.time()
    cached = _TOKEN_CACHE.get(installation_id)
    if cached and cached[1] > now + 60:
        return cached[0]

    app_jwt = create_app_jwt(app_id, private_key)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {app_jwt}",
        "User-Agent": USER_AGENT,
    }

    url = f"{base_url.rstrip('/')}/app/installations/{int(installation_id)}/access_tokens"
    try:
        with httpx.Client(transport=transport, timeout=15.0) as client:
            resp = client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        raise GitHubError(f"Network error obtaining installation token: {exc.__class__.__name__}") from exc

    if resp.status_code not in (200, 201):
        raise GitHubError(f"Failed to obtain installation token (HTTP {resp.status_code}): {resp.text[:200]}")

    data: dict[str, Any] = resp.json()
    token = data["token"]
    # Installation tokens typically expire in 1 hour; cache for 50 minutes (3000s)
    _TOKEN_CACHE[installation_id] = (token, now + 3000.0)
    return token


def get_client_for_installation(
    installation_id: int | None = None,
    *,
    base_url: str = API_URL,
    transport: httpx.BaseTransport | None = None,
) -> GitHubClient:
    """Construct a GitHubClient authenticated for the given installation, or fallback to GITHUB_TOKEN."""
    # 1. GITHUB_TOKEN override (for local dev, GitHub Actions, or direct testing)
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return GitHubClient(token=token, base_url=base_url, transport=transport)

    # 2. GitHub App credentials
    app_id = os.environ.get("GITHUB_APP_ID")
    private_key = load_private_key()

    if app_id and private_key and installation_id is not None:
        inst_token = get_installation_token(
            app_id, private_key, installation_id, base_url=base_url, transport=transport
        )
        return GitHubClient(token=inst_token, base_url=base_url, transport=transport)

    # 3. Fallback: unauthenticated client (public read-only)
    return GitHubClient(base_url=base_url, transport=transport)
