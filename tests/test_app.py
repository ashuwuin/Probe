"""Tests for probe.app (FastAPI GitHub App webhook receiver & worker)."""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from probe import app as app_module
from probe.app import app, process_triage_job, verify_webhook_signature
from probe.github_client import IssueData, PullData, Snapshot
from probe.models import Claim, ClaimsResponse, VerdictResult

SECRET = "super_webhook_secret_123"


def sign_payload(payload_bytes: bytes, secret: str = SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PROBE_INSECURE_DEV", "0")
    app_module._ACTIVE_JOBS.clear()
    return TestClient(app)


# ---------------------------------------------------------------------------
# Signature Verification Tests
# ---------------------------------------------------------------------------

def test_verify_signature_valid():
    body = b'{"action": "opened"}'
    sig = sign_payload(body, SECRET)
    assert verify_webhook_signature(body, sig, SECRET) is True


def test_verify_signature_invalid():
    body = b'{"action": "opened"}'
    sig = sign_payload(body, "wrong_secret")
    assert verify_webhook_signature(body, sig, SECRET) is False


def test_verify_signature_tampered_payload():
    body = b'{"action": "opened"}'
    sig = sign_payload(body, SECRET)
    assert verify_webhook_signature(b'{"action": "closed"}', sig, SECRET) is False


def test_verify_signature_missing_header():
    assert verify_webhook_signature(b"data", None, SECRET) is False
    assert verify_webhook_signature(b"data", "", SECRET) is False
    assert verify_webhook_signature(b"data", "not_sha256", SECRET) is False


def test_verify_signature_dev_mode(monkeypatch):
    monkeypatch.setenv("PROBE_INSECURE_DEV", "1")
    assert verify_webhook_signature(b"data", None, None) is True
    monkeypatch.setenv("PROBE_INSECURE_DEV", "0")
    assert verify_webhook_signature(b"data", None, None) is False


# ---------------------------------------------------------------------------
# Route & Event Handling Tests
# ---------------------------------------------------------------------------

def test_healthcheck(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "service": "probe", "version": "0.1.0"}

    res2 = client.get("/")
    assert res2.status_code == 200
    assert res2.json()["service"] == "probe"


def test_webhook_unauthorized_when_signature_bad(client):
    res = client.post(
        "/webhook",
        content=b'{"test": 1}',
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=invalid"},
    )
    assert res.status_code == 401
    assert "Invalid webhook signature" in res.json()["detail"]


def test_webhook_missing_event_header(client):
    body = b'{"test": 1}'
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 400
    assert "Missing X-GitHub-Event header" in res.json()["detail"]


def test_webhook_ping_event(client):
    body = json.dumps({"zen": "Keep it logically awesome."}).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.json()["message"] == "pong"
    assert res.json()["zen"] == "Keep it logically awesome."


def test_webhook_issues_opened(client):
    payload = {
        "action": "opened",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 42, "updated_at": "2026-11-01T00:00:00Z"},
        "installation": {"id": 12345},
    }
    body = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "accepted"
    assert data["target"] == "octo/demo#42"
    assert data["event"] == "issues"


def test_webhook_issues_deduplication(client):
    payload = {
        "action": "opened",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 42, "updated_at": "2026-11-01T00:00:00Z"},
        "installation": {"id": 12345},
    }
    body = json.dumps(payload).encode("utf-8")

    # Simulate in-flight active job
    app_module._ACTIVE_JOBS.add("octo/demo#42@2026-11-01T00:00:00Z")

    # Request with same state returns already_queued
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 202
    assert res.json()["status"] == "already_queued"


def test_webhook_issues_ignored_action(client):
    payload = {
        "action": "closed",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 42},
    }
    body = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"


def test_webhook_pull_request_synchronize(client):
    payload = {
        "action": "synchronize",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "pull_request": {"number": 99, "head": {"sha": "1234567890abcdef" * 2 + "12345678"}},
        "installation": {"id": 54321},
    }
    body = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "accepted"
    assert data["event"] == "pull_request"
    assert data["target"] == "octo/demo#99"


def test_webhook_issue_comment_rerun_command(client):
    payload = {
        "action": "created",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 15},
        "comment": {"id": 1001, "body": "/probe rerun please", "user": {"login": "alice", "type": "User"}},
        "installation": {"id": 12345},
    }
    body = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "accepted"
    assert data["command"] == "rerun"
    assert data["target"] == "octo/demo#15"


