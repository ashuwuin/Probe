import pytest
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
    claim = Claim(id="c1", type="bug_behavior", repro="import sys; sys.exit(1)", quote="crashes with exit code 1")

    mock_run = MagicMock()
    mock_run.returncode = 1
    mock_run.stdout = ""
    mock_run.stderr = "Error: unhandled exception in core.py"

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

