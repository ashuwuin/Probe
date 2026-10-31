import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess
from probe.models import Claim
from probe.verify.sandbox import run_sandbox_repro, is_docker_available


def test_sandbox_missing_repro_returns_inconclusive():
    claim = Claim(id="c1", type="bug_behavior", repro=None, quote="crashes on load")
    res = run_sandbox_repro(claim)
    assert res.outcome == "inconclusive"
    assert "No reproduction steps" in res.evidence


def test_sandbox_unavailable_when_docker_inactive():
    claim = Claim(id="c1", type="bug_behavior", repro="print(1/0)", quote="crashes with ZeroDivisionError")

    with patch("probe.verify.sandbox.is_docker_available", return_value=False):
        res = run_sandbox_repro(claim)
        assert res.outcome == "inconclusive"
        assert res.details.get("reason") == "sandbox_unavailable"
        assert "sandbox_unavailable" in res.evidence


def test_sandbox_reproduces_failure():
    claim = Claim(id="c1", type="bug_behavior", repro="raise RuntimeError('core error')", quote="crashes with RuntimeError", actual="RuntimeError: core error")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "RuntimeError: unhandled exception in core.py"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "supports"
            assert "Reproduced failure" in res.evidence
            assert res.details["exit_code"] == 1


def test_sandbox_s1_syntax_error_traceback_is_inconclusive():
    # S1: Pasted traceback or broken syntax must be inconclusive, never confirmed
    claim = Claim(id="c1", type="bug_behavior", repro="Traceback (most recent call last):\n  File 'x.py'", quote="see traceback")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "SyntaxError: invalid syntax"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details["reason"] == "syntax_error"


def test_sandbox_s2_wrong_output_without_assert_is_inconclusive():
    # S2: Code exits 0 printing wrong value (no assertion) -> cannot disprove bug automatically
    claim = Claim(id="c1", type="bug_behavior", repro="print(apply_discount(100))", quote="apply_discount gives 0.0")

    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = "0.0"
    mock_run.stderr = ""

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details["reason"] == "no_assertion_in_repro"


def test_sandbox_s2_assertion_clean_run_contradicts():
    # S2: Code with assert passes cleanly with exit 0 -> contradicts crash claim
    claim = Claim(id="c1", type="bug_behavior", repro="assert 1 == 1", quote="fails assertion")

    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = ""
    mock_run.stderr = ""

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "contradicts"


def test_sandbox_s3_name_error_is_inconclusive():
    # S3: Reporter typo (NameError: fromat_price is not defined)
    claim = Claim(id="c1", type="bug_behavior", repro="fromat_price(10)", quote="crashes")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "NameError: name 'fromat_price' is not defined"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details["reason"] == "name_error"


def test_sandbox_missing_dependency_returns_inconclusive():
    claim = Claim(id="c1", type="bug_behavior", repro="import non_existent_pkg", quote="crashes with ImportError")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "ModuleNotFoundError: No module named 'non_existent_pkg'"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details.get("reason") == "missing_dependency"
            assert "Missing dependency" in res.evidence


def test_sandbox_handles_timeout_and_kills_container():
    claim = Claim(id="c1", type="bug_behavior", repro="while True: pass", quote="hangs forever")

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", side_effect=[subprocess.TimeoutExpired(cmd="docker", timeout=15), MagicMock()]) as mock_sub:
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details.get("reason") == "timeout"
            # Verify docker kill was attempted
            assert any("kill" in str(c) for c in mock_sub.call_args_list)


@pytest.mark.parametrize("exit_code", [125, 126, 127, 137])
def test_sandbox_docker_failure_exit_codes_return_inconclusive(exit_code):
    # Docker daemon/runtime failures must NEVER be treated as successful bug reproduction
    claim = Claim(id="c1", type="bug_behavior", repro="print('crash')", quote="crashes")

    mock_run = MagicMock()
    mock_run.returncode = exit_code
    mock_run.stdout = ""
    mock_run.stderr = "docker: Error response from daemon: No such image"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details["reason"] == "docker_error"
            assert res.outcome != "supports"


def test_sandbox_exception_mismatch_returns_inconclusive():
    # Claim specifically alleges KeyError, but script raised ValueError -> unexpected exception
    claim = Claim(id="c1", type="bug_behavior", repro="int('abc')", actual="raises KeyError", quote="crashes with KeyError")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "ValueError: invalid literal for int() with base 10: 'abc'"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details["reason"] == "unexpected_exception"
            assert "KeyError" in res.evidence
            assert "ValueError" in res.evidence


def test_sandbox_exception_match_returns_supports():
    # Claim specifically alleges KeyError, and script indeed raised KeyError -> verified reproduction
    claim = Claim(id="c1", type="bug_behavior", repro="d = {}; d['missing']", actual="raises KeyError", quote="raises KeyError")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "KeyError: 'missing'"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "supports"
            assert "KeyError" in res.evidence


