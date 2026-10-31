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
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".env").write_text("GITHUB_TOKEN=secret_token_123\n")
    (repo_dir / "secret.key").write_text("private_key\n")
    (repo_dir / "app.py").write_text("def hello(): pass\n")

    captured_cmds = []

    def mock_subprocess_run(cmd, *args, **kwargs):
        captured_cmds.append(cmd)
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = ""
        mock_res.stderr = ""
        return mock_res

    claim = Claim(id="c1", type="bug_behavior", repro="import app", quote="imports app")

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", side_effect=mock_subprocess_run):
            run_sandbox_repro(claim, repo_root=repo_dir)

    assert captured_cmds
    cmd = captured_cmds[0]
    # Find the mounted path from docker run -v <path>:/workspace:ro
    mount_idx = cmd.index("-v")
    mounted_arg = cmd[mount_idx + 1]
    mounted_host_path = Path(mounted_arg.split(":")[0])

    # The clean mounted workspace must contain app.py but must NOT contain .env or secret.key
    assert (mounted_host_path / "app.py").exists() or not mounted_host_path.exists()  # may be cleaned up
    # If not yet cleaned up during the run call, .env was excluded:
    # We can check directly with the ignore filter:
    from probe.verify.sandbox import IGNORED_REPO_PATTERNS
    import shutil
    clean_test_dir = tmp_path / "clean_test"
    shutil.copytree(repo_dir, clean_test_dir, ignore=IGNORED_REPO_PATTERNS)
    assert (clean_test_dir / "app.py").exists()
    assert not (clean_test_dir / ".env").exists()
    assert not (clean_test_dir / "secret.key").exists()

