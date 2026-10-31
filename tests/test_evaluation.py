"""Offline tests for probe.evaluation and eval/benchmark.py."""

import importlib.util
import json
from pathlib import Path

import pytest

from probe.evaluation import (
    EvalCase,
    _load_claim_verdict_fn,
    _load_overall_fn,
    baseline_claim_verdict,
    baseline_issue_prediction,
    compute_metrics,
    evaluate_case,
    issue_prediction,
    load_cases,
    render_markdown,
    run_checks,
)
from probe.models import CheckResult, Claim, VerdictResult
from probe.verify.sandbox import is_docker_available

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def claim(cid="c1", ctype="bug_behavior", **kw):
    return Claim(id=cid, type=ctype, quote=kw.pop("quote", "quoted"), **kw)


def check(outcome, checker="static_ast", **details):
    return CheckResult(claim_id="c1", checker=checker, outcome=outcome, evidence=f"{checker} said {outcome}", details=details)


def case(label="invalid", claims=None, cid="x-1", **kw):
    return EvalCase(
        id=cid, url="https://github.com/octo/demo/issues/1", label=label, sha=SHA,
        gold_claims=claims or [claim(symbol="calculate_tax")], **kw,
    )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "billing.py").write_text("TAX = 1\n\ndef calculate_tax(x):\n    return x\n")
    return tmp_path


# ---------------------------------------------------------------- real dataset

def test_real_eval_cases_are_valid():
    cases = load_cases(ROOT / "eval" / "cases")
    assert len(cases) >= 10
    assert {c.label for c in cases} == {"confirmed", "cannot-reproduce", "invalid"}
    for c in cases:
        assert c.url.startswith("https://github.com/")
        assert c.gold_claims, c.id
        ids = {cl.id for cl in c.gold_claims}
        assert set(c.expected_outcomes) <= ids, c.id


# ---------------------------------------------------------------- routing

def test_run_checks_routes_static_and_pr(repo):
    assert [r.checker for r in run_checks(claim(symbol="calculate_tax"), repo)] == ["static_ast"]
    assert run_checks(claim(), repo) == []  # behaviour claim with no file/symbol
    pr = run_checks(claim(ctype="pr_adds_tests"), repo, ["tests/test_x.py"])
    assert [r.checker for r in pr] == ["pr_diff"] and pr[0].outcome == "supports"


# ---------------------------------------------------------------- baseline rules

@pytest.mark.parametrize(
    ("ctype", "results", "expected"),
    [
        ("bug_behavior", [check("contradicts", confidence="high")], "invalid"),
        ("bug_behavior", [check("supports", confidence="high")], "uncertain"),
        ("bug_behavior", [check("inconclusive")], "uncertain"),
        ("bug_behavior", [], "uncertain"),
        ("symbol_exists", [check("supports", confidence="high")], "confirmed"),
        ("bug_behavior", [check("supports"), check("supports", checker="sandbox")], "confirmed"),
        ("bug_behavior", [check("supports"), check("contradicts", checker="sandbox")], "cannot-reproduce"),
        ("bug_behavior", [check("inconclusive", checker="sandbox")], "uncertain"),
        ("pr_adds_tests", [check("supports", checker="pr_diff", confidence="high")], "confirmed"),
        ("pr_adds_tests", [check("contradicts", checker="pr_diff")], "invalid"),
        ("pr_fixes_issue", [check("supports", checker="pr_diff")], "uncertain"),
    ],
)
def test_baseline_rules(ctype, results, expected):
    v = baseline_claim_verdict(claim(ctype=ctype), results)
    assert v.status == expected
    assert v.quote == "quoted"


# ---------------------------------------------------------------- issue level

def _v(cid, status):
    return VerdictResult(claim_id=cid, status=status, confidence="low", evidence="e", quote="q")


