"""Sandbox reproduction checker using isolated Docker containers.

Enforces:
- Zero network (--network none)
- Dropped privileges (--cap-drop ALL, --security-opt no-new-privileges, --user 65534:65534)
- Read-only source mounting (:ro)
- Memory, swap, CPU, and PID limits
- Size-capped tmpfs (/tmp:rw,noexec,nosuid,size=64m)
- Wall-clock timeout kill with container cleanup
- Safe script delivery via stdin (docker run -i ... python -)
- Strict detection of Docker failure codes (125/126/127/137) as inconclusive (never confirmed)
- Verification that raised exception matches claimed exception
- Graceful 'sandbox_unavailable' handling when Docker is not active
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path
from probe.models import Claim, CheckResult

DEFAULT_IMAGE = "python:3.11-slim"
MAX_OUTPUT_BYTES = 100_000


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
    docker_image: str = DEFAULT_IMAGE,
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

    container_name = f"probe-sbx-{claim.id}-{uuid.uuid4().hex[:8]}"

    # Security controls: no network, drop all caps, unprivileged user, memory swap limit
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--name",
        container_name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65534:65534",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
    ]

    # Mount repository read-only and include both root and src-layout in PYTHONPATH
    if repo_root and repo_root.exists():
        docker_cmd.extend([
            "-v", f"{repo_root.resolve()}:/workspace:ro",
            "-e", "PYTHONPATH=/workspace:/workspace/src",
            "-w", "/tmp",
        ])
    else:
        docker_cmd.extend(["-w", "/tmp"])

    # Deliver script via stdin; avoid mid-triage network pulls with --pull never
    docker_cmd.extend(["--pull", "never", docker_image, "python", "-"])

    try:
        res = subprocess.run(
            docker_cmd,
            input=claim.repro,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

        # Cap output size to prevent memory exhaustion
        stdout = (res.stdout or "")[:MAX_OUTPUT_BYTES].strip()
        stderr = (res.stderr or "")[:MAX_OUTPUT_BYTES].strip()
        output_snippet = (stderr or stdout)[-300:] if (stderr or stdout) else "No output"

        # Check for Docker daemon/runtime failures (125: docker daemon/image error, 126: cannot invoke, 127: not found, 137: OOM/killed)
        if (
            res.returncode in (125, 126, 127, 137)
            or "docker: Error response from daemon" in stderr
            or "Unable to find image" in stderr
            or "No such image" in stderr
        ):
            return CheckResult(
                claim_id=claim.id,
                checker="sandbox",
                outcome="inconclusive",
                evidence=f"Docker sandbox environment error (exit {res.returncode}): {output_snippet}",
                details={"reason": "docker_error", "exit_code": res.returncode, "stderr": stderr},
            )

        if res.returncode != 0:
            # 1. Snippet itself has SyntaxError (e.g. pasted traceback or invalid Python)
            if "SyntaxError" in stderr:
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="inconclusive",
                    evidence=f"Reproduction snippet contains SyntaxError: {output_snippet}",
                    details={"reason": "syntax_error", "exit_code": res.returncode, "stderr": stderr},
                )

            # 2. Snippet has NameError (reporter typo in helper name or import)
            if "NameError" in stderr:
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="inconclusive",
                    evidence=f"Reproduction snippet failed with NameError on undefined identifier: {output_snippet}",
                    details={"reason": "name_error", "exit_code": res.returncode, "stderr": stderr},
                )

            # 3. Missing dependency in container environment
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

            # 4. Compare actual exception against claimed exception
            claimed_text = f"{claim.actual or ''} {claim.expected or ''} {claim.quote or ''}"
            claimed_exc_matches = re.findall(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception))\b", claimed_text)
            actual_exc_matches = re.findall(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception))\b", stderr)

            claimed_exc = claimed_exc_matches[0] if claimed_exc_matches else None
            actual_exc = actual_exc_matches[-1] if actual_exc_matches else None

            if claimed_exc and actual_exc and claimed_exc != actual_exc:
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="inconclusive",
                    evidence=f"Script failed with {actual_exc}, but claim specifically alleged {claimed_exc}: {output_snippet}",
                    details={
                        "reason": "unexpected_exception",
                        "claimed_exception": claimed_exc,
                        "actual_exception": actual_exc,
                        "exit_code": res.returncode,
                    },
                )

            # 5. Verified failure reproduction
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
            # Script executed with exit code 0
            has_assert = bool(re.search(r"\bassert\b", claim.repro))
            claims_crash = bool(
                re.search(
                    r"\b(?:crash|crashes|exception|traceback|error|raises?|recursionerror|keyerror|valueerror)\b",
                    (claim.actual or "") + " " + (claim.quote or ""),
                    re.I,
                )
            )

            if has_assert or claims_crash:
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="contradicts",
                    evidence="Ran cleanly with exit code 0 (claimed crash or assertion failure did not occur)",
                    details={"exit_code": 0, "stdout": stdout, "has_assert": has_assert},
                )
            else:
                # Wrong output bugs without assertions cannot be disproven by exit code 0
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="inconclusive",
                    evidence="Ran cleanly with exit code 0; without assertions or explicit crash claims, return values cannot be disproven automatically.",
                    details={"exit_code": 0, "stdout": stdout, "reason": "no_assertion_in_repro"},
                )

    except subprocess.TimeoutExpired:
        # Kill container explicitly so Docker does not leave it running in background
        try:
            subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=5)
        except Exception:
            pass
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
