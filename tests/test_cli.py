import json
import sys
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
    post_or_update_comment,
    main,
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
        "response": '{"claims": [{"id": "c1", "type": "symbol_exists", "symbol": "missing_func", "file": "app.py", "quote": "Crash in missing_func in app.py"}]}',
        "message": {
            "content": '{"claims": [{"type": "symbol_exists", "symbol": "missing_func", "file": "app.py", "quote": "Crash in missing_func in app.py"}]}'
        },
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


def test_render_markdown_report_builtin_layout():
    from probe.cli import _render_builtin_markdown_report
    claims_resp, results, verdicts, status, summary = run_triage_pipeline(
        text="Fixes #42",
        repo_root=Path("."),
        is_pull_request=True,
        use_llm=False,
    )
    md = _render_builtin_markdown_report(
        claims_resp,
        results,
        verdicts,
        overall=status,
        overall_summary=summary,
        repo="ashuwuin/Probe",
        issue_number=42,
    )
    assert REPORT_MARKER in md
    assert "## ![Verdict]" in md
    assert "Probe Triage Report: ashuwuin/Probe#42" in md
    assert "Claim Breakdown" in md
    assert "<details>" in md
    assert "Maintainers make the final triage call" in md


def test_render_markdown_report_delegates_to_kaashvi_report(monkeypatch):
    import types
    mock_gen = MagicMock(return_value=f"{REPORT_MARKER}\n### Kaashvi Report")
    fake_mod = types.ModuleType("probe.report")
    fake_mod.generate_markdown_report = mock_gen
    monkeypatch.setitem(sys.modules, "probe.report", fake_mod)

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
        repo="owner/repo",
        issue_number=99,
    )
    assert "Kaashvi Report" in md
    assert mock_gen.called
    kwargs = mock_gen.call_args.kwargs
    assert kwargs["repo"] == "owner/repo"
    assert kwargs["issue_number"] == 99
    assert kwargs["overall"] == status


def test_regex_extract_no_tests_negation():
    # "This PR has no tests" must NEVER create a claim that PR adds tests
    for text in ["This PR has no tests", "PR submitted without tests", "lacks test coverage"]:
        claims = extract_claims_regex(text, is_pull_request=True).claims
        assert not any(c.type == "pr_adds_tests" for c in claims)


def test_regex_extract_no_tests_in_earlier_sentence_does_not_cancel_real_claim():
    # "Previously there were no tests" should NOT cancel a real "adds unit tests" claim in a different sentence
    text = "Previously there were no tests.\nThis PR adds unit tests for billing."
    claims = extract_claims_regex(text, is_pull_request=True).claims
    assert any(c.type == "pr_adds_tests" for c in claims)


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


def test_find_editable_bot_comment_ignores_other_bots():
    # Other bots (e.g. dependabot, codecov) must NOT be matched or overwritten
    comments = [
        {"id": 1, "body": f"Dependabot update {REPORT_MARKER}", "user": {"login": "dependabot[bot]", "type": "Bot"}},
        {"id": 2, "body": f"Codecov coverage {REPORT_MARKER}", "user": {"login": "codecov[bot]", "type": "Bot"}},
        {"id": 3, "body": f"Actions log {REPORT_MARKER}", "user": {"login": "github-actions[bot]", "type": "Bot"}},
    ]
    target = find_editable_bot_comment(comments)
    assert target is None


