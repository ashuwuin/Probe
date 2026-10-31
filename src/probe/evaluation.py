"""Evaluation harness: score Probe against real closed issues with maintainer labels.

Pure logic only (no network); ``eval/benchmark.py`` fetches snapshots and calls it.

Pipeline per case:  gold (or extracted) claims -> checkers -> claim verdicts
-> one issue-level prediction, compared with the maintainer's label.

Verdicts come from ``probe.verdict`` when that module exists. Until then a
small, conservative *baseline* rule set (below) is used and every report says
so. Maintainer labels are reference signals, not infallible ground truth.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from probe.models import CheckResult, Claim, VerdictResult
from probe.verify.pr_diff import check_pr_claim
from probe.verify.static import check_static_symbol

Label = Literal["confirmed", "cannot-reproduce", "invalid"]
LABELS: tuple[str, ...] = ("confirmed", "cannot-reproduce", "invalid")
PREDICTIONS: tuple[str, ...] = LABELS + ("uncertain",)
DECISIVE = frozenset(LABELS)

# Claim types that carry the report's actual allegation. symbol_exists /
# version_affected are supporting facts: "Label exists" being true does not
# make the bug report true.
CORE_TYPES = frozenset({"bug_behavior", "security_vuln", "pr_fixes_issue", "pr_adds_tests"})
STATIC_TYPES = frozenset({"symbol_exists", "bug_behavior", "security_vuln", "version_affected"})


class EvalCase(BaseModel):
    id: str
    url: str
    kind: Literal["issue", "pull_request"] = "issue"
    label: Label
    sha: str = Field(pattern=r"^[0-9a-f]{40}$|^local$")
    note: str = ""
    gold_claims: list[Claim]
    expected_outcomes: dict[str, Literal["supports", "contradicts", "inconclusive"]] = Field(default_factory=dict)
    changed_files: list[str] = Field(default_factory=list)
    diff: str = ""
    maintainer_labels: list[str] = Field(default_factory=list)
    local_repo: str | None = Field(
        default=None,
        description="Path relative to repo root for local demo repos, e.g. 'examples/repos/shop'",
    )
    text: str = Field(
        default="",
        description="Fallback issue/PR body text for extracted mode when GitHub API is not contacted",
    )
    group: str = Field(
        default="maintainer-labelled issues",
        description="Report results per group; cases picked because the checkers can decide them go in their own group.",
    )
    label_source: str = Field(default="maintainer", description="'maintainer' label, or 'manual' verification by us")

    @property
    def owner_repo(self) -> tuple[str, str]:
        parts = self.url.split("/")
        return parts[3], parts[4]


def load_cases(directory: Path) -> list[EvalCase]:
    return [EvalCase.model_validate_json(p.read_text()) for p in sorted(Path(directory).glob("*.json"))]


# --------------------------------------------------------------------------
# Running checks
# --------------------------------------------------------------------------

def run_checks(claim: Claim, repo_root: Path, changed_files: list[str] | None = None, diff: str = "") -> list[CheckResult]:
    """Route a claim to every deterministic checker that applies to it."""
    results: list[CheckResult] = []
    if claim.type in STATIC_TYPES and (claim.file or claim.symbol):
        results.append(check_static_symbol(repo_root, claim))
    if claim.type in ("pr_adds_tests", "pr_fixes_issue"):
        results.append(check_pr_claim(claim, changed_files or [], diff))
    if claim.repro and claim.type in ("bug_behavior", "security_vuln"):
        sandbox = _load_sandbox_fn()
        if sandbox is not None:
            results.append(sandbox(claim, repo_root=repo_root))
    return results


def _load_sandbox_fn() -> Callable[..., CheckResult] | None:
    """probe.verify.sandbox.run_sandbox_repro when that module exists (it reports
    'sandbox_unavailable' itself when Docker is missing; nothing runs on the host)."""
    try:
        from probe.verify.sandbox import run_sandbox_repro  # type: ignore[import-not-found]
    except ImportError:
        return None
    return run_sandbox_repro


# --------------------------------------------------------------------------
# Baseline verdict rules (used only while probe.verdict is not available)
# --------------------------------------------------------------------------

def _verdict(claim: Claim, status: str, confidence: str, evidence: str) -> VerdictResult:
    return VerdictResult(claim_id=claim.id, status=status, confidence=confidence, evidence=evidence, quote=claim.quote)


def baseline_claim_verdict(claim: Claim, results: list[CheckResult], *, extraction_method: str = "llm") -> VerdictResult:
    """Conservative rules: decide only on concrete evidence, otherwise 'uncertain'.

    Claims from the regex fallback are never marked ``invalid`` (same guard as probe.verdict).
    """
    verdict = _baseline_claim_verdict(claim, results)
    if verdict.status == "invalid" and extraction_method == "regex_fallback":
        return _verdict(claim, "uncertain", "low",
                        f"Low-confidence (regex fallback) extraction; not marking invalid. {verdict.evidence}")
    return verdict


def _baseline_claim_verdict(claim: Claim, results: list[CheckResult]) -> VerdictResult:
    by_checker = {r.checker: r for r in results}
    static = by_checker.get("static_ast")
    diff = by_checker.get("pr_diff")
    sandbox = by_checker.get("sandbox")

    if static and static.outcome == "contradicts" and static.details.get("confidence") == "high":
        return _verdict(claim, "invalid", "high", static.evidence)
    if sandbox and sandbox.outcome == "supports":
        return _verdict(claim, "confirmed", "high", sandbox.evidence)
    if sandbox and sandbox.outcome == "contradicts":
        return _verdict(claim, "cannot-reproduce", "medium", sandbox.evidence)
    if diff and claim.type == "pr_adds_tests":
        if diff.outcome == "supports":
            return _verdict(claim, "confirmed", diff.details.get("confidence", "medium"), diff.evidence)
        if diff.outcome == "contradicts":
            return _verdict(claim, "invalid", "medium", diff.evidence)
    if static and claim.type == "symbol_exists" and static.outcome == "supports":
        return _verdict(claim, "confirmed", static.details.get("confidence", "medium"), static.evidence)

    reasons = "; ".join(r.evidence for r in results) or "No supported check applies to this claim."
    return _verdict(claim, "uncertain", "low", f"Not enough evidence to decide. {reasons}")


def _verdict_module():
    try:
        from probe import verdict as verdict_module  # type: ignore[attr-defined]
    except ImportError:
        return None
    return verdict_module


def _load_claim_verdict_fn() -> tuple[Callable[[Claim, list[CheckResult]], VerdictResult], str]:
    """Probe's real claim rules when available; the baseline only if probe.verdict is absent."""
    module = _verdict_module()
    if module is None:
        return baseline_claim_verdict, "baseline rules (probe.verdict not available yet)"
    for name in ("decide_claim", "evaluate_claim_verdict"):
        fn = getattr(module, name, None)
        if callable(fn):
            # decide_claim is an alias in probe.verdict; report the canonical name when it is one.
            canonical = getattr(module, "evaluate_claim_verdict", None)
            label = "evaluate_claim_verdict" if fn is canonical else name
            return fn, f"probe.verdict.{label}"
    return baseline_claim_verdict, "baseline rules (probe.verdict has no claim-verdict function)"


