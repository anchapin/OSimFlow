"""Issue #1475 — machine-check ``_BYOS_CONTRACT`` against ``inspect.signature``
of the default work functions.

Contract drift between ``osimflow.byos_contract._BYOS_CONTRACT`` and the
default implementations in ``osimflow.work`` previously went undetected
until a BYOS user hit a runtime mismatch (issue #1475). This test
fails CI the moment the two tables diverge.

Notes on scope:
- ``run_openstudio_sim`` is intentionally NOT in ``_BYOS_CONTRACT``.
  It invokes the OpenStudio CLI directly (security-sensitive — see
  AGENTS.md §10 / gotcha #12) and is not user-overridable via BYOS,
  so it has no contract entry to compare against.
- The contract's ``param_names`` records only the *required positional*
  parameters (matching ``required_positional``); optional keyword-only
  parameters and keyword arguments with defaults are tracked by
  ``accepts_kwargs`` instead.
"""

import inspect
from collections.abc import Callable

import pytest

import osimflow.work as work
from osimflow.byos_contract import _BYOS_CONTRACT

# Map the default work-function attribute name to the contract dict key.
DEFAULT_TO_KEY: dict[str, str] = {
    "default_apply_parameters": "apply_parameters",
    "extract_kpis": "extract_kpis",
    "aggregate_results": "aggregate_results",
    "generate_plots": "generate_plots",
}


def _required_positional_param_names(func: Callable[..., object]) -> tuple[str, ...]:
    """Return the names of required positional parameters for ``func``.

    Mirrors the contract's semantics: only parameters without defaults
    that can be passed positionally are tracked in ``param_names``;
    everything else falls under ``accepts_kwargs``.
    """
    sig = inspect.signature(func)
    return tuple(
        p.name
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    )


def _required_positional_count(func: Callable[..., object]) -> int:
    """Return the count of required positional parameters for ``func``."""
    sig = inspect.signature(func)
    return sum(
        1
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    )


@pytest.mark.parametrize(("func_name", "key"), DEFAULT_TO_KEY.items())
def test_contract_matches_default_signature(func_name: str, key: str) -> None:
    func = getattr(work, func_name)
    entry = _BYOS_CONTRACT[key]

    actual_param_names = _required_positional_param_names(func)
    actual_required_positional = _required_positional_count(func)

    assert entry.param_names == actual_param_names, (
        f"{key}: contract.param_names={entry.param_names!r} "
        f"but default signature required-positional params={actual_param_names!r}. "
        f"Update osimflow/byos_contract.py or osimflow/work.py to keep them in sync."
    )
    assert entry.required_positional == actual_required_positional, (
        f"{key}: contract.required_positional={entry.required_positional} "
        f"but default signature required-positional count={actual_required_positional}. "
        f"Update osimflow/byos_contract.py or osimflow/work.py to keep them in sync."
    )


def test_contract_keys_match_default_to_key_mapping() -> None:
    """Sanity: every key we expect to be in the contract actually is.

    Catches the case where a contributor renames a default function
    without updating the mapping, or removes a contract entry.
    """
    missing = [k for k in DEFAULT_TO_KEY.values() if k not in _BYOS_CONTRACT]
    assert not missing, f"DEFAULT_TO_KEY references contract keys not in _BYOS_CONTRACT: {missing}"