def test_supporting_claim_alone_does_not_confirm_issue():
    claims = [claim("c1", "symbol_exists", symbol="Label"), claim("c2", "bug_behavior")]
    assert baseline_issue_prediction(claims, [_v("c1", "confirmed"), _v("c2", "uncertain")]) == "uncertain"


def test_invalid_requires_every_core_claim_contradicted():
    claims = [claim("c1"), claim("c2")]
    assert baseline_issue_prediction(claims, [_v("c1", "invalid"), _v("c2", "uncertain")]) == "uncertain"
    assert baseline_issue_prediction(claims, [_v("c1", "invalid"), _v("c2", "invalid")]) == "invalid"


def test_confirmed_wins_over_other_core_verdicts():
    claims = [claim("c1"), claim("c2")]
    assert baseline_issue_prediction(claims, [_v("c1", "invalid"), _v("c2", "confirmed")]) == "confirmed"


def test_supporting_only_claims_can_be_invalid():
    assert baseline_issue_prediction([claim("c1", "symbol_exists")], [_v("c1", "invalid")]) == "invalid"


@pytest.mark.parametrize(
    "statuses",
    [["confirmed"], ["confirmed", "confirmed"], ["confirmed", "invalid"], ["uncertain"], ["cannot-reproduce"]],
)
def test_supporting_only_claims_never_confirm_an_issue(statuses):
    claims = [claim(f"c{i}", "symbol_exists" if i % 2 else "version_affected") for i in range(len(statuses))]
    verdicts = [_v(f"c{i}", s) for i, s in enumerate(statuses)]
    assert issue_prediction(claims, verdicts) == "uncertain"


def test_crash_report_with_only_existing_symbol_is_uncertain(repo):
    """A traceback-only crash report where extraction found just "calculate_tax exists"."""
    r = evaluate_case(case("invalid", [claim("c1", "symbol_exists", symbol="calculate_tax")]), repo)
    assert r.verdicts[0].status == "confirmed"   # the function does exist...
    assert r.prediction == "uncertain"            # ...but that does not confirm the bug report


def test_no_claims_is_uncertain():
    assert baseline_issue_prediction([], []) == "uncertain"


# ---------------------------------------------------------------- evaluate_case

def test_evaluate_case_end_to_end(repo):
    r = evaluate_case(case("invalid", [claim(symbol="refund_everything")]), repo)
    assert r.checks["c1"][0].outcome == "contradicts"
    assert r.prediction == "invalid" and r.correct


def test_missing_snapshot_is_uncertain_not_crash():
    r = evaluate_case(case(), None, error="NotFoundError: gone")
    assert r.prediction == "uncertain" and r.error == "NotFoundError: gone"
    assert "gone" in r.verdicts[0].evidence


# ---------------------------------------------------------------- metrics

def test_metrics_counts_and_false_invalid(repo):
    results = [
        evaluate_case(case("invalid", [claim(symbol="nope_fn")], cid="a"), repo),     # correct invalid
        evaluate_case(case("confirmed", [claim(symbol="nope_fn")], cid="b"), repo),   # wrong invalid
        evaluate_case(case("cannot-reproduce", [claim(symbol="calculate_tax")], cid="c"), repo),  # uncertain
    ]
    m = compute_metrics(results)
    assert (m.total, m.decisive, m.correct) == (3, 2, 1)
    assert m.false_invalid == ["b"]
    assert m.precision["invalid"] == 0.5 and m.recall["invalid"] == 1.0
    assert m.recall["confirmed"] == 0.0 and m.precision["confirmed"] is None
    assert m.confusion["cannot-reproduce"]["uncertain"] == 1
    assert m.unsupported_decisive == []
    assert m.accuracy_decisive == 0.5 and round(m.accuracy_all, 2) == 0.33


def test_metrics_flags_decisive_verdict_without_evidence(repo):
    def reckless(c, results, **_):
        return VerdictResult(claim_id=c.id, status="confirmed", confidence="high", evidence="trust me", quote=c.quote)

    r = evaluate_case(case("confirmed", [claim()]), repo, decide=reckless)
    assert compute_metrics([r]).unsupported_decisive == ["x-1:c1"]


