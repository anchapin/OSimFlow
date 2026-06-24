"""Automatic measure dependency resolution.

Scans measure directories for Ruby (``.rb``) and Python (``.py``) files,
extracts ``require`` / ``import`` statements, checks availability, and
optionally auto-installs missing packages via ``gem install`` / ``pip install``.

This allows OSimFlow to fail fast with a clear error message when a
measure's dependencies are not installed, rather than producing obscure
runtime errors during simulation.
"""

import importlib.util
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.measure_resolver")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MeasureDependencyError(Exception):
    """Raised when measure dependencies cannot be resolved."""


class MissingRubyGem(MeasureDependencyError):
    """A required Ruby gem is not installed."""

    def __init__(self, gem_name: str, measure_name: str) -> None:
        self.gem_name = gem_name
        self.measure_name = measure_name
        super().__init__(
            f"Ruby gem {gem_name!r} is required by measure {measure_name!r} "
            f"but is not installed. Install it with: gem install {gem_name}"
        )


class MissingPythonPackage(MeasureDependencyError):
    """A required Python package is not installed."""

    def __init__(self, package_name: str, measure_name: str) -> None:
        self.package_name = package_name
        self.measure_name = measure_name
        super().__init__(
            f"Python package {package_name!r} is required by measure {measure_name!r} "
            f"but is not installed. Install it with: pip install {package_name}"
        )


# ---------------------------------------------------------------------------
# Dependency scanning
# ---------------------------------------------------------------------------

# Regex patterns for extracting dependencies
_RUBY_REQUIRE_RE = re.compile(r"^\s*require\s+['\"]([^'\"]+)['\"]", re.MULTILINE)
_PYTHON_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([^\s;]+)", re.MULTILINE)


def _scan_ruby_file(file_path: Path) -> set[str]:
    """Extract ``require`` statements from a Ruby file.

    Handles both ``require 'gem_name'`` and ``require "gem_name"`` forms.
    Skips lines that are comments or that use dynamic requires.
    """
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()

    deps: set[str] = set()
    for match in _RUBY_REQUIRE_RE.finditer(content):
        module_name = match.group(1)
        # Skip stdlib and bundler-only gems that are always available
        if module_name in ("rubygems", "bundler", "rake"):
            continue
        deps.add(module_name)
    return deps


def _scan_python_file(file_path: Path) -> set[str]:
    """Extract ``import`` and ``from ... import`` statements from a Python file.

    Skips relative imports (leading dot), builtins, and common third-party
    packages that are always available in the OpenStudio Python environment.
    """
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()

    skip_modules = {
        "os",
        "sys",
        "re",
        "time",
        "datetime",
        "json",
        "yaml",
        "pathlib",
        "typing",
        "logging",
        "subprocess",
        "shutil",
        "tempfile",
        "collections",
        "functools",
        "itertools",
        "operator",
        "string",
        "math",
        "random",
        "uuid",
        "hashlib",
        "io",
        "csv",
        "xml",
        "html",
        "urllib",
        "socket",
        "threading",
        "multiprocessing",
        "concurrent",
        "traceback",
        "gc",
        "weakref",
        "copy",
        "pprint",
        "textwrap",
        "locale",
        "gettext",
        "ast",
        "dis",
        "inspect",
        "types",
        "warnings",
        "platform",
        "errno",
        "ctypes",
        "struct",
        "array",
        "binascii",
        "zlib",
        "gzip",
        "zipfile",
        "tarfile",
        "configparser",
        "fileinput",
        "linecache",
        "tokenize",
        "keyword",
        "symtable",
        " GargbageCollection",  # noqa: N816
        "openstudio",
        "openstudio_analysis",
    }

    deps: set[str] = set()
    for match in _PYTHON_IMPORT_RE.finditer(content):
        module_name = match.group(1)
        # Skip relative imports
        if module_name.startswith("."):
            continue
        # Normalize: 'os.path' -> 'os', 'typing.Optional' -> 'typing'
        base_module = module_name.split(".")[0].split(" as ")[0].strip()
        if base_module in skip_modules or base_module in {"pip", "setuptools", "wheel"}:
            continue
        deps.add(base_module)
    return deps


