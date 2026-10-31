# Probe examples

Five worked examples, one per triage outcome. Every report in `outputs/` was
produced by running Probe's checkers. None of them are hand-written.

| # | Scenario | Input | Checked against | Expected | Probe today | Status |
|---|---|---|---|---|---|---|
| 01 | Real bug with a runnable repro | [synthetic issue](issues/01_confirmed_bug.md) | demo repo `repos/shop` | `confirmed` | `confirmed` | ✅ matches |
| 02 | Report about a function that does not exist | [synthetic issue](issues/02_invalid_hallucinated_symbol.md) | real `psf/requests` @ `3c7baaa` | `invalid` | `invalid` | ✅ matches |
| 03 | Crash that does not happen | [synthetic issue](issues/03_cannot_reproduce.md) | demo repo `repos/shop` | `cannot-reproduce` | `cannot-reproduce` | ✅ matches |
| 04 | PR that claims tests and adds them | [real PR psf/requests#5414](prs/04_pr_with_tests.md) + [diff](prs/04_pr_with_tests.diff) | the PR's diff | `confirmed` | `confirmed` | ✅ matches |
| 05 | PR that claims tests but adds none | [synthetic PR](prs/05_pr_missing_tests.md) + [diff](prs/05_pr_missing_tests.diff) | the PR's diff | test claim `invalid`; overall `uncertain` | same | ✅ matches |

Reports: [`outputs/*.md`](outputs/), with the raw checks and verdicts in `outputs/*.json`.

## How to read them

- Each claim gets its own verdict. In 05, the "adds unit tests" claim is `invalid`,
  but the "fixes the discount calculation" claim stays `uncertain`: touching the
  right code is not proof that the fix works. One false claim doesn't make the
  whole PR invalid.
- 01 and 03 are verified by running the reproduction inside the isolated zero-network
  Docker container (`probe.verify.sandbox`).
- The demo repo's bug is real: `apply_discount(100, 10)` returns `0.0`, and
  `format_price(-5)` returns `'-5.00 INR'` without crashing.
- Claims are hand-written ("gold") until LLM extraction is merged. Each claim's
  `quote` is checked to appear verbatim in the input text.

## Regenerate

```bash
PYTHONPATH=src python examples/generate.py   # set GITHUB_TOKEN for higher rate limits
```

The script exits non-zero if a result differs from its expected verdict, except
for examples marked as needing the sandbox.
