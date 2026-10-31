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
    _load_overall_fn,
    compute_metrics,
    evaluate_case,
    load_cases,
    render_markdown,
    results_to_json,
)
from probe.github_client import GitHubClient, GitHubError

HERE = Path(__file__).resolve().parent


def _extract(case_text: str, *, is_pull_request: bool):
    """Return the whole ClaimsResponse: its ``method`` (llm / regex_fallback) feeds the verdict rules."""
    try:
        from probe.extract import extract_claims  # type: ignore[attr-defined]
    except ImportError as exc:
        raise SystemExit(f"--mode extracted needs probe.extract.extract_claims ({exc})") from exc
    return extract_claims(case_text, is_pull_request=is_pull_request)


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
    _, overall_source = _load_overall_fn()
    verdict_source = f"{verdict_source} (overall: {overall_source})"
    results = []
    with GitHubClient() as gh:
        for case in cases:
            if case.local_repo:
                root = (HERE.parent / case.local_repo).resolve()
                error = None
            else:
                owner, repo = case.owner_repo
                try:
                    snap = gh.snapshot(owner, repo, case.sha, resolved_by="eval_case_pin")
                    root, error = snap.path, None
                except (GitHubError, ValueError) as exc:
                    root, error = None, f"{exc.__class__.__name__}: {exc}"

            number = 0 if case.synthetic else int(case.url.rstrip("/").split("/")[-1])
            is_pr = case.kind == "pull_request"
            if is_pr and root is not None and not case.diff and not case.synthetic:
                try:
                    pull = gh.get_pull(owner, repo, number)
                    case = case.model_copy(update={"changed_files": pull.changed_files, "diff": pull.diff})
                except (GitHubError, ValueError) as exc:
                    root, error = None, f"{exc.__class__.__name__}: {exc}"

            claims, method = None, "gold"
            if args.mode == "extracted" and root is not None:
                text = case.text
                if not text and not case.synthetic:
                    try:
                        issue = gh.get_issue(owner, repo, number)
                        text = f"{issue.title}\n\n{issue.body}"
                    except Exception:
                        if case.note:
                            text = f"{case.id}\n\n{case.note}"
                        elif case.gold_claims:
                            text = "\n\n".join(c.quote for c in case.gold_claims)
                if text:
                    response = _extract(text, is_pull_request=is_pr)
                    claims, method = response.claims, response.method

            result = evaluate_case(case, root, claims=claims, decide=decide, error=error, extraction_method=method)
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
