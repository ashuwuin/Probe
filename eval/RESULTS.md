# Probe evaluation results

_Generated 2026-10-31 22:10 UTC · mode: **gold** · verdicts: **probe.verdict.evaluate_claim_verdict (overall: probe.verdict.get_overall_verdict)**_

Evaluation suite combining real closed GitHub issues and checkable deterministic test cases. Maintainer-labelled issues reflect real-world reports; checkable cases verify AST, diff, and sandboxed checks with ground truth.

## Summary

| Metric | Value | What it means |
|---|---|---|
| Cases | 17 | 12 maintainer-labelled issues + 5 checkable cases |
| **Wrong `invalid` verdicts** | **0** | Real bugs Probe called invalid (the most harmful error; 0 across all groups) |
| **Decisive verdicts without evidence** | **0** | Must be 0: every decisive verdict needs a check that supports or contradicts the claim |
| Coverage — checkable cases | 5/5 (100%) | Symbol AST, PR diffs, and sandbox reproductions |
| Coverage — maintainer issues | 0/12 (0%) | Real external repos; zero-network sandbox stops at `uncertain` without third-party deps |
| Accuracy on decided cases | 5/5 (100%) | All decided cases (from checkable group) are 100% correct |
| Accuracy (all cases, combined) | 5/17 (29%) | `uncertain` counts as not correct |
| Checker agreement | 16/16 | Check outcomes that matched the hand-annotated expected outcome (gold claims only) |

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
| [checkable-01-symbol](cases/checkable-01-symbol.json) *(synthetic)* | `invalid` | ✅ `invalid` | c1: static_ast=contradicts (symbol_not_found) |
| [checkable-02-pr-tests](https://github.com/psf/requests/pull/5414) | `confirmed` | ✅ `confirmed` | c1: pr_diff=supports (tests_added) |
| [checkable-03-pr-no-tests](cases/checkable-03-pr-no-tests.json) *(synthetic)* | `invalid` | ✅ `invalid` | c1: pr_diff=contradicts (no_test_files) |
| [checkable-04-demo-repro](cases/checkable-04-demo-repro.json) *(synthetic)* | `confirmed` | ✅ `confirmed` | c1: static_ast=supports (symbol_found), sandbox=supports (reproduced) |
| [checkable-05-demo-clean](cases/checkable-05-demo-clean.json) *(synthetic)* | `cannot-reproduce` | ✅ `cannot-reproduce` | c1: sandbox=contradicts (ran_clean) |
| [loguru-1448](https://github.com/Delgan/loguru/issues/1448) | `invalid` | — `uncertain` | c1: static_ast=supports (symbol_found) |
| [loguru-1471](https://github.com/Delgan/loguru/issues/1471) | `invalid` | — `uncertain` | c1: static_ast=supports (symbol_found) |
| [poetry-3610](https://github.com/python-poetry/poetry/issues/3610) | `confirmed` | — `uncertain` | c1: no applicable check |
| [poetry-6627](https://github.com/python-poetry/poetry/issues/6627) | `confirmed` | — `uncertain` | c1: no applicable check |
| [poetry-7033](https://github.com/python-poetry/poetry/issues/7033) | `confirmed` | — `uncertain` | c1: static_ast=inconclusive (external_module) |
| [poetry-7268](https://github.com/python-poetry/poetry/issues/7268) | `invalid` | — `uncertain` | c1: no applicable check |
| [poetry-7465](https://github.com/python-poetry/poetry/issues/7465) | `invalid` | — `uncertain` | c1: static_ast=supports (symbol_found) |
| [poetry-9961](https://github.com/python-poetry/poetry/issues/9961) | `confirmed` | — `uncertain` | c1: static_ast=inconclusive (external_file) |
| [rich-1630](https://github.com/Textualize/rich/issues/1630) | `cannot-reproduce` | — `uncertain` | c1: no applicable check |
| [rich-2988](https://github.com/Textualize/rich/issues/2988) | `cannot-reproduce` | — `uncertain` | c1: static_ast=supports (symbol_found) |
| [textual-2178](https://github.com/Textualize/textual/issues/2178) | `cannot-reproduce` | — `uncertain` | c1: static_ast=supports (symbol_found); c2: static_ast=inconclusive (external_module) |
| [textual-2863](https://github.com/Textualize/textual/issues/2863) | `cannot-reproduce` | — `uncertain` | c1: static_ast=supports (symbol_found); c2: static_ast=supports (symbol_found) |

## Limits of this run

- The Docker sandbox runs with network isolation (`--network none`) in a standard `python:3.11-slim` environment without external project dependencies pre-installed. Real-repo repros requiring third-party libraries safely stop at `uncertain` (`missing_dependency`), preventing false verdicts.
- Gold-claims mode measures the checkers and verdict rules with hand-written claims; it does not measure LLM extraction.
- The dataset is small (see `eval/cases/`); treat percentages as indicative.
- Cases in the "checkable" groups were selected *because* the static and PR checkers can decide them; report them separately from the maintainer-labelled issues, never as one blended accuracy.
- Labels marked `manual` (PR test claims) come from our own inspection of the PR's changed files, not from a maintainer label.