def scan_measure_directory(measure_dir: Path) -> dict[str, Any]:
    """Scan a measure directory for Ruby and Python dependency requirements.

    Recursively scans all ``.rb`` and ``.py`` files under *measure_dir* and
    collects the set of required Ruby gems and Python packages.

    Parameters
    ----------
    measure_dir
        Path to a measure directory (containing ``measure.rb`` or
        ``measure.py`` and any helper modules).

    Returns
    -------
    dict
        A dict with keys ``"ruby"`` and ``"python"``, each containing a
        set of required module/package names.

    Examples
    --------
    >>> info = scan_measure_directory(Path("measures/SetWindowUValue"))
    >>> info["ruby"]
    {'json'}  # if measure.rb requires 'json'
    >>> info["python"]
    {'numpy', 'pandas'}  # if measure.py imports numpy, pandas
    """
    measure_dir = Path(measure_dir)
    ruby_deps: set[str] = set()
    python_deps: set[str] = set()

    for rb_file in measure_dir.rglob("*.rb"):
        ruby_deps.update(_scan_ruby_file(rb_file))

    for py_file in measure_dir.rglob("*.py"):
        python_deps.update(_scan_python_file(py_file))

    return {"ruby": ruby_deps, "python": python_deps}


# ---------------------------------------------------------------------------
# Availability checking
# ---------------------------------------------------------------------------


