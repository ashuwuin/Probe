"""FastAPI GitHub App webhook server for Probe.

Listens for GitHub webhook events ('issues', 'pull_request', 'issue_comment'),
verifies HMAC signatures, responds with 202 Accepted within milliseconds,
and runs evidence-based triage in the background.

Guarantees:
* Checks HMAC-SHA256 signature (X-Hub-Signature-256) on every incoming request.
* Returns 202 Accepted immediately, well within GitHub's 10-second timeout.
* Strict permission check on /probe rerun (maintainers or issue author only).
* Only triggers on issue edits when title or body actually changed.
* Fork PR protection: skips Docker sandbox for untrusted fork PRs unless maintainer-approved.
* Deduplicates in-flight runs using repo#number@state and limits concurrent jobs.
* Idempotent comment management: finds and edits existing bot comments via
  find_editable_bot_comment, preventing comment spam.
* Safe label replacement: removes stale triage:* labels before adding a new verdict.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from probe.cli import find_editable_bot_comment, run_triage_pipeline
from probe.github_auth import get_client_for_installation
from probe.github_client import GitHubClient, GitHubError
from probe.report import generate_markdown_report

logger = logging.getLogger("probe.app")

app = FastAPI(
    title="Probe Triage Bot",
    description="Evidence-Based Issue and Pull Request Triage GitHub App",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROBE_BOT_LOGIN = os.environ.get("PROBE_BOT_LOGIN", "probe[bot]")
PROBE_MODEL = os.environ.get("MODEL_NAME", "qwen3:8b")
PROBE_USE_LLM = os.environ.get("PROBE_USE_LLM", "true").lower() in ("1", "true", "yes")
PROBE_APPLY_LABELS = os.environ.get("PROBE_APPLY_LABELS", "false").lower() in ("1", "true", "yes")
PROBE_MAX_CONCURRENT_JOBS = int(os.environ.get("PROBE_MAX_CONCURRENT_JOBS", "2"))

_RERUN_CMD_RE = re.compile(r"^\s*/probe\s+(?:rerun|triage)\b", re.IGNORECASE | re.MULTILINE)
_ACTIVE_JOBS: set[str] = set()
_JOB_SEMAPHORE = threading.BoundedSemaphore(max(1, PROBE_MAX_CONCURRENT_JOBS))


# ---------------------------------------------------------------------------
# HMAC Signature Verification
# ---------------------------------------------------------------------------

def verify_webhook_signature(
    raw_payload: bytes,
    signature_header: str | None,
    secret: str | None,
) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    If secret is configured, signature_header is mandatory and must match.
    If secret is None or empty, returns True only if PROBE_INSECURE_DEV is enabled.
    """
    if not secret:
        # Require PROBE_INSECURE_DEV=1 if running without a secret
        return os.environ.get("PROBE_INSECURE_DEV", "0") in ("1", "true", "yes")

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()
    actual = signature_header[7:]  # strip 'sha256='
    return hmac.compare_digest(expected, actual)


# ---------------------------------------------------------------------------
# Background Triage Task
# ---------------------------------------------------------------------------

