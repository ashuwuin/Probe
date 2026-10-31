"""Render final verdicts as the Markdown comment Probe posts on an issue or PR.

All text that can come from the reporter or the repository (quotes, evidence that
echoes code or command output) is escaped, so it can't break the table, notify
people with @mentions, or inject HTML into the comment.
"""

from __future__ import annotations

import re
from collections import Counter

from probe.models import ClaimsResponse, VerdictResult

# Hidden marker so the bot can find and edit its own comment instead of posting a new one.
REPORT_MARKER = "<!-- probe-report -->"

BADGES = {
    "confirmed": "✅ **Confirmed**",
    "cannot-reproduce": "🚫 **Cannot reproduce**",
    "invalid": "❌ **Invalid**",
    "uncertain": "⚠️ **Uncertain**",
}
STATUS_ORDER = ("confirmed", "cannot-reproduce", "invalid", "uncertain")

MAX_QUOTE_CHARS = 120
MAX_EVIDENCE_CHARS = 300
MISSING_INFO_HINT = (
    "To help maintainers check this report, please include: steps to reproduce, "
    "the version you used, expected vs. actual behaviour, and the full error output."
)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MENTION_RE = re.compile(r"@(?=[A-Za-z0-9])")


def _cell(text: str | None, limit: int) -> str:
    """Make arbitrary text safe to put in one Markdown table cell."""
    if not text:
        return ""
    text = " ".join(text.split())  # a newline would end the table row
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    text = text.replace("[", "\\[").replace("]", "\\]")  # no clickable links from user text
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = _MENTION_RE.sub("@​", text)  # zero-width space: no notification
    if text.count("`") % 2:
        text = text.replace("`", "'")  # an unbalanced backtick would swallow the rest of the row
    return text


def _title(repo: str, issue_number: int) -> str:
    if _REPO_RE.match(repo) and issue_number > 0:
        # /issues/N also redirects to pull requests.
        link = f"[{repo}#{issue_number}](https://github.com/{repo}/issues/{issue_number})"
        return f"### 🔎 Probe evidence report: {link}"
    return f"### 🔎 Probe evidence report: {_cell(repo, 100)}#{issue_number}"


def _summary(verdicts: list[VerdictResult]) -> str:
    counts = Counter(v.status for v in verdicts)
    noun = "claim" if len(verdicts) == 1 else "claims"
    parts = [f"{BADGES[s]} {counts[s]}" for s in STATUS_ORDER if counts[s]]
    return f"Checked {len(verdicts)} {noun}: " + " · ".join(parts)


def _extraction_notes(extraction: ClaimsResponse) -> list[str]:
    if extraction.method == "llm":
        notes = [f"Claims extracted by `{_cell(extraction.model, 60)}` running locally (Ollama)."]
    else:
        notes = [
            "⚠️ The local model was unavailable, so claims were extracted by pattern matching "
            "(**low confidence**)."
        ]
    if extraction.dropped_claims:
        count = len(extraction.dropped_claims)
        noun = "claim was" if count == 1 else "claims were"
        notes.append(f"{count} extracted {noun} discarded as not supported by the report text.")
    return notes


def generate_markdown_report(
    verdicts: list[VerdictResult],
    repo: str,
    issue_number: int,
    *,
    extraction: ClaimsResponse | None = None,
) -> str:
    """Build the Markdown comment for `repo#issue_number`.

    `extraction` (optional) adds a footer saying which model extracted the claims,
    whether the regex fallback was used, and how many claims were discarded.
    """
    lines = [REPORT_MARKER, _title(repo, issue_number), ""]

    if not verdicts:
        lines += ["No checkable technical claims were found in this report.", "", MISSING_INFO_HINT]
    else:
        lines += [
            _summary(verdicts),
            "",
            "| # | Claim | Verdict | Confidence | Evidence |",
            "|---|---|---|---|---|",
        ]
        for v in verdicts:
            quote = f"“{_cell(v.quote, MAX_QUOTE_CHARS)}”" if v.quote.strip() else ""
            lines.append(
                f"| {_cell(v.claim_id, 12)} | {quote} | {BADGES[v.status]} | {v.confidence} "
                f"| {_cell(v.evidence, MAX_EVIDENCE_CHARS)} |"
            )
        if any(v.status in ("uncertain", "cannot-reproduce") for v in verdicts):
            lines += ["", MISSING_INFO_HINT]

    lines += ["", "---"]
    if extraction is not None:
        lines += [f"<sub>{note}</sub>  " for note in _extraction_notes(extraction)]
    lines.append(
        "<sub>ℹ️ Automated, evidence-based triage by Probe. Verdicts come from code checks, "
        "not from the AI model. A maintainer makes the final call.</sub>"
    )
    return "\n".join(lines) + "\n"
