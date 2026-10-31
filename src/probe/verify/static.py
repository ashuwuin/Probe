"""Static symbol checker: does the file / function / class a claim names exist?

This module never imports or executes repository code. Python files are read
as text and parsed with ``ast.parse``; other languages get a best-effort regex
search for definitions.

Outcome policy (the verdict layer turns outcomes into verdicts):

* ``supports``     - a definition of the symbol (or the named file) was found.
* ``contradicts``  - only when a complete, error-free search of the repository's
                     Python files found no definition AND the name does not
                     appear anywhere in the scanned source text.
* ``inconclusive`` - everything else: close matches ("did you mean"), parse
                     errors, unsupported languages, regex misses, truncated
                     searches, unsafe paths, or names that appear in the text
                     but have no static definition (dynamic definitions,
                     re-exports, C extensions).
"""

from __future__ import annotations

import ast
import configparser
import difflib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from probe.models import CheckResult, Claim

CHECKER = "static_ast"

# Directories that never contain the project's own source.
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", "node_modules",
        "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", "site-packages", "dist", "build", ".eggs",
    }
)
MAX_FILE_BYTES = 1_000_000  # skip generated / minified giants
MAX_FILES = 5_000           # bound the whole-repo walk

PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
# Files searched only for a textual mention of the name (never as proof).
TEXT_ONLY_SUFFIXES = frozenset({".pyx", ".pxd", ".c", ".h", ".cc", ".cpp", ".hpp"})

# Definition patterns for languages we do not parse. ``{name}`` is replaced
# with the re.escape()d symbol name.
_REGEX_DEFINITIONS: dict[str, tuple[str, ...]] = {
    "javascript": (
        r"\bfunction\s*\*?\s*{name}\s*\(",
        r"\b(?:class|const|let|var)\s+{name}\b",
        r"\b{name}\s*[:=]\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
        r"^\s*(?:static\s+)?(?:async\s+)?{name}\s*\([^)]*\)\s*\{{",
    ),
    "rust": (
        r"\b(?:fn|struct|enum|trait|type|const|static|mod|union)\s+{name}\b",
        r"\bmacro_rules!\s*{name}\b",
    ),
    "go": (
        r"\bfunc\s+(?:\([^)]*\)\s*)?{name}\s*[\[(]",
        r"\b(?:type|var|const)\s+{name}\b",
    ),
}
_LANGUAGE_BY_SUFFIX = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "javascript", ".tsx": "javascript",
    ".rs": "rust",
    ".go": "go",
}

# Path segments that mean "an installed dependency", not this repository.
EXTERNAL_PATH_SEGMENTS = frozenset({"site-packages", "dist-packages"})

_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")


class PathEscapeError(ValueError):
    """Raised when a claimed path would resolve outside the repository."""


@dataclass(frozen=True)
class Definition:
    name: str       # short name, e.g. "total"
    qualname: str   # e.g. "Invoice.total"
    kind: str       # function | async_function | class | method | variable
    path: str       # repo-relative POSIX path
    line: int


# --------------------------------------------------------------------------
# Input normalisation (claim fields come from untrusted issue text)
# --------------------------------------------------------------------------

def normalize_symbol(raw: str | None) -> list[str] | None:
    """Turn ``"`Invoice.total()`"`` into ``["Invoice", "total"]``.

    Returns None when the text is not a plain (dotted) identifier.
    """
    if raw is None:
        return None
    text = raw.strip().strip("`'\"").strip()
    text = re.sub(r"\(.*\)$", "", text).strip()  # calculate_tax() / f(x)
    if not text or not _SYMBOL_RE.match(text):
        return None
    return text.split(".")


def normalize_claim_path(raw: str) -> str:
    """Normalise a claimed path to a repo-relative POSIX string.

    Raises PathEscapeError for traversal attempts or absolute Windows paths.
    A leading "/" is treated as "relative to the repo root".
    """
    text = raw.strip().strip("`'\"").strip()
    if "\x00" in text:
        raise PathEscapeError("path contains a NUL byte")
    text = _LINE_SUFFIX_RE.sub("", text)  # billing.py:42 -> billing.py
    text = text.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", text):
        raise PathEscapeError("absolute Windows path")
    parts = [p for p in PurePosixPath(text).parts if p not in ("/", ".")]
    if any(p == ".." for p in parts):
        raise PathEscapeError("path contains '..'")
    if not parts:
        raise PathEscapeError("empty path")
    return "/".join(parts)


