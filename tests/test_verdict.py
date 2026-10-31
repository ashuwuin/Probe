from probe.models import Claim, CheckResult
from probe.verdict import evaluate_claim_verdict, aggregate_verdicts, get_overall_verdict, decide_claim


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


def test_verdict_v1_pr_fixes_issue_touching_file_is_uncertain():
    # V1: Touching the file doesn't prove the fix is correct -> uncertain
    claim = Claim(id="c1", type="pr_fixes_issue", issue_number=42, quote="Fixes #42")
    check = CheckResult(
        claim_id="c1",
        checker="pr_diff",
        outcome="supports",
        evidence="The PR modifies billing.py, which issue #42 names.",
    )
    verdict = evaluate_claim_verdict(claim, [check])
    assert verdict.status == "uncertain"
    assert "requires execution testing" in verdict.evidence


def test_verdict_v2_sandbox_supports_wins_over_diff_inconclusive():
    # V2: Bug reproduced in sandbox, but PR diff was also supplied and returned inconclusive
    claim = Claim(id="c1", type="bug_behavior", repro="calculate_tax(-1)", quote="crashes")
    diff_chk = CheckResult(claim_id="c1", checker="pr_diff", outcome="inconclusive", evidence="no target")
    sbx_chk = CheckResult(claim_id="c1", checker="sandbox", outcome="supports", evidence="Reproduced failure: ValueError")

    verdict = evaluate_claim_verdict(claim, [diff_chk, sbx_chk])
    assert verdict.status == "confirmed"


def test_verdict_v3_overall_confirmed_with_one_invalid_helper():
    # V3: Confirmed bug + 1 mistyped helper name -> overall confirmed (not invalid!)
    c_bug = Claim(id="c1", type="bug_behavior", quote="real bug")
    c_helper = Claim(id="c2", type="symbol_exists", symbol="fake_helper", quote="fake_helper")

    v_bug = evaluate_claim_verdict(
        c_bug,
        [CheckResult(claim_id="c1", checker="sandbox", outcome="supports", evidence="crash")],
    )
    v_helper = evaluate_claim_verdict(
        c_helper,
        [CheckResult(claim_id="c2", checker="static_ast", outcome="contradicts", evidence="missing")],
    )

    overall_status, _ = get_overall_verdict([v_bug, v_helper], [c_bug, c_helper])
    assert overall_status == "confirmed"


def test_verdict_v4_supporting_claim_alone_does_not_confirm():
    # V4: "Label exists" (supporting fact) + unverified bug -> overall uncertain
    c_sym = Claim(id="c1", type="symbol_exists", symbol="calculate_tax", quote="calculate_tax exists")
    c_bug = Claim(id="c2", type="bug_behavior", quote="unverified crash")

    v_sym = evaluate_claim_verdict(
        c_sym,
        [CheckResult(claim_id="c1", checker="static_ast", outcome="supports", evidence="found")],
    )
    v_bug = evaluate_claim_verdict(c_bug, [])

    overall_status, _ = get_overall_verdict([v_sym, v_bug], [c_sym, c_bug])
    assert overall_status == "uncertain"


def test_verdict_regex_fallback_never_returns_invalid():
    # Regex fallback extraction must NEVER lead to an invalid verdict
    claim = Claim(id="c1", type="symbol_exists", symbol="missing_func", quote="missing_func")
    check = CheckResult(
        claim_id="c1",
        checker="static_ast",
        outcome="contradicts",
        evidence="Symbol missing_func not found in repository",
        details={"confidence": "high"},
    )
    # With llm method -> invalid
    res_llm = evaluate_claim_verdict(claim, [check], extraction_method="llm")
    assert res_llm.status == "invalid"

    # With regex_fallback method -> downgraded to uncertain
    res_regex = evaluate_claim_verdict(claim, [check], extraction_method="regex_fallback")
    assert res_regex.status == "uncertain"
    assert "regex fallback" in res_regex.evidence
    assert res_regex.confidence == "low"



def test_decide_claim_alias():
    claim = Claim(id="c1", type="symbol_exists", quote="test")
    res = decide_claim(claim, [])
    assert res.status == "uncertain"
