"""Verdict Aggregator & Deterministic Truth Table.

Translates tool CheckResults into authoritative VerdictResults:
- confirmed: Hard proof that the claim holds (reproduced in sandbox, test added in diff).
- cannot-reproduce: Code executed cleanly when a crash or failed assertion was claimed.
- invalid: Concrete proof that the claim is false (missing symbol, 0% test coverage).
- uncertain: Ambiguous evidence, missing repro steps, unasserted outputs, or sandbox unavailable (WE NEVER GUESS).

Overall issue triage policy:
- Evaluates core allegations (bug_behavior, security_vuln, pr_adds_tests, pr_fixes_issue).
- Supporting facts alone (symbol_exists, version_affected) NEVER make an issue confirmed.
- Confirmed if ANY core claim is confirmed.
- Invalid ONLY if EVERY core claim is invalid (one narrow contradiction does not invalidate a whole issue).
- When extraction method is 'regex_fallback', claims are NEVER marked 'invalid' (downgraded to 'uncertain').
"""

from __future__ import annotations

from probe.models import Claim, CheckResult, VerdictResult, VerdictStatus

CORE_TYPES = frozenset({"bug_behavior", "security_vuln", "pr_fixes_issue", "pr_adds_tests"})


def _guard_regex_fallback(verdict: VerdictResult, extraction_method: str) -> VerdictResult:
    """Never return 'invalid' when the claim was extracted using the regex fallback."""
    if verdict.status == "invalid" and extraction_method == "regex_fallback":
        return VerdictResult(
            claim_id=verdict.claim_id,
            status="uncertain",
            confidence="low",
            evidence=f"{verdict.evidence} (Claim extracted via regex fallback; not invalidated without high-confidence extraction)",
            quote=verdict.quote,
        )
    return verdict


def evaluate_claim_verdict(
    claim: Claim,
    results: list[CheckResult],
    *,
    extraction_method: str = "llm",
) -> VerdictResult:
    """Evaluates all check results for a single claim against the deterministic truth table."""
    results_map = {r.checker: r for r in results if r.claim_id == claim.id}

    static_res = results_map.get("static_ast")
    diff_res = results_map.get("pr_diff")
    sandbox_res = results_map.get("sandbox")

    # Rule 1: Sandbox Reproduction takes top precedence
    # A hard execution reproduction in the sandbox beats a static AST contradiction!
    if sandbox_res and sandbox_res.outcome == "supports":
        return VerdictResult(
            claim_id=claim.id,
            status="confirmed",
            confidence="high",
            evidence=sandbox_res.evidence,
            quote=claim.quote,
        )

    # Rule 2: Static AST Contradiction (Precondition failure)
    # Applied only when sandbox did not successfully reproduce a crash
    if static_res and static_res.outcome == "contradicts":
        has_near_match = "closest match" in static_res.evidence.lower() or "did you mean" in static_res.evidence.lower()
        res = VerdictResult(
            claim_id=claim.id,
            status="invalid",
            confidence="medium" if has_near_match else static_res.details.get("confidence", "high"),
            evidence=static_res.evidence,
            quote=claim.quote,
        )
        return _guard_regex_fallback(res, extraction_method)

    # Rule 3: Sandbox Contradiction (clean execution when crash was claimed)
    if sandbox_res and sandbox_res.outcome == "contradicts":
        return VerdictResult(
            claim_id=claim.id,
            status="cannot-reproduce",
            confidence="medium",
            evidence=sandbox_res.evidence,
            quote=claim.quote,
        )

    # Rule 4: PR Diff & Test Check
    if diff_res:
        if claim.type == "pr_adds_tests":
            if diff_res.outcome == "supports":
                return VerdictResult(
                    claim_id=claim.id,
                    status="confirmed",
                    confidence=diff_res.details.get("confidence", "high"),
                    evidence=diff_res.evidence,
                    quote=claim.quote,
                )
            elif diff_res.outcome == "contradicts":
                res = VerdictResult(
                    claim_id=claim.id,
                    status="invalid",
                    confidence=diff_res.details.get("confidence", "high"),
                    evidence=diff_res.evidence,
                    quote=claim.quote,
                )
                return _guard_regex_fallback(res, extraction_method)
        elif claim.type == "pr_fixes_issue":
            # Touching the reported file/symbol correlates with the issue, but does NOT prove the fix is correct.
            if diff_res.outcome == "supports":
                return VerdictResult(
                    claim_id=claim.id,
                    status="uncertain",
                    confidence="medium",
                    evidence=f"PR touches reported target ({diff_res.evidence}), but fix correctness requires execution testing.",
                    quote=claim.quote,
                )
            else:
                return VerdictResult(
                    claim_id=claim.id,
                    status="uncertain",
                    confidence="low",
                    evidence=diff_res.evidence,
                    quote=claim.quote,
                )

    # Rule 5: Static Symbol Existence (Supporting fact)
    if static_res and static_res.outcome == "supports" and claim.type == "symbol_exists":
        return VerdictResult(
            claim_id=claim.id,
            status="confirmed",
            confidence=static_res.details.get("confidence", "high"),
            evidence=static_res.evidence,
            quote=claim.quote,
        )

    # Rule 6: Behavioral claims with verified symbol but missing repro steps
    if static_res and static_res.outcome == "supports" and claim.type in ("bug_behavior", "security_vuln") and not claim.repro:
        return VerdictResult(
            claim_id=claim.id,
            status="uncertain",
            confidence="medium",
            evidence=f"Symbol verified ({static_res.evidence}), but claim requires reproduction steps to confirm behavior",
            quote=claim.quote,
        )

    # Rule 7: Sandbox unavailable
    if sandbox_res and sandbox_res.details.get("reason") in ("sandbox_unavailable", "image_unavailable"):
        return VerdictResult(
            claim_id=claim.id,
            status="uncertain",
            confidence="low",
            evidence="Docker sandbox unavailable to verify execution behavior safely",
            quote=claim.quote,
        )

    # Rule 8: Default Honest Fallback (Never guess)
    evidence_notes = "; ".join([r.evidence for r in results_map.values() if r.evidence])
    return VerdictResult(
        claim_id=claim.id,
        status="uncertain",
        confidence="low",
        evidence=evidence_notes or "Insufficient evidence to confirm or invalidate claim",
        quote=claim.quote,
    )


