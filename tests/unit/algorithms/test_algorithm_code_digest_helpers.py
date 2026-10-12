"""Unit tests for the issue #1636 algorithm-digest helper fallbacks.

The campaign-level integration tests in
``tests/integration/test_cache_invalidation.py`` cover the happy paths
of the plugin code-hash machinery (plug-in upgrade invalidates the
``GENERATE_SAMPLES`` cache key). These tests cover the defensive
fallback branches of the private helpers so the module stays above its
per-module coverage floor (issue #1571).
"""

import logging
import sys
import types
from pathlib import Path

from osimflow import _campaign_code_hashes as ch


class _StubPath:
    """Duck-typed stand-in for ``Path`` forcing ``IndexError`` on ``parents``.

    Real ``Path.parents`` chains climb all the way to the filesystem
    root, so a shallow dotted package name never overflows it. A plain
    list raises ``IndexError`` past its length, exercising the bounded
    guard in ``_algorithm_package_scope``.
    """

    def __init__(self, name: str, parent_chain: list["_StubPath"]) -> None:
        self.name = name
        self._parents = parent_chain

    @property
    def parent(self) -> "_StubPath":
        return self._parents[0]

    @property
    def parents(self) -> list["_StubPath"]:
        return self._parents


def test_plugin_distribution_version_maps_installed_dist(monkeypatch) -> None:
    monkeypatch.setattr(ch, "packages_distributions", lambda: {"osimflow": ["osimflow"]})
    monkeypatch.setattr(ch, "version", lambda dist: "9.9.9")
    assert ch._plugin_distribution_version("osimflow.algorithms") == "9.9.9"


def test_plugin_distribution_version_swallows_metadata_errors(monkeypatch) -> None:
    def _boom() -> dict[str, list[str]]:
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(ch, "packages_distributions", _boom)
    assert ch._plugin_distribution_version("anything") == "unknown"


def test_package_scope_rejects_non_string_module_name(tmp_path: Path) -> None:
    mod = tmp_path / "pkg" / "mod.py"
    assert ch._algorithm_package_scope(mod, None) is None
    assert ch._algorithm_package_scope(mod, "") is None


def test_package_scope_rejects_innermost_dir_mismatch(tmp_path: Path) -> None:
    mod = tmp_path / "weird" / "mod.py"
    assert ch._algorithm_package_scope(mod, "pkg.sub") is None


def test_package_scope_rejects_mid_chain_mismatch(tmp_path: Path) -> None:
    mod = tmp_path / "other" / "sub" / "mod.py"
    assert ch._algorithm_package_scope(mod, "pkg.sub.mod") is None


def test_package_scope_index_error_returns_none() -> None:
    parent_b = _StubPath("b", [])
    parent_c = _StubPath("c", [parent_b])
    mod = _StubPath("d.py", [parent_c, parent_b])
    assert ch._algorithm_package_scope(mod, "a.b.c.d") is None


def test_implementation_files_unmatched_init_scopes_to_package_tree(
    tmp_path: Path,
) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    init_file = pkg / "__init__.py"
    init_file.write_text("", encoding="utf-8")
    helper = pkg / "helper.py"
    helper.write_text("X = 1\n", encoding="utf-8")

    class _Cls:
        pass

    _Cls.__module__ = "zzz_unmatched_pkg"
    files = ch._algorithm_implementation_files(_Cls, init_file)
    resolved = {p.resolve() for p in files}
    assert init_file.resolve() in resolved
    assert helper.resolve() in resolved


def test_algorithm_code_digest_missing_source_file_falls_back(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    missing = tmp_path / "gone.py"
    fake_mod = types.ModuleType("issue1636_gone_module")
    fake_mod.__file__ = str(missing)
    monkeypatch.setitem(sys.modules, "issue1636_gone_module", fake_mod)

    class _Gone:
        pass

    _Gone.__module__ = "issue1636_gone_module"
    gone = _Gone()

    with caplog.at_level(logging.WARNING, logger="osimflow.code_hashes"):
        first = ch._algorithm_code_digest(gone)
    second = ch._algorithm_code_digest(gone)
    assert first == second
    assert len(first) == 64
    assert "no source file" in caplog.text