def _load_overall_fn() -> tuple[Callable[[list[Claim], list[VerdictResult]], str], str]:
    """probe.verdict.get_overall_verdict when available, so the benchmark, CLI and
    GitHub comment share one rule; otherwise baseline_issue_prediction."""
    module = _verdict_module()
    fn = getattr(module, "get_overall_verdict", None) if module else None
    if not callable(fn):
        return baseline_issue_prediction, "baseline overall rule"

    def overall(claims: list[Claim], verdicts: list[VerdictResult]) -> str:
        status, _summary = fn(verdicts, claims)  # claims are needed for the core-claim rule
        return status

    return overall, "probe.verdict.get_overall_verdict"


def issue_prediction(claims: list[Claim], verdicts: list[VerdictResult]) -> str:
    """Issue-level verdict using Probe's shared rule (probe.verdict) when it exists."""
    return _load_overall_fn()[0](claims, verdicts)


def baseline_issue_prediction(claims: list[Claim], verdicts: list[VerdictResult]) -> str:
    """Fallback overall rule (used only while probe.verdict is unavailable).

    * Core claims (bug, security, PR claims) carry the report's allegation:
      ``confirmed`` if any is confirmed, ``invalid`` only if every one is invalid,
      ``cannot-reproduce`` if one could not be reproduced, otherwise ``uncertain``.
    * Supporting facts (``symbol_exists``, ``version_affected``) never confirm an
      issue: "the function exists" does not show the reported bug is real. When a
      report has only supporting claims, it can be ``invalid`` (every named thing is
      missing) but otherwise stays ``uncertain``.
    """
    by_id = {v.claim_id: v for v in verdicts}
    core = [c for c in claims if c.type in CORE_TYPES]
    if core:
        statuses = [by_id[c.id].status for c in core if c.id in by_id]
        if not statuses:
            return "uncertain"
        if "confirmed" in statuses:
            return "confirmed"
        if all(s == "invalid" for s in statuses):
            return "invalid"
        if "cannot-reproduce" in statuses:
            return "cannot-reproduce"
        return "uncertain"

    supporting = [by_id[c.id].status for c in claims if c.id in by_id]
    if supporting and all(s == "invalid" for s in supporting):
        return "invalid"
    return "uncertain"


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

