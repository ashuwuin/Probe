"""Tests for probe.verify.static (AST / regex symbol checker)."""

import os
import shutil
from pathlib import Path

import pytest

from probe.models import CheckResult, Claim
from probe.verify import static
from probe.verify.static import (
    PathEscapeError,
    check_static_symbol,
    normalize_claim_path,
    normalize_symbol,
)

FIXTURES = Path(__file__).parent / "fixtures"


def claim(symbol=None, file=None, cid="c1", ctype="symbol_exists"):
    return Claim(id=cid, type=ctype, symbol=symbol, file=file, quote="quoted text")


@pytest.fixture
def repo(tmp_path):
    """A small repo: src/payments/billing.py plus JS and Rust files."""
    pkg = tmp_path / "src" / "payments"
    pkg.mkdir(parents=True)
    shutil.copy(FIXTURES / "billing.py", pkg / "billing.py")
    (pkg / "__init__.py").write_text("")
    web = tmp_path / "web"
    web.mkdir()
    shutil.copy(FIXTURES / "cart.js", web / "cart.js")
    shutil.copy(FIXTURES / "ledger.rs", tmp_path / "ledger.rs")
    (tmp_path / "README.md").write_text("# demo\n")
    return tmp_path


# ---------------------------------------------------------------- contract

def test_returns_check_result_with_static_checker_name(repo):
    result = check_static_symbol(repo, claim("calculate_tax"))
    assert isinstance(result, CheckResult)
    assert result.checker == "static_ast"
    assert result.claim_id == "c1"
    CheckResult.model_validate_json(result.model_dump_json())  # JSON-serialisable


# ---------------------------------------------------------------- found (supports)

@pytest.mark.parametrize(
    ("symbol", "kind", "line"),
    [
        ("calculate_tax", "function", 8),
        ("fetch_exchange_rate", "async_function", 15),
        ("Invoice", "class", 19),
        ("TAX_RATE", "variable", 3),
        ("DEFAULT_CURRENCY", "variable", 4),   # annotated assignment
        ("MAX_AMOUNT", "variable", 5),          # tuple unpacking
        ("Money", "variable", 32),              # assigned in an except handler
    ],
)
def test_finds_module_level_definitions_in_named_file(repo, symbol, kind, line):
    result = check_static_symbol(repo, claim(symbol, "src/payments/billing.py"))
    assert result.outcome == "supports"
    assert result.line_number == line
    assert result.details["kind"] == kind
    assert result.details["file"] == "src/payments/billing.py"
    assert result.details["confidence"] == "high"


def test_finds_method_by_qualified_name(repo):
    result = check_static_symbol(repo, claim("Invoice.total", "src/payments/billing.py"))
    assert result.outcome == "supports"
    assert result.details["kind"] == "method"
    assert result.details["qualname"] == "Invoice.total"


def test_finds_symbol_by_module_dotted_path(repo):
    result = check_static_symbol(repo, claim("payments.billing.calculate_tax"))
    assert result.outcome == "supports"
    assert result.line_number == 8


def test_wrong_class_for_method_is_not_a_match(repo):
    result = check_static_symbol(repo, claim("Cart.total", "src/payments/billing.py"))
    assert result.outcome != "supports"


@pytest.mark.parametrize("raw", ["calculate_tax()", "`calculate_tax`", " calculate_tax(amount) ", "'calculate_tax'"])
def test_symbol_text_is_normalised(repo, raw):
    assert check_static_symbol(repo, claim(raw, "src/payments/billing.py")).outcome == "supports"


def test_bare_file_name_resolves_to_unique_repo_path(repo):
    result = check_static_symbol(repo, claim("calculate_tax", "billing.py"))
    assert result.outcome == "supports"
    assert result.details["file"] == "src/payments/billing.py"
    assert result.details["confidence"] == "high"


def test_file_with_line_suffix_is_accepted(repo):
    assert check_static_symbol(repo, claim("calculate_tax", "billing.py:12")).outcome == "supports"


def test_leading_slash_is_treated_as_repo_relative(repo):
    assert check_static_symbol(repo, claim("calculate_tax", "/src/payments/billing.py")).outcome == "supports"


def test_missing_file_falls_back_to_whole_repo_search(repo):
    result = check_static_symbol(repo, claim("calculate_tax", "src/tax.py"))
    assert result.outcome == "supports"
    assert result.details["file"] == "src/payments/billing.py"
    assert result.details["file_mismatch"] is True
    assert result.details["confidence"] == "medium"
    assert "src/tax.py" in result.evidence


