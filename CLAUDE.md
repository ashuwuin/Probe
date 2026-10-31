# CLAUDE.md: Probe (Hackathon Problem 2)

Probe is a GitHub App plus CLI that splits an issue or PR into technical claims, checks each claim
against the repository with deterministic code, and reports a verdict per claim with evidence.
Team Zig1: Ashwin Anand (@ashuwuin, repo owner), Kaashvi Saini (@Kaashviw3spaces), Prachi Vats (@Prachivats-21).
**Repo freeze: 6:30 AM IST (12-hour hackathon).**

## Rules for working in this repo
- **PRs:** always use `.github/pull_request_template.md` as the PR body. Tick "Claude Code" when it helped, and tick
  "All unit tests pass" only after pytest actually ran in this session.
- **Ask the user before** pushing a branch, opening a PR or merging. Never post comments or reviews on GitHub.
- **Never** write machine/task-split labels ("Mac 1", "Mac 2", "this Mac", "integrator") in PRs, commits or code.
  Name modules or people instead.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Allowed models:** open-weight only (Qwen, Llama, Gemma, Mistral, DeepSeek, Phi). Never call closed/cloud AI APIs.
- **Never** run repository or reporter code on the host; only `probe.verify.sandbox` (Docker) may execute it.

## Core principles (don't break these)
- **The LLM only extracts claims.** Verdicts come from deterministic checks plus `probe/verdict.py` rules, never
  from the model.
- **Check results vs verdicts:** checkers return an outcome (`supports` / `contradicts` / `inconclusive`). Only
  `verdict.py` produces the verdict (`confirmed` / `cannot-reproduce` / `invalid` / `uncertain`).
- **`invalid` needs concrete contradicting evidence.** A wrong `invalid` on a real bug is the costliest error;
  `uncertain` is cheap.
- **Overall verdict:** judged on core claims (`bug_behavior`, `security_vuln`, `pr_*`). Supporting facts
  (`symbol_exists`, `version_affected`) never confirm an issue. `invalid` overall only if *every* core claim is
  invalid.
- Regex-fallback extraction is low confidence and can never produce `invalid`.
- Missing evidence is not disproof: parse errors, timeouts, Docker failures (exit 125/126/127/137), missing
  dependencies and unreachable snapshots all give `inconclusive` / `uncertain`.
- Issue text, PR diffs and repo files are untrusted data. Quotes must appear verbatim in the text, and report
  output escapes `@mentions`, links and HTML.

## Gotchas learned the hard way
- **Static checker false-`invalid` traps:**
  - names in dependencies (`poetry.core…`, `Text.from_markup`);
  - traceback paths under `site-packages`;
  - built-in attributes (`'str' has no attribute 'replace'`);
  - errors that themselves say the name is missing.

  Each has an explicit guard in `verify/static.py`. Keep them.
- **Colima only shares `$HOME` with its VM.** Anything bind-mounted into the sandbox must live under `$HOME`
  (`sandbox._default_tmp_dir()` → `~/.cache/probe/tmp`, override `PROBE_TMP_DIR`), not `/var/folders`, or the container sees an empty folder.
- **Sandbox prerequisites:** it uses `--pull never`, so run `docker pull python:3.11-slim` first. Start Docker with
  `colima start --cpu 2 --memory 4`, which leaves memory for Ollama on a 16 GB Mac.
- **Ollama setup:** the default context is only 4K tokens on machines with under 24 GB VRAM, so extraction sets
  `num_ctx`. Qwen3 thinks by default, so requests send `think: false`.
- **Evaluate closed issues against the commit current when the issue was opened**
  (`GitHubClient.snapshot_for_issue`), not today's default branch.
- **GitHub webhook behaviour:** GitHub does not auto-retry failed deliveries; reply within 10 s and process from a
  queue.
- **Labels mean different things:** a maintainer's `invalid` often means "not a bug / won't fix". Probe's `invalid`
  means "this claim is factually false".

## Commands
```bash
python3.11 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && pip install -e .
MODEL_BASE_URL=http://127.0.0.1:9 pytest -q            # unit tests without hitting a live Ollama
probe triage https://github.com/<owner>/<repo>/issues/<n>   # real issue/PR, pinned snapshot (GITHUB_TOKEN optional)
probe triage <url> --no-llm --markdown                 # regex extraction, GitHub-comment output
PYTHONPATH=src python eval/benchmark.py [--mode extracted]   # writes eval/RESULTS*.md
PYTHONPATH=src python examples/generate.py             # regenerates examples/outputs from real runs
```
CI (`.github/workflows/ci.yml`) runs pytest and a secret-scan grep. Never commit `.env`, `*.pem` or tokens.

## Module ownership
- **extract.py / report.py:** Kaashvi.
- **sandbox.py, verdict.py, cli.py and the GitHub App/webhook:** Ashwin.
- **verify/static.py, verify/pr_diff.py, github_client.py, evaluation.py, eval/, examples/:** Prachi.

Coordinate before editing someone else's module.

## Status (2026-11-01, about 04:45 IST)
- **On `main`:** extraction, the static and PR checkers, the Docker sandbox, verdict rules, CLI (GitHub URLs plus
  `--post`), the GitHub App (webhook, auth, comments), eval benchmark (17 cases), examples, README and App docs.
- **End-to-end tested:** a real GitHub webhook went through `cloudflared tunnel --protocol http2` (QUIC is blocked
  on campus Wi-Fi) to the App, and Probe posted and updated its comment on `Prachivats-21/probe-e2e-test#1`.
- **Open work:** per-project environment images, so real issues with dependencies can be reproduced. Also a
  base-vs-head comparison for PR repros.