# Alias for compatibility with PR #4 benchmark suite
decide_claim = evaluate_claim_verdict


def aggregate_verdicts(
    claims: list[Claim],
    all_results: list[CheckResult],
    *,
    extraction_method: str = "llm",
) -> list[VerdictResult]:
    """Generates a VerdictResult for each claim."""
    return [
        evaluate_claim_verdict(c, all_results, extraction_method=extraction_method)
        for c in claims
    ]


def get_overall_verdict(verdicts: list[VerdictResult], claims: list[Claim] | None = None) -> tuple[VerdictStatus, str]:
    """Computes overall status from claim verdicts using core-claim prioritization.

    - Core claims (bug_behavior, security_vuln, pr_adds_tests, pr_fixes_issue) carry the report's allegation.
    - Supporting-only claims (symbol_exists, version_affected) NEVER confirm an issue on their own.
    - If ANY core claim is confirmed -> 'confirmed'.
    - 'invalid' ONLY if EVERY core claim is invalid (narrow contradictions do not dismiss whole issues).
    - If any core claim is cannot-reproduce and none confirmed/invalid -> 'cannot-reproduce'.
    - Otherwise -> 'uncertain'.
    """
    if not verdicts:
        return "uncertain", "No claims were evaluated"

    by_id = {v.claim_id: v for v in verdicts}

    if claims:
        core_claims = [c for c in claims if c.type in CORE_TYPES]
        if core_claims:
            statuses = [by_id[c.id].status for c in core_claims if c.id in by_id]
        else:
            # Supporting-only claims should NEVER confirm an issue
            statuses = [v.status for v in verdicts]
            if all(s == "invalid" for s in statuses):
                return "invalid", "All technical claims were proven invalid (e.g. non-existent symbols)"
            return "uncertain", "Only supporting facts (such as symbol existence) were verified; bug behavior was not confirmed."
    else:
        statuses = [v.status for v in verdicts]

    if not statuses:
        return "uncertain", "No claims were evaluated"

    if "confirmed" in statuses:
        return "confirmed", "One or more core technical claims were successfully confirmed"
    if all(s == "invalid" for s in statuses):
        return "invalid", "All core technical claims were proven invalid (e.g. non-existent symbols or missing tests)"
    if "cannot-reproduce" in statuses:
        return "cannot-reproduce", "Reproduction steps ran cleanly without error"
    return "uncertain", "Evidence was inconclusive or required steps are missing"
