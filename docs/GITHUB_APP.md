# Probe — GitHub App Setup & Webhook Deployment Guide

Probe can run as a fully autonomous GitHub App that listens to webhook events, verifies technical claims in incoming issues and pull requests, and posts structured evidence reports.

---

## 1. GitHub App Configuration

### 1.1 Create App on GitHub
1. Navigate to **GitHub Settings → Developer Settings → GitHub Apps → New GitHub App**.
2. **App Name:** `Probe-Triage` (or your chosen bot name).
3. **Webhook URL:** Your deployment URL or tunnel endpoint (e.g. `https://smee.io/<channel>` or `https://<tunnel-domain>/webhook`).
4. **Webhook Secret:** Generate a high-entropy secret string and save it for `GITHUB_WEBHOOK_SECRET`.

### 1.2 Permissions
Under **Repository permissions**, grant:
| Permission | Access | Purpose |
|:---|:---|:---|
| **Issues** | Read & Write | Read issue text/metadata, post triage comments, apply triage labels |
| **Pull requests** | Read & Write | Read PR descriptions, inspect diffs and file changes, post PR comments |
| **Contents** | Read-only | Fetch repository snapshots at specific commit SHAs |
| **Metadata** | Read-only | Default mandatory permission for repository access |

### 1.3 Subscribed Webhook Events
Check the following events:
- [x] **Issues** (`opened`, `edited`)
- [x] **Pull requests** (`opened`, `synchronize`, `edited`)
- [x] **Issue comment** (`created`, `edited` — enables `/probe rerun` command)

### 1.4 Private Key Generation
1. Click **Generate a private key**.
2. Save the downloaded `.pem` file safely (e.g., `secrets/probe-app.private-key.pem`).
3. Note your **App ID** displayed at the top of the app settings page.

---

## 2. Environment Configuration

Copy `.env.example` to `.env` in the repository root:

```bash
cp .env.example .env
```

Configure your environment variables:

```ini
# GitHub App Credentials
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY_PATH=secrets/probe-app.private-key.pem
GITHUB_WEBHOOK_SECRET=your_webhook_secret_here

# Bot Identity
PROBE_BOT_LOGIN=probe[bot]
PROBE_APPLY_LABELS=false
PROBE_MAX_CONCURRENT_JOBS=2

# AI Claim Extraction (local Ollama)
MODEL_BASE_URL=http://localhost:11434/v1
MODEL_NAME=qwen3:8b
PROBE_USE_LLM=true
```

> [!TIP]
> **Alternative Token Auth:** For rapid local testing or CI workflows, you can simply set `GITHUB_TOKEN=ghp_...` instead of configuring App ID and Private Key.

---

## 3. Running the Server Locally

### 3.1 Start the Webhook Listener
Run the built-in CLI command:

```bash
probe serve --host 0.0.0.0 --port 8000 --reload
```

Or run via `uvicorn`:
```bash
uvicorn probe.app:app --host 0.0.0.0 --port 8000 --reload
```

### 3.2 Forward Webhooks via Smee.io
For local testing behind NAT/firewall:
```bash
npm install --global smee-client
smee -u https://smee.io/<your-channel> -t http://127.0.0.1:8000/webhook
```

---

## 4. Key Guarantees & Operational Behavior

1. **HMAC-SHA256 Signature Verification**:
   - Every incoming webhook must have a valid `X-Hub-Signature-256` matching `GITHUB_WEBHOOK_SECRET`.
   - Unauthorized requests immediately receive `401 Unauthorized`.

2. **Sub-second Response (`202 Accepted`)**:
   - GitHub requires webhook endpoints to acknowledge receipt within 10 seconds.
   - Probe responds with `202 Accepted` in milliseconds and executes triage asynchronously in background workers.

3. **In-Flight Deduplication**:
   - Rapid subsequent commits or issue edits are deduplicated via `repo#number@state`, preventing redundant executions.

4. **Idempotent Comment Management**:
   - Probe inspects existing comments using `find_editable_bot_comment`.
   - If an existing Probe report is found on the issue/PR (verified with hidden marker `<!-- probe-report -->` and `probe[bot]` identity), it updates the comment in place rather than creating duplicate spam.

5. **Interactive Re-run Commands & Permission Control**:
   - Authorized users (repository `OWNER`, `MEMBER`, `COLLABORATOR`, or the issue author) can type:
     ```
     /probe rerun
     ```
     or
     ```
     /probe triage
     ```
     in an issue or PR comment to trigger a fresh triage run.
   - Unauthorized comments by third-party strangers are ignored.
   - The bot ignores comments created by bot accounts to strictly prevent infinite webhook feedback loops.

6. **Fork PR Sandbox Protection**:
   - For pull requests originating from forks, Docker sandbox reproduction is automatically skipped for untrusted authors, executing only deterministic AST and unified diff checkers.
   - Maintainers can explicitly authorize sandbox reproduction for a fork PR by commenting `/probe rerun`.

7. **Concurrency Limiting & Abuse Prevention**:
   - Concurrency is throttled to `PROBE_MAX_CONCURRENT_JOBS` (default: 2) to protect host resources.
   - Issue edits only trigger re-triage if the `title` or `body` actually changed (`changes` field check).

8. **Safe Label Replacement**:
   - When `PROBE_APPLY_LABELS=true` is enabled, previous `triage:*` labels are removed before applying a new verdict to avoid contradictory labels. Defaults to `false` to avoid unintended automated label mutations.
