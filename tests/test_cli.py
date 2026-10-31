from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from probe.cli import (
    extract_claims_regex,
    extract_claims_ollama,
    extract_claims,
    run_triage_pipeline,
    render_terminal_report,
    render_markdown_report,
)
from probe.models import Claim, ClaimsResponse


def test_regex_extract_finds_symbols_and_files():
    text = "Crash in calculate_tax() located in billing.py"
    resp = extract_claims_regex(text)
    assert len(resp.claims) >= 1
    c = resp.claims[0]
    assert c.symbol == "calculate_tax"
    assert c.file == "billing.py"
    assert "calculate_tax()" in c.quote


def test_regex_extract_finds_pr_claims():
    text = "Fixes #104 and adds tests for validation"
    resp = extract_claims_regex(text)
    types = [c.type for c in resp.claims]
    assert "pr_fixes_issue" in types
    assert "pr_adds_tests" in types
    fixes = next(c for c in resp.claims if c.type == "pr_fixes_issue")
    assert fixes.issue_number == 104


def test_regex_extract_extracts_code_block():
    text = "Found a bug:\n```python\nfrom x import y\ny()\n```"
    resp = extract_claims_regex(text)
    assert len(resp.claims) == 1
    assert "from x import y" in (resp.claims[0].repro or "")


def test_extract_claims_ollama_mocked():
    text = "Bug in calculate_tax in billing.py"
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "response": '{"claims": [{"id": "c1", "type": "symbol_exists", "symbol": "calculate_tax", "file": "billing.py", "quote": "Bug in calculate_tax in billing.py"}]}'
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_resp):
        res = extract_claims_ollama(text)
        assert len(res.claims) == 1
        assert res.claims[0].symbol == "calculate_tax"
        assert res.method == "llm"


def test_extract_claims_ollama_drops_hallucinated_quote():
    text = "Bug in calculate_tax in billing.py"
    mock_resp = MagicMock()
    # Quote is not in source text
    mock_resp.json.return_value = {
        "response": '{"claims": [{"id": "c1", "type": "symbol_exists", "symbol": "foo", "quote": "this quote is completely made up"}]}'
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_resp):
        res = extract_claims_ollama(text)
        assert len(res.claims) == 0
        assert len(res.dropped_claims) == 1
        assert res.dropped_claims[0]["reason"] == "quote_not_in_text"


def test_run_triage_pipeline_invalid_symbol(tmp_path):
    (tmp_path / "app.py").write_text("def hello(): pass\n")
    text = "Crash in missing_func() in app.py"
    claims_resp, results, verdicts, status, summary = run_triage_pipeline(
        text=text,
        repo_root=tmp_path,
        use_llm=False,
    )
    assert status == "invalid"
    assert any(v.status == "invalid" for v in verdicts)


def test_run_triage_pipeline_pr_adds_tests():
    sample_diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,1 +1,2 @@
 def hello():
+    pass
diff --git a/tests/test_app.py b/tests/test_app.py
--- /dev/null
+++ b/tests/test_app.py
@@ -0,0 +1,2 @@
+def test_hello():
+    assert True
"""
    text = "Adds tests for app"
    claims_resp, results, verdicts, status, summary = run_triage_pipeline(
        text=text,
        repo_root=Path("."),
        raw_diff=sample_diff,
        use_llm=False,
    )
    assert status == "confirmed"
    assert any(v.status == "confirmed" for v in verdicts)


def test_render_markdown_report_formatting():
    claims_resp, results, verdicts, status, summary = run_triage_pipeline(
        text="Fixes #42",
        repo_root=Path("."),
        use_llm=False,
    )
    md = render_markdown_report(claims_resp, results, verdicts, status, summary)
    assert "## ![Verdict]" in md
    assert "Probe Triage Report" in md
    assert "Claim Breakdown" in md
    assert "<details>" in md
    assert "Maintainers make the final triage call" in md
