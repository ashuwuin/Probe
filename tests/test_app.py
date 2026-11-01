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
    monkeypatch.setenv("PROBE_APPLY_LABELS", "false")
    app_module._ACTIVE_JOBS.clear()
    return TestClient(app)


# ---------------------------------------------------------------------------
# Signature & Payload Verification Tests
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


def test_webhook_malformed_json_body(client):
    body = b"this is definitely not valid json"
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 400
    assert "Invalid JSON body" in res.json()["detail"]


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


def test_webhook_issues_opened_by_stranger_disallows_sandbox(client):
    payload = {
        "action": "opened",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 420, "updated_at": "2026-11-01T00:00:00Z", "author_association": "NONE"},
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
    assert data["allow_sandbox"] is False


def test_webhook_issues_opened_by_maintainer_allows_sandbox(client):
    payload = {
        "action": "opened",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 421, "updated_at": "2026-11-01T00:00:00Z", "author_association": "MEMBER"},
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
    assert data["allow_sandbox"] is True


def test_webhook_issues_edited_with_body_change_accepted(client):
    payload = {
        "action": "edited",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 42, "updated_at": "2026-11-01T01:00:00Z"},
        "changes": {"body": {"from": "old body text"}},
        "installation": {"id": 12345},
    }
    body = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 202
    assert res.json()["status"] == "accepted"


def test_webhook_issues_edited_without_content_change_ignored(client):
    payload = {
        "action": "edited",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 42, "updated_at": "2026-11-01T01:00:00Z"},
        "changes": {"labels": {"from": []}},  # label change only, neither title nor body
        "installation": {"id": 12345},
    }
    body = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert "neither title nor body changed" in res.json()["reason"]


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


def test_webhook_pull_request_synchronize_internal(client):
    payload = {
        "action": "synchronize",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "pull_request": {
            "number": 99,
            "head": {"sha": "1234567890abcdef" * 2 + "12345678", "repo": {"full_name": "octo/demo", "fork": False}},
            "base": {"repo": {"full_name": "octo/demo"}},
            "author_association": "MEMBER",
        },
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
    assert data["allow_sandbox"] is True


def test_webhook_pull_request_stranger_fork_disallows_sandbox(client):
    payload = {
        "action": "opened",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "pull_request": {
            "number": 100,
            "head": {"sha": "abcdef1234567890" * 2 + "12345678", "repo": {"full_name": "attacker/demo", "fork": True}},
            "base": {"repo": {"full_name": "octo/demo"}},
            "author_association": "NONE",
        },
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
    assert data["allow_sandbox"] is False


def test_webhook_issue_comment_rerun_by_maintainer(client):
    payload = {
        "action": "created",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 15, "user": {"login": "alice"}},
        "comment": {
            "id": 1001,
            "body": "/probe rerun please",
            "user": {"login": "charlie", "type": "User"},
            "author_association": "MEMBER",
        },
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
    assert data["allow_sandbox"] is True


def test_webhook_issue_comment_rerun_by_issue_author(client):
    payload = {
        "action": "created",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 15, "user": {"login": "alice"}},
        "comment": {
            "id": 1002,
            "body": "/probe rerun",
            "user": {"login": "alice", "type": "User"},
            "author_association": "NONE",  # author is not repo member, but opened the issue
        },
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
    assert data["allow_sandbox"] is True


def test_webhook_issue_comment_rerun_pr_by_fork_author_disallows_sandbox(client):
    """Author of a fork PR triggering rerun should NOT get sandbox execution."""
    payload = {
        "action": "created",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {
            "number": 25,
            "user": {"login": "fork_dev"},
            "pull_request": {"url": "https://api.github.com/repos/octo/demo/pulls/25"},
        },
        "comment": {
            "id": 10025,
            "body": "/probe rerun",
            "user": {"login": "fork_dev", "type": "User"},
            "author_association": "NONE",
        },
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
    assert data["is_pr"] is True
    assert data["allow_sandbox"] is False


def test_webhook_issue_comment_rerun_pr_by_maintainer_allows_sandbox(client):
    """Maintainer triggering rerun on a PR authorizes sandbox execution."""
    payload = {
        "action": "created",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {
            "number": 25,
            "user": {"login": "fork_dev"},
            "pull_request": {"url": "https://api.github.com/repos/octo/demo/pulls/25"},
        },
        "comment": {
            "id": 10026,
            "body": "/probe rerun",
            "user": {"login": "maintainer_dan", "type": "User"},
            "author_association": "COLLABORATOR",
        },
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
    assert data["is_pr"] is True
    assert data["allow_sandbox"] is True


def test_webhook_issue_comment_rerun_by_unauthorized_stranger_rejected(client):
    payload = {
        "action": "created",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 15, "user": {"login": "alice"}},
        "comment": {
            "id": 1003,
            "body": "/probe rerun",
            "user": {"login": "random_stranger", "type": "User"},
            "author_association": "NONE",
        },
        "installation": {"id": 12345},
    }
    body = json.dumps(payload).encode("utf-8")
    res = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": sign_payload(body)},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ignored"
    assert "not authorized" in data["reason"]


def test_webhook_issue_comment_ignored_bot(client):
    payload = {
        "action": "created",
        "repository": {"name": "demo", "owner": {"login": "octo"}},
        "issue": {"number": 15},
        "comment": {"id": 1004, "body": "/probe rerun", "user": {"login": "probe[bot]", "type": "Bot"}},
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
        "comment": {"id": 1005, "body": "looks good to me", "user": {"login": "alice", "type": "User"}},
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


def test_process_triage_job_replaces_labels_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "PROBE_APPLY_LABELS", True)
    mock_gh = MagicMock()
    mock_gh.__enter__.return_value = mock_gh

    mock_issue = MagicMock()
    mock_issue.title = "Bug"
    mock_issue.body = "desc"
    mock_gh.get_issue.return_value = mock_issue

    mock_snapshot = MagicMock()
    mock_snapshot.path = tmp_path
    mock_gh.snapshot_for_issue.return_value = mock_snapshot
    mock_gh.list_comments.return_value = []
    mock_gh.post_comment.return_value = {"id": 111}

    with patch("probe.app.run_triage_pipeline") as mock_pipeline:
        mock_pipeline.return_value = (
            ClaimsResponse(claims=[]),
            [],
            [],
            "confirmed",
            "Confirmed bug",
        )

        res = process_triage_job(
            owner="octo",
            repo="demo",
            number=42,
            is_pr=False,
            dedup_key="octo/demo#42@v2",
            client=mock_gh,
        )
        assert res["status"] == "success"
        mock_gh.replace_triage_label.assert_called_once_with("octo", "demo", 42, "triage:confirmed")


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
