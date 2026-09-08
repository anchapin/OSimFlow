"""Code-hash computation for Campaign cache keys.

This module extracts the code-hash machinery from ``osimflow.campaign``
(issue #1462).  It owns:

- the AST-based transitive ``osimflow``-internal import closure of the
  work layer (issue #1446),
- the ``bin`` / ``work`` / BYOS file-content hashes mixed into
  :class:`osimflow.cache.CacheKey`,
- the BYOS folding helper (issue #1011),
- the algorithm-implementation digest mixed into the
  ``GENERATE_<ALGO>_SAMPLES`` cache key (issue #1636).

``compute_code_hashes`` intentionally keeps the legacy
``Campaign._compute_code_hashes(self, cfg=None)`` signature shape so
the Campaign method can delegate one-to-one — including the unbound
``Campaign._compute_code_hashes(_Stub())`` test path, which relies on
the function not depending on real instance state.
"""

import ast
import hashlib
import inspect
import logging
import sys
from collections.abc import Iterator
from importlib.metadata import packages_distributions, version
from pathlib import Path
from typing import Any

from .algorithms import BaseAlgorithm
from .cache import sha256_of_files
from .config import CampaignConfig

_IMPORT_CLOSURE_CACHE: dict[Path, frozenset[Path]] = {}

log = logging.getLogger("osimflow.code_hashes")


def _osimflow_prefixes(dotted: str) -> Iterator[str]:
    """Yield the dotted prefixes of ``dotted`` restricted to ``osimflow.*``.

    The bare ``osimflow`` package is deliberately excluded: its
    ``__init__.py`` imports the entire public API surface, so hashing it
    would collapse the per-module closure into a de-facto whole-package
    hash (issue #1446). Subpackage ``__init__.py`` files (depth >= 1) are
    included because Python executes them when their submodules are
    imported.
    """
    parts = dotted.split(".")
    if parts[0] != "osimflow":
        return
    for depth in range(2, len(parts) + 1):
        yield ".".join(parts[:depth])


def _module_file_for(package_root: Path, dotted: str) -> Path | None:
    """Resolve a dotted ``osimflow.*`` module name to a file on disk.

    Purely path-based — no modules are imported and site-packages is
    never consulted. Returns ``None`` for third-party / stdlib names and
    for ``osimflow.*`` names without a ``.py`` file or ``__init__.py``
    under ``package_root``.
    """
    parts = dotted.split(".")
    if parts[0] != "osimflow" or len(parts) < 2:
        return None
    base = package_root.joinpath(*parts[1:])
    module_file = base.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_init = base / "__init__.py"
    if package_init.is_file():
        return package_init
    return None


def _module_name_for_file(package_root: Path, module_file: Path) -> str:
    """Dotted ``osimflow.*`` module name for a file inside ``package_root``."""
    try:
        rel = module_file.resolve().relative_to(package_root.resolve())
    except ValueError:
        return ""
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(["osimflow", *parts])


def _importfrom_targets(
    node: ast.ImportFrom,
    package_parts: list[str],
) -> list[str]:
    """``osimflow.*`` module names referenced by one ``from ... import``.

    ``package_parts`` is the dotted package path the import is relative
    to (empty parts or a non-``osimflow`` base yield no targets, which
    is how the stdlib and site-packages stay out of the closure).
    """
    if node.level > 0:
        base_parts = package_parts[: -(node.level - 1)] if node.level > 1 else list(package_parts)
        if node.module:
            base_parts = [*base_parts, *node.module.split(".")]
    elif node.module:
        base_parts = node.module.split(".")
    else:
        return []
    if not base_parts or base_parts[0] != "osimflow":
        return []
    base = ".".join(base_parts)
    targets = list(_osimflow_prefixes(base))
    for alias in node.names:
        targets.extend(_osimflow_prefixes(f"{base}.{alias.name}"))
    return targets


