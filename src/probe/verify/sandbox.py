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

import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from probe.models import Claim, CheckResult

DEFAULT_IMAGE = "python:3.11-slim"
MAX_OUTPUT_BYTES = 100_000

# Strict secret and credential filenames that must NEVER be mounted into the execution sandbox.
# Avoid broad substrings (such as *token*) so real modules like tokenizer.py are preserved.
IGNORED_SECRET_NAMES = frozenset({
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".netrc",
    ".pypirc",
    ".npmrc",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
})


def _ignore_secrets_filter(dir_path: str, names: list[str]) -> set[str]:
    """Excludes credentials, private keys, environment files, and venvs from sandbox mounts.

    Preserves code files containing words like 'token' (e.g. tokenizer.py).
    """
    ignored: set[str] = set()
    for name in names:
        name_lower = name.lower()
        if (
            name_lower in IGNORED_SECRET_NAMES
            or name_lower.startswith(".env")
            or name_lower.endswith((".pem", ".key", ".crt"))
        ):
            ignored.add(name)
    return ignored


def copy_sanitized_repo(repo_root: Path, target_dir: Path) -> None:
    """Copies a safe repository snapshot to target_dir.

    1. If inside a git repository, prefers 'git archive HEAD', creating an allowlist
       of strictly tracked files. Untracked files (.env, .netrc, secrets) and external
       symlinks are never copied.
    2. Fallback: uses shutil.copytree with symlinks=True (never dereferencing external
       symlinks into the copy) and filters out private credentials.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    if (repo_root / ".git").exists() and shutil.which("git") and shutil.which("tar"):
        try:
            p_git = subprocess.Popen(
                ["git", "archive", "HEAD"],
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            p_tar = subprocess.Popen(
                ["tar", "-x", "-C", str(target_dir)],
                stdin=p_git.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if p_git.stdout:
                p_git.stdout.close()
            p_tar.communicate(timeout=10)
            p_git.wait(timeout=5)
            if p_tar.returncode == 0:
                return
        except Exception:
            pass

    shutil.copytree(
        repo_root,
        target_dir,
        symlinks=True,
        ignore=_ignore_secrets_filter,
        dirs_exist_ok=True,
    )


def _extract_final_exception(stderr: str) -> str | None:
    """Extracts the exception class name from the final line of a Python traceback.

    Specifically inspects the start of the final line (e.g. 'ValueError: invalid literal'
    or 'json.decoder.JSONDecodeError: Expecting value') to prevent error messages that
    mention other error words from misleading the parser. Supports module prefixes.
    """
    for line in reversed(stderr.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^((?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exception))(?::|\s|$)", line)
        if m:
            return m.group(1)
    return None


def _exc_matches(name1: str | None, name2: str | None) -> bool:
    """Checks whether two exception class names match, ignoring module prefixes if one is unqualified."""
    if not name1 or not name2:
        return False
    if name1 == name2:
        return True
    return name1.split(".")[-1] == name2.split(".")[-1]


def is_docker_available(image: str = DEFAULT_IMAGE) -> bool:
    """Checks whether Docker CLI is installed, the daemon is running, and the sandbox image is present."""
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        if res.returncode != 0:
            return False
        img_res = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return img_res.returncode == 0
    except Exception:
        return False


def _default_tmp_dir() -> Path:
    base = os.environ.get("PROBE_TMP_DIR")
    if base:
        p = Path(base)
    elif sys.platform == "darwin":
        p = Path.home() / ".cache" / "probe" / "tmp"
    else:
        p = Path(tempfile.gettempdir())
    p.mkdir(parents=True, exist_ok=True)
    return p


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

    clean_repo_dir: tempfile.TemporaryDirectory | None = None
    try:
        # Mount repository read-only and include both root and src-layout in PYTHONPATH.
        # SECURITY: Always mount a sanitized copy of the repo excluding .env, git, venv, and credentials.
        if repo_root and repo_root.exists():
            clean_repo_dir = tempfile.TemporaryDirectory(prefix="probe-clean-repo-", dir=_default_tmp_dir())
            clean_workspace = Path(clean_repo_dir.name) / "repo"
            copy_sanitized_repo(repo_root, clean_workspace)
            # Ensure workspace directory and files are readable by container user 65534 on Linux hosts
            try:
                os.chmod(clean_repo_dir.name, 0o755)
                os.chmod(clean_workspace, 0o755)
                for root_dir, dirs, files in os.walk(clean_workspace):
                    for d in dirs:
                        try:
                            os.chmod(os.path.join(root_dir, d), 0o755)
                        except OSError:
                            pass
                    for f in files:
                        try:
                            os.chmod(os.path.join(root_dir, f), 0o644)
                        except OSError:
                            pass
            except OSError:
                pass
            docker_cmd.extend([
                "-v", f"{clean_workspace.resolve()}:/workspace:ro",
                "-e", "PYTHONPATH=/workspace:/workspace/src",
                "-w", "/tmp",
            ])
        else:
            docker_cmd.extend(["-w", "/tmp"])

        # Deliver script via stdin; avoid mid-triage network pulls with --pull never
        docker_cmd.extend(["--pull", "never", docker_image, "python", "-"])

        # Execute container with bounded memory streams to prevent host OOM
        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
            res = subprocess.run(
                docker_cmd,
                input=claim.repro,
                stdout=out_f,
                stderr=err_f,
                text=True,
                timeout=timeout_seconds,
            )

            # Cap output size keeping the TAIL (where tracebacks and error messages appear)
            if hasattr(res, "stdout") and isinstance(res.stdout, str):
                stdout = res.stdout[-MAX_OUTPUT_BYTES:].strip()
                stderr = (res.stderr or "")[-MAX_OUTPUT_BYTES:].strip()
            else:
                out_len = out_f.tell()
                out_f.seek(max(0, out_len - MAX_OUTPUT_BYTES))
                stdout = out_f.read().decode("utf-8", errors="replace").strip()

                err_len = err_f.tell()
                err_f.seek(max(0, err_len - MAX_OUTPUT_BYTES))
                stderr = err_f.read().decode("utf-8", errors="replace").strip()

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

        # Pre-parse expected exceptions and claimed exception from the claim.
        # NEVER extract claimed exception from claim.expected!
        # Detect expected exceptions from claim.expected AND from expected patterns in quote/actual
        # (e.g. "should raise ValueError", "instead of ValueError", "expected to raise KeyError")
        expected_patterns = [
            r"(?:should|expect(?:ed|s)?(?:\s+to)?|must|supposed\s+to|ought\s+to)\s+(?:raise|throw|produce)\s+((?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exception))",
            r"(?:instead\s+of|rather\s+than)\s+((?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exception))",
            r"((?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exception))\s+(?:is|was|were)\s+expected\b",
            r"\bexpect(?:ing|s|ed)?\s+((?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exception))\b",
        ]
        expected_exc_matches: list[str] = []
        expected_text = claim.expected or ""
        expected_exc_matches.extend(
            re.findall(r"\b((?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exception))\b", expected_text)
        )
        combined_context = f"{claim.quote or ''} {claim.actual or ''}"
        for pat in expected_patterns:
            for match in re.finditer(pat, combined_context, re.I):
                expected_exc_matches.append(match.group(1))

        actual_text = claim.actual or ""
        actual_claimed_matches = re.findall(
            r"\b((?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exception))\b", actual_text
        )
        actual_bug_matches = [
            e for e in actual_claimed_matches
            if not any(_exc_matches(e, exp) for exp in expected_exc_matches)
        ]

        quote_text = claim.quote or ""
        quote_exc_matches = re.findall(
            r"\b((?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[A-Z][A-Za-z0-9]*(?:Error|Exception))\b", quote_text
        )
        quote_bug_matches = [
            e for e in quote_exc_matches
            if not any(_exc_matches(e, exp) for exp in expected_exc_matches)
        ]

        has_assert = bool(re.search(r"\bassert\b", claim.repro))
        if actual_bug_matches:
            claimed_exc = actual_bug_matches[0]
        elif quote_bug_matches:
            claimed_exc = quote_bug_matches[0]
        elif has_assert:
            claimed_exc = "AssertionError"
        else:
            claimed_exc = None

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

            # 4. Missing input file / file not found in sandbox environment
            if "FileNotFoundError" in stderr:
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="inconclusive",
                    evidence=f"Missing input file in sandbox environment: {output_snippet}",
                    details={
                        "reason": "missing_input_file",
                        "exit_code": res.returncode,
                        "stderr": stderr,
                    },
                )

            # 5. Extract actual exception specifically from the final line of traceback
            actual_exc = _extract_final_exception(stderr)

            # Plain exit code without unhandled exception (e.g. sys.exit(1)) is inconclusive
            if not actual_exc:
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="inconclusive",
                    evidence=f"Script exited with status {res.returncode} but did not raise an unhandled Python exception: {output_snippet}",
                    details={"reason": "no_exception_raised", "exit_code": res.returncode, "stderr": stderr},
                )

            # If report expected an exception (e.g. "should raise ValueError") and actual code correctly raised it,
            # the code behaved as expected (not a reproduced bug).
            if any(_exc_matches(exp, actual_exc) for exp in expected_exc_matches) and not (actual_bug_matches and any(_exc_matches(act, actual_exc) for act in actual_bug_matches)):
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="inconclusive",
                    evidence=f"Script raised {actual_exc}, which was the expected behavior stated in the report (not a reproduced bug): {output_snippet}",
                    details={
                        "reason": "expected_exception_raised",
                        "actual_exception": actual_exc,
                        "exit_code": res.returncode,
                    },
                )

            # If claim names no error and has no assert, an unexpected error does not confirm the claim
            if not claimed_exc:
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="inconclusive",
                    evidence=f"Script failed with {actual_exc}, but no specific error was claimed in the report: {output_snippet}",
                    details={
                        "reason": "no_claimed_error",
                        "actual_exception": actual_exc,
                        "exit_code": res.returncode,
                    },
                )

            # If claimed error does not match the actual error
            if not _exc_matches(claimed_exc, actual_exc):
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

            # 7. Verified failure reproduction (claimed error matched actual error)
            return CheckResult(
                claim_id=claim.id,
                checker="sandbox",
                outcome="supports",
                evidence=f"Reproduced failure ({actual_exc}): {output_snippet}",
                details={
                    "reason": "reproduced",
                    "exit_code": res.returncode,
                    "exception": actual_exc,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )
        else:
            # Script executed with exit code 0
            has_assert = bool(re.search(r"\bassert\b", claim.repro))
            claims_crash = bool(
                claimed_exc
                or (claim.actual and re.search(r"\b(?:crash|crashes|exception|traceback|error|raises?)\b", claim.actual, re.I))
            )

            # If snippet was an expected behavior demonstration, clean execution is expected
            is_expected_demo = bool(
                re.search(r"\b(?:expected|should work|works|passes)\b", claim.quote or "", re.I)
                and not re.search(r"\b(?:actual|repro|bug|crash|fail)\b", claim.quote or "", re.I)
            )
            if is_expected_demo:
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="inconclusive",
                    evidence="Snippet ran cleanly with exit code 0, matching expected behavior demonstration (not a bug reproduction).",
                    details={"exit_code": 0, "stdout": stdout, "reason": "expected_behavior_snippet"},
                )

            if has_assert or claims_crash:
                return CheckResult(
                    claim_id=claim.id,
                    checker="sandbox",
                    outcome="contradicts",
                    evidence="Ran cleanly with exit code 0 (claimed crash or assertion failure did not occur)",
                    details={"reason": "ran_clean", "exit_code": 0, "stdout": stdout, "has_assert": has_assert},
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
    finally:
        if clean_repo_dir is not None:
            clean_repo_dir.cleanup()