def test_webhook_issue_comment_ignored_bot(client):
    payload = {
        "action": "created",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 15},
        "comment": {"id": 1002, "body": "/probe rerun", "user": {"login": "probe[bot]", "type": "Bot"}},
    }
    body = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert "bot" in res.json()["reason"]


def test_webhook_issue_comment_ignored_no_command(client):
    payload = {
        "action": "created",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 15},
        "comment": {"id": 1003, "body": "looks good to me", "user": {"login": "alice", "type": "User"}},
    }
    body = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert "no /probe command" in res.json()["reason"]


# ---------------------------------------------------------------------------
# Background Triage Task Execution Tests
# ---------------------------------------------------------------------------

def test_process_triage_job_issue_posts_new_comment(tmp_path):
    mock_gh = MagicMock()
    mock_gh.__enter__.return_value = mock_gh

    mock_issue = MagicMock()
    mock_issue.title = "Bug in billing"
    mock_issue.body = "apply_discount fails"
    mock_gh.get_issue.return_value = mock_issue

    mock_snapshot = MagicMock()
    mock_snapshot.path = tmp_path
    mock_gh.snapshot_for_issue.return_value = mock_snapshot

    mock_gh.list_comments.return_value = []
    mock_gh.post_comment.return_value = {"id": 777}

    with patch("probe.app.run_triage_pipeline") as mock_pipeline:
        mock_pipeline.return_value = (
            ClaimsResponse(claims=[Claim(id="c1", type="bug_behavior", quote="apply_discount fails")]),
            [],
            [VerdictResult(claim_id="c1", status="uncertain", confidence="low", evidence="no repro", quote="apply_discount fails")],
            "uncertain",
            "Insufficient evidence",
        )

        result = process_triage_job(
            owner="octo",
            repo="demo",
            number=42,
            is_pr=False,
            dedup_key="octo/demo#42@v1",
            client=mock_gh,
        )

        assert result["status"] == "success"
        assert result["action"] == "posted"
        assert result["comment_id"] == 777
        assert result["overall"] == "uncertain"

        mock_gh.post_comment.assert_called_once()
        mock_gh.add_labels.assert_called_once_with("octo", "demo", 42, ["triage:uncertain"])


def test_process_triage_job_pr_updates_existing_comment(tmp_path):
    mock_gh = MagicMock()
    mock_gh.__enter__.return_value = mock_gh

    mock_pull = MagicMock()
    mock_pull.title = "Add tests"
    mock_pull.body = "Adds tests for auth"
    mock_pull.diff = "diff --git a/test.py"
    mock_pull.changed_files = ["test.py"]
    mock_gh.get_pull.return_value = mock_pull

    mock_snapshot = MagicMock()
    mock_snapshot.path = tmp_path
    mock_gh.snapshot_for_pull.return_value = mock_snapshot

    mock_gh.list_comments.return_value = [
        {
            "id": 888,
            "body": "<!-- probe-report -->\n### Old Report",
            "user": {"login": "probe[bot]", "type": "Bot"},
        }
    ]
    mock_gh.update_comment.return_value = {"id": 888}

    with patch("probe.app.run_triage_pipeline") as mock_pipeline:
        mock_pipeline.return_value = (
            ClaimsResponse(claims=[Claim(id="c1", type="pr_adds_tests", quote="Adds tests for auth")]),
            [],
            [VerdictResult(claim_id="c1", status="confirmed", confidence="high", evidence="tests found", quote="Adds tests for auth")],
            "confirmed",
            "Tests added",
        )

        result = process_triage_job(
            owner="octo",
            repo="demo",
            number=99,
            is_pr=True,
            dedup_key="octo/demo#99@sha",
            client=mock_gh,
        )

        assert result["status"] == "success"
        assert result["action"] == "updated"
        assert result["comment_id"] == 888
        assert result["overall"] == "confirmed"

        mock_gh.update_comment.assert_called_once()
        mock_gh.post_comment.assert_not_called()
        mock_gh.add_labels.assert_called_once_with("octo", "demo", 99, ["triage:confirmed"])
