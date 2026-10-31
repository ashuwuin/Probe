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
    find_editable_bot_comment,
    REPORT_MARKER,
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


def test_regex_extract_s4_adds_unit_tests():
    # S4: Must catch "adds unit tests"
    text = "Fixes #42 and adds unit tests for billing"
    resp = extract_claims_regex(text)
    types = [c.type for c in resp.claims]
    assert "pr_adds_tests" in types


def test_regex_extract_ignores_pasted_traceback():
    # S1 in CLI: Don't extract a pasted traceback as a runnable repro script
    text = "Here is the error:\n```\nTraceback (most recent call last):\n  File 'x.py', line 1\nSyntaxError: bad\n```"
    resp = extract_claims_regex(text)
    for c in resp.claims:
        assert c.repro is None


def test_regex_extract_valid_code_block():
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


def test_extract_claims_ollama_exact_quote_check():
    text = "Bug in calculate_tax in billing.py"
    mock_resp = MagicMock()
    # Case mismatch or hallucinated quote must be dropped
    mock_resp.json.return_value = {
        "response": '{"claims": [{"id": "c1", "type": "symbol_exists", "symbol": "foo", "quote": "BUG IN CALCULATE_TAX"}]}'
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_resp):
        res = extract_claims_ollama(text)
        assert len(res.claims) == 0
        assert len(res.dropped_claims) == 1
        assert res.dropped_claims[0]["reason"] == "quote_not_in_text"


def test_extract_claims_is_pull_request_flag():
    text = "Fixes #42 and adds unit tests for billing"
    # For issues (is_pull_request=False), PR claims must be dropped
    resp_issue = extract_claims_regex(text, is_pull_request=False)
    types_issue = [c.type for c in resp_issue.claims]
    assert "pr_fixes_issue" not in types_issue
    assert "pr_adds_tests" not in types_issue

    # For PRs (is_pull_request=True), PR claims must be kept
    resp_pr = extract_claims_regex(text, is_pull_request=True)
    types_pr = [c.type for c in resp_pr.claims]
    assert "pr_fixes_issue" in types_pr
    assert "pr_adds_tests" in types_pr


def test_run_triage_pipeline_regex_fallback_never_invalid(tmp_path):
    (tmp_path / "app.py").write_text("def hello(): pass\n")
    text = "Crash in missing_func() in app.py"
    claims_resp, results, verdicts, status, summary = run_triage_pipeline(
        text=text,
        repo_root=tmp_path,
        use_llm=False,
    )
    # Under regex fallback, claims must never be marked invalid
    assert status == "uncertain"
    assert all(v.status != "invalid" for v in verdicts)


def test_run_triage_pipeline_llm_mode_can_be_invalid(tmp_path):
    (tmp_path / "app.py").write_text("def hello(): pass\n")
    text = "Crash in missing_func in app.py"

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "response": '{"claims": [{"id": "c1", "type": "symbol_exists", "symbol": "missing_func", "file": "app.py", "quote": "Crash in missing_func in app.py"}]}'
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.post", return_value=mock_resp):
        claims_resp, results, verdicts, status, summary = run_triage_pipeline(
            text=text,
            repo_root=tmp_path,
            use_llm=True,
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
    text = "Adds unit tests for app"
    claims_resp, results, verdicts, status, summary = run_triage_pipeline(
        text=text,
        repo_root=Path("."),
        raw_diff=sample_diff,
        is_pull_request=True,
        use_llm=False,
    )
    assert status == "confirmed"
    assert any(v.status == "confirmed" for v in verdicts)


def test_render_markdown_report_formatting():
    claims_resp, results, verdicts, status, summary = run_triage_pipeline(
        text="Fixes #42",
        repo_root=Path("."),
        is_pull_request=True,
        use_llm=False,
    )
    md = render_markdown_report(
        claims_resp,
        results,
        verdicts,
        overall=status,
        overall_summary=summary,
    )
    assert REPORT_MARKER in md
    assert "## ![Verdict]" in md
    assert "Probe Triage Report" in md
    assert "Claim Breakdown" in md
    assert "<details>" in md
    assert "Maintainers make the final triage call" in md


def test_find_editable_bot_comment_finds_probe_bot():
    comments = [
        {"id": 1, "body": "regular user comment", "user": {"login": "alice", "type": "User"}},
        {"id": 2, "body": f"Old report {REPORT_MARKER}", "user": {"login": "probe[bot]", "type": "Bot"}},
    ]
    target = find_editable_bot_comment(comments)
    assert target is not None
    assert target["id"] == 2


def test_find_editable_bot_comment_ignores_user_spoof():
    # User tried to inject REPORT_MARKER into their comment -> must be ignored!
    comments = [
        {"id": 1, "body": f"Sneaky comment {REPORT_MARKER}", "user": {"login": "attacker", "type": "User"}},
    ]
    target = find_editable_bot_comment(comments)
    assert target is None