def test_checker_agreement(repo):
    c = case("invalid", [claim(symbol="calculate_tax")], expected_outcomes={"c1": "contradicts"})
    m = compute_metrics([evaluate_case(c, repo)])
    assert m.check_agreement == (0, 1)
    assert "expected contradicts, got supports" in m.check_mismatches[0]


def test_render_markdown(repo):
    results = [evaluate_case(case("invalid", [claim(symbol="nope|fn")]), repo)]
    md = render_markdown(results, compute_metrics(results), mode="gold", verdict_source="baseline", generated="now")
    assert "# Probe evaluation results" in md
    assert "Wrong `invalid` verdicts" in md and "Decisive verdicts without evidence" in md
    assert "| `invalid` |" in md


# ---------------------------------------------------------------- CLI runner

def _load_benchmark():
    spec = importlib.util.spec_from_file_location("probe_benchmark", ROOT / "eval" / "benchmark.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_cli_with_fake_client(tmp_path, repo, monkeypatch):
    bench = _load_benchmark()
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "a.json").write_text(case("invalid", [claim(symbol="nope_fn")], cid="a").model_dump_json())
    (cases_dir / "b.json").write_text(case("confirmed", cid="b").model_dump_json())

    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): ...
        def snapshot(self, owner, repo_name, sha, resolved_by):
            if sha != SHA:
                raise ValueError("bad sha")
            return type("Snap", (), {"path": repo})()

    monkeypatch.setattr(bench, "GitHubClient", FakeClient)
    out, js = tmp_path / "R.md", tmp_path / "r.json"
    assert bench.main(["--cases", str(cases_dir), "--out", str(out), "--json", str(js)]) == 0
    assert "Probe evaluation results" in out.read_text()
    data = json.loads(js.read_text())
    assert [d["prediction"] for d in data] == ["invalid", "uncertain"]


def test_benchmark_cli_no_cases(tmp_path):
    assert _load_benchmark().main(["--cases", str(tmp_path)]) == 1


# ---------------------------------------------------------------- examples/

