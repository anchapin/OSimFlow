"""DAG step descriptors split out of ``campaign.py`` (issue #1890).

``StepInputs`` / ``StepOutputs`` / ``DAGStep`` / ``SimResult`` and the
per-step signature helpers. The ``_STEP_DEPENDENCIES`` table itself stays in
``osimflow.campaign`` (tests patch it there).
"""

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from osimflow.campaign import Campaign


# Type aliases — these are the schemas of intermediate DAG outputs.
# Issue #1542: the SampleSpec / VariableSpec TypedDicts live in
# ``osimflow._campaign_types`` and are re-exported from the top import
# block so ``from osimflow.campaign import SampleSpec`` (used by tests
# and collaborators) keeps working.
SampleDict = dict[str, Path]  # sample_id -> path (per-sample work dir)


@dataclasses.dataclass(frozen=True)
class SimResult:
    """Structured result from ``step_run_openstudio_sim``.

    The ``success`` field is ``False`` when one or more samples recorded a
    non‑zero ``sim_exit_code`` (i.e. partial or complete fan‑out failure).
    Callers can inspect this field instead of needing to parse ``run.json``.
    """

    samples: SampleDict
    success: bool


@dataclasses.dataclass(frozen=True)
class StepOutputs:
    """Describes the files produced by a DAG step."""

    produced: tuple[str, ...] = ()
    """Glob patterns relative to work_dir (e.g. 'apply/*/modified.osw')."""

    kpi_pattern: str | None = None
    """For fan-out steps: glob pattern for per-sample KPI JSON files."""


@dataclasses.dataclass(frozen=True)
class StepInputs:
    """Describes the files required by a DAG step before it can run."""

    required: tuple[str, ...] = ()
    """Exact file paths required (checked via is_file())."""

    required_patterns: tuple[str, ...] = ()
    """Glob patterns relative to work_dir; ALL files matching must exist."""

    count: int | None = None
    """Expected number of files from a fan-out step (None = no check)."""


@dataclasses.dataclass(frozen=True)
class DAGStep:
    """Describes a DAG step for data-driven execution (issues #1276, #1392).

    Extends ``StepInputs``/``StepOutputs`` with step execution metadata so
    that ``_run_one_generation`` can iterate over steps from configuration
    instead of calling step methods by name.

    Issue #1392: each step now declares its own input/output contract via
    :attr:`inputs_signature` and :attr:`outputs_signature`.  The prior
    if/elif chain in ``_run_one_generation`` was the only contract for
    step-method signatures — any new step declared in ``_STEP_DEPENDENCIES``
    had no documented path to register its own args.  With the new
    signatures, ``step_method(*inputs_signature(state))`` is generic;
    new steps just plug in their own callable.
    """

    inputs: StepInputs
    outputs: StepOutputs
    method: str
    condition: Callable[..., bool] | None = None
    fan_out: bool = False
    inputs_signature: Callable[..., tuple[Any, ...]] | None = None
    """Callable returning the positional arg tuple for ``method``.

    Receives the per-generation state namespace (a ``SimpleNamespace``
    exposing ``samples``, ``parameterized``, ``simulated``, ``kpi_files``,
    ``aggregated``, ``campaign``, ``algo``, ``generation``).  Each step's
    signature callable picks the slots it needs and ignores the rest.
    When ``None``, ``method`` is called with no args.
    """
    outputs_signature: Callable[[Any], tuple[str, Any] | None] | None = None
    """Callable taking the method's return value and returning
    ``(slot_name, slot_value)`` to merge into the per-generation state.

    Built-in slot names: ``"parameterized"``, ``"simulated"``,
    ``"kpi_files"``, ``"aggregated"``.  When ``None``, the return value
    is ignored (e.g. for ``step_generate_samples`` whose result is
    captured before the dispatcher loop runs)."""


def _always_run(campaign: "Campaign", algo: Any, **kwargs: Any) -> bool:
    return True


# ---------------------------------------------------------------------------
# Per-step inputs/outputs signatures (issue #1392).
#
# These helpers close over the per-generation state namespace exposed by
# ``_run_one_generation``.  Each step reads only the slots it needs and
# returns the positional arg tuple for its method.
#
# Output-signature helpers take the method's return value and return a
# ``(slot_name, slot_value)`` pair that the dispatcher writes back into
# the per-generation state.  Steps whose return value is not consumed by
# any downstream step leave ``outputs_signature=None``.
# ---------------------------------------------------------------------------


def _sig_lhs_samples(
    state: Any, campaign: "Campaign", algo: Any, generation: int
) -> tuple[Any, ...]:
    return (algo, generation)


def _sig_preflight(state: Any, campaign: "Campaign", algo: Any, generation: int) -> tuple[Any, ...]:
    return ()


def _sig_apply(state: Any, campaign: "Campaign", algo: Any, generation: int) -> tuple[Any, ...]:
    return (state.samples, generation)


def _sig_validate_measure(
    state: Any, campaign: "Campaign", algo: Any, generation: int
) -> tuple[Any, ...]:
    return ()


def _sig_run_sim(state: Any, campaign: "Campaign", algo: Any, generation: int) -> tuple[Any, ...]:
    return (state.parameterized, generation)


def _sig_extract_kpis(
    state: Any, campaign: "Campaign", algo: Any, generation: int
) -> tuple[Any, ...]:
    return (state.simulated, generation)


def _out_parameterized(result: Any) -> tuple[str, Any]:
    return ("parameterized", result)


def _out_simulated(result: Any) -> tuple[str, Any]:
    """``step_run_openstudio_sim`` returns a ``SimResult`` whose ``.samples``
    field is the per-sample ``{sample_id: sim_dir}`` mapping the
    dispatcher feeds into ``step_extract_kpis``."""

    return ("simulated", result.samples)


def _out_kpi_files(result: Any) -> tuple[str, Any]:
    return ("kpi_files", result)
