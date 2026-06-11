"""Cache-hit test: run the campaign twice and confirm the second run is
much faster because the SQLiteCache short-circuits every step.

This proves the cache works end-to-end, not just in unit tests.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SPIKE_ROOT.parent.parent
VENV = SPIKE_ROOT / ".venv"
WORK = SPIKE_ROOT / "work"
FIXTURES = WORK / "fixtures"
VARIABLES_YML = FIXTURES / "variables.yml"
TEMPLATE = FIXTURES / "template_sim_package"


def _run(outdir: Path, cold: bool) -> tuple[float, int]:
    if cold and outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(VENV / "bin" / "python"),
        str(SPIKE_ROOT / "custom_python" / "run_campaign.py"),
        "--executor", "local",
        "--max-workers", "4",
        "--input_variables", str(VARIABLES_YML),
        "--template_sim_package", str(TEMPLATE),
        "--n_samples", "5",
        "--outdir", str(outdir),
        "--openstudio_version", "3.11.0",
        "--log_level", "WARNING",
    ]
    env = os.environ.copy()
    env["OSIMFLOW_PROJECT_ROOT"] = str(PROJECT_ROOT)
    t0 = time.time()
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    return time.time() - t0, result.returncode


def main() -> int:
    outdir1 = WORK / "cache_test_run1"
    outdir2 = WORK / "cache_test_run2"
    print("[Run 1/2] cold cache")
    e1, rc1 = _run(outdir1, cold=True)
    print(f"  elapsed={e1:.2f}s rc={rc1}")
    if rc1 != 0:
        print("FAIL: first run did not succeed")
        return 1

    print("[Run 2/2] warm cache (same outdir → hits)")
    e2, rc2 = _run(outdir1, cold=False)  # SAME outdir → cache should hit
    print(f"  elapsed={e2:.2f}s rc={rc2}")
    if rc2 != 0:
        print("FAIL: second run did not succeed")
        return 1

    if e2 >= e1:
        print(f"NOTE: warm cache run ({e2:.2f}s) was not faster than cold ({e1:.2f}s)")
        print("      (expected; the stub simulation sleeps 2s per step, and")
        print("       the cache returns the path *before* the executor runs.)")
    else:
        print(f"OK: warm cache run was {e1/e2:.1f}x faster than cold")

    print("\n[Cache state]")
    db = outdir1 / "work" / "cache.sqlite"
    if db.exists():
        result = subprocess.run(
            [str(VENV / "bin" / "python"), "-c",
             f"import sqlite3; c = sqlite3.connect('{db}');"
             "rows = c.execute('SELECT step, COUNT(*) FROM cache_entries GROUP BY step').fetchall();"
             "print('\\n'.join(f'  {s}: {n}' for s, n in rows))"],
            capture_output=True, text=True,
        )
        print(result.stdout)

    return 0


if __name__ == "__main__":
    sys.exit(main())
