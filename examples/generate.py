"""Regenerate examples/outputs/ by running Probe's real checkers on each example.

Usage (from the repository root):

    PYTHONPATH=src python examples/generate.py

Examples that need the Docker sandbox are still run (static checks apply), and
their output is marked "pending sandbox" until reproduction is wired in. The
Markdown layout is an interim format until probe.report is merged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from probe.evaluation import _load_claim_verdict_fn, issue_prediction, run_checks
from probe.github_client import GitHubClient
from probe.models import Claim

HERE = Path(__file__).resolve().parent
BADGE = {"confirmed": "✅", "invalid": "❌", "cannot-reproduce": "🔁", "uncertain": "⚠️"}


def _cell(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _repo_root(spec: dict, gh: GitHubClient | None) -> tuple[Path, str]:
    if "local" in spec:
        return (HERE / spec["local"]).resolve(), f"`examples/{spec['local']}` (demo repo)"
    owner, repo = spec["github"].split("/")
    if gh is None:
        raise RuntimeError("GitHub access needed")
    snap = gh.snapshot(owner, repo, spec["sha"], resolved_by="example_pin")
    return snap.path, f"[{spec['github']}@{spec['sha'][:7]}](https://github.com/{spec['github']}/tree/{spec['sha']})"


def run_example(example: dict, gh: GitHubClient | None) -> dict:
    text = (HERE / example["input"]).read_text()
    claims = [Claim(**c) for c in example["claims"]]
    for c in claims:
        if c.quote not in text:
            raise ValueError(f"{example['id']}:{c.id} quote is not in the input text")

    diff = (HERE / example["diff"]).read_text() if example.get("diff") else ""
    root, source = _repo_root(example["repo"], gh)
    decide, verdict_source = _load_claim_verdict_fn()

    checks = {c.id: run_checks(c, root, [], diff) for c in claims}
    verdicts = [decide(c, checks[c.id]) for c in claims]
    overall = issue_prediction(claims, verdicts)
    claim_status = {v.claim_id: v.status for v in verdicts}
    matches = claim_status == example["expected_claim_verdicts"] and overall == example["expected_overall"]
    status = "matches expected" if matches else (
        "pending sandbox" if example.get("needs_sandbox") else "DIFFERS FROM EXPECTED"
    )
    return {
        "id": example["id"],
        "title": example["title"],
        "source": source,
        "provenance": example["provenance"],
        "verdict_source": verdict_source,
        "overall": overall,
        "expected_overall": example["expected_overall"],
        "expected_claim_verdicts": example["expected_claim_verdicts"],
        "status": status,
        "claims": [c.model_dump() for c in claims],
        "checks": {k: [r.model_dump() for r in v] for k, v in checks.items()},
        "verdicts": [v.model_dump() for v in verdicts],
    }


def render(result: dict) -> str:
    lines = [
        f"### 🔎 Probe evidence report: {result['title']}",
        "",
        f"**Overall:** {BADGE[result['overall']]} `{result['overall']}`  ",
        f"**Checked against:** {result['source']}  ",
        f"**Input:** {result['provenance']}",
        "",
        "| # | Claim (quoted) | Verdict | Evidence |",
        "|---|---|---|---|",
    ]
    for v in result["verdicts"]:
        lines.append(
            f"| {v['claim_id']} | \"{_cell(v['quote'], 120)}\" | {BADGE[v['status']]} `{v['status']}` "
            f"({v['confidence']}) | {_cell(v['evidence'])} |"
        )
    lines += ["", "<details><summary>Checks that ran</summary>", ""]
    for cid, results in result["checks"].items():
        if not results:
            lines.append(f"- `{cid}`: no deterministic check applies")
        for r in results:
            reason = r["details"].get("reason")
            reason_str = f" ({reason})" if reason else ""
            lines.append(f"- `{cid}` · `{r['checker']}` → **{r['outcome']}**{reason_str}: {_cell(r['evidence'])}")
    lines += [
        "",
        "</details>",
        "",
        "---",
        f"Expected overall verdict: `{result['expected_overall']}` · Status: **{result['status']}** · "
        f"Verdicts from: {result['verdict_source']}",
        "",
        "_Automated triage; a maintainer makes the final call._",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    examples = json.loads((HERE / "manifest.json").read_text())
    out = HERE / "outputs"
    out.mkdir(exist_ok=True)
    failed = False
    with GitHubClient() as gh:
        for example in examples:
            result = run_example(example, gh)
            (out / f"{example['id']}.md").write_text(render(result))
            (out / f"{example['id']}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
            failed |= result["status"] == "DIFFERS FROM EXPECTED"
            print(f"{example['id']:32} overall={result['overall']:17} expected={result['expected_overall']:17} {result['status']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