def _load_examples_module():
    spec = importlib.util.spec_from_file_location("probe_examples", ROOT / "examples" / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_quotes_appear_in_inputs():
    manifest = json.loads((ROOT / "examples" / "manifest.json").read_text())
    for example in manifest:
        text = (ROOT / "examples" / example["input"]).read_text()
        for c in example["claims"]:
            Claim(**c)
            assert c["quote"] in text, (example["id"], c["id"])


@pytest.mark.parametrize(
    ("example_id", "status"),
    [("01_confirmed_bug", "pending sandbox"), ("03_cannot_reproduce", "pending sandbox"), ("05_pr_missing_tests", "matches expected")],
)
def test_local_examples_offline(example_id, status):
    gen = _load_examples_module()
    manifest = {e["id"]: e for e in json.loads((ROOT / "examples" / "manifest.json").read_text())}
    result = gen.run_example(manifest[example_id], gh=None)
    if is_docker_available():
        assert result["status"] == "matches expected"
    else:
        assert result["status"] == status
    assert "Probe evidence report" in gen.render(result)



# ---------------------------------------------------------------- shared rules (probe.verdict)

def test_benchmark_uses_probe_verdict_when_it_exists():
    """Fails if probe.verdict is importable but the benchmark silently falls back to the baseline."""
    verdict = pytest.importorskip("probe.verdict")
    decide, source = _load_claim_verdict_fn()
    assert decide is not baseline_claim_verdict, source
    assert source.startswith("probe.verdict.")
    overall, overall_source = _load_overall_fn()
    assert overall is not baseline_issue_prediction
    assert overall_source == "probe.verdict.get_overall_verdict"
    assert callable(getattr(verdict, "evaluate_claim_verdict"))


def test_shared_overall_rule_never_confirms_on_supporting_facts_alone():
    """Whatever rule is active (probe.verdict or baseline): "the function exists"
    must not make a bug report confirmed."""
    claims = [claim("c1", "symbol_exists", symbol="calculate_tax")]
    assert issue_prediction(claims, [_v("c1", "confirmed")]) == "uncertain"


def test_shared_overall_rule_one_narrow_contradiction_is_not_invalid():
    claims = [claim("c1"), claim("c2", "symbol_exists")]
    assert issue_prediction(claims, [_v("c1", "confirmed"), _v("c2", "invalid")]) == "confirmed"
    assert issue_prediction([claim("c1"), claim("c2")], [_v("c1", "invalid"), _v("c2", "uncertain")]) == "uncertain"


def test_group_rows_report_coverage_per_group(repo):
    a = evaluate_case(case("invalid", [claim(symbol="nope_fn")], cid="a", group="checkable"), repo)
    b = evaluate_case(case("confirmed", cid="b"), repo)
    md = render_markdown([a, b], compute_metrics([a, b]), mode="gold", verdict_source="x", generated="now")
    assert "| checkable | 1 | 1/1 | 1/1 (100%) | 0 |" in md
    assert "| maintainer-labelled issues | 1 | 0/1 | n/a | 0 |" in md



# ---------------------------------------------------------------- extracted mode / extraction method

def test_checker_agreement_skipped_for_extracted_claims(repo):
    c = case("invalid", [claim(symbol="calculate_tax")], expected_outcomes={"c1": "contradicts"})
    extracted = [claim("c1", symbol="refund_everything")]  # same id, different claim
    r = evaluate_case(c, repo, claims=extracted)
    assert r.used_gold_claims is False
    m = compute_metrics([r])
    assert m.check_agreement == (0, 0) and m.check_mismatches == []
    md = render_markdown([r], m, mode="extracted", verdict_source="x", generated="now")
    assert "| Checker agreement | n/a (extracted mode) |" in md


def test_gold_mode_marks_used_gold_claims(repo):
    assert evaluate_case(case(), repo).used_gold_claims is True


@pytest.mark.parametrize("decide_name", ["baseline", "active"])
def test_regex_fallback_claim_is_never_invalid(repo, decide_name):
    decide = baseline_claim_verdict if decide_name == "baseline" else _load_claim_verdict_fn()[0]
    c = case("invalid", [claim(symbol="refund_everything")])
    llm = evaluate_case(c, repo, claims=c.gold_claims, decide=decide, extraction_method="llm")
    regex = evaluate_case(c, repo, claims=c.gold_claims, decide=decide, extraction_method="regex_fallback")
    assert llm.checks["c1"][0].outcome == "contradicts"
    assert llm.verdicts[0].status == "invalid"
    assert regex.verdicts[0].status == "uncertain"
    assert regex.prediction == "uncertain"


def test_extraction_method_is_passed_to_decide(repo):
    seen = []

    def spy(c, results, *, extraction_method):
        seen.append(extraction_method)
        return baseline_claim_verdict(c, results, extraction_method=extraction_method)

    evaluate_case(case(), repo, decide=spy)
    evaluate_case(case(), repo, claims=[claim()], decide=spy, extraction_method="regex_fallback")
    assert seen == ["gold", "regex_fallback"]


def test_overall_rule_errors_are_not_hidden(monkeypatch):
    """A signature mismatch in get_overall_verdict must surface, not silently use another rule."""
    import probe.evaluation as ev

    fake = type("FakeVerdict", (), {"get_overall_verdict": staticmethod(lambda verdicts: ("confirmed", ""))})
    monkeypatch.setattr(ev, "_verdict_module", lambda: fake)
    overall, source = ev._load_overall_fn()
    assert source == "probe.verdict.get_overall_verdict"
    with pytest.raises(TypeError):
        overall([claim()], [_v("c1", "uncertain")])
