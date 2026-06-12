# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for OSimFlow CLI.

Builds:
  - Linux/macOS: onedir (folder bundle for .deb/.rpm/.AppImage/.dmg wrapping)
  - Windows:     onefile (single .exe)

Usage:
    pyinstaller packaging/osimflow.spec

Size budget: ≤ 220 MB unpacked (onedir) or onefile on disk.
See docs/installation.md for per-platform install instructions.
"""

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared Analysis block
# ---------------------------------------------------------------------------

block_cipher = None

# Comprehensive excludes list — these modules are either:
# - Lazy-imported optional extras (boto3, submitit, mlflow, SALib, pymoo)
# - Qt bindings pulled in by matplotlib's qt_compat hook but unused (agg-only)
# - Test suites shipped inside scientific packages (30-50 MB savings)
# - Unused stdlib / development tools
EXCLUDES = [
    # Qt — OSimFlow uses agg backend only (headless plots)
    "PySide6",
    "PyQt5",
    "PyQt6",
    "PyQt4",
    "pyqtgraph",
    # Matplotlib unused sub-packages
    "matplotlib.tests",
    "matplotlib.sphinxext",
    "matplotlib.style",
    "mpl_toolkits",
    # Scientific stack test suites (30-50 MB savings)
    "numpy.tests",
    "numpy._core.tests",
    "numpy.distutils.tests",
    "scipy.tests",
    "pandas.tests",
    "pyarrow.tests",
    # Unused scipy sub-modules (5-15 MB savings)
    "scipy.io",
    "scipy.odr",
    "scipy.cluster",
    "scipy.fftpack",
    "scipy.datasets",
    "scipy.constants",
    # Lazy-imported optional extras (not needed at bundle time)
    "boto3",
    "botocore",
    "submitit",
    "mlflow",
    "SALib",
    "pymoo",
    "openstudio",
    # Development / CI tools (never needed at runtime)
    "pytest",
    "moto",
    "ruff",
    "mypy",
    "pre_commit",
    "coverage",
    "setuptools",
    "pip",
    "wheel",
    "build",
    "twine",
    # FastAPI / uvicorn (optional [api] extra, lazy)
    "fastapi",
    "uvicorn",
    "starlette",
    "sse_starlette",
    # TUI / viz (optional extras, lazy)
    "streamlit",
    "rich",
    # Misc unused
    "tkinter",
    "unittest",
    "xmlrpc",
    "pydoc",
    "doctest",
    "difflib",
    "inspect",  # keep — needed by Campaign for BYOS
    # ^ Actually inspect IS needed. Remove it.
    "curses",
    "pdb",
    "profile",
    "pstats",
    "lib2to3",
    "py_compile",
    "compileall",
]

# Remove 'inspect' from excludes — it's needed for BYOS function discovery
EXCLUDES = [m for m in EXCLUDES if m != "inspect"]

a = Analysis(
    [str(Path("osimflow/__main__.py"))],
    pathex=[],
    binaries=[],
    datas=[
        # Bundle the work scripts so they're available at runtime.
        ("osimflow/_work_scripts", "osimflow/_work_scripts"),
    ],
    hiddenimports=[
        "osimflow",
        "osimflow._work_scripts",
        "osimflow._work_scripts.generate_lhs",
        "osimflow._work_scripts.apply_params_to_model",
        "osimflow._work_scripts.extract_kpis",
        "osimflow._work_scripts.aggregate_results",
        "osimflow._work_scripts.generate_plots",
        "osimflow.campaign",
        "osimflow.config",
        "osimflow.work",
        "osimflow.cache",
        "osimflow.monitoring",
        "osimflow.executors",
        "osimflow.algorithms",
        "osimflow.algorithms.lhs",
        "osimflow.weather",
        # Scientific stack — ensure PyInstaller picks them up
        "numpy",
        "scipy",
        "scipy.stats",
        "scipy.stats.qmc",
        "pandas",
        "pyarrow",
        "matplotlib",
        "seaborn",
        "yaml",
        "tqdm",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# Platform-specific bundling
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    # Windows: onefile (single .exe)
    pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="osimflow",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    # Linux + macOS: onedir (folder bundle)
    pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="osimflow",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="osimflow",
    )