def test_no_file_searches_whole_repo(repo):
    result = check_static_symbol(repo, claim("Invoice"))
    assert result.outcome == "supports"
    assert result.details["file_mismatch"] is False


def test_symbol_in_other_file_than_claimed(repo):
    (repo / "src" / "util.py").write_text("def helper():\n    pass\n")
    result = check_static_symbol(repo, claim("calculate_tax", "src/util.py"))
    assert result.outcome == "supports"
    assert result.details["file"] == "src/payments/billing.py"
    assert result.details["file_mismatch"] is True


# ---------------------------------------------------------------- not found

def test_absent_symbol_contradicts(repo):
    result = check_static_symbol(repo, claim("parse_url_v2", "src/payments/billing.py"))
    assert result.outcome == "contradicts"
    assert result.details["reason"] == "symbol_not_found"
    assert result.details["confidence"] == "high"


def test_close_match_is_inconclusive_with_suggestion(repo):
    result = check_static_symbol(repo, claim("calculate_taxes", "src/payments/billing.py"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "close_match"
    assert "Did you mean `calculate_tax`" in result.evidence
    assert result.line_number == 8


def test_name_mentioned_but_not_defined_is_inconclusive(repo):
    (repo / "src" / "compat.py").write_text("from ._speedups import fast_hash\n")
    result = check_static_symbol(repo, claim("fast_hash"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "mentioned_not_defined"


def test_name_mentioned_in_c_extension_is_inconclusive(repo):
    (repo / "src" / "_speedups.c").write_text("static PyObject* crc_digest(PyObject* self) { return NULL; }\n")
    result = check_static_symbol(repo, claim("crc_digest"))
    assert result.outcome == "inconclusive"


def test_parse_error_blocks_contradiction(repo):
    shutil.copy(FIXTURES / "broken_syntax.py", repo / "src" / "broken_syntax.py")
    result = check_static_symbol(repo, claim("does_not_exist_anywhere"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "parse_error"
    assert "src/broken_syntax.py" in result.details["parse_errors"]


def test_parse_error_in_named_file_still_finds_symbol_elsewhere(repo):
    shutil.copy(FIXTURES / "broken_syntax.py", repo / "src" / "broken_syntax.py")
    result = check_static_symbol(repo, claim("calculate_tax", "src/broken_syntax.py"))
    assert result.outcome == "supports"
    assert result.details["file"] == "src/payments/billing.py"


def test_truncated_search_blocks_contradiction(repo, monkeypatch):
    monkeypatch.setattr(static, "MAX_FILES", 1)
    result = check_static_symbol(repo, claim("does_not_exist_anywhere"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "search_truncated"


def test_repo_without_python_never_contradicts(tmp_path):
    shutil.copy(FIXTURES / "cart.js", tmp_path / "cart.js")
    result = check_static_symbol(tmp_path, claim("does_not_exist_anywhere"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "no_python_sources"


def test_vendored_directories_are_ignored(repo):
    venv = repo / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "vendored.py").write_text("def only_in_venv():\n    pass\n")
    nm = repo / "node_modules" / "x"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("function onlyInNodeModules() {}\n")
    assert check_static_symbol(repo, claim("only_in_venv")).outcome == "contradicts"
    assert check_static_symbol(repo, claim("onlyInNodeModules")).outcome == "contradicts"


# ---------------------------------------------------------------- non-Python (regex)

@pytest.mark.parametrize("symbol", ["addItem", "removeItem", "Cart", "checkout"])
def test_regex_finds_javascript_definitions(repo, symbol):
    result = check_static_symbol(repo, claim(symbol, "web/cart.js"))
    assert result.outcome == "supports"
    assert result.details["method"] == "regex"
    assert result.details["confidence"] == "medium"


@pytest.mark.parametrize("symbol", ["Ledger", "post_entry"])
def test_regex_finds_rust_definitions(repo, symbol):
    assert check_static_symbol(repo, claim(symbol, "ledger.rs")).outcome == "supports"


def test_regex_miss_is_inconclusive_never_contradicts(repo):
    result = check_static_symbol(repo, claim("applyCoupon", "web/cart.js"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "regex_miss"


def test_regex_hit_found_by_whole_repo_search_has_low_confidence(repo):
    result = check_static_symbol(repo, claim("addItem"))
    assert result.outcome == "supports"
    assert result.details["confidence"] == "low"
    assert result.details["file"] == "web/cart.js"


def test_unsupported_language_file_is_inconclusive(repo):
    (repo / "Main.java").write_text("class Main {}\n")
    result = check_static_symbol(repo, claim("Main", "Main.java"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "unsupported_language"


# ---------------------------------------------------------------- file-only claims

def test_file_only_claim_supports_existing_file(repo):
    result = check_static_symbol(repo, claim(file="README.md"))
    assert result.outcome == "supports"
    assert result.details["reason"] == "file_found"


def test_file_only_claim_finds_non_source_file_by_suffix(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("x")
    result = check_static_symbol(repo, claim(file="guide.md"))
    assert result.outcome == "supports"
    assert result.details["file"] == "docs/guide.md"


def test_file_only_claim_contradicts_missing_file(repo):
    result = check_static_symbol(repo, claim(file="src/payments/refunds.py"))
    assert result.outcome == "contradicts"
    assert result.details["reason"] == "file_not_found"


def test_file_only_claim_ambiguous_is_inconclusive(repo):
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    (repo / "a" / "utils.py").write_text("")
    (repo / "b" / "utils.py").write_text("")
    result = check_static_symbol(repo, claim(file="utils.py"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "file_ambiguous"


# ---------------------------------------------------------------- unsafe / bad input

@pytest.mark.parametrize("bad", ["../secrets.py", "src/../../etc/passwd", "..\\..\\x.py", "C:/Windows/x.py"])
def test_path_traversal_is_refused(repo, bad):
    result = check_static_symbol(repo, claim("calculate_tax", bad))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "path_outside_repo"


def test_symlink_escaping_repo_is_refused(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.py").write_text("def leaked():\n    pass\n")
    os.symlink(outside / "secret.py", repo / "link.py")
    result = check_static_symbol(repo, claim("leaked", "link.py"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "path_outside_repo"
    # The whole-repo walk must not follow the symlink either.
    assert check_static_symbol(repo, claim("leaked")).outcome != "supports"


def test_symlinked_directory_outside_repo_is_not_walked(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside_dir")
    (outside / "mod.py").write_text("def outside_fn():\n    pass\n")
    os.symlink(outside, repo / "linked_dir")
    assert check_static_symbol(repo, claim("outside_fn")).outcome == "contradicts"


@pytest.mark.parametrize("bad_symbol", ["calculate tax", "rm -rf /", "a..b", "1abc", ""])
def test_non_identifier_symbol_is_inconclusive(repo, bad_symbol):
    result = check_static_symbol(repo, claim(bad_symbol or None, file=None if not bad_symbol else "billing.py"))
    assert result.outcome == "inconclusive"


def test_claim_with_nothing_to_check(repo):
    result = check_static_symbol(repo, claim())
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "nothing_to_check"


def test_missing_repo_is_inconclusive(tmp_path):
    result = check_static_symbol(tmp_path / "nope", claim("calculate_tax"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "repo_unavailable"


def test_checker_never_executes_repository_code(tmp_path):
    marker = tmp_path / "executed.txt"
    (tmp_path / "evil.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\n\ndef target():\n    pass\n"
    )
    assert check_static_symbol(tmp_path, claim("target", "evil.py")).outcome == "supports"
    assert not marker.exists()


def test_oversized_files_are_skipped(repo, monkeypatch):
    monkeypatch.setattr(static, "MAX_FILE_BYTES", 10)
    result = check_static_symbol(repo, claim("calculate_tax"))
    assert result.outcome != "supports"


def test_results_are_deterministic(repo):
    (repo / "dup").mkdir()
    (repo / "dup" / "copy.py").write_text("def calculate_tax(x):\n    return x\n")
    first = check_static_symbol(repo, claim("calculate_tax"))
    for _ in range(3):
        assert check_static_symbol(repo, claim("calculate_tax")) == first
    assert first.details["matches"] == 2


# ---------------------------------------------------------------- helpers

def test_normalize_symbol():
    assert normalize_symbol("Invoice.total()") == ["Invoice", "total"]
    assert normalize_symbol("`x`") == ["x"]
    assert normalize_symbol("not valid!") is None
    assert normalize_symbol(None) is None


def test_normalize_claim_path():
    assert normalize_claim_path("./src//billing.py") == "src/billing.py"
    assert normalize_claim_path("src\\billing.py") == "src/billing.py"
    assert normalize_claim_path("`billing.py:42:7`") == "billing.py"
    for bad in ["../x", "a/../../b", "C:/x", "", "a\x00b"]:
        with pytest.raises(PathEscapeError):
            normalize_claim_path(bad)


# ---------------------------------------------------------------- external code (dependencies)

@pytest.mark.parametrize(
    "path",
    [
        "~/.local/share/pipx/venvs/poetry/lib/python3.12/site-packages/poetry/core/masonry/utils/module.py",
        "C:\\Python311\\Lib\\site-packages\\rich\\text.py",
        "/usr/lib/python3/dist-packages/foo.py",
    ],
)
def test_file_inside_installed_dependency_is_inconclusive(repo, path):
    result = check_static_symbol(repo, claim("__init__", path))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "external_file"


def test_made_up_dotted_name_without_dependency_evidence_contradicts(repo):
    """PaymentGateway is not defined, imported or declared anywhere: absence is real evidence."""
    result = check_static_symbol(repo, claim("PaymentGateway.charge"))
    assert result.outcome == "contradicts"
    assert result.details["reason"] == "symbol_not_found"


@pytest.mark.parametrize(
    ("import_line", "symbol"),
    [
        ("import rich\n", "rich.Text.from_markup"),
        ("from rich.text import Text\n", "Text.from_markup"),
        ("import numpy as np\n", "np.frombuffer"),
        ("from requests import Session\n", "requests.get"),
    ],
)
def test_dotted_symbol_imported_from_dependency_is_inconclusive(repo, import_line, symbol):
    (repo / "src" / "payments" / "render.py").write_text(import_line)
    result = check_static_symbol(repo, claim(symbol))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "external_module"
    assert "imported from outside" in result.evidence


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("pyproject.toml", '[project]\nname = "poetry"\ndependencies = ["poetry-core (>=1.0)", "cleo>=2"]\n'),
        ("pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.8"\n"poetry-core" = "1.4.0"\n'),
        ("requirements.txt", "# pinned\ncleo==2.0\nPoetry_Core>=1.0  # core\n-r other.txt\n"),
        ("setup.cfg", "[options]\ninstall_requires =\n    cleo\n    poetry-core>=1.0\n"),
        ("setup.py", 'from setuptools import setup\nsetup(install_requires=["cleo", "poetry.core>=1"])\n'),
    ],
)
def test_dotted_prefix_matching_declared_dependency_is_inconclusive(repo, filename, content):
    (repo / filename).write_text(content)
    result = check_static_symbol(repo, claim("poetry.core.vcs.git.ParsedUrl.parse"))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "external_module"
    assert "poetry_core" in result.evidence


def test_import_of_repo_own_package_is_not_external(repo):
    (repo / "src" / "app.py").write_text("from payments import billing\nimport payments\n")
    result = check_static_symbol(repo, claim("billing.refund_everything"))
    assert result.outcome == "contradicts"


def test_relative_imports_are_not_external(repo):
    (repo / "src" / "payments" / "api.py").write_text("from .gateway import Gateway\n")
    result = check_static_symbol(repo, claim("Gateway.charge"))
    assert result.outcome == "contradicts"


def test_malformed_dependency_files_do_not_crash(repo):
    (repo / "pyproject.toml").write_text("this is [not toml")
    (repo / "setup.cfg").write_text("[options\nbroken")
    assert check_static_symbol(repo, claim("PaymentGateway.charge")).outcome == "contradicts"


def test_dotted_symbol_with_local_module_prefix_can_still_contradict(repo):
    result = check_static_symbol(repo, claim("billing.refund_everything"))
    assert result.outcome == "contradicts"


def test_dotted_symbol_with_local_class_prefix_can_still_contradict(repo):
    result = check_static_symbol(repo, claim("Invoice.refund_everything"))
    assert result.outcome == "contradicts"


# ---------------------------------------------------------------- re-exports and messages

def test_package_reexport_is_supported_with_medium_confidence(tmp_path):
    pkg = tmp_path / "src" / "requests"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .api import get\n")
    (pkg / "api.py").write_text("def get(url):\n    return url\n")
    result = check_static_symbol(tmp_path, claim("requests.get"))
    assert result.outcome == "supports"
    assert result.details["reason"] == "symbol_found_reexport"
    assert result.details["confidence"] == "medium"
    assert result.details["file"] == "src/requests/api.py"


def test_reexport_does_not_match_methods_or_wrong_package(tmp_path):
    pkg = tmp_path / "shop"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "api.py").write_text("class Client:\n    def get(self):\n        pass\n")
    assert check_static_symbol(tmp_path, claim("shop.get")).details["reason"] != "symbol_found_reexport"
    (tmp_path / "other.py").write_text("def get():\n    pass\n")
    assert check_static_symbol(tmp_path, claim("shop.get")).details["reason"] != "symbol_found_reexport"


def test_message_when_named_file_exists_but_symbol_missing(repo):
    result = check_static_symbol(repo, claim("parse_url_v2", "src/payments/billing.py"))
    assert result.outcome == "contradicts"
    assert "not defined in the named file `src/payments/billing.py`" in result.evidence
    assert "was not found" not in result.evidence


# ---------------------------------------------------------------- installed copies of this project

@pytest.fixture
def src_layout_repo(tmp_path):
    pkg = tmp_path / "src" / "payments"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    shutil.copy(FIXTURES / "billing.py", pkg / "billing.py")
    core = tmp_path / "src" / "poetry"          # namespace dir without core/
    core.mkdir()
    (core / "__init__.py").write_text("def __init__():\n    pass\n")
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        "/home/u/.venv/lib/python3.12/site-packages/payments/billing.py",
        "C:\\Python311\\Lib\\site-packages\\payments\\billing.py",
        "/usr/lib/python3/dist-packages/payments/billing.py",
        "`~/.local/lib/python3.11/site-packages/payments/billing.py`",
    ],
)
def test_installed_copy_of_this_project_maps_to_src_layout(src_layout_repo, path):
    result = check_static_symbol(src_layout_repo, claim("calculate_tax", path))
    assert result.outcome == "supports"
    assert result.details["file"] == "src/payments/billing.py"
    assert result.details["confidence"] == "high"


def test_installed_copy_maps_to_flat_layout(repo):
    # `repo` fixture has src/payments/billing.py; build a flat-layout package too
    (repo / "shop").mkdir()
    (repo / "shop" / "cart.py").write_text("def add_item():\n    pass\n")
    result = check_static_symbol(repo, claim("add_item", "/x/site-packages/shop/cart.py"))
    assert result.outcome == "supports" and result.details["file"] == "shop/cart.py"


def test_installed_copy_symbol_missing_in_mapped_file_can_contradict(src_layout_repo):
    result = check_static_symbol(src_layout_repo, claim("refund_everything", "/x/site-packages/payments/billing.py"))
    assert result.outcome == "contradicts"
    assert "not defined in the named file `src/payments/billing.py`" in result.evidence


def test_namespace_package_from_other_distribution_stays_external(src_layout_repo):
    """poetry#9961: site-packages/poetry/core/... is poetry-core, not this repo's src/poetry/."""
    path = "~/.local/share/pipx/venvs/poetry/lib/python3.12/site-packages/poetry/core/masonry/utils/module.py"
    result = check_static_symbol(src_layout_repo, claim("__init__", path))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "external_file"


@pytest.mark.parametrize(
    "path",
    [
        "/x/site-packages/numpy/core/numeric.py",       # package not in repo
        "/x/site-packages/payments",                     # no file part
        "/x/site-packages/../../etc/passwd",             # traversal after the segment
        "/x/site-packages/payments/../../../secret.py",
    ],
)
def test_unmappable_installed_paths_stay_external(src_layout_repo, path):
    result = check_static_symbol(src_layout_repo, claim("calculate_tax", path))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "external_file"


# ---------------------------------------------------------------- absence that agrees with the report

@pytest.mark.parametrize(
    ("symbol", "quote", "actual"),
    [
        ("session_id_version", "Login fails", "AttributeError: 'HikAx' object has no attribute 'session_id_version'"),
        ("save_json", "ImportError: cannot import name 'save_json' from 'swemail.util'", None),
        ("helper_fn", "NameError: name 'helper_fn' is not defined", None),
        ("search_keys", "Search functionality: `search_keys` doesn't exist.", None),
        ("listen", "AttributeError: 'CloudEvents' object has no attribute \"listen\"", None),
    ],
)
def test_absence_matching_the_reported_error_is_inconclusive(repo, symbol, quote, actual):
    c = Claim(id="c1", type="bug_behavior", symbol=symbol, quote=quote, actual=actual)
    result = check_static_symbol(repo, c)
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "reported_missing"


@pytest.mark.parametrize("symbol", ["replace", "append", "items", "startswith", "len", "isinstance"])
def test_builtin_attribute_names_are_inconclusive(repo, symbol):
    result = check_static_symbol(repo, claim(symbol))
    assert result.outcome == "inconclusive"
    assert result.details["reason"] == "builtin_attribute"


def test_report_that_merely_mentions_a_name_still_contradicts(repo):
    c = Claim(id="c1", type="bug_behavior", symbol="refund_everything",
              quote="refund_everything() crashes with a ValueError on negative amounts")
    assert check_static_symbol(repo, c).outcome == "contradicts"


def test_reported_missing_only_applies_to_the_same_name(repo):
    c = Claim(id="c1", type="bug_behavior", symbol="refund_everything",
              quote="AttributeError: 'Invoice' object has no attribute 'refund'")
    assert check_static_symbol(repo, c).outcome == "contradicts"