def map_installed_path(root: Path, raw: str) -> str | None:
    """Map an installed-package path from a traceback onto this repository.

    ``…/site-packages/requests/utils.py`` -> ``requests/utils.py`` or
    ``src/requests/utils.py`` when that file exists in the repo (the reporter
    hit a bug in the installed copy of *this* project). Returns None when the
    package folder is absent, or present but without that file. The latter is
    a namespace package from another distribution, e.g. ``poetry/core/…`` is
    poetry-core, not poetry.
    """
    parts = [seg for seg in re.split(r"[\\/]+", raw.strip().strip("`'\"")) if seg]
    idx = max((i for i, seg in enumerate(parts) if seg.lower() in EXTERNAL_PATH_SEGMENTS), default=-1)
    rest = parts[idx + 1 :]
    if idx < 0 or len(rest) < 2:
        return None
    try:
        rel = normalize_claim_path("/".join(rest))
    except PathEscapeError:
        return None
    for prefix in ("", "src/"):
        candidate = f"{prefix}{rel}"
        try:
            if resolve_inside(root, candidate).is_file():
                return candidate
        except PathEscapeError:
            return None
    return None


def resolve_inside(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root``; reject anything (incl. symlinks) that escapes."""
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        raise PathEscapeError("path resolves outside the repository")
    return candidate


# --------------------------------------------------------------------------
# Repository walking and parsing
# --------------------------------------------------------------------------

def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


SOURCE_SUFFIXES = PYTHON_SUFFIXES | TEXT_ONLY_SUFFIXES | frozenset(_LANGUAGE_BY_SUFFIX)


def iter_source_files(root: Path, suffixes: frozenset[str] | None = SOURCE_SUFFIXES) -> tuple[list[Path], bool]:
    """Deterministically list files under ``root`` (``suffixes=None`` means any type).

    Returns (files, truncated). Skips vendored/hidden directories, symlinks that
    leave the repository, and files larger than MAX_FILE_BYTES.
    """
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if suffixes is not None and path.suffix not in suffixes:
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    continue  # symlink pointing outside the repo, or broken
                if resolved.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(path)
            if len(files) >= MAX_FILES:
                return files, True
    return files, False


def python_definitions(path: Path, rel: str) -> list[Definition]:
    """Collect definitions from a Python file. Raises SyntaxError/ValueError on parse failure."""
    source = path.read_bytes()
    tree = ast.parse(source, filename=rel)
    found: list[Definition] = []

    def add(name: str, qual: str, kind: str, line: int) -> None:
        found.append(Definition(name, qual, kind, rel, line))

    def assigned_names(target: ast.expr) -> list[ast.Name]:
        if isinstance(target, ast.Name):
            return [target]
        if isinstance(target, (ast.Tuple, ast.List)):
            out: list[ast.Name] = []
            for elt in target.elts:
                out.extend(assigned_names(elt.value if isinstance(elt, ast.Starred) else elt))
            return out
        return []

    def visit(body: list[ast.stmt], prefix: str, in_class: bool, in_function: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{node.name}"
                if in_class:
                    kind = "method"
                elif isinstance(node, ast.AsyncFunctionDef):
                    kind = "async_function"
                else:
                    kind = "function"
                add(node.name, qual, kind, node.lineno)
                visit(node.body, f"{qual}.", in_class=False, in_function=True)
            elif isinstance(node, ast.ClassDef):
                qual = f"{prefix}{node.name}"
                add(node.name, qual, "class", node.lineno)
                visit(node.body, f"{qual}.", in_class=True, in_function=in_function)
            elif not in_function and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name_node in assigned_names(target):
                        add(name_node.id, f"{prefix}{name_node.id}", "variable", node.lineno)
            elif isinstance(node, (ast.If, ast.Try)) and not in_function:
                # Module/class-level conditional definitions (e.g. try/except ImportError).
                blocks = [node.body, node.orelse]
                if isinstance(node, ast.Try):
                    blocks += [h.body for h in node.handlers] + [node.finalbody]
                for block in blocks:
                    visit(block, prefix, in_class, in_function)

    visit(tree.body, "", in_class=False, in_function=False)
    return found


def _module_path(rel: str) -> str:
    parts = list(PurePosixPath(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def symbol_matches(defn: Definition, parts: list[str]) -> bool:
    """Match a short name, a qualified name (Invoice.total) or a module path (billing.calculate_tax)."""
    if defn.name != parts[-1]:
        return False
    if len(parts) == 1:
        return True
    dotted = ".".join(parts)
    full = f"{_module_path(defn.path)}.{defn.qualname}"
    return defn.qualname == dotted or full == dotted or full.endswith(f".{dotted}")


def reexport_match(defn: Definition, parts: list[str]) -> bool:
    """``requests.get`` -> top-level ``get`` in ``src/requests/api.py``.

    The package prefix (``requests``) appears in the module path and the name is
    a top-level definition, so the package very likely re-exports it. Weaker
    than an exact match; callers report it with lower confidence.
    """
    if len(parts) < 2 or defn.name != parts[-1] or defn.qualname != defn.name:
        return False
    prefix = parts[:-1]
    segments = _module_path(defn.path).split(".")
    return any(segments[i : i + len(prefix)] == prefix for i in range(len(segments) - len(prefix) + 1))


def regex_definition_line(path: Path, language: str, name: str) -> int | None:
    """Return the 1-based line of the first regex definition match, or None."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    escaped = re.escape(name)
    for pattern in _REGEX_DEFINITIONS[language]:
        match = re.search(pattern.format(name=escaped), text, flags=re.MULTILINE)
        if match:
            return text.count("\n", 0, match.start()) + 1
    return None


def _norm_dist(name: str) -> str:
    """PEP 503-style normalisation: 'Poetry-Core' / 'poetry.core' -> 'poetry_core'."""
    return re.sub(r"[-_.]+", "_", name).lower()


_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement_names(lines) -> set[str]:
    names = set()
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue  # comments, -r/-e/--index-url options
        match = _REQ_NAME_RE.match(line)
        if match:
            names.add(_norm_dist(match.group(1)))
    return names


def declared_dependencies(root: Path) -> set[str]:
    """Normalised dependency names from pyproject.toml, requirements*.txt, setup.cfg, setup.py."""
    deps: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        except (tomllib.TOMLDecodeError, ValueError):
            data = {}
        project = data.get("project", {}) or {}
        deps |= _requirement_names(project.get("dependencies", []) or [])
        for group in (project.get("optional-dependencies", {}) or {}).values():
            deps |= _requirement_names(group or [])
        deps |= _requirement_names((data.get("build-system", {}) or {}).get("requires", []) or [])
        poetry = (data.get("tool", {}) or {}).get("poetry", {}) or {}
        deps |= {_norm_dist(k) for k in (poetry.get("dependencies", {}) or {}) if k != "python"}
        deps |= {_norm_dist(k) for k in (poetry.get("dev-dependencies", {}) or {})}
        for group in (poetry.get("group", {}) or {}).values():
            deps |= {_norm_dist(k) for k in ((group or {}).get("dependencies", {}) or {})}
    for req in sorted(root.glob("requirements*.txt")) + sorted(root.glob("requirements/*.txt")):
        try:
            deps |= _requirement_names(req.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read_string(setup_cfg.read_text(encoding="utf-8", errors="replace"))
            deps |= _requirement_names(parser.get("options", "install_requires", fallback="").splitlines())
        except configparser.Error:
            pass
    setup_py = root / "setup.py"
    if setup_py.is_file():
        text = setup_py.read_text(encoding="utf-8", errors="replace")
        block = re.search(r"install_requires\s*=\s*\[(.*?)\]", text, re.S)
        if block:
            deps |= _requirement_names(re.findall(r"[\"']([^\"']+)[\"']", block.group(1)))
    return deps


def external_imports(path: Path, repo_modules: set[str]) -> set[str]:
    """Names bound by absolute imports of modules that are not part of this repository.

    ``import rich`` -> {"rich"}; ``from rich.text import Text`` -> {"Text", "rich"};
    ``import numpy as np`` -> {"np", "numpy"}. Relative imports and imports of the
    repo's own packages are ignored. Raises SyntaxError/ValueError like ast.parse.
    """
    tree = ast.parse(path.read_bytes())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in repo_modules:
                    names.add(top)
                    if alias.asname:
                        names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            if top not in repo_modules:
                names.add(top)
                names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
    return names


import builtins as _builtins

# Attribute names of built-in types: "'str' object has no attribute 'replace'" is
# about str, not about this repository.
BUILTIN_ATTRIBUTES = frozenset(
    name
    for typ in (str, bytes, bytearray, list, tuple, dict, set, frozenset, int, float, complex, bool, object, type(None))
    for name in dir(typ)
    if not name.startswith("__")
) | frozenset(n for n in dir(_builtins) if not n.startswith("_"))

_MISSING_PATTERNS = (
    r"has no attribute ['\"`]{name}['\"`]",
    r"cannot import name ['\"`]{name}['\"`]",
    r"name ['\"`]{name}['\"`] is not defined",
    r"no module named ['\"`][\w.]*\b{name}['\"`]",
    r"`?{name}`? (?:does not|doesn't|did not|didn't) exist",
    r"`?{name}`? (?:is|was) (?:missing|removed|not found)",
    r"(?:missing|removed|no such) (?:function|method|attribute|class)? ?`?{name}`?",
)


def _reported_as_missing(claim: Claim, name: str) -> bool:
    """True when the report itself says ``name`` is missing (AttributeError, ImportError, ...).

    Then "not defined in the repo" agrees with the reporter rather than contradicting them.
    """
    text = " ".join(t for t in (claim.quote, claim.actual or "", claim.expected or "") if t)
    escaped = re.escape(name)
    return any(re.search(p.format(name=escaped), text, re.IGNORECASE) for p in _MISSING_PATTERNS)


# --------------------------------------------------------------------------
# The checker
# --------------------------------------------------------------------------

def _result(claim: Claim, outcome: str, evidence: str, *, line: int | None = None, **details) -> CheckResult:
    return CheckResult(
        claim_id=claim.id,
        checker=CHECKER,
        outcome=outcome,
        evidence=evidence,
        line_number=line,
        details=details,
    )


def _locate_file(root: Path, rel: str, all_files: list[Path] | None) -> tuple[Path | None, list[str]]:
    """Find the claimed file: exact path first, then repo files with the same path suffix."""
    exact = resolve_inside(root, rel)
    if exact.is_file():
        return exact, []
    if all_files is None:
        return None, []
    suffix = f"/{rel}"
    candidates = sorted(
        _rel(root, f) for f in all_files if f"/{_rel(root, f)}".endswith(suffix)
    )
    return None, candidates


def check_static_symbol(repo_root: Path, claim: Claim) -> CheckResult:
    """Check whether the file and/or symbol named in ``claim`` exist in ``repo_root``."""
    try:
        root = Path(repo_root).resolve(strict=True)
    except OSError:
        return _result(claim, "inconclusive", "Repository snapshot is not available.", reason="repo_unavailable")
    if not root.is_dir():
        return _result(claim, "inconclusive", "Repository snapshot is not a directory.", reason="repo_unavailable")

    parts = normalize_symbol(claim.symbol)
    if claim.symbol and parts is None:
        return _result(
            claim, "inconclusive",
            f"'{claim.symbol}' is not a plain identifier, so it cannot be looked up statically.",
            reason="unsupported_symbol",
        )
    if not parts and not claim.file:
        return _result(claim, "inconclusive", "The claim names no file or symbol to check.", reason="nothing_to_check")

    rel_file: str | None = None
    if claim.file:
        claimed_path = claim.file
        segments = {seg.lower() for seg in re.split(r"[\\/]+", claim.file)}
        if segments & EXTERNAL_PATH_SEGMENTS:
            mapped = map_installed_path(root, claim.file)
            if mapped is None:
                return _result(
                    claim, "inconclusive",
                    "The claimed file is inside an installed package (site-packages) that this repository "
                    "does not contain; its absence here says nothing about the claim.",
                    reason="external_file",
                )
            claimed_path = mapped
        try:
            rel_file = normalize_claim_path(claimed_path)
            resolve_inside(root, rel_file)
        except PathEscapeError as exc:
            return _result(
                claim, "inconclusive",
                f"Refused to inspect {claim.file[:200]!r}: {exc}.",
                reason="path_outside_repo",
            )

    all_files, truncated = iter_source_files(root)

    target: Path | None = None
    file_candidates: list[str] = []
    if rel_file:
        # File lookup considers every file type (README.md, setup.cfg, ...).
        any_files, any_truncated = iter_source_files(root, suffixes=None) if parts is None else (all_files, truncated)
        target, file_candidates = _locate_file(root, rel_file, any_files)
        if target is None and len(file_candidates) == 1:
            target = resolve_inside(root, file_candidates[0])

    # ---- file-only claims --------------------------------------------------
    if not parts:
        if target is not None:
            rel = _rel(root, target)
            return _result(claim, "supports", f"File `{rel}` exists.", file=rel, reason="file_found", confidence="high")
        if file_candidates:
            return _result(
                claim, "inconclusive",
                f"`{rel_file}` is ambiguous; matching files: {', '.join(file_candidates[:5])}.",
                reason="file_ambiguous", candidates=file_candidates[:20],
            )
        if any_truncated:
            return _result(claim, "inconclusive", f"`{rel_file}` not found, but the repository search was truncated.",
                           reason="search_truncated")
        return _result(claim, "contradicts", f"File `{rel_file}` does not exist in the repository.",
                       reason="file_not_found", confidence="high")

    name = parts[-1]
    pretty = ".".join(parts)
    parse_errors: list[str] = []
    definitions_cache: dict[Path, list[Definition]] = {}

    def defs_for(path: Path) -> list[Definition]:
        if path not in definitions_cache:
            rel = _rel(root, path)
            try:
                definitions_cache[path] = python_definitions(path, rel)
            except (SyntaxError, ValueError, RecursionError, MemoryError, OSError):
                parse_errors.append(rel)
                definitions_cache[path] = []
        return definitions_cache[path]

    # ---- 1. look in the claimed file first ----------------------------------
    if target is not None:
        rel = _rel(root, target)
        if target.suffix in PYTHON_SUFFIXES:
            hits = [d for d in defs_for(target) if symbol_matches(d, parts)]
            if hits:
                d = hits[0]
                return _result(
                    claim, "supports",
                    f"`{pretty}` is defined in `{d.path}` at line {d.line} ({d.kind}).",
                    line=d.line, file=d.path, kind=d.kind, qualname=d.qualname,
                    reason="symbol_found", confidence="high", matches=len(hits),
                )
        elif target.suffix in _LANGUAGE_BY_SUFFIX:
            line = regex_definition_line(target, _LANGUAGE_BY_SUFFIX[target.suffix], name)
            if line is not None:
                return _result(
                    claim, "supports",
                    f"A definition of `{name}` matches in `{rel}` at line {line} (regex, not parsed).",
                    line=line, file=rel, reason="symbol_found_regex", confidence="medium", method="regex",
                )
            return _result(
                claim, "inconclusive",
                f"No regex definition of `{name}` in `{rel}`; regex search cannot prove absence.",
                file=rel, reason="regex_miss", method="regex",
            )
        else:
            return _result(
                claim, "inconclusive",
                f"`{rel}` is not a supported language for static symbol checks.",
                file=rel, reason="unsupported_language",
            )

    # ---- 2. whole-repository search ----------------------------------------
    py_hits: list[Definition] = []
    regex_hit: tuple[str, int] | None = None
    for path in all_files:
        if path.suffix in PYTHON_SUFFIXES:
            py_hits.extend(d for d in defs_for(path) if symbol_matches(d, parts))
        elif regex_hit is None and path.suffix in _LANGUAGE_BY_SUFFIX:
            line = regex_definition_line(path, _LANGUAGE_BY_SUFFIX[path.suffix], name)
            if line is not None:
                regex_hit = (_rel(root, path), line)

    claimed_note = ""
    if target is not None:
        claimed_note = f" It is not defined in the named file `{_rel(root, target)}`."
    elif rel_file:
        claimed_note = (
            f" `{rel_file}` matches several files." if file_candidates else f" `{rel_file}` was not found."
        )

    if not py_hits and len(parts) > 1:
        reexports = [d for p in all_files if p.suffix in PYTHON_SUFFIXES for d in defs_for(p) if reexport_match(d, parts)]
        if reexports:
            d = reexports[0]
            return _result(
                claim, "supports",
                f"`{pretty}` is most likely the {d.kind} defined in `{d.path}` at line {d.line} "
                f"(re-exported by its package).{claimed_note}",
                line=d.line, file=d.path, kind=d.kind, qualname=d.qualname,
                reason="symbol_found_reexport", confidence="medium",
                file_mismatch=bool(rel_file), matches=len(reexports),
            )

    if py_hits:
        d = py_hits[0]
        return _result(
            claim, "supports",
            f"`{pretty}` is defined in `{d.path}` at line {d.line} ({d.kind}).{claimed_note}",
            line=d.line, file=d.path, kind=d.kind, qualname=d.qualname,
            reason="symbol_found", confidence="medium" if rel_file else "high",
            file_mismatch=bool(rel_file), matches=len(py_hits),
            other_locations=[f"{h.path}:{h.line}" for h in py_hits[1:6]],
        )
    if regex_hit:
        rel, line = regex_hit
        return _result(
            claim, "supports",
            f"A definition of `{name}` matches in `{rel}` at line {line} (regex, not parsed).{claimed_note}",
            line=line, file=rel, reason="symbol_found_regex", confidence="low", method="regex",
            file_mismatch=bool(rel_file),
        )

    # ---- 3. not found: decide between contradicts and inconclusive ----------
    if _reported_as_missing(claim, name):
        return _result(
            claim, "inconclusive",
            f"`{pretty}` is not defined in the repository, which matches the reported error "
            f"(the report itself says `{name}` is missing), so its absence is not evidence against the claim.",
            reason="reported_missing",
        )
    if len(parts) == 1 and name in BUILTIN_ATTRIBUTES:
        return _result(
            claim, "inconclusive",
            f"`{name}` is an attribute of Python built-in types (e.g. str, list, dict), not something this "
            "repository would define; its absence here says nothing about the claim.",
            reason="builtin_attribute",
        )

    py_files = [p for p in all_files if p.suffix in PYTHON_SUFFIXES]
    known = sorted({d.name for p in py_files for d in defs_for(p)})

    if len(parts) > 1:
        # "Text.from_markup" or "poetry.core.vcs.git.ParsedUrl.parse": absence here
        # proves nothing when the leading name comes from a dependency. Require
        # evidence of that: the name is imported from a non-repo module somewhere,
        # or a dotted prefix matches a declared dependency (poetry.core ~ poetry-core).
        # A made-up "PaymentGateway.charge" has neither and falls through.
        repo_modules: set[str] = set()
        for path in py_files:
            repo_modules.update(PurePosixPath(_rel(root, path)).with_suffix("").parts)
        repo_modules.discard("src")
        imported: set[str] = set()
        for path in py_files:
            try:
                imported |= external_imports(path, repo_modules)
            except (SyntaxError, ValueError, RecursionError, MemoryError, OSError):
                continue  # already recorded in parse_errors by defs_for
        deps = declared_dependencies(root)
        prefixes = {_norm_dist("_".join(parts[:k])) for k in range(1, len(parts))}
        via = (
            f"`{parts[0]}` is imported from outside this repository"
            if parts[0] in imported
            else next((f"`{d}` is a declared dependency" for d in sorted(prefixes & deps)), None)
        )
        if via:
            return _result(
                claim, "inconclusive",
                f"`{pretty}` is not defined here; {via}, so the symbol probably lives in that dependency.",
                reason="external_module",
                files_scanned=len(all_files),
            )
    close = difflib.get_close_matches(name, known, n=3, cutoff=0.75)
    details = {"files_scanned": len(all_files), "parse_errors": parse_errors[:20], "truncated": truncated}

    if close:
        loc = {}
        for p in py_files:
            for d in defs_for(p):
                if d.name in close and d.name not in loc:
                    loc[d.name] = d
        suggestions = [f"{loc[c].qualname} ({loc[c].path}:{loc[c].line})" for c in close]
        first = loc[close[0]]
        return _result(
            claim, "inconclusive",
            f"`{pretty}` is not defined anywhere in the repository. Did you mean "
            f"`{first.qualname}` in `{first.path}` at line {first.line}?{claimed_note}",
            line=first.line, file=first.path, reason="close_match", suggestions=suggestions, **details,
        )

    if truncated:
        return _result(claim, "inconclusive", f"`{pretty}` not found, but the repository search was truncated.",
                       reason="search_truncated", **details)
    if not py_files:
        return _result(claim, "inconclusive", "No Python source files to search; absence cannot be established.",
                       reason="no_python_sources", **details)
    if parse_errors:
        return _result(
            claim, "inconclusive",
            f"`{pretty}` not found, but {len(parse_errors)} file(s) could not be parsed "
            f"(e.g. `{parse_errors[0]}`), so absence is not certain.",
            reason="parse_error", **details,
        )

    word = re.compile(rf"\b{re.escape(name)}\b")
    for path in all_files:
        try:
            if word.search(path.read_text(encoding="utf-8", errors="replace")):
                rel = _rel(root, path)
                return _result(
                    claim, "inconclusive",
                    f"`{pretty}` has no static definition, but the name appears in `{rel}` "
                    "(possibly an import, dynamic definition or extension module).",
                    file=rel, reason="mentioned_not_defined", **details,
                )
        except OSError:
            continue

    return _result(
        claim, "contradicts",
        f"`{pretty}` is not defined or mentioned in any of the {len(py_files)} Python file(s) in the repository."
        f"{claimed_note}",
        reason="symbol_not_found", confidence="high", **details,
    )
