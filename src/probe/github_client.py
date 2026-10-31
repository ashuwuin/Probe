"""Read-only GitHub client: fetch issue / PR context and immutable repo snapshots.

Used by the CLI and the evaluation harness. It only performs GET requests and
never writes to GitHub. Public repositories work without credentials; pass a
token (or set ``GITHUB_TOKEN``) for higher rate limits.

Key guarantees:

* Every snapshot is pinned to a full 40-character commit SHA and cached by it,
  so a cached snapshot can never be stale.
* For issues, ``commit_at`` resolves the default branch as it was when the
  issue was opened, so old (closed) issues are checked against the code the
  reporter actually saw.
* Tarballs are extracted with ``tarfile``'s ``data`` filter (no absolute
  paths, no ``..``, no links escaping the tree, no device files) plus size and
  file-count limits.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

API_URL = "https://api.github.com"
USER_AGENT = "probe-triage (+https://github.com/ashuwuin/Probe)"
DEFAULT_TIMEOUT = 30.0
MAX_TARBALL_BYTES = 200 * 1024 * 1024      # compressed download limit
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024    # sum of extracted file sizes
MAX_EXTRACTED_FILES = 50_000
MAX_DIFF_BYTES = 5 * 1024 * 1024
MAX_PR_FILES = 3_000                       # GitHub's own cap for the files endpoint

_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})/"
    r"(?P<kind>issues|pull)/(?P<number>[1-9][0-9]{0,9})"
    r"(?:[/?#].*)?$"
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFoundError(GitHubError):
    """404: repository, issue or commit does not exist (or is private)."""


class RateLimitError(GitHubError):
    """403/429 caused by GitHub rate limits."""

    def __init__(self, message: str, status: int | None = None, reset_at: int | None = None) -> None:
        super().__init__(message, status)
        self.reset_at = reset_at


class InvalidURLError(ValueError):
    """The input is not a github.com issue or pull request URL."""


# --------------------------------------------------------------------------
# Data returned by the client
# --------------------------------------------------------------------------

class GitHubTarget(BaseModel):
    owner: str
    repo: str
    kind: Literal["issue", "pull_request"]
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


class IssueData(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    body: str = ""
    state: str
    state_reason: str | None = None
    labels: list[str] = Field(default_factory=list)
    author: str | None = None
    created_at: datetime
    closed_at: datetime | None = None
    html_url: str
    is_pull_request: bool = False


class PullData(IssueData):
    head_sha: str
    base_sha: str
    base_ref: str
    changed_files: list[str] = Field(default_factory=list)
    changed_files_truncated: bool = False
    diff: str = ""
    diff_truncated: bool = False
    merged: bool = False


class Snapshot(BaseModel):
    """A repository extracted at one immutable commit."""

    owner: str
    repo: str
    sha: str
    path: Path
    resolved_by: str = Field(description="How the SHA was chosen, e.g. 'pr_head' or 'default_branch_at:<ts>'")
    from_cache: bool = False


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_github_url(url: str) -> GitHubTarget:
    """Parse ``https://github.com/<owner>/<repo>/(issues|pull)/<n>``."""
    match = _URL_RE.match(url.strip())
    if not match:
        raise InvalidURLError(f"Not a GitHub issue or pull request URL: {url[:200]!r}")
    repo = match["repo"]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if repo in (".", ".."):
        raise InvalidURLError("Invalid repository name")
    return GitHubTarget(
        owner=match["owner"],
        repo=repo,
        kind="pull_request" if match["kind"] == "pull" else "issue",
        number=int(match["number"]),
    )


def _check_name(value: str, what: str) -> str:
    if not _NAME_RE.match(value) or value in (".", ".."):
        raise ValueError(f"Invalid {what}: {value!r}")
    return value