def _iter_import_targets(
    tree: ast.Module,
    package_root: Path,
    module_file: Path,
) -> Iterator[str]:
    """Yield every ``osimflow.*`` module name imported anywhere in ``tree``.

    Handles ``import a.b.c``, ``from a.b import c``, and relative imports
    (``from . import x`` / ``from .x import y``). Imports are syntactically
    statements, so the walk descends only through statement-bearing fields
    (``body`` / ``orelse`` / ``finalbody`` / exception handlers / match
    cases) and skips expression subtrees entirely — this keeps the walk
    cheap while still covering conditional and function-level imports.
    Over-approximating is safe for cache invalidation.
    """
    targets: list[str] = []
    module_name = _module_name_for_file(package_root, module_file)
    if module_file.name == "__init__.py":
        package_parts = module_name.split(".")
    else:
        package_parts = module_name.split(".")[:-1]
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.extend(_osimflow_prefixes(alias.name))
            continue
        if isinstance(node, ast.ImportFrom):
            targets.extend(_importfrom_targets(node, package_parts))
            continue
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            children = getattr(node, field, None)
            if children:
                stack.extend(children)
    yield from targets


def _transitive_import_closure(package_root: Path) -> frozenset[Path]:
    """Transitive ``osimflow``-internal import closure of the work layer.

    Roots: ``osimflow.work``, ``osimflow.apply_params``, and every
    ``osimflow._work_scripts`` module. Walks ``import`` / ``from ...
    import`` statements (AST-based — deterministic, fast, no import side
    effects) and keeps only files under ``package_root``; stdlib and
    third-party imports are excluded (issue #1446).

    Cached per ``package_root`` at module level: the closure is a
    property of the source tree, and the *contents* of the listed files
    are re-read on every ``compute_code_hashes`` call, so the one-time
    walk adds no measurable cost to ``Campaign`` construction.
    """
    cached = _IMPORT_CLOSURE_CACHE.get(package_root)
    if cached is not None:
        return cached
    roots = ["osimflow.work", "osimflow.apply_params"]
    scripts_dir = package_root / "_work_scripts"
    if scripts_dir.is_dir():
        roots.append("osimflow._work_scripts")
        for script in sorted(scripts_dir.glob("*.py")):
            if script.name != "__init__.py":
                roots.append(f"osimflow._work_scripts.{script.stem}")
    seen: set[Path] = set()
    visited: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        module_file = _module_file_for(package_root, name)
        if module_file is None:
            continue
        seen.add(module_file)
        try:
            tree = ast.parse(module_file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue
        pending.extend(_iter_import_targets(tree, package_root, module_file))
    closure = frozenset(seen)
    _IMPORT_CLOSURE_CACHE[package_root] = closure
    return closure


def _byos_file_hash(path: Path | None) -> str:
    """SHA-256 of a BYOS user script, or a sentinel when unset/missing.

    Issue #1011. The returned string is mixed into the cache key for
    ``APPLY_PARAMETERS`` and ``EXTRACT_KPIS`` so that editing the
    user-supplied script invalidates the cached results. Three outcomes:

    * ``path is None`` → ``"byos-unset"``. No BYOS script configured;
      the cache key falls back to ``code_hashes["bin"]`` unchanged.
    * ``path.resolve().is_file() is False`` → ``"byos-missing"``. The
      configured script is unreadable; do not raise — return a stable
      sentinel so the cache key remains deterministic (issue #1011
      stop condition).
    * otherwise → ``sha256_of_files([path.resolve()])`` of the file
      bytes, using the same hashing primitive as ``compute_code_hashes``
      so the rest of the cache key is consistent.
    """
    if path is None:
        return "byos-unset"
    try:
        resolved = path.resolve()
    except OSError:
        return "byos-missing"
    if not resolved.is_file():
        return "byos-missing"
    return sha256_of_files([resolved])


def _combine_code_hash(*hashes: str) -> str:
    """SHA-256 of the concatenation of multiple code-hash strings.

    Used by ``code_hash_with_byos`` (issue #1011) to fold
    the BYOS user-script hash into the existing ``code_hashes["bin"]``
    without changing the schema of :class:`CacheKey.code_sha256`. Any
    change in any input produces a different output, so editing the
    user script invalidates the cache key.
    """
    h = hashlib.sha256()
    for part in hashes:
        h.update(part.encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def _plugin_distribution_version(module_name: str) -> str:
    """Best-effort installed-distribution version for an algorithm module.

    Maps the module's top-level package to its distribution via
    ``importlib.metadata.packages_distributions()`` and returns the
    distribution version. Returns ``"unknown"`` on any failure — this
    only feeds the *fallback* algorithm digest, so it must never raise
    (issue #1636: never crash the campaign over hash computation).
    """
    try:
        top_level = module_name.split(".", maxsplit=1)[0]
        dists = packages_distributions().get(top_level, [])
        if not dists:
            return "unknown"
        return version(dists[0])
    except Exception:  # noqa: BLE001 — metadata lookup is best-effort only
        return "unknown"


def _algorithm_fallback_digest(cls: type, reason: str) -> str:
    """Stable identity digest when the implementation source is unreadable.

    Used when ``inspect.getfile(type(algo))`` fails (C extension,
    ``__main__``-defined class, unimportable module) or the resolved
    file is missing (zipimport). Hashes the qualified class name, the
    module name, and — when discoverable — the plugin distribution
    version. A same-identity plugin upgrade that keeps the version
    unchanged will NOT invalidate the cache key through this path; a
    warning is logged so operators can see the downgrade in coverage.
    """
    identity = f"{cls.__module__}.{getattr(cls, '__qualname__', cls.__name__)}"
    digest_input = f"algo-fallback|{identity}|{_plugin_distribution_version(cls.__module__)}"
    log.warning(
        "cannot hash algorithm implementation source for %s (%s); "
        "falling back to a name/distribution-version digest — same-identity "
        "plugin code changes will NOT invalidate the GENERATE_SAMPLES "
        "cache key",
        identity,
        reason,
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def _algorithm_package_scope(module_file: Path, module_name: object) -> Path | None:
    """Distribution/package root directory for an algorithm module.

    Resolves the module's dotted ``__name__`` against its on-disk
    location: for ``pkg.sub.algo`` implemented in
    ``<root>/pkg/sub/algo.py`` the root is ``<root>/pkg``. The
    directory names walking up from the module file must spell the
    dotted package chain — a module-name/path mismatch (a module
    registered under an unrelated name) would otherwise scope the hash
    at an arbitrary ancestor such as ``site-packages`` itself, so the
    mismatch returns ``None`` (caller falls back to a bounded scope).

    Returns ``None`` for a top-level plain module (``foo.py``, no
    package) — its hash scope is the file itself.
    """
    if not isinstance(module_name, str) or not module_name:
        return None
    parts = module_name.split(".")
    chain = parts if module_file.name == "__init__.py" else parts[:-1]
    if not chain:
        return None
    # The innermost directory must match the innermost package name.
    if module_file.parent.name != chain[-1]:
        return None
    # Every directory above must spell the remaining dotted prefixes.
    for i in range(1, len(chain)):
        try:
            if module_file.parents[i].name != chain[len(chain) - 1 - i]:
                return None
        except IndexError:
            return None
    return module_file.parents[len(chain) - 1]


def _algorithm_implementation_files(cls: type, module_file: Path) -> list[Path]:
    """Source-file set covering one algorithm implementation (issue #1636).

    Scope rules, most specific first:

    * the module resolves to a package/distribution root outside
      ``osimflow`` → every ``*.py`` under that root (recursively). A
      whole third-party distribution is small and self-contained, and
      over-approximation is safe for cache invalidation (the same
      stance as ``_transitive_import_closure``);
    * the module resolves *into* the ``osimflow`` package itself (the
      built-in algorithms) → every ``*.py`` under the algorithm
      module's own package directory, so unrelated ``osimflow`` edits
      do not invalidate GENERATE keys;
    * the module is a package ``__init__`` whose dotted name does not
      match its path → the package tree of ``module_file.parent``
      (bounded — it is a real package directory);
    * anything else (top-level module, name/path mismatch) → the
      module file alone.

    The set is sorted and deduplicated so the hash is deterministic.
    """
    package_root = Path(__file__).resolve().parent
    scope = _algorithm_package_scope(module_file, getattr(cls, "__module__", None))
    if scope is not None and (scope == package_root or package_root in scope.parents):
        # Built-in algorithm: cap the scope at the algorithm's own
        # package directory instead of hashing the whole osimflow tree.
        scope = module_file.parent
    if scope is None:
        if module_file.name == "__init__.py":
            candidates: Iterator[Path] = module_file.parent.rglob("*.py")
            return sorted({p.resolve() for p in candidates if p.is_file()})
        return [module_file]
    files = scope.rglob("*.py")
    return sorted({p.resolve() for p in files if p.is_file()})


def _algorithm_code_digest(algo: BaseAlgorithm) -> str:
    """SHA-256 over the resolved algorithm implementation's source files.

    Issue #1636: third-party algorithm plug-ins discovered via the
    ``osimflow.algorithms`` entry-point group live outside the
    ``osimflow`` package, so ``_transitive_import_closure`` (kept
    inside ``package_root``) never sees their code and the
    ``GENERATE_<ALGO>_SAMPLES`` cache key silently replayed samples
    from an upgraded-but-same-named plug-in. This digest starts at
    ``inspect.getfile(type(algo))`` and hashes the implementation's
    package/distribution file set; it is mixed into the step's
    ``code_sha256`` by ``Campaign.step_generate_samples``.

    Never raises: when the implementation source cannot be located or
    read (C extension, ``__main__``, zipimport), falls back to
    ``_algorithm_fallback_digest`` with a warning.
    """
    cls = type(algo)
    try:
        module_file = Path(inspect.getfile(cls)).resolve()
    except (TypeError, OSError) as exc:
        return _algorithm_fallback_digest(cls, f"inspect.getfile failed: {exc}")
    if not module_file.is_file():
        return _algorithm_fallback_digest(cls, f"no source file at {module_file}")
    files = _algorithm_implementation_files(cls, module_file)
    if not files:
        return _algorithm_fallback_digest(cls, f"no .py files beside {module_file}")
    return sha256_of_files(files)


def compute_code_hashes(
    campaign: Any,
    cfg: CampaignConfig | None = None,
) -> dict[str, str]:
    """SHA-256 of every work script, plus the work.py module.

    The work scripts live in ``osimflow._work_scripts`` (shipped with
    the wheel). A development checkout (``pip install -e .``) also has
    copies in ``bin/`` (backward-compatible shims). We hash the UNION
    of both directories whenever either exists — sorted, deduped —
    so dev checkouts and wheel installs agree on the cache key.
    Fixes issue #1021.

    The union is extended with the transitive osimflow-internal
    import closure of ``osimflow.work``, ``osimflow.apply_params``,
    and every ``osimflow._work_scripts`` module (issue #1446), so
    edits to indirectly imported modules (e.g.
    ``algorithms.doe_analysis``, ``version_detection``) invalidate
    the affected cache keys instead of silently serving stale
    results.

    The work.py module is included because it is the work layer that
    the Campaign itself depends on; if a contributor edits it, we
    must re-run downstream steps.

    When ``cfg`` is provided, also include the resolved file-content
    hashes of ``cfg.custom_apply_script`` and ``cfg.custom_kpi_extractor``
    under the ``byos_apply`` and ``byos_kpi`` keys (issue #1011).
    These are mixed into the per-sample cache key for ``APPLY_PARAMETERS``
    and ``EXTRACT_KPIS`` respectively, so editing a BYOS user script
    invalidates the cached results. When ``cfg`` is omitted (the
    legacy ``Campaign._compute_code_hashes(_Stub())`` test path),
    the byos entries fall back to the ``"byos-unset"`` sentinel and
    ``code_hashes["bin"]`` continues to be the cache-key hash.

    ``campaign`` is the delegating Campaign (or a test stub); the
    function only reads ``getattr(campaign, "cfg", None)`` as a
    fallback when ``cfg`` is ``None``, mirroring the historical
    method behaviour so the unbound test path keeps working.
    """
    from . import work  # noqa: PLC0415

    # Resolve both work-script directories and take the union
    # (sorted, deduped) whenever either exists.  NOTE: this module
    # lives directly inside the ``osimflow`` package, so ``.parent``
    # is the package root — the same directory ``campaign.py``'s
    # ``Path(__file__).resolve().parent`` resolved to before the
    # extraction (issue #1462).
    package_root = Path(__file__).resolve().parent
    repo_root = package_root.parent
    candidates: list[Path] = []
    for d in (package_root / "_work_scripts", repo_root / "bin"):
        if d.is_dir():
            candidates.extend(d.glob("*.py"))
    work_file = Path(inspect.getfile(work))
    # Also fold in the work-layer modules so editing them invalidates
    # per-sample cache entries (issue #1022). Without this addition,
    # the per-sample steps used ``bin = _work_scripts/*.py + bin/*.py``
    # only, and editing ``osimflow.work`` or ``osimflow.apply_params``
    # silently kept cached results warm — wrong. The ``work`` hash
    # below still covers ``work.py`` separately for ``AGGREGATE_RESULTS``
    # because aggregate re-runs don't depend on the per-sample work
    # scripts (the docstring after #1036 spells out the two-hash scheme).
    try:
        apply_params_file = Path(inspect.getfile(sys.modules["osimflow"].apply_params))
    except (AttributeError, KeyError):
        apply_params_file = None
    for f in (work_file, apply_params_file):
        if f is not None and f.is_file():
            candidates.append(f)
    # Transitive osimflow-internal import closure of the work layer
    # (issue #1446): the hashed modules import further osimflow
    # modules (e.g. generate_plots.py → algorithms.doe_analysis,
    # work.py → version_detection / weather / storage / ...) whose
    # edits change per-step behaviour without touching any explicitly
    # listed file. Deriving the file set from the import closure
    # removes the hand-maintained-list drift permanently.
    candidates.extend(_transitive_import_closure(package_root))
    files = sorted(set(candidates), key=str)
    # Pick the effective cfg. When ``__init__`` calls this with its
    # cfg we get the BYOS entries; when tests call it with no cfg
    # (e.g. ``Campaign._compute_code_hashes(_Stub())``) we fall back
    # to ``campaign.cfg`` if a stub happens to carry one, then to None.
    effective_cfg = cfg if cfg is not None else getattr(campaign, "cfg", None)
    byos_apply_path = effective_cfg.custom_apply_script if effective_cfg is not None else None
    byos_kpi_path = effective_cfg.custom_kpi_extractor if effective_cfg is not None else None
    return {
        "bin": sha256_of_files(files),
        "work": sha256_of_files([work_file]),
        "byos_apply": _byos_file_hash(byos_apply_path),
        "byos_kpi": _byos_file_hash(byos_kpi_path),
    }


def code_hash_with_byos(code_hashes: dict[str, str], byos_key: str) -> str:
    """Cache-key ``code_sha256`` optionally mixed with a BYOS hash.

    When ``code_hashes[byos_key]`` is the ``"byos-unset"`` sentinel
    (no user script configured), returns ``code_hashes["bin"]``
    unchanged so existing cached entries continue to hit after
    upgrading — no impact when BYOS is not configured (issue #1011
    acceptance criterion).

    When the user script is configured, returns the SHA-256 of the
    concatenation ``bin|byos`` so any edit to the user script
    produces a distinct cache key and forces the affected per-sample
    step to re-run.
    """
    base = code_hashes["bin"]
    byos_hash = code_hashes.get(byos_key, "byos-unset")
    if byos_hash == "byos-unset":
        return base
    return _combine_code_hash(base, byos_hash)
