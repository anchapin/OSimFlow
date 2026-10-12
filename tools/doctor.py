"""``make doctor``: check the dev environment and print the next step (issue #1895)."""

import importlib.util
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MODULES = ("pytest", "ruff", "mypy", "pre_commit")
EXTRA_MODULES = {
    "aws": "boto3",
    "azure": "azure.batch",
    "slurm": "submitit",
    "kubernetes": "kubernetes",
    "api": "fastapi",
    "sensitivity": "SALib",
    "optimization": "pymoo",
    "ga": "deap",
}


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def run_checks(venv_dir: Path = REPO_ROOT / ".venv") -> tuple[list[str], list[str], str]:
    """Return (ok lines, warning lines, next step)."""
    ok: list[str] = []
    warn: list[str] = []
    next_step = "make smoke"

    if (venv_dir / "bin" / "python").exists() or (venv_dir / "Scripts" / "python.exe").exists():
        ok.append(f"virtualenv: {venv_dir}")
    else:
        return ok, [f"virtualenv missing: {venv_dir}"], "make install"

    ok.append(f"python: {sys.version.split()[0]}")

    if _has_module("osimflow"):
        ok.append("osimflow importable (editable install)")
    else:
        warn.append("osimflow not installed")
        next_step = "make install"

    missing_dev = [m for m in REQUIRED_MODULES if not _has_module(m)]
    if missing_dev:
        warn.append(f"dev tools missing: {', '.join(missing_dev)}")
        next_step = "make install"
    else:
        ok.append("dev tools: pytest, ruff, mypy, pre-commit")

    missing_extras = [e for e, m in EXTRA_MODULES.items() if not _has_module(m)]
    if missing_extras:
        warn.append(f"extras not installed: {', '.join(missing_extras)}")
        next_step = "make install"
    else:
        ok.append("extras: " + ", ".join(EXTRA_MODULES))

    if shutil.which("openstudio") or shutil.which("openstudio.cli"):
        ok.append("OpenStudio CLI found")
    else:
        warn.append("OpenStudio CLI not on PATH (stub mode only; `make smoke` still works)")

    if shutil.which("uv"):
        ok.append("uv found (`make install-locked` mirrors CI)")
    else:
        warn.append("uv not found (optional; only needed for `make install-locked`)")
    return ok, warn, next_step


def main() -> int:
    ok, warn, next_step = run_checks()
    for line in ok:
        print(f"[ok]   {line}")
    for line in warn:
        print(f"[warn] {line}")
    print(f"Next step: {next_step}")
    return 1 if any("missing" in w or "not installed" in w for w in warn) else 0


if __name__ == "__main__":
    sys.exit(main())
