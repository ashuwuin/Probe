# Probe (ClaimCheck)

> **Evidence-Based Open Source Issue and Pull Request Triage**  
> *Problem 2 — Hacktoberfest Hack Day 2026 (NST × Rishihood University)*

Maintainers of popular open-source repositories are inundated with bug reports, security disclosures, and pull requests that contain incorrect, fabricated, or unreproducible claims—a problem exacerbated by generative AI slop. 

**Probe** does not attempt to guess whether a human or an AI wrote a report. Instead, Probe breaks down incoming reports into atomic, testable technical claims, verifies each claim against the repository code, commit history, AST, PR diffs, and sandboxed reproductions, and reports evidence-backed verdicts. When evidence is insufficient, Probe clearly communicates **uncertainty** rather than hallucinating answers.

---

## Architecture & Pipeline

```
issue/PR text ──► extract.py (qwen3:8b via Ollama, or regex fallback) ──► Claims
                                                                            │
          repo snapshot ──► verify/static.py  (AST: does the file/symbol exist?)
          PR diff       ──► verify/pr_diff.py (adds tests? touches the issue's code?)
          repro code    ──► verify/sandbox.py (Docker, --network none, limits, read-only)
                                                                            │
                                    CheckResults ──► verdict.py ──► VerdictResult per claim
                                                                  + get_overall_verdict()
                                                                            │
                                         report.py ──► Markdown comment / terminal / JSON
```

### Core Design Principle: The LLM Extracts, Deterministic Code Decides

Large Language Models excel at natural language parsing but are inherently prone to hallucinations. Asking an LLM whether a bug exists produces unverified opinions. Probe uses the open-weight model **`qwen3:8b`** strictly to parse prose into structured claims (`symbol_exists`, `bug_behavior`, `security_vuln`, `pr_fixes_issue`, `pr_adds_tests`, `version_affected`).

Every verdict is computed by deterministic Python code:
1. **Static AST Analysis (`probe.verify.static`)**: Checks whether claimed functions, classes, and files exist in the AST without executing code. Accurately maps installed package tracebacks (`site-packages/<pkg>/...`) back to repo sources.
2. **PR Diff Verification (`probe.verify.pr_diff`)**: Inspects git diffs to verify if claimed tests were added and whether modifications intersect the reported bug paths.
3. **Isolated Sandbox (`probe.verify.sandbox`)**: Executes claimed reproduction scripts in a zero-network Docker container to verify whether crashes or assertions reproduce.
4. **Deterministic Truth Table (`probe.verdict`)**: Maps evidence outcomes to final verdicts. Supporting claims (like symbol existence) alone can never confirm a bug; invalid verdicts require strict contradictions.

---

## Verdict Statuses

| Status | Badge | Description |
|---|---|---|
| **Confirmed** | ✅ `confirmed` | The claim was reproduced in the sandbox, or a PR diff verified added tests for a test claim. |
| **Cannot Reproduce** | 🚫 `cannot-reproduce` | Repro code ran successfully without reproducing the claimed error or bug. |
| **Invalid** | ❌ `invalid` | The claimed symbol/file was proven non-existent or contradicted by code evidence. |
| **Uncertain** | ⚠️ `uncertain` | Missing reproduction steps, external dependencies, or inconclusive evidence (e.g. `pr_fixes_issue` claims remain uncertain without execution testing). Accompanied by missing-information hints for maintainers. |

---

## Safety & Security Guarantees

- **Zero-Network Sandboxed Execution**: All reproduction code runs inside isolated Docker containers with `--network none`, a read-only repository mount, non-root user, memory limit (512 MB), process limits (64 PIDs), and a 15-second wall-clock timeout.
- **Prompt Injection Defense**: Issue and PR text is wrapped in data delimiters (`<<<BEGIN_REPORT>>> ... <<<END_REPORT>>>`) and internal delimiter attempts are sanitized. The model has no tool-calling capabilities and its output is strictly validated against a Pydantic JSON schema.
- **Quote & Symbol Anchoring**: Every extracted claim must anchor to an exact character sequence quoted from the original report. Claimed symbols, files, and expected error types are verified to appear verbatim in the reporter's text.
- **Comment Hijacking Prevention**: When updating triage reports on GitHub, Probe checks for a hidden marker (`<!-- probe-report -->`) and validates bot authorship to prevent editing unauthorized comments.
- **Zero Secrets**: Secrets and tokens are kept in `.env` (gitignored) and never mounted into execution containers.

---

## Prerequisites & Setup

### Requirements
- **Python 3.11+**
- **Docker** or **Colima** (for sandboxed reproduction)
- **Ollama** with **`qwen3:8b`** (local open-weight runtime)

### 1. Clone & Install Dependencies
```bash
git clone https://github.com/ashuwuin/Probe.git
cd Probe

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Pull Local Model & Docker Sandbox Image
```bash
# Pull open-weight Qwen model via Ollama
ollama pull qwen3:8b

