"""Tests for probe.verify.pr_diff (PR changed-files / diff checker)."""

import pytest

from probe.models import CheckResult, Claim
from probe.verify.pr_diff import check_pr_claim, is_test_file, parse_unified_diff


def claim(ctype, file=None, symbol=None, issue_number=None, cid="c1"):
    return Claim(id=cid, type=ctype, file=file, symbol=symbol, issue_number=issue_number, quote="quoted text")


DIFF_WITH_TESTS = """\
diff --git a/src/billing.py b/src/billing.py
index 1111111..2222222 100644
--- a/src/billing.py
+++ b/src/billing.py
@@ -8,4 +8,6 @@ def calculate_tax(amount: float) -> float:
     \"\"\"Return tax for amount.\"\"\"
+    if amount < 0:
+        raise ValueError("amount must be non-negative")
     return round(amount * TAX_RATE, 2)
diff --git a/tests/test_billing.py b/tests/test_billing.py
new file mode 100644
--- /dev/null
+++ b/tests/test_billing.py
@@ -0,0 +1,6 @@
+import pytest
+from billing import calculate_tax
+
+def test_negative_amount_raises():
+    with pytest.raises(ValueError):
+        calculate_tax(-1)
"""

DIFF_NO_TESTS = """\
diff --git a/src/billing.py b/src/billing.py
--- a/src/billing.py
+++ b/src/billing.py
@@ -1,2 +1,3 @@
 TAX_RATE = 0.18
+TAX_RATE_REDUCED = 0.05
 DEFAULT_CURRENCY = "INR"
"""

DIFF_DELETES_TESTS = """\
diff --git a/tests/test_old.py b/tests/test_old.py
deleted file mode 100644
--- a/tests/test_old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_a():
-    pass
"""


# ---------------------------------------------------------------- contract

def test_result_contract():
    result = check_pr_claim(claim("pr_adds_tests"), ["tests/test_billing.py"])
    assert isinstance(result, CheckResult)
    assert result.checker == "pr_diff"
    assert result.claim_id == "c1"
    CheckResult.model_validate_json(result.model_dump_json())


# ---------------------------------------------------------------- test-file detection

@pytest.mark.parametrize(
    "path",
    [
        "tests/test_billing.py", "test_billing.py", "pkg/billing_test.py", "conftest.py",
        "src/testing/helpers.py", "test/unit/x.py", "pkg/__tests__/cart.js",
        "web/cart.test.ts", "web/cart.spec.jsx", "ledger/ledger_test.go", "./tests/x.py",
    ],
)
def test_is_test_file_true(path):
    assert is_test_file(path)


@pytest.mark.parametrize(
    "path", ["src/billing.py", "src/latest.py", "contest.py", "docs/testing.md", "src/attestation.py", "tests.py"]
)
def test_is_test_file_false(path):
    assert not is_test_file(path)


# ---------------------------------------------------------------- pr_adds_tests

def test_adds_tests_supported_by_diff():
    result = check_pr_claim(claim("pr_adds_tests"), ["src/billing.py", "tests/test_billing.py"], DIFF_WITH_TESTS)
    assert result.outcome == "supports"
    assert result.details["test_files"] == ["tests/test_billing.py"]
    assert any("test_negative_amount_raises" in t for t in result.details["added_tests"])
    assert result.details["confidence"] == "high"


def test_adds_tests_supported_by_file_names_without_diff():
    result = check_pr_claim(claim("pr_adds_tests"), ["src/billing.py", "tests/test_billing.py"])
    assert result.outcome == "supports"
    assert result.details["confidence"] == "medium"
    assert "no diff" in result.evidence


def test_adds_tests_contradicted_when_no_test_files():
    result = check_pr_claim(claim("pr_adds_tests"), ["src/billing.py", "README.md"], DIFF_NO_TESTS)
    assert result.outcome == "contradicts"
    assert result.details["reason"] == "no_test_files"


def test_adds_tests_contradicted_without_diff():
    result = check_pr_claim(claim("pr_adds_tests"), ["src/billing.py"])
    assert result.outcome == "contradicts"


def test_test_function_added_in_non_test_file_supports():
    diff = """\
diff --git a/src/billing.py b/src/billing.py
--- a/src/billing.py
+++ b/src/billing.py
@@ -1,1 +1,3 @@
 TAX_RATE = 0.18
+def test_rate_is_positive():
+    assert TAX_RATE > 0
"""
    result = check_pr_claim(claim("pr_adds_tests"), ["src/billing.py"], diff)
    assert result.outcome == "supports"


