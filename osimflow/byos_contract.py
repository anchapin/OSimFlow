"""Single source of truth for the BYOS function-signature contract.

Issue #1061: the previous design kept two near-identical contract tables
(one in :mod:`osimflow.byos`, one inside the inline subprocess runner
script).  That duplication was a maintenance hazard — a contributor who
added a new BYOS function to one copy and forgot the other would
silently pass subprocess validation while failing in-process validation
(or vice versa).  This module is the canonical home for the table.

Both the parent process (``osimflow.byos``) and the inline subprocess
runner read from here:

* The parent process imports :data:`_BYOS_CONTRACT` directly.
* The inline runner reads a snapshot that ``tools/_generate_byos_runner.py``
  bakes into ``osimflow/_byos_runner_generated.py``.  Re-run the generator
  via ``make contract`` (or ``python tools/_generate_byos_runner.py``)
  after editing this file to keep the inline runner in sync.

The structural test in ``tests/unit/test_byos.py`` asserts the generator
produced the expected snapshot — preventing drift between this module
and the generated runner.

See AGENTS.md §6 / §10 and ``user_scripts/README.md`` for the full BYOS
contract.
"""

from __future__ import annotations

from typing import NamedTuple


class ByosContractEntry(NamedTuple):
    """A single BYOS contract entry.

    Attributes:
        required_positional: Number of required positional parameters
            the function must accept (parameters without defaults).
        param_names: Documented parameter names in declaration order.
        accepts_kwargs: Whether the function is allowed to accept
            ``**kwargs`` (for example, ``extract_kpis`` accepts
            ``openstudio_version`` and other optional keyword args).
    """

    required_positional: int
    param_names: tuple[str, ...]
    accepts_kwargs: bool


_BYOS_CONTRACT: dict[str, ByosContractEntry] = {
    "apply_parameters": ByosContractEntry(
        required_positional=4,
        param_names=("template", "parameters", "sample_id", "out"),
        accepts_kwargs=False,
    ),
    "extract_kpis": ByosContractEntry(
        required_positional=3,
        param_names=("simulation_dir", "sample_id", "out"),
        accepts_kwargs=True,
    ),
    # Deprecated alias; same contract as ``apply_parameters``.
    "apply": ByosContractEntry(
        required_positional=4,
        param_names=("template", "parameters", "sample_id", "out"),
        accepts_kwargs=False,
    ),
}


__all__ = ["ByosContractEntry", "_BYOS_CONTRACT"]