def test_sandbox_unclaimed_error_returns_inconclusive():
    # If the report alleges no error (e.g. general failure), a random crash must NOT be confirmed
    claim = Claim(id="c1", type="bug_behavior", repro="print('fail')", quote="checkout is broken")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "IndexError: list index out of range"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details["reason"] == "no_claimed_error"


def test_sandbox_plain_sys_exit_returns_inconclusive():
    # A script exiting with sys.exit(1) without raising an unhandled Python exception is inconclusive
    claim = Claim(id="c1", type="bug_behavior", repro="import sys; sys.exit(1)", quote="fails on exit 1")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = ""

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details["reason"] == "no_exception_raised"


def test_sandbox_missing_input_file_returns_inconclusive():
    # A script failing due to a missing input file (e.g. input.csv) must be inconclusive, never confirmed
    claim = Claim(id="c1", type="bug_behavior", repro="open('input.csv').read()", quote="crashes reading input")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "FileNotFoundError: [Errno 2] No such file or directory: 'input.csv'"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details["reason"] == "missing_input_file"


def test_sandbox_output_tail_preserved_when_exceeding_limit():
    # When output exceeds MAX_OUTPUT_BYTES, the tail containing the exception traceback must be kept
    claim = Claim(id="c1", type="bug_behavior", repro="print('large')", actual="ValueError: fatal error", quote="raises ValueError")

    long_prefix = "A" * 150_000
    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = f"{long_prefix}\nValueError: fatal error"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "supports"
            assert "ValueError" in res.evidence


def test_sandbox_sanitizes_repo_excluding_secrets(tmp_path):
    # Secrets like .env, tokens, git, and venvs must NEVER be mounted into the container
    # But legitimate modules like tokenizer.py must NOT be dropped!
    from probe.verify.sandbox import copy_sanitized_repo

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".env").write_text("GITHUB_TOKEN=secret_token_123\n")
    (repo_dir / ".netrc").write_text("machine github.com password secret\n")
    (repo_dir / ".npmrc").write_text("//registry.npmjs.org/:_authToken=secret\n")
    (repo_dir / "id_rsa").write_text("-----BEGIN RSA PRIVATE KEY-----\n")
    (repo_dir / "secret.key").write_text("private_key\n")
    (repo_dir / "app.py").write_text("def hello(): pass\n")
    (repo_dir / "tokenizer.py").write_text("def tokenize(): pass\n")

    clean_test_dir = tmp_path / "clean_test"
    copy_sanitized_repo(repo_dir, clean_test_dir)

    # Legitimate code must be preserved
    assert (clean_test_dir / "app.py").exists()
    assert (clean_test_dir / "tokenizer.py").exists()

    # Private credential files must be excluded
    assert not (clean_test_dir / ".env").exists()
    assert not (clean_test_dir / ".netrc").exists()
    assert not (clean_test_dir / ".npmrc").exists()
    assert not (clean_test_dir / "id_rsa").exists()
    assert not (clean_test_dir / "secret.key").exists()


def test_sandbox_symlink_does_not_dereference_outside_repo(tmp_path):
    # Symlinks pointing to outside directories must NOT copy external files into the workspace
    from probe.verify.sandbox import copy_sanitized_repo

    outside_dir = tmp_path / "outside_home"
    outside_dir.mkdir()
    (outside_dir / "private_data.txt").write_text("super secret\n")

    repo_dir = tmp_path / "repo_with_link"
    repo_dir.mkdir()
    (repo_dir / "main.py").write_text("print('hello')\n")
    (repo_dir / "link_to_outside").symlink_to(outside_dir)

    clean_dir = tmp_path / "clean_copy"
    copy_sanitized_repo(repo_dir, clean_dir)

    # Main code is there
    assert (clean_dir / "main.py").exists()
    # The link itself is a symlink, NOT a dereferenced directory containing private_data.txt
    assert (clean_dir / "link_to_outside").is_symlink()


def test_sandbox_expected_error_field_does_not_confirm_false_report():
    # Report: "parse('') should raise ValueError but returns {}"
    # The code actually DOES raise ValueError correctly, so the report was false.
    # Probe must NOT mark this as confirmed!
    claim = Claim(
        id="c1",
        type="bug_behavior",
        repro="parse('')",
        expected="ValueError: input cannot be empty",
        actual="returns {}",
        quote="parse('') should raise ValueError but returns {}",
    )

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "ValueError: input cannot be empty"

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.outcome != "supports"
            assert res.details.get("reason") in ("expected_exception_raised", "no_claimed_error")


def test_sandbox_final_line_exception_not_fooled_by_message():
    # Error message contains another error name: 'ValueError: got KeyError instead'
    # Final line parser must extract ValueError, NOT KeyError!
    from probe.verify.sandbox import _extract_final_exception
    stderr = "Traceback (most recent call last):\n  File 'x.py', line 1\nValueError: expected KeyError but got none"
    assert _extract_final_exception(stderr) == "ValueError"

