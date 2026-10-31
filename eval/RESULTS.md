# Probe evaluation results

_Generated 2026-10-31 19:08 UTC · mode: **gold** · verdicts: **baseline rules (probe.verdict not available yet)**_

Each case is a real, closed GitHub issue. The maintainer's label is the reference answer; labels are signals, not infallible ground truth. Every case is checked against the commit that was current when the issue was opened.

## Summary

| Metric | Value | What it means |
|---|---|---|
| Cases | 12 | |
| **Wrong `invalid` verdicts** | **0** | Real bugs Probe called invalid (the most harmful error) |
| **Decisive verdicts without evidence** | **0** | Must be 0: every decisive verdict needs a check that supports or contradicts the claim |
| Coverage | 0/12 (0%) | Cases where Probe gave a decisive verdict instead of `uncertain` |
| Accuracy (decisive cases only) | n/a | Correct among the cases Probe decided |
| Accuracy (all cases) | 0% | `uncertain` counts as not correct |
| Checker agreement | 10/10 | Check outcomes that matched the hand-annotated expected outcome |

### Per label

| Label | Precision | Recall |
|---|---|---|
| `confirmed` | n/a | 0% |
| `cannot-reproduce` | n/a | 0% |
| `invalid` | n/a | 0% |

### Confusion matrix (rows: maintainer label, columns: Probe)

| | `confirmed` | `cannot-reproduce` | `invalid` | `uncertain` |
|---|---|---|---|---|
| `confirmed` | 0 | 0 | 0 | 4 |
| `cannot-reproduce` | 0 | 0 | 0 | 4 |
| `invalid` | 0 | 0 | 0 | 4 |

## Cases

| Case | Maintainer label | Probe | Claim checks |
|---|---|---|---|
| [loguru-1448](https://github.com/Delgan/loguru/issues/1448) | `invalid` | — `uncertain` | c1: static_ast=supports (symbol_found) |
| [loguru-1471](https://github.com/Delgan/loguru/issues/1471) | `invalid` | — `uncertain` | c1: static_ast=supports (symbol_found) |
| [poetry-3610](https://github.com/python-poetry/poetry/issues/3610) | `confirmed` | — `uncertain` | c1: no applicable check |
| [poetry-6627](https://github.com/python-poetry/poetry/issues/6627) | `confirmed` | — `uncertain` | c1: no applicable check |
| [poetry-7033](https://github.com/python-poetry/poetry/issues/7033) | `confirmed` | — `uncertain` | c1: static_ast=inconclusive (close_match) |
| [poetry-7268](https://github.com/python-poetry/poetry/issues/7268) | `invalid` | — `uncertain` | c1: no applicable check |
| [poetry-7465](https://github.com/python-poetry/poetry/issues/7465) | `invalid` | — `uncertain` | c1: static_ast=supports (symbol_found) |
| [poetry-9961](https://github.com/python-poetry/poetry/issues/9961) | `confirmed` | — `uncertain` | c1: static_ast=inconclusive (external_file) |
| [rich-1630](https://github.com/Textualize/rich/issues/1630) | `cannot-reproduce` | — `uncertain` | c1: no applicable check |
| [rich-2988](https://github.com/Textualize/rich/issues/2988) | `cannot-reproduce` | — `uncertain` | c1: static_ast=supports (symbol_found) |
| [textual-2178](https://github.com/Textualize/textual/issues/2178) | `cannot-reproduce` | — `uncertain` | c1: static_ast=supports (symbol_found); c2: static_ast=inconclusive (external_module) |
| [textual-2863](https://github.com/Textualize/textual/issues/2863) | `cannot-reproduce` | — `uncertain` | c1: static_ast=supports (symbol_found); c2: static_ast=supports (symbol_found) |

## Limits of this run

- The Docker sandbox is not wired in yet, so no case can be `confirmed` or `cannot-reproduce` by reproduction; behaviour claims stay `uncertain`.
- Gold-claims mode measures the checkers and verdict rules with hand-written claims; it does not measure LLM extraction.
- The dataset is small (see `eval/cases/`); treat percentages as indicative.