def _check_sha(sha: str) -> str:
    if not _SHA_RE.match(sha):
        raise ValueError(f"Not a full 40-character commit SHA: {sha!r}")
    return sha


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def default_cache_dir() -> Path:
    base = os.environ.get("PROBE_CACHE_DIR")
    return Path(base) if base else Path.home() / ".cache" / "probe" / "repos"


def safe_extract_tarball(data: bytes, dest: Path) -> int:
    """Extract a GitHub tarball into ``dest``, dropping the top-level folder.

    Returns the number of regular files written. Raises GitHubError when the
    archive exceeds the size / count limits or contains unsafe members.
    """
    total = 0
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = []
        for member in tar:
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue  # the top-level "<owner>-<repo>-<sha>/" directory itself
            if member.isfile():
                count += 1
                total += member.size
                if count > MAX_EXTRACTED_FILES or total > MAX_EXTRACTED_BYTES:
                    raise GitHubError("Repository snapshot exceeds Probe's size limits")
            elif not (member.isdir() or member.issym()):
                continue  # skip hard links, devices, fifos
            member.name = parts[1]
            members.append(member)
        try:
            tar.extractall(dest, members=members, filter="data")
        except tarfile.FilterError as exc:
            raise GitHubError(f"Unsafe archive member rejected: {exc}") from exc
    return count


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class GitHubClient:
    """Thin, read-only wrapper around the GitHub REST API."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = API_URL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self.cache_dir = cache_dir or default_cache_dir()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- low level ---------------------------------------------------------

    def _get(self, path: str, *, params: dict | None = None, accept: str | None = None) -> httpx.Response:
        headers = {"Accept": accept} if accept else None
        try:
            response = self._http.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Network error calling GitHub: {exc.__class__.__name__}") from exc
        if response.status_code == 200:
            return response
        if response.status_code == 404:
            raise NotFoundError(f"Not found: {path}", 404)
        remaining = response.headers.get("x-ratelimit-remaining")
        if response.status_code == 429 or (response.status_code == 403 and remaining == "0"):
            reset = response.headers.get("x-ratelimit-reset")
            raise RateLimitError(
                "GitHub API rate limit exceeded; set GITHUB_TOKEN or wait for the reset",
                response.status_code,
                int(reset) if reset and reset.isdigit() else None,
            )
        raise GitHubError(f"GitHub returned HTTP {response.status_code} for {path}", response.status_code)

    def _json(self, path: str, **params) -> dict | list:
        return self._get(path, params=params or None).json()

    # ---- repository --------------------------------------------------------

    def default_branch(self, owner: str, repo: str) -> str:
        data = self._json(f"/repos/{_check_name(owner, 'owner')}/{_check_name(repo, 'repo')}")
        return data["default_branch"]

    def commit_at(self, owner: str, repo: str, when: datetime, ref: str | None = None) -> str:
        """SHA of the last commit on ``ref`` (default branch) at or before ``when``."""
        _check_name(owner, "owner")
        _check_name(repo, "repo")
        ref = ref or self.default_branch(owner, repo)
        if when.tzinfo is None:
            raise ValueError("'when' must be timezone-aware")
        until = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commits = self._json(f"/repos/{owner}/{repo}/commits", sha=ref, until=until, per_page=1)
        if not commits:
            raise NotFoundError(f"No commit on {ref} before {until}", 404)
        return _check_sha(commits[0]["sha"])

    def resolve_ref(self, owner: str, repo: str, ref: str) -> str:
        """Resolve a tag/branch name to a full commit SHA."""
        data = self._json(f"/repos/{_check_name(owner, 'owner')}/{_check_name(repo, 'repo')}/commits/{ref}")
        return _check_sha(data["sha"])

    # ---- issues and pull requests -----------------------------------------

    def get_issue(self, owner: str, repo: str, number: int) -> IssueData:
        data = self._json(f"/repos/{_check_name(owner, 'owner')}/{_check_name(repo, 'repo')}/issues/{int(number)}")
        return IssueData(
            owner=owner,
            repo=repo,
            number=data["number"],
            title=data.get("title") or "",
            body=data.get("body") or "",
            state=data.get("state", "unknown"),
            state_reason=data.get("state_reason"),
            labels=sorted(label["name"] for label in data.get("labels", []) if isinstance(label, dict)),
            author=(data.get("user") or {}).get("login"),
            created_at=_parse_time(data["created_at"]),
            closed_at=_parse_time(data.get("closed_at")),
            html_url=data.get("html_url", ""),
            is_pull_request="pull_request" in data,
        )

    def get_pull(self, owner: str, repo: str, number: int, *, include_diff: bool = True) -> PullData:
        _check_name(owner, "owner")
        _check_name(repo, "repo")
        issue = self.get_issue(owner, repo, number)
        pr = self._json(f"/repos/{owner}/{repo}/pulls/{int(number)}")

        files: list[str] = []
        page = 1
        truncated = False
        while True:
            batch = self._json(f"/repos/{owner}/{repo}/pulls/{int(number)}/files", per_page=100, page=page)
            files.extend(f["filename"] for f in batch)
            if len(batch) < 100:
                break
            if len(files) >= MAX_PR_FILES:
                truncated = True
                break
            page += 1

        diff, diff_truncated = "", False
        if include_diff:
            raw = self._get(f"/repos/{owner}/{repo}/pulls/{int(number)}", accept="application/vnd.github.diff").text
            if len(raw.encode()) > MAX_DIFF_BYTES:
                raw = raw.encode()[:MAX_DIFF_BYTES].decode(errors="ignore")
                diff_truncated = True
            diff = raw

        return PullData(
            **issue.model_dump(exclude={"is_pull_request"}),
            is_pull_request=True,
            head_sha=_check_sha(pr["head"]["sha"]),
            base_sha=_check_sha(pr["base"]["sha"]),
            base_ref=pr["base"]["ref"],
            changed_files=files,
            changed_files_truncated=truncated,
            diff=diff,
            diff_truncated=diff_truncated,
            merged=bool(pr.get("merged")),
        )

    # ---- snapshots ---------------------------------------------------------

    def snapshot(self, owner: str, repo: str, sha: str, *, resolved_by: str) -> Snapshot:
        """Return the repository at ``sha``, downloading and caching it if needed."""
        _check_name(owner, "owner")
        _check_name(repo, "repo")
        _check_sha(sha)
        dest = self.cache_dir / owner.lower() / repo.lower() / sha
        if (dest / ".probe-complete").exists():
            return Snapshot(owner=owner, repo=repo, sha=sha, path=dest, resolved_by=resolved_by, from_cache=True)

        response = self._get(f"/repos/{owner}/{repo}/tarball/{sha}")
        if len(response.content) > MAX_TARBALL_BYTES:
            raise GitHubError("Repository tarball exceeds Probe's download limit")

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".{sha[:12]}-", dir=dest.parent))
        try:
            safe_extract_tarball(response.content, tmp)
            (tmp / ".probe-complete").write_text(f"{owner}/{repo}@{sha}\n")
            if dest.exists():  # a previous, incomplete attempt
                shutil.rmtree(dest)
            tmp.rename(dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return Snapshot(owner=owner, repo=repo, sha=sha, path=dest, resolved_by=resolved_by)

    def snapshot_for_issue(self, issue: IssueData) -> Snapshot:
        """The default branch as it was when the issue was opened."""
        sha = self.commit_at(issue.owner, issue.repo, issue.created_at)
        return self.snapshot(
            issue.owner, issue.repo, sha,
            resolved_by=f"default_branch_at:{issue.created_at.isoformat()}",
        )

    def snapshot_for_pull(self, pull: PullData) -> Snapshot:
        """The PR's head commit."""
        return self.snapshot(pull.owner, pull.repo, pull.head_sha, resolved_by="pr_head")
