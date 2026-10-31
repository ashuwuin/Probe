"""Tests for eval/compare_extraction.py (claim matching and scoring), no network."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("compare_extraction", ROOT / "eval" / "compare_extraction.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # needed by @dataclass during exec
    spec.loader.exec_module(module)
    return module


ce = _load()


def C(cid, ctype, quote, **kw):
    return {"id": cid, "type": ctype, "quote": quote, **kw}


# ---------------------------------------------------------------- matching rule

@pytest.mark.parametrize(
    ("gold", "other", "expected"),
    [
        (C("g", "bug_behavior", "x", symbol="calculate_tax"), C("e", "bug_behavior", "y", symbol="`calculate_tax()`"), True),
        (C("g", "bug_behavior", "x", symbol="billing.calculate_tax"), C("e", "bug_behavior", "y", symbol="calculate_tax"), True),
        (C("g", "bug_behavior", "x", file="src/billing.py"), C("e", "bug_behavior", "y", file="./billing.py:42"), True),
        (C("g", "bug_behavior", "the app crashes on negative amounts"),
         C("e", "bug_behavior", "crashes on negative amounts sometimes"), True),
        (C("g", "bug_behavior", "x", symbol="calculate_tax"), C("e", "symbol_exists", "y", symbol="calculate_tax"), False),
        (C("g", "bug_behavior", "the app crashes on negative amounts"), C("e", "bug_behavior", "totally different words"), False),
        (C("g", "bug_behavior", "x", file="src/billing.py"), C("e", "bug_behavior", "y", file="old_billing.py"), False),
    ],
)
def test_claims_match(gold, other, expected):
    assert ce.claims_match(gold, other) is expected


def test_match_is_one_to_one():
    gold = [C("g1", "bug_behavior", "a", symbol="f"), C("g2", "bug_behavior", "b", symbol="f")]
    other = [C("e1", "bug_behavior", "c", symbol="f")]
    pairs = ce.match_claims(gold, other)
    assert [(g["id"], e["id"]) for g, e in pairs] == [("g1", "e1")]


def test_quote_overlap_is_relative_to_gold_words():
    assert ce.quote_overlap("a b c d", "a b x y z w") == 0.5
    assert ce.quote_overlap("", "anything") == 0.0


# ---------------------------------------------------------------- scoring

def test_score_recall_precision_and_fields():
    s = ce.Score()
    gold = [
        C("g1", "bug_behavior", "q", symbol="apply_discount", file="shop/billing.py", repro="assert f() == 1"),
        C("g2", "pr_adds_tests", "adds unit tests"),
    ]
    other = [
        C("e1", "bug_behavior", "q", symbol="apply_discount", file="billing.py", repro=None),
        C("e2", "symbol_exists", "noise", symbol="other"),
    ]
    s.add(gold, other)
    assert (s.gold, s.extracted, s.matched) == (2, 2, 1)
    assert s.recall == 0.5 and s.precision == 0.5
    assert s.field_hits["symbol"] == 1 and s.field_totals["symbol"] == 1
    assert s.field_hits["file"] == 1
    assert s.field_hits["repro"] == 0 and s.field_totals["repro"] == 1
    assert s.by_type_found["bug_behavior"] == 1 and s.by_type_found["pr_adds_tests"] == 0


def _row(cid, claims, prediction, verdicts, extraction=None):
    row = {"id": cid, "claims": claims, "prediction": prediction, "verdicts": verdicts}
    if extraction is not None:
        row["extraction"] = extraction
    return row


def test_compare_end_to_end_with_regex_baseline():
    text = "`apply_discount()` returns 0.0 instead of 90.0 and crashes with ValueError."
    gold = [_row("c1", [C("c1", "bug_behavior", text, symbol="apply_discount")], "uncertain",
                 [{"claim_id": "c1", "status": "uncertain"}])]
    extracted = [_row("c1", [C("x1", "bug_behavior", text, symbol="apply_discount")], "uncertain",
                      [{"claim_id": "x1", "status": "uncertain"}],
                      {"method": "llm", "model": "qwen3:8b", "seconds": 3.5,
                       "dropped_claims": [{"reason": "quote_not_in_text"}], "is_pull_request": False,
                       "text_source": "github_issue", "text": text, "model_digest": "abc", "temperature": 0})]
    result = ce.compare(gold, extracted)
    assert result["model"].matched == 1 and result["model"].recall == 1.0
    assert result["verdict_same"] == 1 and result["claim_verdict_same"] == 1
    assert result["drops"]["quote_not_in_text"] == 1
    assert result["methods"]["llm"] == 1
    assert result["regex"].gold == 1  # baseline ran on the same text
    md = ce.render(result)
    assert "LLM claim extraction vs. gold claims" in md and "`abc`" in md
    assert "Regex fallback (same texts)" in md


def test_cases_without_extraction_metadata_are_skipped():
    gold = [_row("c1", [C("c1", "bug_behavior", "q")], "uncertain", [])]
    extracted = [_row("c1", [], "uncertain", [])]
    assert ce.compare(gold, extracted)["verdict_total"] == 0


def test_main_requires_metadata(tmp_path):
    g, e = tmp_path / "g.json", tmp_path / "e.json"
    g.write_text(json.dumps([_row("c1", [], "uncertain", [])]))
    e.write_text(json.dumps([_row("c1", [], "uncertain", [])]))
    assert ce.main(["--gold", str(g), "--extracted", str(e), "--out", str(tmp_path / "o.md")]) == 1


def test_fallback_texts_are_flagged_in_report():
    gold = [_row("s1", [C("c1", "bug_behavior", "q")], "uncertain", [])]
    extracted = [_row("s1", [], "uncertain", [],
                      {"method": "llm", "model": "m", "seconds": 1.0, "dropped_claims": [],
                       "is_pull_request": False, "text_source": "gold_quotes_fallback", "text": "q"})]
    assert "had no text of their own (s1)" in ce.render(ce.compare(gold, extracted))
