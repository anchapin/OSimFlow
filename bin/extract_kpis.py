#!/usr/bin/env python3
"""Backward-compatible wrapper — delegates to osimflow._work_scripts.extract_kpis."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import osimflow._work_scripts.extract_kpis as _mod

# Re-export all public and private names for backward compatibility.
_sys_mod = sys.modules[__name__]
for _attr in dir(_mod):
    if not _attr.startswith("__"):
        setattr(_sys_mod, _attr, getattr(_mod, _attr))

sys.modules[__name__] = _sys_mod

if __name__ == "__main__":
    sys.exit(_mod.main())
