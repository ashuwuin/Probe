"""Verdict Aggregator & Truth Table.

Translates tool CheckResults into authoritative VerdictResults:
- confirmed: Hard proof that the claim holds (reproduced in sandbox, tests verified).
- cannot-reproduce: Code executed cleanly when failure was claimed.
- invalid: Concrete proof that the claim is false (missing symbol, 0% test coverage).
- uncertain: Ambiguous evidence, missing repro steps, or sandbox unavailable (WE NEVER GUESS).
"""

from probe.models import Claim, CheckResult, VerdictResult, VerdictStatus


def evaluate_claim_verdict(claim: Claim, results: list[CheckResult]) -> VerdictResult:
    """Evaluates all check results for a single claim against the deterministic truth table."""
    results_map = {r.checker: r for r in results if r.claim_id == claim.id}

    # Rule 1: Static AST Check
    static_res = results_map.get("static_ast")
    if static_res:
        if static_res.outcome == "contradicts":
            has_near_match = "closest match" in static_res.evidence.lower() or "did you mean" in static_res.evidence.lower()
            return VerdictResult(
                claim_id=claim.id,
                status="invalid",
                confidence="medium" if has_near_match else "high",
                evidence=static_res.evidence,
                quote=claim.quote,
            )
        elif static_res.outcome == "supports" and claim.type == "symbol_exists":
            confidence = static_res.details.get("confidence", "high")
            return VerdictResult(
                claim_id=claim.id,
                status="confirmed",
                confidence=confidence,
                evidence=static_res.evidence,
                quote=claim.quote,
            )

    # Rule 2: PR Diff & Test Check
    diff_res = results_map.get("pr_diff")
    if diff_res:
        if diff_res.outcome == "contradicts":
            return VerdictResult(
                claim_id=claim.id,
                status="invalid",
                confidence=diff_res.details.get("confidence", "high"),
                evidence=diff_res.evidence,
                quote=claim.quote,
            )
        elif diff_res.outcome == "supports":
            confidence = diff_res.details.get("confidence", "high")
            return VerdictResult(
                claim_id=claim.id,
                status="confirmed",
                confidence=confidence,
                evidence=diff_res.evidence,
                quote=claim.quote,
            )
        elif diff_res.outcome == "inconclusive":
            return VerdictResult(
                claim_id=claim.id,
                status="uncertain",
                confidence="medium",
                evidence=diff_res.evidence,
                quote=claim.quote,
            )

    # Rule 3: Sandbox Reproduction Check
    sandbox_res = results_map.get("sandbox")
    if sandbox_res:
        if sandbox_res.outcome == "supports":
            return VerdictResult(
                claim_id=claim.id,
                status="confirmed",
                confidence="high",
                evidence=sandbox_res.evidence,
                quote=claim.quote,
            )
        elif sandbox_res.outcome == "contradicts":
            return VerdictResult(
                claim_id=claim.id,
                status="cannot-reproduce",
                confidence="medium",
                evidence=sandbox_res.evidence,
                quote=claim.quote,
            )
        elif sandbox_res.details.get("reason") == "sandbox_unavailable":
            return VerdictResult(
                claim_id=claim.id,
                status="uncertain",
                confidence="low",
                evidence="Docker sandbox unavailable to verify execution behavior safely",
                quote=claim.quote,
            )

    # Rule 4: If symbol exists but no repro steps provided for behavioral claims
    if static_res and static_res.outcome == "supports" and claim.type in ("bug_behavior", "security_vuln") and not claim.repro:
        return VerdictResult(
            claim_id=claim.id,
            status="uncertain",
            confidence="medium",
            evidence=f"Symbol verified ({static_res.evidence}), but claim requires reproduction steps to confirm behavior",
            quote=claim.quote,
        )

    # Rule 5: Default Honest Fallback (Never guess)
    evidence_notes = "; ".join([r.evidence for r in results_map.values() if r.evidence])
    return VerdictResult(
        claim_id=claim.id,
        status="uncertain",
        confidence="low",
        evidence=evidence_notes or "Insufficient evidence to confirm or invalidate claim",
        quote=claim.quote,
    )


def aggregate_verdicts(claims: list[Claim], all_results: list[CheckResult]) -> list[VerdictResult]:
    """Generates a VerdictResult for each claim."""
    return [evaluate_claim_verdict(c, all_results) for c in claims]


def get_overall_verdict(verdicts: list[VerdictResult]) -> tuple[VerdictStatus, str]:
    """Computes overall status from all individual claim verdicts.

    Precedence: invalid > confirmed > cannot-reproduce > uncertain.
    """
    if not verdicts:
        return "uncertain", "No claims were evaluated"

    statuses = [v.status for v in verdicts]

    if "invalid" in statuses:
        return "invalid", "One or more technical claims were proven invalid"
    elif "confirmed" in statuses:
        return "confirmed", "One or more technical claims were successfully reproduced and confirmed"
    elif "cannot-reproduce" in statuses:
        return "cannot-reproduce", "Reproduction steps ran cleanly without error"
    else:
        return "uncertain", "Evidence was inconclusive or required steps are missing"
