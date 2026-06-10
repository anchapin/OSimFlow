"""Canonical BYOS (Bring Your Own Script) loader.

This is the single entry point for loading user-supplied Python scripts.
The CLI (``__main__.py``) and the Campaign (``campaign.py``) both use
``load_user_function`` to discover the callable in a user's ``.py`` file.

The function-name convention is:

* ``apply_parameters`` — for the parameter-application override.
* ``extract_kpis`` — for the KPI-extraction override.

See AGENTS.md §9 *Task routing hints* and ``user_scripts/README.md``
for the full BYOS contract.
"""

import importlib.util
import logging
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

log = logging.getLogger("osimflow.byos")

# The canonical function names a BYOS script must expose.  The first
# match wins.  Order matters: ``apply_parameters`` is the primary name
# documented in AGENTS.md; ``apply`` is kept as a fallback for
# backwards-compatibility with scripts written against the old
# ``osimflow.apply_params._load_custom_apply`` loader (removed in the
# fix for issue #36).
_CANDIDATE_NAMES = ("apply_parameters", "extract_kpis", "apply")


def load_user_function(path: Path) -> Callable[..., Any]:
    """Import a user ``.py`` file and return the first matching callable.

    Searches for functions named ``apply_parameters``, ``extract_kpis``,
    or ``apply`` (legacy).  The function signature is the entire contract —
    no separate CLI surface to maintain.

    Raises:
        ImportError: the file cannot be loaded as a Python module.
        AttributeError: no callable with a recognised name was found.
    """
    spec = importlib.util.spec_from_file_location(f"user_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for candidate in _CANDIDATE_NAMES:
        candidate_obj = getattr(mod, candidate, None)
        if callable(candidate_obj):
            if candidate == "apply":
                warnings.warn(
                    f"User script {path} uses the deprecated function name "
                    f"'apply'. Rename it to 'apply_parameters' for forward "
                    f"compatibility. Support for 'apply' will be removed in a "
                    f"future release.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            # Cast: the BYOS contract is validated at call time via
            # ``inspect.signature``; mypy cannot prove the module attr type.
            return cast(Callable[..., Any], candidate_obj)
    raise AttributeError(
        f"User script {path} must define `apply_parameters(...)` or `extract_kpis(...)`."
    )