@dataclass
class CaseResult:
    case: EvalCase
    claims: list[Claim]
    checks: dict[str, list[CheckResult]]
    verdicts: list[VerdictResult]
    prediction: str
    error: str | None = None
    used_gold_claims: bool = True

    @property
    def correct(self) -> bool:
        return self.prediction == self.case.label

    @property
    def decisive(self) -> bool:
        return self.prediction in DECISIVE


def evaluate_case(
    case: EvalCase,
    repo_root: Path | None,
    *,
    claims: list[Claim] | None = None,
    decide: Callable[..., VerdictResult] | None = None,
    error: str | None = None,
    extraction_method: str = "gold",
) -> CaseResult:
    """Run one case. ``extraction_method`` is passed to the verdict rules
    ("gold" for hand-written claims, else the ClaimsResponse.method, e.g.
    "regex_fallback", which the rules never turn into ``invalid``)."""
    decide = decide or _load_claim_verdict_fn()[0]
    used_gold = claims is None
    claims = case.gold_claims if claims is None else claims
    if repo_root is None:
        verdicts = [_verdict(c, "uncertain", "low", f"Repository snapshot unavailable: {error}") for c in claims]
        return CaseResult(case, claims, {}, verdicts, "uncertain", error=error, used_gold_claims=used_gold)
    checks = {c.id: run_checks(c, repo_root, case.changed_files, case.diff) for c in claims}
    verdicts = [decide(c, checks[c.id], extraction_method=extraction_method) for c in claims]
    return CaseResult(case, claims, checks, verdicts, issue_prediction(claims, verdicts), error=error,
                      used_gold_claims=used_gold)


