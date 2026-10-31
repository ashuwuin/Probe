"""Sandbox reproduction checker using isolated Docker containers.

Enforces:
- Zero network (--network none)
- Read-only source mounting (:ro)
- Memory, CPU, and PID limits
- Wall-clock timeout kill
- Graceful 'sandbox_unavailable' handling when Docker is not active
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from probe.models import Claim, CheckResult


def is_docker_available() -> bool:
    """Checks whether the Docker CLI is installed and the Docker daemon is running."""
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


def run_sandbox_repro(
    claim: Claim,
    repo_root: Path | None = None,
    timeout_seconds: int = 15,
    docker_image: str = "python:3.11-slim",
) -> CheckResult:
    """Runs a reproduction script inside a hardened, isolated Docker container."""
    if not claim.repro or not claim.repro.strip():
        return CheckResult(
            claim_id=claim.id,
            checker="sandbox",
            outcome="inconclusive",
            evidence="No reproduction steps or code snippet provided in claim",
            details={"reason": "no_repro_snippet"},
        )

    if not is_docker_available():
        return CheckResult(
            claim_id=claim.id,
            checker="sandbox",
            outcome="inconclusive",
            evidence="sandbox_unavailable: Docker daemon is not active on host",
            details={"reason": "sandbox_unavailable"},
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        script_file = tmp_path / "repro.py"
        script_file.write_text(claim.repro, encoding="utf-8")

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "128",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "-v",
            f"{script_file.resolve()}:/app/repro.py:ro",
        ]

        if repo_root and repo_root.exists():
            docker_cmd.extend([
                "-v", f"{repo_root.resolve()}:/workspace:ro",
                "-e", "PYTHONPATH=/workspace",
                "-w", "/tmp",
            ])
        else:
            docker_cmd.extend(["-w", "/tmp"])

        docker_cmd.extend([docker_image, "python", "/app/repro.py"])

        try:
            res = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )

            stdout = res.stdout.strip()
            stderr = res.stderr.strip()
            output_snippet = (stderr or stdout)[-300:] if (stderr or stdout) else "No output"

            if res.returncode != 0:
                # Check if failure was caused by missing environment dependencies rather than the claimed bug
                if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
                    return CheckResult(
                        claim_id=claim.id,
                        checker="sandbox",
                        outcome="inconclusive",
                        evidence=f"Missing dependency in sandbox environment: {output_snippet}",
                        details={
                            "reason": "missing_dependency",
                            "exit_code": res.returncode,
                            "stderr": stderr,
                        },
                    )

                # Script crashed as claimed
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="supports",
                    evidence=f"Reproduced failure (exit code {res.returncode}): {output_snippet}",
                    details={
                        "exit_code": res.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                    },
                )
            else:
                # Script executed cleanly without error
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="contradicts",
                    evidence=f"Ran cleanly with exit code 0 (could not reproduce claimed crash)",
                    details={"exit_code": 0, "stdout": stdout},
                )

        except subprocess.TimeoutExpired:
            return CheckResult(
                claim_id=claim.id,
                checker="sandbox",
                outcome="inconclusive",
                evidence=f"Execution timed out after {timeout_seconds}s (infinite loop or hang prevented)",
                details={"reason": "timeout", "timeout_seconds": timeout_seconds},
            )
        except Exception as e:
            return CheckResult(
                claim_id=claim.id,
                checker="sandbox",
                outcome="inconclusive",
                evidence=f"Sandbox execution error: {str(e)}",
                details={"error": str(e)},
            )
