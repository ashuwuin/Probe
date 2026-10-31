"""PR diff checker: do a pull request's changes match what its description claims?

Works only on data GitHub already gives us (the list of changed files and the
unified diff). It never checks out or runs the PR's code.

Supported claim types:

* ``pr_adds_tests``  - did the PR change test files / add test functions?
* ``pr_fixes_issue`` - does the PR touch the file or symbol the linked issue names?

Outcome policy:

* ``pr_adds_tests``: ``supports`` when test files gain lines (or, without a
  diff, when a test file is in the changed list); ``contradicts`` when the PR
  changes files but none are tests and the diff adds no test functions;
  ``inconclusive`` otherwise (no change data, test files only deleted).
* ``pr_fixes_issue``: ``supports`` means only "the PR touches the code the
  issue names" - it does NOT prove the fix works. Not touching it is
  ``inconclusive``, because a fix can legitimately live in another file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from probe.models import CheckResult, Claim

CHECKER = "pr_diff"

TEST_DIR_NAMES = frozenset({"tests", "test", "testing", "__tests__", "spec", "specs"})
_TEST_FILE_PATTERNS = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r"^.*_test\.py$"),
    re.compile(r"^conftest\.py$"),
    re.compile(r"^.*_test\.go$"),
    re.compile(r"^.*\.(?:test|spec)\.[cm]?[jt]sx?$"),
)
# Lines in a diff that add a test (Python, Rust, JS/TS, Go).
_ADDED_TEST_RE = re.compile(
    r"^\+\s*(?:"
    r"(?:async\s+)?def\s+test\w*\s*\("
    r"|class\s+Test\w*"
    r"|#\[(?:tokio::)?test\]"
    r"|(?:it|test|describe)\s*\("
    r"|func\s+Test\w*\s*\("
    r")"
)
_DIFF_GIT_RE = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$')
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@ ?(.*)$")


def normalize_path(path: str) -> str:
    """Repo-relative POSIX form: strip quotes, './', 'a/'/'b/' diff prefixes."""
    text = path.strip().strip('"').replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def is_test_file(path: str) -> bool:
    p = PurePosixPath(normalize_path(path))
    if any(part.lower() in TEST_DIR_NAMES for part in p.parts[:-1]):
        return True
    return any(pat.match(p.name) for pat in _TEST_FILE_PATTERNS)


@dataclass
class FileDiff:
    path: str
    added: int = 0
    removed: int = 0
    deleted: bool = False
    added_tests: list[str] = field(default_factory=list)
    hunk_contexts: list[str] = field(default_factory=list)
    changed_lines: list[str] = field(default_factory=list)


def parse_unified_diff(raw_diff: str) -> dict[str, FileDiff]:
    """Parse a unified (git) diff into per-file statistics.

    Hunk bodies are consumed using the line counts in each ``@@`` header, so
    content lines that happen to start with ``---``/``+++`` are not mistaken
    for file headers. Tolerates truncated or partial diffs.
    """
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    old_path: str | None = None
    old_left = new_left = 0

    def start(path: str) -> FileDiff:
        path = normalize_path(path)
        return files.setdefault(path, FileDiff(path))

    def strip_prefix(path: str) -> str:
        path = path.strip().strip('"')
        return path[2:] if path.startswith(("a/", "b/")) else path

    for line in raw_diff.splitlines():
        if old_left > 0 or new_left > 0:  # inside a hunk body
            if line.startswith("\\"):  # "\ No newline at end of file"
                continue
            if current is None:
                old_left = new_left = 0
                continue
            if line.startswith("+"):
                new_left -= 1
                current.added += 1
                current.changed_lines.append(line[1:])
                if _ADDED_TEST_RE.match(line):
                    current.added_tests.append(line[1:].strip()[:120])
                continue
            if line.startswith("-"):
                old_left -= 1
                current.removed += 1
                current.changed_lines.append(line[1:])
                continue
            if line.startswith(" ") or line == "":
                old_left -= 1
                new_left -= 1
                continue
            old_left = new_left = 0  # malformed/truncated hunk: fall through to header parsing

        m = _DIFF_GIT_RE.match(line)
        if m:
            current = start(m.group(2))
            old_path = m.group(1)
            continue
        if line.startswith("--- "):
            target = line[4:].strip()
            old_path = None if target == "/dev/null" else strip_prefix(target)
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                if old_path is not None:
                    current = start(old_path)
                if current is not None:
                    current.deleted = True
            else:
                current = start(strip_prefix(target))
            continue
        if line.startswith("deleted file mode") and current is not None:
            current.deleted = True
            continue
        hm = _HUNK_RE.match(line)
        if hm and current is not None:
            old_left = int(hm.group(1)) if hm.group(1) is not None else 1
            new_left = int(hm.group(2)) if hm.group(2) is not None else 1
            if hm.group(3):
                current.hunk_contexts.append(hm.group(3))
    return files


def _result(claim: Claim, outcome: str, evidence: str, **details) -> CheckResult:
    return CheckResult(claim_id=claim.id, checker=CHECKER, outcome=outcome, evidence=evidence, details=details)


def _check_adds_tests(claim: Claim, changed: list[str], diff: dict[str, FileDiff]) -> CheckResult:
    test_files = sorted(p for p in changed if is_test_file(p))
    added_test_functions = {path: fd.added_tests for path, fd in diff.items() if fd.added_tests}

    if diff:
        grown = sorted(p for p, fd in diff.items() if is_test_file(p) and fd.added > 0 and not fd.deleted)
        if grown or added_test_functions:
            names = [t for tests in added_test_functions.values() for t in tests]
            where = grown or sorted(added_test_functions)
            return _result(
                claim, "supports",
                f"The diff adds lines to {len(where)} test file(s): {', '.join(where[:5])}"
                + (f"; new tests include `{names[0]}`." if names else "."),
                reason="tests_added", test_files=where, added_tests=names[:20], confidence="high",
            )
        if test_files:
            return _result(
                claim, "inconclusive",
                f"Test file(s) {', '.join(test_files[:5])} changed, but the diff adds no test lines "
                "(only deletions or renames).",
                reason="tests_only_removed", test_files=test_files,
            )
    elif test_files:
        return _result(
            claim, "supports",
            f"The PR changes {len(test_files)} test file(s): {', '.join(test_files[:5])} "
            "(file names only; no diff was available).",
            reason="test_files_changed", test_files=test_files, confidence="medium",
        )

    return _result(
        claim, "contradicts",
        f"The PR changes {len(changed)} file(s) but none is a test file, "
        "and the diff adds no test functions.",
        reason="no_test_files", changed_files=changed[:50], confidence="medium",
        note="Tests in non-standard locations are not detected.",
    )


def _path_matches(changed_path: str, claimed: str) -> bool:
    """Exact match, or the claimed path is a trailing path segment (billing.py ~ src/billing.py)."""
    return changed_path == claimed or changed_path.endswith(f"/{claimed}")


def _check_fixes_issue(claim: Claim, changed: list[str], diff: dict[str, FileDiff]) -> CheckResult:
    issue = f"issue #{claim.issue_number}" if claim.issue_number else "the linked issue"
    claimed_file = normalize_path(claim.file) if claim.file else None
    symbol = claim.symbol.strip().strip("`").removesuffix("()").split(".")[-1] if claim.symbol else None

    if not claimed_file and not symbol:
        return _result(
            claim, "inconclusive",
            f"No file or symbol from {issue} to compare against the PR's changes.",
            reason="no_issue_target",
        )

    touched = sorted(p for p in changed if claimed_file and _path_matches(p, claimed_file))

    symbol_files: list[str] = []
    if symbol and diff:
        word = re.compile(rf"\b{re.escape(symbol)}\b")
        for path, fd in sorted(diff.items()):
            if any(word.search(t) for t in fd.hunk_contexts + fd.changed_lines):
                symbol_files.append(path)

    if touched or symbol_files:
        parts = []
        if touched:
            parts.append(f"modifies `{touched[0]}`")
        if symbol_files:
            parts.append(f"changes code around `{symbol}` in `{symbol_files[0]}`")
        return _result(
            claim, "supports",
            f"The PR {' and '.join(parts)}, which {issue} names. "
            "This shows the PR touches the reported code, not that the fix is correct.",
            reason="issue_target_touched", touched_files=touched, symbol_files=symbol_files,
            confidence="medium" if symbol_files else "low",
        )

    target = " / ".join(f"`{t}`" for t in (claimed_file, symbol) if t)
    return _result(
        claim, "inconclusive",
        f"The PR does not change {target} from {issue}; the fix may be in a different file.",
        reason="issue_target_not_touched", changed_files=changed[:50],
    )


def check_pr_claim(claim: Claim, changed_files: list[str], raw_diff: str = "") -> CheckResult:
    """Check a PR-level claim against the PR's changed files and (optional) unified diff."""
    diff = parse_unified_diff(raw_diff) if raw_diff else {}
    changed = sorted({normalize_path(p) for p in changed_files if p and p.strip()} | set(diff))

    if not changed:
        return _result(claim, "inconclusive", "No changed files or diff were provided.", reason="no_change_data")

    if claim.type == "pr_adds_tests":
        return _check_adds_tests(claim, changed, diff)
    if claim.type == "pr_fixes_issue":
        return _check_fixes_issue(claim, changed, diff)
    return _result(
        claim, "inconclusive",
        f"Claim type '{claim.type}' is not handled by the PR diff checker.",
        reason="unsupported_claim_type",
    )