def _check_ruby_gem(gem_name: str) -> bool:
    """Check whether a Ruby gem is installed and loadable."""
    try:
        result = subprocess.run(
            ["ruby", "-e", f"require '{gem_name}'"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _check_python_package(package_name: str) -> bool:
    """Check whether a Python package is importable."""
    # Try importlib first (fastest)
    spec = importlib.util.find_spec(package_name)
    if spec is not None:
        return True
    # Fallback: pip show
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", package_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def check_dependencies(
    ruby_deps: set[str],
    python_deps: set[str],
) -> tuple[list[str], list[str]]:
    """Check which of the requested dependencies are missing.

    Parameters
    ----------
    ruby_deps
        Set of Ruby gem names to check.
    python_deps
        Set of Python package names to check.

    Returns
    -------
    tuple[list[str], list[str]]
        A two-element tuple ``(missing_ruby, missing_python)`` where each
        element is a list of package names that are not available.
    """

    missing_ruby: list[str] = []
    for gem in sorted(ruby_deps):
        if not _check_ruby_gem(gem):
            missing_ruby.append(gem)

    missing_python: list[str] = []
    for pkg in sorted(python_deps):
        if not _check_python_package(pkg):
            missing_python.append(pkg)

    return missing_ruby, missing_python


# ---------------------------------------------------------------------------
# Auto-installation
# ---------------------------------------------------------------------------


def _gem_install(gem_name: str) -> bool:
    """Attempt to install a Ruby gem via ``gem install``."""
    try:
        result = subprocess.run(
            ["gem", "install", gem_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _pip_install(package_name: str) -> bool:
    """Attempt to install a Python package via ``pip install``."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_measure_dependencies(
    measure_dir: Path,
    *,
    auto_install: bool = False,
) -> dict[str, Any]:
    """Resolve dependencies for a measure directory.

    Scans the measure for Ruby and Python dependency requirements,
    checks availability, and optionally auto-installs missing packages.

    Parameters
    ----------
    measure_dir
        Path to the measure directory to scan.
    auto_install
        When ``True``, attempt to install missing packages via
        ``gem install`` / ``pip install``.  When ``False`` (default),
        only checks availability and raises if any packages are missing.

    Returns
    -------
    dict
        A dict with keys:
        - ``measure_name``: the directory name (used in error messages)
        - ``ruby_deps``: set of required Ruby gem names
        - ``python_deps``: set of required Python package names
        - ``missing_ruby``: list of missing Ruby gem names
        - ``missing_python``: list of missing Python package names
        - ``installed``: list of package names that were auto-installed

    Raises
    ------
    MissingRubyGem
        If a required Ruby gem is not installed and *auto_install* is
        ``False`` (or the auto-install attempt failed).
    MissingPythonPackage
        If a required Python package is not installed and
        *auto_install* is ``False`` (or the auto-install attempt failed).

    Examples
    --------
    >>> info = resolve_measure_dependencies(Path("measures/MyMeasure"))
    >>> if info["missing_ruby"] or info["missing_python"]:
    ...     print("Missing deps:", info["missing_ruby"], info["missing_python"])
    """

    measure_dir = Path(measure_dir)
    measure_name = measure_dir.name

    scanned = scan_measure_directory(measure_dir)
    ruby_deps = scanned["ruby"]
    python_deps = scanned["python"]

    log.info(
        "resolve_dependencies: measure=%s ruby_deps=%s python_deps=%s",
        measure_name,
        sorted(ruby_deps),
        sorted(python_deps),
    )

    missing_ruby, missing_python = check_dependencies(ruby_deps, python_deps)

    installed: list[str] = []

    if auto_install:
        for gem in missing_ruby[:]:
            log.info("auto-installing Ruby gem: %s", gem)
            if _gem_install(gem):
                missing_ruby.remove(gem)
                installed.append(f"ruby:{gem}")
                log.info("Ruby gem %s installed successfully", gem)
            else:
                log.warning("Ruby gem %s auto-install failed", gem)

        for pkg in missing_python[:]:
            log.info("auto-installing Python package: %s", pkg)
            if _pip_install(pkg):
                missing_python.remove(pkg)
                installed.append(f"python:{pkg}")
                log.info("Python package %s installed successfully", pkg)
            else:
                log.warning("Python package %s auto-install failed", pkg)

    # Re-check after auto-install
    if not auto_install or installed:
        missing_ruby, missing_python = check_dependencies(ruby_deps, python_deps)

    result: dict[str, Any] = {
        "measure_name": measure_name,
        "ruby_deps": ruby_deps,
        "python_deps": python_deps,
        "missing_ruby": missing_ruby,
        "missing_python": missing_python,
        "installed": installed,
    }

    if missing_ruby:
        raise MissingRubyGem(missing_ruby[0], measure_name)

    if missing_python:
        raise MissingPythonPackage(missing_python[0], measure_name)

    return result


def resolve_sim_package_dependencies(
    sim_package: Path,
    *,
    auto_install: bool = False,
) -> list[dict[str, Any]]:
    """Resolve dependencies for all measures in a simulation package.

    Scans the ``measures/`` subdirectory of *sim_package* and resolves
    dependencies for each measure found therein.

    Parameters
    ----------
    sim_package
        Path to a simulation package directory containing a ``measures/``
        subdirectory.
    auto_install
        Passed through to :func:`resolve_measure_dependencies`.

    Returns
    -------
    list[dict[str, Any]]
        A list of result dicts (one per measure directory), in the same
        format as :func:`resolve_measure_dependencies` returns.

    Raises
    ------
    MissingRubyGem
    MissingPythonPackage
        As raised by :func:`resolve_measure_dependencies`.
    """
    sim_package = Path(sim_package)
    measures_dir = sim_package / "measures"

    if not measures_dir.is_dir():
        log.info(
            "resolve_sim_package_dependencies: no measures/ directory in %s",
            sim_package,
        )
        return []

    results: list[dict[str, Any]] = []
    for measure_dir in sorted(measures_dir.iterdir()):
        if measure_dir.is_dir():
            result = resolve_measure_dependencies(measure_dir, auto_install=auto_install)
            results.append(result)

    return results