def test_post_or_update_comment_creates_new():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/issues/10/comments" in str(request.url):
            return httpx.Response(200, json=[])
        if request.method == "POST" and "/issues/10/comments" in str(request.url):
            body = json.loads(request.content)
            assert "Triage Report" in body["body"]
            return httpx.Response(201, json={"id": 101, "html_url": "https://github.com/octo/demo/issues/10#issuecomment-101"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    res = post_or_update_comment("octo", "demo", 10, "### Triage Report", token="fake_token", client=client)
    assert res["action"] == "created"
    assert res["id"] == 101
    assert "issuecomment-101" in res["html_url"]


def test_post_or_update_comment_updates_existing_bot():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/issues/10/comments" in str(request.url):
            return httpx.Response(200, json=[
                {"id": 555, "body": f"Old report {REPORT_MARKER}", "user": {"login": "probe[bot]", "type": "Bot"}}
            ])
        if request.method == "PATCH" and "/issues/comments/555" in str(request.url):
            body = json.loads(request.content)
            assert "New Triage Report" in body["body"]
            return httpx.Response(200, json={"id": 555, "html_url": "https://github.com/octo/demo/issues/10#issuecomment-555"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    res = post_or_update_comment("octo", "demo", 10, "### New Triage Report", token="fake_token", client=client)
    assert res["action"] == "updated"
    assert res["id"] == 555
    assert "issuecomment-555" in res["html_url"]


def test_post_or_update_comment_ignores_user_spoof_and_creates_new():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/issues/10/comments" in str(request.url):
            return httpx.Response(200, json=[
                {"id": 888, "body": f"Spoofed {REPORT_MARKER}", "user": {"login": "attacker", "type": "User"}}
            ])
        if request.method == "POST" and "/issues/10/comments" in str(request.url):
            return httpx.Response(201, json={"id": 999, "html_url": "https://github.com/octo/demo/issues/10#issuecomment-999"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    res = post_or_update_comment("octo", "demo", 10, "### Report", token="fake_token", client=client)
    assert res["action"] == "created"
    assert res["id"] == 999


def test_post_or_update_comment_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GITHUB_TOKEN is required"):
        post_or_update_comment("octo", "demo", 10, "body", token=None)


def test_post_or_update_comment_uses_authenticated_user_login(monkeypatch):
    monkeypatch.delenv("PROBE_BOT_LOGIN", raising=False)
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET" and "/user" in url_str and "/issues" not in url_str:
            return httpx.Response(200, json={"login": "Kaashviw3spaces"})
        if request.method == "GET" and "/issues/10/comments" in url_str:
            return httpx.Response(200, json=[
                {"id": 42, "body": f"Earlier report {REPORT_MARKER}", "user": {"login": "Kaashviw3spaces", "type": "User"}}
            ])
        if request.method == "PATCH" and "/issues/comments/42" in url_str:
            return httpx.Response(200, json={"id": 42, "html_url": "https://github.com/octo/demo/issues/10#issuecomment-42"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    res = post_or_update_comment("octo", "demo", 10, "### Updated Report", token="fake_token", client=client)
    assert res["action"] == "updated"
    assert res["id"] == 42


def test_post_or_update_comment_paginates_and_finds_page_2():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET" and "/issues/10/comments" in url_str:
            if "page=2" in url_str or request.url.params.get("page") == "2":
                return httpx.Response(200, json=[
                    {"id": 777, "body": f"Page 2 report {REPORT_MARKER}", "user": {"login": "probe[bot]", "type": "Bot"}}
                ])
            # Page 1: 100 regular user comments
            return httpx.Response(200, json=[
                {"id": i, "body": f"User comment {i}", "user": {"login": "user", "type": "User"}}
                for i in range(100)
            ])
        if request.method == "PATCH" and "/issues/comments/777" in url_str:
            return httpx.Response(200, json={"id": 777, "html_url": "https://github.com/octo/demo/issues/10#issuecomment-777"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    res = post_or_update_comment("octo", "demo", 10, "### New Report", token="fake_token", bot_login="probe[bot]", client=client)
    assert res["action"] == "updated"
    assert res["id"] == 777


def test_run_triage_pipeline_truncated_diff_makes_pr_claims_inconclusive():
    # If a big PR says "adds tests" but diff is truncated, it must be inconclusive, NOT invalid!
    text = "Fixes #10 and adds unit tests"
    claims_resp, results, verdicts, status, summary = run_triage_pipeline(
        text=text,
        repo_root=Path("."),
        raw_diff="diff --git a/app.py b/app.py\n+x = 1\n",
        changed_files=["app.py"],
        is_pull_request=True,
        use_llm=False,
        diff_truncated=True,
    )
    # The PR claim must NOT be invalid / contradicts
    pr_verdicts = [v for v in verdicts if v.claim_id in [c.id for c in claims_resp.claims if c.type == "pr_adds_tests"]]
    for pv in pr_verdicts:
        assert pv.status != "invalid"
        assert pv.status == "uncertain"
    assert status != "invalid"


def test_cli_main_triage_github_issue_url(monkeypatch, tmp_path, capsys):
    from datetime import datetime, timezone
    from probe.github_client import IssueData, Snapshot

    # Create dummy app.py in repo snapshot
    (tmp_path / "app.py").write_text("def hello(): pass\n")

    fake_issue = IssueData(
        owner="octo",
        repo="demo",
        number=7,
        title="Crash in hello()",
        body="hello() crashes on empty string",
        state="open",
        created_at=datetime.now(timezone.utc),
        html_url="https://github.com/octo/demo/issues/7",
    )
    fake_snapshot = Snapshot(
        owner="octo",
        repo="demo",
        sha="1234567890123456789012345678901234567890",
        path=tmp_path,
        resolved_by="default_branch",
    )

    with patch("probe.cli.GitHubClient") as mock_client_cls:
        client_instance = MagicMock()
        client_instance.__enter__.return_value = client_instance
        client_instance.__exit__.return_value = None
        client_instance.get_issue.return_value = fake_issue
        client_instance.snapshot_for_issue.return_value = fake_snapshot
        mock_client_cls.return_value = client_instance

        monkeypatch.setattr(
            sys, "argv",
            ["probe", "triage", "https://github.com/octo/demo/issues/7", "--markdown", "--no-llm"]
        )
        main()

        out = capsys.readouterr().out
        assert REPORT_MARKER in out
        assert "octo/demo#7" in out
        assert client_instance.get_issue.called
        assert client_instance.snapshot_for_issue.called
        assert client_instance.__exit__.called


def test_cli_main_triage_github_pull_url(monkeypatch, tmp_path, capsys):
    from datetime import datetime, timezone
    from probe.github_client import PullData, Snapshot

    (tmp_path / "app.py").write_text("def hello(): pass\n")

    fake_pull = PullData(
        owner="octo",
        repo="demo",
        number=12,
        title="Add tests for hello",
        body="Fixes #7 and adds unit tests",
        state="open",
        created_at=datetime.now(timezone.utc),
        html_url="https://github.com/octo/demo/pull/12",
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_ref="main",
        changed_files=["tests/test_hello.py"],
        diff_truncated=False,
        changed_files_truncated=False,
        diff="""diff --git a/tests/test_hello.py b/tests/test_hello.py
--- /dev/null
+++ b/tests/test_hello.py
@@ -0,0 +1,2 @@
+def test_hello():
+    pass
""",
    )
    fake_snapshot = Snapshot(
        owner="octo",
        repo="demo",
        sha="a" * 40,
        path=tmp_path,
        resolved_by="pr_head",
    )

    with patch("probe.cli.GitHubClient") as mock_client_cls:
        client_instance = MagicMock()
        client_instance.__enter__.return_value = client_instance
        client_instance.__exit__.return_value = None
        client_instance.get_pull.return_value = fake_pull
        client_instance.snapshot_for_pull.return_value = fake_snapshot
        mock_client_cls.return_value = client_instance

        monkeypatch.setattr(
            sys, "argv",
            ["probe", "triage", "https://github.com/octo/demo/pull/12", "--markdown", "--no-llm"]
        )
        main()

        out = capsys.readouterr().out
        assert REPORT_MARKER in out
        assert "octo/demo#12" in out
        assert client_instance.get_pull.called
        assert client_instance.snapshot_for_pull.called
        assert client_instance.__exit__.called


def test_cli_main_triage_github_url_with_post(monkeypatch, tmp_path, capsys):
    from datetime import datetime, timezone
    from probe.github_client import IssueData, Snapshot

    (tmp_path / "app.py").write_text("def hello(): pass\n")

    fake_issue = IssueData(
        owner="octo",
        repo="demo",
        number=7,
        title="Crash in hello()",
        body="hello() crashes on empty string",
        state="open",
        created_at=datetime.now(timezone.utc),
        html_url="https://github.com/octo/demo/issues/7",
    )
    fake_snapshot = Snapshot(
        owner="octo",
        repo="demo",
        sha="1234567890123456789012345678901234567890",
        path=tmp_path,
        resolved_by="default_branch",
    )

    with patch("probe.cli.GitHubClient") as mock_client_cls, \
         patch("probe.cli.post_or_update_comment") as mock_post:
        client_instance = MagicMock()
        client_instance.__enter__.return_value = client_instance
        client_instance.__exit__.return_value = None
        client_instance.get_issue.return_value = fake_issue
        client_instance.snapshot_for_issue.return_value = fake_snapshot
        mock_client_cls.return_value = client_instance
        mock_post.return_value = {"action": "created", "id": 123, "html_url": "https://github.com/octo/demo/issues/7#issuecomment-123"}

        monkeypatch.setattr(
            sys, "argv",
            ["probe", "triage", "https://github.com/octo/demo/issues/7", "--post", "--no-llm"]
        )
        main()

        out = capsys.readouterr().out
        assert "Comment created: https://github.com/octo/demo/issues/7#issuecomment-123" in out
        assert mock_post.called
        kwargs = mock_post.call_args.kwargs
        assert kwargs["owner"] == "octo"
        assert kwargs["repo"] == "demo"
        assert kwargs["issue_number"] == 7
        assert REPORT_MARKER in kwargs["body"]


def test_cli_main_triage_github_url_not_found(monkeypatch):
    from probe.github_client import NotFoundError

    with patch("probe.cli.GitHubClient") as mock_client_cls:
        client_instance = MagicMock()
        client_instance.__enter__.return_value = client_instance
        client_instance.__exit__.return_value = None
        client_instance.get_issue.side_effect = NotFoundError("Not found", 404)
        mock_client_cls.return_value = client_instance

        monkeypatch.setattr(
            sys, "argv",
            ["probe", "triage", "https://github.com/octo/demo/issues/999", "--no-llm"]
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


# ---------------------------------------------------------------- repros on pull requests

def test_pull_request_repro_is_not_run_against_pr_head(tmp_path):
    """On a PR the repro would run the patched code: a clean run is a working fix, not
    'cannot reproduce', and a crash may be a broken fix, not a confirmed bug."""
    from probe import cli as cli_module

    text = "Fixes the crash in `apply_discount()`.\n\n```python\nimport math\nassert math.floor(1.5) == 1\n```\n"
    with patch.object(cli_module, "run_sandbox_repro") as sandbox:
        _, results, verdicts, overall, _ = cli_module.run_triage_pipeline(
            text, tmp_path, raw_diff="diff --git a/x.py b/x.py\n", is_pull_request=True, use_llm=False
        )
    sandbox.assert_not_called()
    sandbox_results = [r for r in results if r.checker == "sandbox"]
    assert sandbox_results and all(r.outcome == "inconclusive" for r in sandbox_results)
    assert all(r.details.get("reason") == "pr_head_not_comparable" for r in sandbox_results)
    assert all(v.status not in ("confirmed", "cannot-reproduce") for v in verdicts if v.claim_id in {r.claim_id for r in sandbox_results})


def test_issue_repro_still_runs_in_sandbox(tmp_path):
    from probe import cli as cli_module
    from probe.models import CheckResult

    text = "`apply_discount()` crashes.\n\n```python\nimport math\nassert math.floor(1.5) == 2\n```\n"
    fake = lambda claim, repo_root=None: CheckResult(claim_id=claim.id, checker="sandbox", outcome="supports", evidence="reproduced")
    with patch.object(cli_module, "run_sandbox_repro", side_effect=fake) as sandbox:
        cli_module.run_triage_pipeline(text, tmp_path, is_pull_request=False, use_llm=False)
    assert sandbox.called


# ---------------------------------------------------------------- .env and --post safety

def test_main_loads_dotenv_without_overriding(tmp_path, monkeypatch):
    import os
    from probe import cli

    (tmp_path / ".env").write_text("PROBE_TEST_FROM_DOTENV=from_file\nPROBE_TEST_ALREADY_SET=from_file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PROBE_TEST_FROM_DOTENV", raising=False)
    monkeypatch.setenv("PROBE_TEST_ALREADY_SET", "from_env")
    monkeypatch.setattr(sys, "argv", ["probe", "triage", "text", "--post"])
    with pytest.raises(SystemExit):
        cli.main()
    try:
        assert os.environ["PROBE_TEST_FROM_DOTENV"] == "from_file"
        assert os.environ["PROBE_TEST_ALREADY_SET"] == "from_env"
    finally:
        os.environ.pop("PROBE_TEST_FROM_DOTENV", None)


@pytest.mark.parametrize(
    "extra",
    [[], ["--repo-name", "octo/demo"], ["-n", "5"]],
)
def test_post_without_explicit_target_is_refused(tmp_path, monkeypatch, capsys, extra):
    from probe import cli

    issue = tmp_path / "issue_42.md"
    issue.write_text("`apply_discount()` crashes with KeyError")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["probe", "triage", str(issue), "--post", "--no-llm", *extra])
    with patch("probe.cli.post_or_update_comment") as post, \
         patch("probe.cli.run_triage_pipeline") as pipeline:
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 2
    assert "--post needs" in capsys.readouterr().err
    post.assert_not_called()
    pipeline.assert_not_called()
def test_python_module_entrypoint():
    """Ensure probe.__main__ executes main()."""
    import runpy
    with patch("probe.cli.main") as mock_main:
        runpy.run_module("probe", run_name="__main__")
        mock_main.assert_called_once()
