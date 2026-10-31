"""Offline tests for probe.evaluation and eval/benchmark.py."""

import importlib.util
import json
from pathlib import Path

import pytest

from probe.evaluation import (
    EvalCase,
    baseline_claim_verdict,
    compute_metrics,
    evaluate_case,
    issue_prediction,
    load_cases,
    render_markdown,
    run_checks,
)
from probe.models import CheckResult, Claim, VerdictResult

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
    assert issue_prediction(claims, [_v("c1", "confirmed"), _v("c2", "uncertain")]) == "uncertain"


def test_invalid_requires_every_core_claim_contradicted():
    claims = [claim("c1"), claim("c2")]
    assert issue_prediction(claims, [_v("c1", "invalid"), _v("c2", "uncertain")]) == "uncertain"
    assert issue_prediction(claims, [_v("c1", "invalid"), _v("c2", "invalid")]) == "invalid"


def test_confirmed_wins_over_other_core_verdicts():
    claims = [claim("c1"), claim("c2")]
    assert issue_prediction(claims, [_v("c1", "invalid"), _v("c2", "confirmed")]) == "confirmed"


def test_only_supporting_claims_are_used_when_no_core_claims():
    assert issue_prediction([claim("c1", "symbol_exists")], [_v("c1", "invalid")]) == "invalid"


def test_no_claims_is_uncertain():
    assert issue_prediction([], []) == "uncertain"


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
    def reckless(c, results):
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
    assert result["status"] == status
    assert "Probe evidence report" in gen.render(result)
