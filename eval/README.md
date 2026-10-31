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

### Maintainer-labelled issues (real repos)
The benchmark evaluates 12 real, closed issues from Loguru, Poetry, Rich, and Textual.
Probe enforces strict safety:
- **0 wrong `invalid` verdicts**: Probe never falsely accuses a real bug report of being invalid.
- **0 decisive verdicts without evidence**: Every non-uncertain verdict requires supporting or contradicting checks.
- Real-repo reproduction snippets in these external projects require their respective project dependencies (e.g. `click`, `cffi`, `rich`). In Probe's hardened zero-network sandbox (`--network none`), network access is disabled at triage time to prevent SSRF and token leaks, and third-party packages are not pre-installed in the clean `python:3.11-slim` image. When an uninstalled dependency is encountered, Probe safely stops at `uncertain` with reason `missing_dependency` rather than guessing.

### Checkable cases group
To evaluate Probe's deterministic checkers and decision rules with ground truth evidence, the benchmark includes a **checkable cases** group:
- **Symbol & file AST checks**: e.g. non-existent functions like `parse_url_v2` in `psf/requests` contradicted by static AST analysis (`invalid`).
- **PR test diff checks**: e.g. real PR `psf/requests#5414` adding tests (`confirmed`), and PRs claiming tests without test files (`invalid`).
- **Demo-repo reproductions**: e.g. self-contained bug reproductions in `examples/repos/shop` verifying failure reproduction (`confirmed`) and clean execution (`cannot-reproduce`).

On the checkable cases group, Probe achieves **100% decided coverage** (5/5) and **100% accuracy on decided cases** (5/5), with 0 false invalid verdicts.