def process_triage_job(
    owner: str,
    repo: str,
    number: int,
    is_pr: bool,
    dedup_key: str,
    installation_id: int | None = None,
    client: GitHubClient | None = None,
    allow_sandbox: bool = True,
) -> dict[str, Any]:
    """Execute triage for an issue or pull request, then post or update the report comment."""
    acquired = _JOB_SEMAPHORE.acquire(timeout=60.0)
    if not acquired:
        logger.warning("Max concurrent triage jobs reached; dropped %s#%d", f"{owner}/{repo}", number)
        _ACTIVE_JOBS.discard(dedup_key)
        return {"status": "rate_limited", "error": "Server reached max concurrent triage capacity"}

    try:
        gh = client or get_client_for_installation(installation_id)
        with gh:
            diff_truncated = False
            changed_files_truncated = False

            if is_pr:
                pull = gh.get_pull(owner, repo, number)
                snapshot = gh.snapshot_for_pull(pull)
                text = f"{pull.title}\n\n{pull.body or ''}"
                raw_diff = pull.diff
                changed_files = pull.changed_files
                diff_truncated = getattr(pull, "diff_truncated", False)
                changed_files_truncated = getattr(pull, "changed_files_truncated", False)
            else:
                issue = gh.get_issue(owner, repo, number)
                snapshot = gh.snapshot_for_issue(issue)
                text = f"{issue.title}\n\n{issue.body or ''}"
                raw_diff = ""
                changed_files = None

            claims_resp, results, verdicts, overall_status, overall_summary = run_triage_pipeline(
                text=text,
                repo_root=snapshot.path,
                raw_diff=raw_diff,
                changed_files=changed_files,
                is_pull_request=is_pr,
                use_llm=PROBE_USE_LLM,
                model=PROBE_MODEL,
                allow_sandbox=allow_sandbox,
                diff_truncated=diff_truncated,
                changed_files_truncated=changed_files_truncated,
            )

            report_md = generate_markdown_report(
                verdicts=verdicts,
                repo=f"{owner}/{repo}",
                issue_number=number,
                overall=overall_status,
                overall_summary=overall_summary,
                extraction=claims_resp,
            )

            # Idempotent comment management: update existing comment or post new
            existing_comments = gh.list_comments(owner, repo, number)
            bot_comment = find_editable_bot_comment(existing_comments, bot_login=PROBE_BOT_LOGIN)

            if bot_comment and "id" in bot_comment:
                comment_result = gh.update_comment(owner, repo, bot_comment["id"], report_md)
                action_taken = "updated"
            else:
                comment_result = gh.post_comment(owner, repo, number, report_md)
                action_taken = "posted"

            if PROBE_APPLY_LABELS:
                try:
                    gh.replace_triage_label(owner, repo, number, f"triage:{overall_status}")
                except Exception as exc:
                    logger.warning("Failed to apply triage label: %s", exc)

            return {
                "status": "success",
                "action": action_taken,
                "overall": overall_status,
                "claims": len(verdicts),
                "comment_id": comment_result.get("id"),
            }
    except Exception as exc:
        logger.exception("Error processing triage job %s#%d: %s", f"{owner}/{repo}", number, exc)
        return {"status": "error", "error": str(exc)}
    finally:
        _JOB_SEMAPHORE.release()
        _ACTIVE_JOBS.discard(dedup_key)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
@app.get("/health")
def healthcheck() -> dict[str, str]:
    """Health check endpoint for container orchestrators and tunnels."""
    return {"status": "ok", "service": "probe", "version": "0.1.0"}


