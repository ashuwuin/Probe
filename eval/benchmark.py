"""Run Probe's evaluation benchmark against real closed GitHub issues.

Usage (from the repository root):

    PYTHONPATH=src python eval/benchmark.py                 # gold claims (default)
    PYTHONPATH=src python eval/benchmark.py --mode extracted  # LLM-extracted claims (needs Ollama)

Set GITHUB_TOKEN for higher API rate limits. Snapshots are cached by commit SHA
in ~/.cache/probe/repos (override with PROBE_CACHE_DIR), so reruns are offline-fast.
Writes eval/RESULTS.md and eval/results.json.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from probe.evaluation import (
    _load_claim_verdict_fn,
    compute_metrics,
    evaluate_case,
    load_cases,
    render_markdown,
    results_to_json,
)
from probe.github_client import GitHubClient, GitHubError

HERE = Path(__file__).resolve().parent


def _extract(case_text: str):
    try:
        from probe.extract import extract_claims  # type: ignore[attr-defined]
    except ImportError as exc:
        raise SystemExit(f"--mode extracted needs probe.extract.extract_claims ({exc})") from exc
    return extract_claims(case_text).claims


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=HERE / "cases")
    parser.add_argument("--out", type=Path, default=HERE / "RESULTS.md")
    parser.add_argument("--json", type=Path, default=HERE / "results.json")
    parser.add_argument("--mode", choices=["gold", "extracted"], default="gold")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if not cases:
        print(f"No cases found in {args.cases}", file=sys.stderr)
        return 1

    decide, verdict_source = _load_claim_verdict_fn()
    results = []
    with GitHubClient() as gh:
        for case in cases:
            owner, repo = case.owner_repo
            try:
                snap = gh.snapshot(owner, repo, case.sha, resolved_by="eval_case_pin")
                root, error = snap.path, None
            except (GitHubError, ValueError) as exc:
                root, error = None, f"{exc.__class__.__name__}: {exc}"

            claims = None
            if args.mode == "extracted" and root is not None:
                issue = gh.get_issue(owner, repo, int(case.url.rstrip("/").split("/")[-1]))
                claims = _extract(f"{issue.title}\n\n{issue.body}")

            result = evaluate_case(case, root, claims=claims, decide=decide, error=error)
            results.append(result)
            print(f"{case.id:16} label={case.label:17} probe={result.prediction:17} {'ERROR ' + error if error else ''}")

    metrics = compute_metrics(results)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    args.out.write_text(render_markdown(results, metrics, mode=args.mode, verdict_source=verdict_source, generated=generated))
    args.json.write_text(results_to_json(results) + "\n")

    agree, checks = metrics.check_agreement
    print(
        f"\ncases={metrics.total} decisive={metrics.decisive} correct={metrics.correct} "
        f"wrong_invalid={len(metrics.false_invalid)} unsupported_decisive={len(metrics.unsupported_decisive)} "
        f"checker_agreement={agree}/{checks}\nwrote {args.out} and {args.json}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
