from probe.models import Claim, CheckResult
from probe.verdict import evaluate_claim_verdict, aggregate_verdicts, get_overall_verdict


def test_verdict_static_contradicts_is_invalid():
    claim = Claim(id="c1", type="symbol_exists", symbol="parse_url_v2", quote="RCE in parse_url_v2()")
    check = CheckResult(
        claim_id="c1",
        checker="static_ast",
        outcome="contradicts",
        evidence="Symbol parse_url_v2 not found in repository",
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "invalid"
    assert verdict.confidence == "high"


def test_verdict_static_near_match_is_medium_confidence():
    claim = Claim(id="c1", type="symbol_exists", symbol="parse_url_v2", quote="bug in parse_url_v2")
    check = CheckResult(
        claim_id="c1",
        checker="static_ast",
        outcome="contradicts",
        evidence="Symbol parse_url_v2 not found; closest match: parse_url (line 42)",
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "invalid"
    assert verdict.confidence == "medium"


def test_verdict_sandbox_supports_is_confirmed():
    claim = Claim(id="c2", type="bug_behavior", quote="crashes on large URL")
    check = CheckResult(
        claim_id="c2",
        checker="sandbox",
        outcome="supports",
        evidence="Reproduced failure (exit code 1): RecursionError",
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "confirmed"
    assert verdict.confidence == "high"


def test_verdict_sandbox_contradicts_is_cannot_reproduce():
    claim = Claim(id="c3", type="bug_behavior", quote="crashes on load")
    check = CheckResult(
        claim_id="c3",
        checker="sandbox",
        outcome="contradicts",
        evidence="Ran cleanly with exit code 0",
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "cannot-reproduce"
    assert verdict.confidence == "medium"


def test_verdict_pr_diff_contradicts_is_invalid():
    claim = Claim(id="c4", type="pr_adds_tests", quote="Fixes #120, adds tests")
    check = CheckResult(
        claim_id="c4",
        checker="pr_diff",
        outcome="contradicts",
        evidence="PR claims to add tests, but 0 test files were modified",
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "invalid"
    assert verdict.confidence == "high"


def test_verdict_sandbox_unavailable_is_uncertain():
    claim = Claim(id="c5", type="bug_behavior", quote="crashes on click")
    check = CheckResult(
        claim_id="c5",
        checker="sandbox",
        outcome="inconclusive",
        evidence="sandbox_unavailable",
        details={"reason": "sandbox_unavailable"},
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "uncertain"
    assert "unavailable" in verdict.evidence


def test_overall_verdict_precedence():
    v_invalid = evaluate_claim_verdict(
        Claim(id="c1", type="symbol_exists", quote="q1"),
        [CheckResult(claim_id="c1", checker="static_ast", outcome="contradicts", evidence="missing")],
    )
    v_confirmed = evaluate_claim_verdict(
        Claim(id="c2", type="bug_behavior", quote="q2"),
        [CheckResult(claim_id="c2", checker="sandbox", outcome="supports", evidence="crash")],
    )

    # invalid outranks confirmed
    overall_status, _ = get_overall_verdict([v_invalid, v_confirmed])
    assert overall_status == "invalid"

    # confirmed outranks cannot-reproduce
    v_cannot = evaluate_claim_verdict(
        Claim(id="c3", type="bug_behavior", quote="q3"),
        [CheckResult(claim_id="c3", checker="sandbox", outcome="contradicts", evidence="clean")],
    )
    overall_status, _ = get_overall_verdict([v_confirmed, v_cannot])
    assert overall_status == "confirmed"


def test_verdict_symbol_exists_supported_is_confirmed():
    claim = Claim(id="c1", type="symbol_exists", symbol="calculate_tax", quote="calculate_tax exists")
    check = CheckResult(
        claim_id="c1",
        checker="static_ast",
        outcome="supports",
        evidence="calculate_tax is defined in billing.py at line 14",
        details={"confidence": "high"},
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "confirmed"
    assert verdict.confidence == "high"


def test_verdict_pr_fixes_issue_supported_is_confirmed():
    claim = Claim(id="c2", type="pr_fixes_issue", issue_number=42, quote="Fixes #42")
    check = CheckResult(
        claim_id="c2",
        checker="pr_diff",
        outcome="supports",
        evidence="The PR modifies billing.py, which issue #42 names.",
        details={"confidence": "medium"},
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "confirmed"
    assert verdict.confidence == "medium"


def test_verdict_bug_behavior_without_repro_is_uncertain():
    claim = Claim(id="c3", type="bug_behavior", symbol="calculate_tax", repro=None, quote="crashes on tax")
    check = CheckResult(
        claim_id="c3",
        checker="static_ast",
        outcome="supports",
        evidence="calculate_tax defined in billing.py",
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "uncertain"
    assert "requires reproduction steps" in verdict.evidence

