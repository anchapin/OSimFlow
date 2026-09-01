"""Contract test: ``make install`` must bootstrap ``.venv`` (issue #1447).

AGENTS.md §2 and docs/DEVELOPMENT.md §1 both promise that ``make install``
creates ``.venv/`` on a fresh clone. This test statically pins that promise
so the Makefile cannot silently regress to failing with
``env: .venv/bin/python: No such file or directory``.

Two fast, hermetic checks (no pip install, no network):
1. Static: the ``install`` target declares an order-only prerequisite on
   ``$(VENV)`` (``.venv``) and a ``python3 -m venv`` bootstrap recipe exists.
2. Dry-run: ``make -n install`` in a scratch dir without ``.venv`` prints
   the bootstrap recipes *before* the ``pip install -e`` line, proving the
   ordering make will actually use.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"


def _read_makefile() -> str:
    assert MAKEFILE.is_file(), f"Makefile missing at repo root: {MAKEFILE}"
    return MAKEFILE.read_text(encoding="utf-8")


@pytest.mark.contract
def test_install_target_has_venv_order_only_prerequisite() -> None:
    """The ``install`` target must take ``$(VENV)``/``.venv`` as an
    order-only prerequisite so the venv is created when absent but never
    rebuilt when it already exists.
    """
    text = _read_makefile()
    install_rule = re.search(r"^install:[^\n]*$", text, flags=re.MULTILINE)
    assert install_rule is not None, "No 'install:' target found in Makefile"
    prereqs = install_rule.group(0)
    assert re.search(r"\|\s*\$\(VENV\)|\|\s*\.venv", prereqs), (
        f"'install' target lacks an order-only venv prerequisite: {prereqs!r}"
    )


@pytest.mark.contract
def test_makefile_has_venv_bootstrap_recipe() -> None:
    """A ``python3 -m venv`` recipe targeting ``$(VENV)``/``.venv`` must
    exist in the Makefile.
    """
    text = _read_makefile()
    assert re.search(r"^\$\((?:VENV)\):\s*$", text, flags=re.MULTILINE) or re.search(
        r"^\.venv:\s*$", text, flags=re.MULTILINE
    ), "No '$(VENV):' / '.venv:' target rule found in Makefile"
    assert re.search(r"python3 -m venv (\$\(VENV\)|\.venv)", text), (
        "No 'python3 -m venv' bootstrap recipe found in Makefile"
    )


@pytest.mark.contract
def test_dry_run_orders_venv_bootstrap_before_pip_install(tmp_path: Path) -> None:
    """``make -n install`` in a scratch dir (no ``.venv``) must print the
    venv-bootstrap recipes before the ``pip install -e`` recipe — this is
    the exact ordering a fresh clone will experience.
    """
    if shutil.which("make") is None:  # pragma: no cover - CI always has make
        pytest.skip("make not available")

    makefile = tmp_path / "Makefile"
    makefile.write_text(_read_makefile(), encoding="utf-8")

    result = subprocess.run(
        ["make", "-f", str(makefile), "-n", "install"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=False,
    )
    assert result.returncode == 0, (
        f"make -n install failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    joined = "\n".join(lines)

    venv_idx = next(
        (i for i, ln in enumerate(lines) if re.search(r"python3 -m venv \.venv$", ln)),
        None,
    )
    pip_idx = next((i for i, ln in enumerate(lines) if "pip install -e" in ln), None)

    assert venv_idx is not None, f"Dry run never bootstraps the venv. Output:\n{joined}"
    assert pip_idx is not None, f"Dry run never runs the pip install recipe. Output:\n{joined}"
    assert ".venv/bin/python -m pip install --upgrade pip" in joined, (
        f"Dry run does not upgrade pip inside the fresh venv. Output:\n{joined}"
    )
    assert venv_idx < pip_idx, (
        f"Venv bootstrap recipes must run before the pip install recipe.\nOutput:\n{joined}"
    )
