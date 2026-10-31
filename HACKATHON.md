# HACKATHON.md

## Team
- **Team name:** Zig1
- **Members:**
  - Ashwin Anand (GitHub: @ashuwuin)
  - Kaashvi Saini (GitHub: @Kaashviw3spaces)
  - Prachi Vats (GitHub: @Prachivats-21)

## Problem statement
**Problem 2: Evidence-Based Open Source Issue and Pull Request Triage.**

We are building **Probe** (ClaimCheck), a GitHub App that reads incoming issues and pull requests, splits them into individual technical claims, and checks each claim against the repository:

- **Static check:** do the files, functions and classes the report names actually exist? Python `ast`.
- **Sandboxed reproduction:** run the reporter's reproduction steps with no network, no secrets, and CPU, memory and time limits.
- **PR test coverage:** do the tests in the PR actually exercise the changed lines?

It posts one comment with a verdict per claim: `confirmed`, `cannot-reproduce`, `invalid` or `uncertain`, and the evidence behind each one. When there is not enough evidence, it says `uncertain` instead of guessing.

## Models used, and why
| Model | Runs via | Used for | Why |
|---|---|---|---|
| `qwen2.5:7b` (Qwen 2.5 7B, Apache-2.0, 4-bit) | Ollama on localhost | Extracting structured claims (file, symbol, behaviour, repro steps) from issue/PR text as JSON | Reliable JSON-schema output in its size class; ~5–6 GB RAM fits our 16 GB Apple Silicon laptops. Thinking mode is disabled for speed. |

The LLM only *extracts* claims. Every verdict comes from deterministic checks (AST, sandbox run, coverage), never from the model's opinion.

No closed or cloud AI APIs are used.

## Coding assistants
- Claude Code
- Gemini AGY (Antigravity)

Each PR names the coding assistant when it was used.