@dataclass
class Metrics:
    total: int
    decisive: int
    correct: int
    confusion: dict[str, Counter]
    precision: dict[str, float | None]
    recall: dict[str, float | None]
    false_invalid: list[str]
    unsupported_decisive: list[str]
    check_agreement: tuple[int, int]
    check_mismatches: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.decisive / self.total if self.total else 0.0

    @property
    def accuracy_all(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def accuracy_decisive(self) -> float | None:
        return self.correct / self.decisive if self.decisive else None


def compute_metrics(results: list[CaseResult]) -> Metrics:
    confusion = {label: Counter() for label in LABELS}
    for r in results:
        confusion[r.case.label][r.prediction] += 1

    precision: dict[str, float | None] = {}
    recall: dict[str, float | None] = {}
    for label in LABELS:
        predicted = sum(confusion[t][label] for t in LABELS)
        actual = sum(confusion[label].values())
        tp = confusion[label][label]
        precision[label] = tp / predicted if predicted else None
        recall[label] = tp / actual if actual else None

    false_invalid = [r.case.id for r in results if r.prediction == "invalid" and r.case.label != "invalid"]

    unsupported = []
    for r in results:
        for v in r.verdicts:
            if v.status in DECISIVE:
                decisive_checks = [c for c in r.checks.get(v.claim_id, []) if c.outcome != "inconclusive"]
                if not decisive_checks:
                    unsupported.append(f"{r.case.id}:{v.claim_id}")

    agree = total = 0
    mismatches = []
    for r in results:
        if r.error or not r.used_gold_claims:
            continue  # expected_outcomes are keyed by gold claim ids; extracted ids don't correspond
        for claim_id, expected in r.case.expected_outcomes.items():
            for check in r.checks.get(claim_id, []):
                total += 1
                if check.outcome == expected:
                    agree += 1
                else:
                    mismatches.append(f"{r.case.id}:{claim_id} expected {expected}, got {check.outcome} ({check.details.get('reason')})")

    return Metrics(
        total=len(results),
        decisive=sum(r.decisive for r in results),
        correct=sum(r.correct for r in results),
        confusion=confusion,
        precision=precision,
        recall=recall,
        false_invalid=false_invalid,
        unsupported_decisive=unsupported,
        check_agreement=(agree, total),
        check_mismatches=mismatches,
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _cell(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace("|", "\\|")


def _group_rows(results: list[CaseResult]) -> list[str]:
    rows = []
    for group in dict.fromkeys(r.case.group for r in results):
        members = [r for r in results if r.case.group == group]
        decided = [r for r in members if r.decisive]
        correct = sum(r.correct for r in decided)
        wrong_invalid = sum(r.prediction == "invalid" and r.case.label != "invalid" for r in members)
        accuracy = f"{correct}/{len(decided)} ({correct / len(decided):.0%})" if decided else "n/a"
        rows.append(f"| {group} | {len(members)} | {len(decided)}/{len(members)} | {accuracy} | {wrong_invalid} |")
    return rows


def render_markdown(results: list[CaseResult], metrics: Metrics, *, mode: str, verdict_source: str, generated: str) -> str:
    agree, checks = metrics.check_agreement
    if any(r.used_gold_claims for r in results):
        agreement = f"{agree}/{checks}"
    else:
        agreement = "n/a (extracted mode)"
    lines = [
        "# Probe evaluation results",
        "",
        f"_Generated {generated} · mode: **{mode}** · verdicts: **{verdict_source}**_",
        "",
        "Each case is a real, closed GitHub issue. The maintainer's label is the reference answer; "
        "labels are signals, not infallible ground truth. Every case is checked against the commit that "
        "was current when the issue was opened.",
        "",
        "## Summary",
        "",
        "| Metric | Value | What it means |",
        "|---|---|---|",
        f"| Cases | {metrics.total} | |",
        f"| **Wrong `invalid` verdicts** | **{len(metrics.false_invalid)}** | Real bugs Probe called invalid (the most harmful error) |",
        f"| **Decisive verdicts without evidence** | **{len(metrics.unsupported_decisive)}** | Must be 0: every decisive verdict needs a check that supports or contradicts the claim |",
        f"| Coverage | {metrics.decisive}/{metrics.total} ({_pct(metrics.coverage)}) | Cases where Probe gave a decisive verdict instead of `uncertain` |",
        f"| Accuracy (decisive cases only) | {_pct(metrics.accuracy_decisive)} | Correct among the cases Probe decided |",
        f"| Accuracy (all cases) | {_pct(metrics.accuracy_all)} | `uncertain` counts as not correct |",
        f"| Checker agreement | {agreement} | Check outcomes that matched the hand-annotated expected outcome (gold claims only) |",
        "",
        "### By group",
        "",
        "| Group | Cases | Decided | Accuracy on decided | Wrong `invalid` |",
        "|---|---|---|---|---|",
        *_group_rows(results),
        "",
        "### Per label",
        "",
        "| Label | Precision | Recall |",
        "|---|---|---|",
    ]
    for label in LABELS:
        lines.append(f"| `{label}` | {_pct(metrics.precision[label])} | {_pct(metrics.recall[label])} |")
    lines += ["", "### Confusion matrix (rows: maintainer label, columns: Probe)", "",
              "| | " + " | ".join(f"`{p}`" for p in PREDICTIONS) + " |",
              "|---|" + "---|" * len(PREDICTIONS)]
    for label in LABELS:
        lines.append(f"| `{label}` | " + " | ".join(str(metrics.confusion[label][p]) for p in PREDICTIONS) + " |")

    lines += ["", "## Cases", "", "| Case | Maintainer label | Probe | Claim checks |", "|---|---|---|---|"]
    for r in results:
        if r.error:
            detail = f"snapshot error: {r.error}"
        else:
            parts = []
            for c in r.claims:
                outcomes = []
                for x in r.checks.get(c.id, []):
                    reason = x.details.get("reason")
                    reason_part = f" ({reason})" if reason else ""
                    outcomes.append(f"{x.checker}={x.outcome}{reason_part}")
                parts.append(f"{c.id}: {', '.join(outcomes) or 'no applicable check'}")
            detail = "; ".join(parts)
        mark = "✅" if r.correct else ("⚠️" if r.decisive else "—")
        lines.append(f"| [{r.case.id}]({r.case.url}) | `{r.case.label}` | {mark} `{r.prediction}` | {_cell(detail, 220)} |")

    if metrics.check_mismatches:
        lines += ["", "### Checker mismatches", ""] + [f"- {m}" for m in metrics.check_mismatches]

    lines += [
        "",
        "## Limits of this run",
        "",
        "- The Docker sandbox runs with network isolation (`--network none`) in a standard `python:3.11-slim` environment "
        "without external project dependencies pre-installed. Real-repo repros requiring third-party libraries safely stop at "
        "`uncertain` (`missing_dependency`), preventing false verdicts.",
        "- Gold-claims mode measures the checkers and verdict rules with hand-written claims; it does not "
        "measure LLM extraction.",
        "- The dataset is small (see `eval/cases/`); treat percentages as indicative.",
        "- Cases in the \"checkable\" groups were selected *because* the static and PR checkers can decide them; "
        "report them separately from the maintainer-labelled issues, never as one blended accuracy.",
        "- Labels marked `manual` (PR test claims) come from our own inspection of the PR's changed files, "
        "not from a maintainer label.",
        "",
    ]
    return "\n".join(lines)


def results_to_json(results: list[CaseResult]) -> str:
    return json.dumps(
        [
            {
                "id": r.case.id,
                "label": r.case.label,
                "prediction": r.prediction,
                "error": r.error,
                "claims": [c.model_dump() for c in r.claims],
                "checks": {k: [c.model_dump() for c in v] for k, v in r.checks.items()},
                "verdicts": [v.model_dump() for v in r.verdicts],
            }
            for r in results
        ],
        indent=2,
        ensure_ascii=False,
    )