# Pull lightweight Python runner image for sandbox
docker pull python:3.11-slim
```

---

## Usage

### Triaging directly from a GitHub URL

You can triage an issue or PR directly by passing its GitHub URL. Probe automatically fetches the report body, git diffs, and downloads/caches an immutable repository snapshot pinned to the exact commit:

```bash
# Triage an issue URL and print the Markdown report:
PYTHONPATH=src python -m probe.cli triage https://github.com/owner/repo/issues/42 --markdown

# Triage a Pull Request URL:
PYTHONPATH=src python -m probe.cli triage https://github.com/owner/repo/pull/101 --markdown

# Post or update the report comment directly on GitHub (requires GITHUB_TOKEN):
PYTHONPATH=src python -m probe.cli triage https://github.com/owner/repo/issues/42 --post
```

### Triaging from Local Text or Diff Files

```bash
PYTHONPATH=src python -m probe.cli triage issue_report.txt \
  --repo /path/to/repo \
  --issue \
  --repo-name owner/repo \
  -n 42 \
  --markdown
```

### Triaging a Pull Request

```bash
PYTHONPATH=src python -m probe.cli triage pr_description.txt \
  --repo /path/to/repo \
  --pr \
  --diff pr_changes.diff \
  --repo-name owner/repo \
  -n 101 \
  --markdown
```

### Offline / CI Mode (No LLM)

Run with `--no-llm` to use the deterministic AST and regex extraction fallback:
```bash
PYTHONPATH=src python -m probe.cli triage issue_report.txt --repo . --issue --no-llm --markdown
```

### Example Markdown Output

```markdown
<!-- probe-report -->
### 🔎 Probe Evidence Report: [owner/repo#42](https://github.com/owner/repo/issues/42)

**Overall Verdict:** ⚠️ `uncertain` (medium) — Not enough reproducible evidence to confirm or invalidate.

| # | Claim | Verdict | Confidence | Evidence |
|---|---|---|---|---|
| `c1` | "`calculate_tax()` in `billing.py` crashes on negative values" | ⚠️ `uncertain` | medium | Symbol found at `billing.py:8`, but reproduction snippet requires sandbox verification. |
| `c2` | "RCE in `parse_url_v2()` allows remote code execution" | ❌ `invalid` | high | Symbol `parse_url_v2` was not found in the repository AST. |

> ℹ️ **Missing Information**: This report lacks an isolated reproduction snippet. Maintainers can request a minimal test script to confirm behavior.

*Automated evidence-based triage. Verdicts are derived strictly from deterministic AST checks and sandboxed execution, not model speculation.*
```

---

## Evaluation & Benchmarks

Probe is evaluated against real closed issues and pull requests from major open-source projects including [Loguru](https://github.com/Delgan/loguru), [Poetry](https://github.com/python-poetry/poetry), [Rich](https://github.com/Textualize/rich), and [Textual](https://github.com/Textualize/textual), as well as a checkable group of symbol AST, PR diff, and sandboxed test cases.

- Detailed methodology and benchmark results: see [`eval/RESULTS.md`](eval/RESULTS.md).
- Real scenario inputs and outputs: see [`examples/`](examples/).

### Evaluation Principles & Sandbox Dependency Notes:
- **Zero False `invalid` Verdicts**: Real bugs are never falsely labelled invalid.
- **No Decisive Verdict Without Evidence**: Every non-uncertain verdict requires concrete AST or execution evidence.
- **Sandbox Network & Dependency Isolation**: Probe runs reproduction snippets inside a hardened zero-network container (`--network none`) based on `python:3.11-slim`. Real-world reproduction scripts in external projects frequently require third-party libraries (e.g. `click`, `cffi`, `rich`) that are not pre-installed in the minimal base image. Because Probe disables network access at triage time to prevent SSRF and token leakage, missing imports safely stop at `uncertain` (with reason `missing_dependency`). To evaluate deterministic decision logic with ground truth, Probe's benchmark includes a **checkable cases** group covering symbol AST checks, PR test additions/omissions, and self-contained demo reproductions.

---

## Team & Credits

**Team Zig1**
- **Ashwin Anand** ([@ashuwuin](https://github.com/ashuwuin)) — Integration, CI/scaffolding, sandbox execution, CLI
- **Kaashvi Saini** ([@Kaashviw3spaces](https://github.com/Kaashviw3spaces)) — AI claim extraction (`qwen3:8b`), guardrails, Markdown reporting
- **Prachi Vats** ([@Prachivats-21](https://github.com/Prachivats-21)) — Static code checkers, AST symbol verification, evaluation suite & GitHub client

### Models & Tools
- **Runtime Model**: `qwen3:8b` via local Ollama (Apache-2.0 open weights). No closed or cloud AI APIs are used.
- **AI Coding Assistants**: Gemini AGY (Antigravity), Claude Code.

## License

This project is open-source under the [MIT License](LICENSE).