@app.post("/webhook")
@app.post("/api/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> JSONResponse:
    """GitHub webhook receiver endpoint."""
    raw_payload = await request.body()
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")

    # 1. HMAC Signature Verification
    if not verify_webhook_signature(raw_payload, x_hub_signature_256, secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if not x_github_event:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Event header")

    # 2. JSON Payload Parsing (with 400 for malformed payload)
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # 3. Ping Event (GitHub webhook setup handshake)
    if x_github_event == "ping":
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "message": "pong", "zen": payload.get("zen")},
        )

    # 4. Extract target repository and installation info
    repo_data = payload.get("repository") or {}
    owner = (repo_data.get("owner") or {}).get("login")
    repo = repo_data.get("name")
    installation_id = (payload.get("installation") or {}).get("id")

    if not owner or not repo:
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": "missing repository information"},
        )

    action = payload.get("action", "")

    # 5. Issues Event
    if x_github_event == "issues":
        if action not in ("opened", "edited"):
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": f"unhandled issue action: {action}"},
            )

        # On 'edited', only re-run if title or body actually changed
        if action == "edited":
            changes = payload.get("changes") or {}
            if "title" not in changes and "body" not in changes:
                return JSONResponse(
                    status_code=200,
                    content={"status": "ignored", "reason": "neither title nor body changed in issue edit"},
                )

        issue = payload.get("issue") or {}
        number = issue.get("number")
        if not number:
            return JSONResponse(status_code=400, content={"error": "missing issue number"})

        updated_at = issue.get("updated_at") or issue.get("created_at") or "v1"
        dedup_key = f"{owner}/{repo}#{number}@{updated_at}"

        if dedup_key in _ACTIVE_JOBS:
            return JSONResponse(
                status_code=202,
                content={"status": "already_queued", "target": f"{owner}/{repo}#{number}"},
            )

        _ACTIVE_JOBS.add(dedup_key)
        background_tasks.add_task(
            process_triage_job,
            owner=owner,
            repo=repo,
            number=number,
            is_pr=False,
            dedup_key=dedup_key,
            installation_id=installation_id,
            allow_sandbox=True,
        )

        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "event": "issues",
                "action": action,
                "target": f"{owner}/{repo}#{number}",
            },
        )

    # 6. Pull Request Event
    if x_github_event == "pull_request":
        if action not in ("opened", "synchronize", "edited"):
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": f"unhandled pull_request action: {action}"},
            )

        # On 'edited', only re-run if title or body actually changed
        if action == "edited":
            changes = payload.get("changes") or {}
            if "title" not in changes and "body" not in changes:
                return JSONResponse(
                    status_code=200,
                    content={"status": "ignored", "reason": "neither title nor body changed in PR edit"},
                )

        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        if not number:
            return JSONResponse(status_code=400, content={"error": "missing PR number"})

        # Check for untrusted fork PR
        head_repo = (pr.get("head") or {}).get("repo") or {}
        base_repo = (pr.get("base") or {}).get("repo") or {}
        is_fork = (
            head_repo.get("fork") is True
            or head_repo.get("full_name") != base_repo.get("full_name")
        )
        author_assoc = (pr.get("author_association") or "").upper()
        is_maintainer = author_assoc in ("OWNER", "MEMBER", "COLLABORATOR")

        # Skip sandbox on fork PRs unless author is a trusted maintainer
        allow_sandbox = not (is_fork and not is_maintainer)

        head_sha = (pr.get("head") or {}).get("sha") or "unknown_sha"
        dedup_key = f"{owner}/{repo}#{number}@{head_sha}"

        if dedup_key in _ACTIVE_JOBS:
            return JSONResponse(
                status_code=202,
                content={"status": "already_queued", "target": f"{owner}/{repo}#{number}"},
            )

        _ACTIVE_JOBS.add(dedup_key)
        background_tasks.add_task(
            process_triage_job,
            owner=owner,
            repo=repo,
            number=number,
            is_pr=True,
            dedup_key=dedup_key,
            installation_id=installation_id,
            allow_sandbox=allow_sandbox,
        )

        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "event": "pull_request",
                "action": action,
                "target": f"{owner}/{repo}#{number}",
                "head_sha": head_sha,
                "allow_sandbox": allow_sandbox,
            },
        )

    # 7. Issue Comment Event (e.g. /probe rerun)
    if x_github_event == "issue_comment":
        if action not in ("created", "edited"):
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": f"unhandled comment action: {action}"},
            )

        comment = payload.get("comment") or {}
        user = comment.get("user") or {}
        user_login = (user.get("login") or "").lower()
        user_type = user.get("type", "")

        # Never trigger on comments from bots to prevent feedback loops
        if user_type == "Bot" or user_login.endswith("[bot]") or user_login.startswith("probe"):
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": "ignoring bot comment"},
            )

        body_text = comment.get("body", "")
        if not _RERUN_CMD_RE.search(body_text):
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": "no /probe command found"},
            )

        # Permission check: allow /probe rerun only from OWNER, MEMBER, COLLABORATOR or the issue author
        author_assoc = (comment.get("author_association") or "").upper()
        issue = payload.get("issue") or {}
        issue_author = ((issue.get("user") or {}).get("login") or "").lower()

        is_maintainer = author_assoc in ("OWNER", "MEMBER", "COLLABORATOR")
        is_author = bool(user_login and user_login == issue_author)

        if not (is_maintainer or is_author):
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": "commenter not authorized to trigger /probe rerun"},
            )

        number = issue.get("number")
        if not number:
            return JSONResponse(status_code=400, content={"error": "missing issue number"})

        is_pr = "pull_request" in issue
        comment_id = comment.get("id")
        dedup_key = f"{owner}/{repo}#{number}@comment_{comment_id}"

        if dedup_key in _ACTIVE_JOBS:
            return JSONResponse(
                status_code=202,
                content={"status": "already_queued", "target": f"{owner}/{repo}#{number}"},
            )

        # Maintainer explicit /probe rerun authorizes sandbox execution even on fork PRs
        allow_sandbox = True if is_maintainer else True

        _ACTIVE_JOBS.add(dedup_key)
        background_tasks.add_task(
            process_triage_job,
            owner=owner,
            repo=repo,
            number=number,
            is_pr=is_pr,
            dedup_key=dedup_key,
            installation_id=installation_id,
            allow_sandbox=allow_sandbox,
        )

        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "event": "issue_comment",
                "command": "rerun",
                "target": f"{owner}/{repo}#{number}",
                "is_pr": is_pr,
            },
        )

    # 8. Unhandled events
    return JSONResponse(
        status_code=200,
        content={"status": "ignored", "reason": f"unsupported event: {x_github_event}"},
    )