def test_only_deleting_tests_is_inconclusive():
    result = check_pr_claim(claim("pr_adds_tests"), ["tests/test_old.py"], DIFF_DELETES_TESTS)
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "tests_only_removed"


def test_no_change_data_is_inconclusive():
    assert check_pr_claim(claim("pr_adds_tests"), []).outcome == "inconclusive"
    assert check_pr_claim(claim("pr_adds_tests"), ["", "  "]).outcome == "inconclusive"


def test_files_from_diff_are_used_when_list_is_empty():
    result = check_pr_claim(claim("pr_adds_tests"), [], DIFF_WITH_TESTS)
    assert result.outcome == "supports"


# ---------------------------------------------------------------- pr_fixes_issue

def test_fixes_issue_supported_when_named_file_touched():
    result = check_pr_claim(claim("pr_fixes_issue", file="src/billing.py", issue_number=12), ["src/billing.py"])
    assert result.outcome == "supports"
    assert "issue #12" in result.evidence
    assert "not that the fix is correct" in result.evidence
    assert result.details["confidence"] == "low"


def test_fixes_issue_bare_file_name_matches_nested_path():
    result = check_pr_claim(claim("pr_fixes_issue", file="billing.py"), ["src/billing.py"])
    assert result.outcome == "supports"


def test_fixes_issue_partial_name_does_not_match():
    result = check_pr_claim(claim("pr_fixes_issue", file="billing.py"), ["src/old_billing.py"])
    assert result.outcome == "inconclusive"


def test_fixes_issue_symbol_in_hunk_context_supports_with_medium_confidence():
    result = check_pr_claim(
        claim("pr_fixes_issue", file="billing.py", symbol="calculate_tax()", issue_number=12),
        ["src/billing.py", "tests/test_billing.py"],
        DIFF_WITH_TESTS,
    )
    assert result.outcome == "supports"
    assert result.details["symbol_files"][0] == "src/billing.py"
    assert result.details["confidence"] == "medium"


def test_fixes_issue_symbol_only():
    result = check_pr_claim(claim("pr_fixes_issue", symbol="calculate_tax"), ["src/billing.py"], DIFF_WITH_TESTS)
    assert result.outcome == "supports"


def test_fixes_issue_untouched_target_is_inconclusive_not_contradicts():
    result = check_pr_claim(claim("pr_fixes_issue", file="src/refunds.py", issue_number=7), ["src/billing.py"])
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "issue_target_not_touched"
    assert "issue #7" in result.evidence


def test_fixes_issue_without_target_is_inconclusive():
    result = check_pr_claim(claim("pr_fixes_issue", issue_number=7), ["src/billing.py"])
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "no_issue_target"


# ---------------------------------------------------------------- other types

def test_unsupported_claim_type_is_inconclusive():
    result = check_pr_claim(claim("bug_behavior"), ["src/billing.py"])
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "unsupported_claim_type"


# ---------------------------------------------------------------- diff parser

def test_parse_counts_and_new_files():
    files = parse_unified_diff(DIFF_WITH_TESTS)
    assert set(files) == {"src/billing.py", "tests/test_billing.py"}
    assert files["src/billing.py"].added == 2
    assert files["src/billing.py"].hunk_contexts == ["def calculate_tax(amount: float) -> float:"]
    assert files["tests/test_billing.py"].added == 6
    assert files["tests/test_billing.py"].deleted is False


def test_parse_deleted_file():
    files = parse_unified_diff(DIFF_DELETES_TESTS)
    assert files["tests/test_old.py"].deleted is True
    assert files["tests/test_old.py"].removed == 2


def test_parse_content_lines_that_look_like_headers():
    diff = """\
diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -1,2 +1,3 @@
--- old heading
+++ new heading
+extra
\\ No newline at end of file
"""
    files = parse_unified_diff(diff)
    assert list(files) == ["notes.txt"]
    assert files["notes.txt"].added == 2
    assert files["notes.txt"].removed == 1


def test_parse_plain_unified_diff_without_git_header():
    diff = """\
--- a/src/x.py
+++ b/src/x.py
@@ -1 +1 @@
-a = 1
+a = 2
"""
    files = parse_unified_diff(diff)
    assert files["src/x.py"].added == 1 and files["src/x.py"].removed == 1


def test_parse_garbage_does_not_crash():
    assert parse_unified_diff("not a diff\n@@ nonsense @@\n+++\n") == {}
