"""Tests for probe.github_auth (GitHub App JWT and token management)."""

import time
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from probe import github_auth
from probe.github_auth import (
    create_app_jwt,
    get_client_for_installation,
    get_installation_token,
    load_private_key,
)
from probe.github_client import GitHubError


@pytest.fixture(scope="module")
def rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return pem, public_pem


def test_create_app_jwt(rsa_keys):
    priv_pem, pub_pem = rsa_keys
    token = create_app_jwt("123456", priv_pem)
    decoded = jwt.decode(token, pub_pem, algorithms=["RS256"])
    assert decoded["iss"] == "123456"
    assert decoded["exp"] > time.time()
    assert decoded["iat"] <= time.time()


def test_load_private_key_env_var(monkeypatch, rsa_keys):
    priv_pem, _ = rsa_keys
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", priv_pem)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH", raising=False)
    assert load_private_key() == priv_pem.strip()


def test_load_private_key_path(monkeypatch, tmp_path, rsa_keys):
    priv_pem, _ = rsa_keys
    key_file = tmp_path / "app.pem"
    key_file.write_text(priv_pem)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(key_file))
    assert load_private_key() == priv_pem.strip()


def test_load_private_key_none(monkeypatch):
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH", raising=False)
    assert load_private_key() is None


def test_get_installation_token_success_and_cache(rsa_keys, monkeypatch):
    priv_pem, _ = rsa_keys
    github_auth._TOKEN_CACHE.clear()

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/app/installations/999/access_tokens" and request.method == "POST":
            auth = request.headers.get("authorization", "")
            assert auth.startswith("Bearer ")
            return httpx.Response(201, json={"token": "ghs_test_token_12345", "expires_at": "2030-01-01T00:00:00Z"})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)

    token1 = get_installation_token(12345, priv_pem, 999, transport=transport)
    assert token1 == "ghs_test_token_12345"
    assert len(calls) == 1

    # Second call uses cache, no additional HTTP request
    token2 = get_installation_token(12345, priv_pem, 999, transport=transport)
    assert token2 == "ghs_test_token_12345"
    assert len(calls) == 1


def test_get_installation_token_failure(rsa_keys):
    priv_pem, _ = rsa_keys
    github_auth._TOKEN_CACHE.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(GitHubError, match="Failed to obtain installation token"):
        get_installation_token(12345, priv_pem, 888, transport=transport)


def test_get_client_for_installation_with_github_token(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_direct_token")
    client = get_client_for_installation(123)
    assert client._http.headers.get("authorization") == "Bearer ghp_direct_token"


def test_app_credentials_preempt_github_token_for_installation(monkeypatch, rsa_keys):
    priv_pem, _ = rsa_keys
    github_auth._TOKEN_CACHE.clear()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_personal_token_should_not_be_used")
    monkeypatch.setenv("GITHUB_APP_ID", "4242")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", priv_pem)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/777/access_tokens":
            return httpx.Response(201, json={"token": "ghs_app_inst_token"})
        return httpx.Response(404, json={})

    client = get_client_for_installation(777, transport=httpx.MockTransport(handler))
    assert client._http.headers.get("authorization") == "Bearer ghs_app_inst_token"


def test_get_client_for_installation_with_app_credentials(monkeypatch, rsa_keys):
    priv_pem, _ = rsa_keys
    github_auth._TOKEN_CACHE.clear()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "4242")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", priv_pem)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/777/access_tokens":
            return httpx.Response(201, json={"token": "ghs_app_inst_token"})
        return httpx.Response(404, json={})

    client = get_client_for_installation(777, transport=httpx.MockTransport(handler))
    assert client._http.headers.get("authorization") == "Bearer ghs_app_inst_token"


def test_get_client_for_installation_fallback_unauth(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH", raising=False)

    client = get_client_for_installation(None)
    assert "authorization" not in client._http.headers
