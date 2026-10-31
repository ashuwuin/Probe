# Probe evaluation

Probe is evaluated on **real, closed GitHub issues** whose maintainers labelled them
`invalid`, `cannot reproduce` or `confirmed`. The latest results are in [`RESULTS.md`](RESULTS.md).

## Dataset (`cases/*.json`)

12 issues from Delgan/loguru, python-poetry/poetry, Textualize/rich and Textualize/textual,
4 per label. Each case records:

- `label`: the maintainer's label, used as the reference answer. Labels are
  signals, not infallible ground truth.
  Note that a maintainer's `invalid` often means "not a bug / won't fix", while
  Probe's `invalid` means "this specific technical claim is factually false", so
  the two do not always measure the same thing.
- `sha`: the default-branch commit at the moment the issue was opened, so Probe
  checks the code the reporter actually saw rather than today's code.
- `gold_claims`: hand-written claims. Each `quote` was verified to appear
  verbatim in the issue.
- `expected_outcomes`: what each deterministic check should return, used to score
  the checkers on their own.
- `note`: why the maintainer chose the label.

The set deliberately includes hard cases:
- an AI-generated report about real code (loguru#1448);
- bugs whose faulty code lives in a dependency (poetry#7033, poetry#9961, textual#2178);
- behaviour-only reports with no code reference.

## Metrics

| Metric | Why it matters |
|---|---|
| Wrong `invalid` verdicts | Calling a real bug invalid is the most harmful error. Target: 0 |
| Decisive verdicts without evidence | Every `confirmed` / `invalid` / `cannot-reproduce` must rest on a check that supports or contradicts the claim. Target: 0 |
| Coverage | Share of cases with a decisive verdict instead of `uncertain` |
| Accuracy (decisive / all) | Correctness among decided cases, and over all cases |
| Precision / recall per label, confusion matrix | Where the errors are |
| Checker agreement | Do the static and PR checks return the annotated outcome? |

## Run

```bash
PYTHONPATH=src python eval/benchmark.py                   # gold claims
PYTHONPATH=src python eval/benchmark.py --mode extracted  # LLM claims (needs probe.extract + Ollama)
```

Snapshots are downloaded once and cached by SHA in `~/.cache/probe/repos`.
Set `GITHUB_TOKEN` for higher rate limits.

Verdicts come from `probe.verdict.decide_claim(claim, results)` when that exists.
Until then a conservative baseline in `probe/evaluation.py` is used, and
`RESULTS.md` says which one produced the numbers.

## Current status (honest reading)

With static and PR checks only, Probe returns `uncertain` for all 12 issues. It
makes no wrong accusations, but it decides nothing. Most real reports are
behaviour claims that only a reproduction can settle. The Docker sandbox, and
running `--mode extracted` once extraction lands, are what will move coverage.
