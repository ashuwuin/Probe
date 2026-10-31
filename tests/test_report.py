"""Tests for probe.report.generate_markdown_report."""

import pytest

from probe.models import ClaimsResponse, VerdictResult
from probe.report import BADGES, MISSING_INFO_HINT, REPORT_MARKER, generate_markdown_report


def verdict(status="invalid", confidence="high", claim_id="c1", quote="RCE in parse_url_v2()",
            evidence="`parse_url_v2` not found in the repository."):
    return VerdictResult(
        claim_id=claim_id, status=status, confidence=confidence, evidence=evidence, quote=quote
    )


def table_rows(report: str) -> list[str]:
    return [line for line in report.splitlines() if line.startswith("| c")]


def overall_line(report: str) -> str:
    return next(line for line in report.splitlines() if line.startswith("**Overall:**"))


# --------------------------------------------------------------------------- structure


def test_report_starts_with_marker_and_linked_title():
    report = generate_markdown_report([verdict()], "acme/billing", 7)
    lines = report.splitlines()
    assert lines[0] == REPORT_MARKER
    assert lines[1] == (
        "### 🔎 Probe evidence report: "
        "[acme/billing#7](https://github.com/acme/billing/issues/7)"
    )


def test_one_row_per_verdict_with_badge_confidence_and_evidence():
    verdicts = [
        verdict("invalid", "high", "c1"),
        verdict("confirmed", "medium", "c2", quote="calculate_tax crashes", evidence="found"),
    ]
    rows = table_rows(generate_markdown_report(verdicts, "acme/billing", 7))
    assert rows == [
        "| c1 | “RCE in parse_url_v2()” | ❌ **Invalid** | high "
        "| `parse_url_v2` not found in the repository. |",
        "| c2 | “calculate_tax crashes” | ✅ **Confirmed** | medium | found |",
    ]


@pytest.mark.parametrize("status", ["confirmed", "cannot-reproduce", "invalid", "uncertain"])
def test_every_status_has_a_badge(status):
    report = generate_markdown_report([verdict(status)], "acme/billing", 7)
    assert BADGES[status] in table_rows(report)[0]


def test_summary_counts_each_status():
    verdicts = [verdict("invalid"), verdict("invalid", claim_id="c2"), verdict("uncertain", claim_id="c3")]
    report = generate_markdown_report(verdicts, "acme/billing", 7)
    assert "Checked 3 claims: ❌ **Invalid** 2 · ⚠️ **Uncertain** 1" in report


# --------------------------------------------------------------------------- overall verdict


def test_overall_line_uses_the_callers_verdict_and_summary():
    report = generate_markdown_report(
        [verdict("invalid", "medium")],
        "acme/billing",
        7,
        overall="invalid",
        overall_summary="One or more technical claims were proven invalid",
    )
    assert overall_line(report) == (
        "**Overall:** ❌ **Invalid** (medium confidence): "
        "One or more technical claims were proven invalid"
    )


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["uncertain", "confirmed", "invalid"], "invalid"),
        (["uncertain", "cannot-reproduce", "confirmed"], "confirmed"),
        (["uncertain", "cannot-reproduce"], "cannot-reproduce"),
        (["uncertain"], "uncertain"),
    ],
)
def test_overall_is_derived_when_not_given(statuses, expected):
    verdicts = [verdict(s, claim_id=f"c{i}") for i, s in enumerate(statuses, 1)]
    report = generate_markdown_report(verdicts, "acme/billing", 7)
    assert overall_line(report).startswith(f"**Overall:** {BADGES[expected]}")


def test_derived_overall_confidence_is_the_strongest_for_that_status():
    verdicts = [verdict("invalid", "low"), verdict("invalid", "high", claim_id="c2")]
    assert "(high confidence)" in overall_line(generate_markdown_report(verdicts, "a/b", 1))


def test_empty_report_is_uncertain_and_asks_for_details():
    report = generate_markdown_report([], "acme/billing", 7)
    assert overall_line(report) == f"**Overall:** {BADGES['uncertain']}"
    assert "No checkable technical claims were found" in report
    assert MISSING_INFO_HINT in report
    assert table_rows(report) == []


@pytest.mark.parametrize("status, hint", [("uncertain", True), ("cannot-reproduce", True),
                                          ("invalid", False), ("confirmed", False)])
def test_missing_info_hint_only_when_evidence_is_lacking(status, hint):
    assert (MISSING_INFO_HINT in generate_markdown_report([verdict(status)], "a/b", 1)) is hint


# --------------------------------------------------------------------------- footer


def test_footer_names_the_local_model_and_dropped_claims():
    extraction = ClaimsResponse(claims=[], model="qwen3:8b", dropped_claims=[{"reason": "x"}] * 2)
    report = generate_markdown_report([verdict()], "a/b", 1, extraction=extraction)
    assert "Claims extracted by `qwen3:8b` running locally (Ollama)." in report
    assert "2 extracted claims were discarded" in report


def test_footer_warns_when_regex_fallback_was_used():
    extraction = ClaimsResponse(claims=[], method="regex_fallback", model="qwen3:8b", confidence="low")
    report = generate_markdown_report([verdict()], "a/b", 1, extraction=extraction)
    assert "pattern matching (**low confidence**)" in report
    assert "`qwen3:8b`" not in report


def test_footer_always_says_verdicts_come_from_code():
    assert "Verdicts come from code checks, not from the AI model" in generate_markdown_report(
        [], "a/b", 1
    )


# --------------------------------------------------------------------------- escaping


@pytest.mark.parametrize(
    "hostile, must_not_contain, must_contain",
    [
        ("a | b", " a | b", "a \\| b"),  # can't add table columns
        ("line1\nline2", "line1\nline2", "line1 line2"),  # can't end the row
        ("cc @maintainer", "@maintainer", "@​maintainer"),  # no notification
        ("<script>x</script>", "<script>", "&lt;script&gt;"),  # no HTML
        ("[click](http://evil)", "[click](http", "\\[click\\](http://evil)"),  # no links
        ("`unbalanced", "`unbalanced", "'unbalanced"),  # can't swallow the row
    ],
)
def test_user_text_is_escaped(hostile, must_not_contain, must_contain):
    report = generate_markdown_report(
        [verdict(quote=hostile, evidence=hostile)], "acme/billing", 7
    )
    row = table_rows(report)[0]
    assert must_not_contain not in row
    assert must_contain in row
    assert row.count(" | ") == 4  # still exactly 5 columns


def test_long_text_is_truncated():
    row = table_rows(generate_markdown_report([verdict(quote="x" * 500)], "a/b", 1))[0]
    assert "x" * 120 not in row
    assert "…" in row


def test_malformed_repo_is_not_turned_into_a_link():
    title = generate_markdown_report([], "evil](javascript:alert(1))", 1).splitlines()[1]
    assert "](javascript:" not in title.replace("\\]", "")
    assert "https://github.com" not in title
