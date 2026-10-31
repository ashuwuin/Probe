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


def test_sandbox_cannot_reproduce_clean_run():
    claim = Claim(id="c1", type="bug_behavior", repro="print('all good')", quote="supposed to crash")

    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = "all good"
    mock_run.stderr = ""

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", return_value=mock_run):
            res = run_sandbox_repro(claim)
            assert res.outcome == "contradicts"
            assert "Ran cleanly with exit code 0" in res.evidence


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


def test_sandbox_handles_timeout():
    claim = Claim(id="c1", type="bug_behavior", repro="while True: pass", quote="hangs forever")

    with patch("probe.verify.sandbox.is_docker_available", return_value=True):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=15)):
            res = run_sandbox_repro(claim)
            assert res.outcome == "inconclusive"
            assert res.details.get("reason") == "timeout"
