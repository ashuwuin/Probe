# Probe evaluation results

_Generated 2026-10-31 21:57 UTC · mode: **extracted** · verdicts: **probe.verdict.evaluate_claim_verdict (overall: probe.verdict.get_overall_verdict)**_

Each case is a real, closed GitHub issue. The maintainer's label is the reference answer; labels are signals, not infallible ground truth. Every case is checked against the commit that was current when the issue was opened.

## Summary

| Metric | Value | What it means |
|---|---|---|
| Cases | 17 | |
| **Wrong `invalid` verdicts** | **0** | Real bugs Probe called invalid (the most harmful error) |
| **Decisive verdicts without evidence** | **0** | Must be 0: every decisive verdict needs a check that supports or contradicts the claim |
| Coverage | 5/17 (29%) | Cases where Probe gave a decisive verdict instead of `uncertain` |
| Accuracy (decisive cases only) | 100% | Correct among the cases Probe decided |
| Accuracy (all cases) | 29% | `uncertain` counts as not correct |
| Checker agreement | n/a (extracted mode) | Check outcomes that matched the hand-annotated expected outcome (gold claims only) |

### By group

| Group | Cases | Decided | Accuracy on decided | Wrong `invalid` |
|---|---|---|---|---|
| checkable cases | 5 | 5/5 | 5/5 (100%) | 0 |
| maintainer-labelled issues | 12 | 0/12 | n/a | 0 |

### Per label

| Label | Precision | Recall |
|---|---|---|
| `confirmed` | 100% | 33% |
| `cannot-reproduce` | 100% | 20% |
| `invalid` | 100% | 33% |

### Confusion matrix (rows: maintainer label, columns: Probe)

| | `confirmed` | `cannot-reproduce` | `invalid` | `uncertain` |
|---|---|---|---|---|
| `confirmed` | 2 | 0 | 0 | 4 |
| `cannot-reproduce` | 0 | 1 | 0 | 4 |
| `invalid` | 0 | 0 | 2 | 4 |

## Cases

| Case | Maintainer label | Probe | Claim checks |
|---|---|---|---|
| [checkable-01-symbol](https://github.com/psf/requests/issues/9991) | `invalid` | ✅ `invalid` | c1: static_ast=contradicts (symbol_not_found); c2: static_ast=contradicts (symbol_not_found) |
| [checkable-02-pr-tests](https://github.com/psf/requests/pull/5414) | `confirmed` | ✅ `confirmed` | c1: static_ast=inconclusive (close_match); c2: pr_diff=supports (tests_added) |
| [checkable-03-pr-no-tests](https://github.com/psf/requests/pull/9992) | `invalid` | ✅ `invalid` | c1: pr_diff=contradicts (no_test_files) |
| [checkable-04-demo-repro](https://github.com/demo/shop/issues/1) | `confirmed` | ✅ `confirmed` | c1: static_ast=supports (symbol_found), sandbox=supports (reproduced) |
| [checkable-05-demo-clean](https://github.com/demo/shop/issues/2) | `cannot-reproduce` | ✅ `cannot-reproduce` | c1: sandbox=contradicts (ran_clean); c2: static_ast=supports (symbol_found) |
| [loguru-1448](https://github.com/Delgan/loguru/issues/1448) | `invalid` | — `uncertain` | c1: static_ast=supports (symbol_found); c2: static_ast=supports (symbol_found); c3: static_ast=supports (symbol_found) |
| [loguru-1471](https://github.com/Delgan/loguru/issues/1471) | `invalid` | — `uncertain` | c1: static_ast=supports (symbol_found); c2: static_ast=supports (symbol_found); c3: static_ast=inconclusive (close_match) |
| [poetry-3610](https://github.com/python-poetry/poetry/issues/3610) | `confirmed` | — `uncertain` | c1: static_ast=inconclusive (unsupported_language); c2: static_ast=inconclusive (unsupported_language); c3: static_ast=inconclusive (unsupported_language); c4: static_ast=inconclusive (mentioned_not_defined) |
| [poetry-6627](https://github.com/python-poetry/poetry/issues/6627) | `confirmed` | — `uncertain` | c1: no applicable check |
| [poetry-7033](https://github.com/python-poetry/poetry/issues/7033) | `confirmed` | — `uncertain` | c1: static_ast=inconclusive (close_match) |
| [poetry-7268](https://github.com/python-poetry/poetry/issues/7268) | `invalid` | — `uncertain` | c1: no applicable check; c2: no applicable check; c3: static_ast=inconclusive (mentioned_not_defined); c4: static_ast=inconclusive (mentioned_not_defined); c5: no applicable check |
| [poetry-7465](https://github.com/python-poetry/poetry/issues/7465) | `invalid` | — `uncertain` | c1: no applicable check; c2: no applicable check; c3: no applicable check |
| [poetry-9961](https://github.com/python-poetry/poetry/issues/9961) | `confirmed` | — `uncertain` | c1: no applicable check; c2: static_ast=inconclusive (unsupported_language); c3: static_ast=inconclusive (unsupported_language) |
| [rich-1630](https://github.com/Textualize/rich/issues/1630) | `cannot-reproduce` | — `uncertain` | c1: no applicable check; c2: static_ast=supports (symbol_found) |
| [rich-2988](https://github.com/Textualize/rich/issues/2988) | `cannot-reproduce` | — `uncertain` | c1: sandbox=inconclusive (missing_dependency); c2: no applicable check |
| [textual-2178](https://github.com/Textualize/textual/issues/2178) | `cannot-reproduce` | — `uncertain` | c1: no applicable check; c2: static_ast=supports (symbol_found); c3: static_ast=supports (symbol_found); c4: no applicable check; c5: no applicable check; c6: no applicable check |
| [textual-2863](https://github.com/Textualize/textual/issues/2863) | `cannot-reproduce` | — `uncertain` | c1: no applicable check; c2: no applicable check; c3: static_ast=supports (symbol_found) |

## Limits of this run

- The Docker sandbox runs with network isolation (`--network none`) in a standard `python:3.11-slim` environment without external project dependencies pre-installed. Real-repo repros requiring third-party libraries safely stop at `uncertain` (`missing_dependency`), preventing false verdicts.
- Gold-claims mode measures the checkers and verdict rules with hand-written claims; it does not measure LLM extraction.
- The dataset is small (see `eval/cases/`); treat percentages as indicative.
- Cases in the "checkable" groups were selected *because* the static and PR checkers can decide them; report them separately from the maintainer-labelled issues, never as one blended accuracy.
- Labels marked `manual` (PR test claims) come from our own inspection of the PR's changed files, not from a maintainer label.
