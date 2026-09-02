"""Guard test for GitHub issue #1469 — every DAG step must invoke
``_verify_step_inputs`` with its own step name *before* any of the step's
work executes.

AGENTS.md §2 requires cross-step dependencies to be declared in
``_STEP_DEPENDENCIES`` and forbids bypassing ``_verify_step_inputs`` when
adding a step.  Before this guard, the only tests exercised the mechanism
for two steps (APPLY_PARAMETERS, RUN_OPENSTUDIO_SIM) plus the unknown-step
early return — a new or refactored step that skipped verification would
have kept every existing test green.

Architecture note (verified against ``osimflow/campaign.py`` as of this
test): since the data-driven dispatcher (issues #1276 / #1392), the
per-step verifier call for steps declared in ``_STEP_DEPENDENCIES`` lives
in ``Campaign._run_one_generation``, which calls
``self._verify_step_inputs(step_name)`` immediately before resolving and
invoking the step's method.  Two finalize steps sit outside that loop
(issue #1419):

* ``AGGREGATE_RESULTS`` — not in ``_STEP_DEPENDENCIES``, but
  ``step_aggregate_results`` calls ``_verify_step_inputs("AGGREGATE_RESULTS")``
  directly as its first action, before submitting ``aggregate_results``
  to the executor.  It is guarded here via the same parametrized table.
* ``GENERATE_BASIC_PLOTS`` — not in ``_STEP_DEPENDENCIES`` and it makes no
  ``_verify_step_inputs`` call at all (by design: it consumes only the
  ``aggregated`` dict handed to it by ``_finalize_full_campaign`` after
  AGGREGATE_RESULTS has already been verified).  It is therefore
  intentionally *not* in the guarded table below; adding a verifier call
  for it would require a ``campaign.py`` change, which is out of scope
  for this test-only fix.

Contract-checker registration (issue #1469 acceptance): the guarded step
list is registered by importing ``_STEP_DEPENDENCIES`` directly from
``osimflow.campaign`` — the single source of truth.  ``tools/
check_agents_contract.py`` does *not* currently cross-check DAG step
names against AGENTS.md §2 (only ``__init__`` exports, ``bin/*.py``,
executors, and CLI flags), so this import is the registration mechanism:
the ``test_guard_table_covers_every_dag_step`` completeness test below
fails whenever a step is added to ``_STEP_DEPENDENCIES`` without a
matching entry here, forcing the guard table to stay in sync.

Execution model: the parametrized cases share one instrumented stub-mode
campaign per algorithm (``lhs``, ``sobol``, ``uq``).  A full campaign
takes several seconds, and the guard must stay in the required fast CI
gate (the CI ``test`` job deselects ``slow``-marked tests, and the
``slow`` job is explicitly non-required — issue #623), so re-running the
campaign once per step would needlessly tax every PR.  During each shared
run, two recorders are installed:

* ``Campaign._verify_step_inputs`` → appends ``("verify", step_name)``
* every guarded step's execution path → appends ``("work", step_name)``

For steps dispatched from the per-generation loop the work recorder wraps
the ``Campaign`` step method (the dispatcher verifies *before* the method
is resolved).  For ``AGGREGATE_RESULTS`` the work recorder wraps the
``aggregate_results`` work function in ``osimflow.campaign``'s namespace
(the verifier call sits *inside* the step method, ahead of the executor
submit).

The ordering assertion is "first verify event for the step precedes the
step's last work event".  The last-work formulation tolerates the single
documented exception — ``step_generate_samples`` is invoked once before
the dispatcher loop to seed the per-generation state namespace
(``campaign.py`` pre-loop sample generation), while its *dispatched*
invocation is the one the verifier guards.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest

import osimflow.campaign as campaign_module
from osimflow import Campaign, CampaignConfig
from osimflow.campaign import _STEP_DEPENDENCIES
from osimflow.executors import LocalExecutor

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"

# Which algorithm config makes each step's ``DAGStep.condition`` pass.
# ``lhs`` (the default) satisfies every unconditional step plus the
# ``generation == 0`` preflight gate; the two COMPUTE_* steps are gated on
# their respective algorithms.  AGGREGATE_RESULTS runs in every campaign
# (single-shot finalize).
_ALGORITHM_FOR_STEP: dict[str, str] = {
    "GENERATE_LHS_SAMPLES": "lhs",
    "PREFLIGHT_RUN_MODEL": "lhs",
    "APPLY_PARAMETERS": "lhs",
    "VALIDATE_MEASURE_VARIABLES": "lhs",
    "RUN_OPENSTUDIO_SIM": "lhs",
    "EXTRACT_KPIS": "lhs",
    "COMPUTE_SENSITIVITY_INDICES": "sobol",
    "COMPUTE_UQ_INDICES": "uq",
    # Not in _STEP_DEPENDENCIES (issue #1419 single-shot finalize step)
    # but verified via the direct call inside step_aggregate_results.
    "AGGREGATE_RESULTS": "lhs",
}

# The parametrized guard table (issue #1469 acceptance criterion).
_GUARDED_STEPS: list[str] = list(_ALGORITHM_FOR_STEP)

# One instrumented campaign per algorithm, shared by all parametrized
# cases in this module (per test process — under xdist each worker
# populates its own cache).  Values are ordered event logs.
_EVENTS_BY_ALGORITHM: dict[str, list[tuple[str, str]]] = {}


def _run_instrumented_campaign(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    algorithm: str,
) -> list[tuple[str, str]]:
    """Run one stub-mode *algorithm* campaign with verify/work recorders.

    Recorders are installed for **every** guarded step in a single pass so
    all parametrized cases can share the resulting event log.  Returns the
    ordered event log.  Downstream campaign failures after a step's
    recorded events are tolerated (the ordering assertions only need the
    events already recorded), mirroring the pytest.raises guidance in
    issue #1469 for steps with heavy scaffolding.
    """
    if algorithm in _EVENTS_BY_ALGORITHM:
        return _EVENTS_BY_ALGORITHM[algorithm]

    events: list[tuple[str, str]] = []

    original_verify = Campaign._verify_step_inputs

    def recording_verify(self: Campaign, name: str) -> None:
        events.append(("verify", name))
        original_verify(self, name)

    monkeypatch.setattr(Campaign, "_verify_step_inputs", recording_verify)

    # Work recorders for dispatcher-loop steps: the verifier fires in the
    # dispatcher immediately before the method is resolved, so recording
    # at method entry is exactly "after the verifier, before the work".
    for step_name, dag_entry in _STEP_DEPENDENCIES.items():
        original_method = getattr(Campaign, dag_entry.method)

        def recording_method(
            self: Campaign,
            *args: Any,
            _orig: Any = original_method,
            _step: str = step_name,
            **kwargs: Any,
        ) -> Any:
            events.append(("work", _step))
            return _orig(self, *args, **kwargs)

        monkeypatch.setattr(Campaign, dag_entry.method, recording_method)

    # Work recorder for AGGREGATE_RESULTS: the verifier call sits inside
    # step_aggregate_results, ahead of the executor submit, so the work
    # recorder wraps the submitted work function instead.
    original_work_fn = campaign_module.aggregate_results

    def recording_work_fn(*args: Any, **kwargs: Any) -> Any:
        events.append(("work", "AGGREGATE_RESULTS"))
        return original_work_fn(*args, **kwargs)

    monkeypatch.setattr(campaign_module, "aggregate_results", recording_work_fn)

    tmp_path = tmp_path_factory.mktemp(f"guard_{algorithm}")
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "variables.yml").write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wwr\n"
        "    distribution: uniform\n"
        "    min: 0.2\n"
        "    max: 0.6\n"
        "    measure_argument: SetEnvelopePerformance.wwr\n"
    )
    template_pkg = tmp_path / "template"
    shutil.copytree(EXAMPLE_PKG, template_pkg)
    outdir = tmp_path / "out"
    outdir.mkdir()

    # Sobol's balance properties need a power-of-two base sample count.
    n_samples = 4 if algorithm == "sobol" else 2

    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=n_samples,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        skip_preflight=True,
        algorithm=algorithm,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    try:
        campaign.run()
    except Exception:
        # The guard asserts verifier-before-work ordering for events that
        # were already recorded; a later unrelated stub failure (e.g. the
        # SALib post-processing dividing by stub-KPI zero variance) must
        # not mask it.  If the failure happened *before* a guarded step,
        # that step's assertions fail with a diagnostic instead.
        pass

    _EVENTS_BY_ALGORITHM[algorithm] = events
    return events


def test_guard_table_covers_every_dag_step() -> None:
    """Every step in ``_STEP_DEPENDENCIES`` has a guarded parametrized case.

    This is the registration hook from issue #1469's acceptance criteria:
    adding a step to the DAG table without extending the guard table (and
    thereby without a parametrized verifier test) fails right here.
    """
    missing = set(_STEP_DEPENDENCIES) - set(_GUARDED_STEPS)
    assert not missing, (
        f"_STEP_DEPENDENCIES gained step(s) {sorted(missing)} with no "
        f"entry in _ALGORITHM_FOR_STEP; add the algorithm that satisfies "
        f"the step's condition so the parametrized guard covers it "
        f"(issue #1469)."
    )


def test_every_dag_step_has_step_method() -> None:
    """Method-name mapping is complete: every DAG entry resolves on Campaign.

    Issue #1469 also asks for the conventional-name check (STEP_NAME.lower()
    -> ``step_<lower>``).  ``GENERATE_LHS_SAMPLES`` is the one historic
    deviation: it maps to ``step_generate_samples`` (the dispatcher-era
    name) rather than ``step_generate_lhs_samples`` — the declared
    ``DAGStep.method`` field is authoritative, so the guard asserts against
    it directly.
    """
    for name, dag_entry in _STEP_DEPENDENCIES.items():
        assert dag_entry.method.startswith("step_"), (
            f"DAG step {name!r} declares method {dag_entry.method!r} which "
            f"does not follow the step_* naming convention."
        )
        assert hasattr(Campaign, dag_entry.method), (
            f"DAG step {name!r} declares method {dag_entry.method!r} but "
            f"Campaign has no such attribute — the step would be skipped "
            f"by the dispatcher with a warning and never verified."
        )


@pytest.mark.parametrize("step_name", _GUARDED_STEPS)
def test_step_invokes_verify_step_inputs_before_work(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    step_name: str,
) -> None:
    """The verifier fires with *step_name* before the step's work executes.

    Issue #1469 acceptance criterion: monkeypatch
    ``Campaign._verify_step_inputs``, invoke each step's Campaign method
    through the real stub-mode execution path, and assert the verifier was
    called with that step's name before any work executes.  A refactor
    that drops or reorders the verifier call for any step fails here.
    """
    events = _run_instrumented_campaign(
        monkeypatch, tmp_path_factory, _ALGORITHM_FOR_STEP[step_name]
    )

    verify_positions = [i for i, event in enumerate(events) if event == ("verify", step_name)]
    assert verify_positions, (
        f"_verify_step_inputs was never called with {step_name!r} during a "
        f"full stub-mode campaign — the step ran with unverified inputs "
        f"(issue #1469). Recorded events: {events}"
    )

    work_positions = [i for i, event in enumerate(events) if event == ("work", step_name)]
    assert work_positions, (
        f"The execution path for step {step_name!r} never ran, so the "
        f"verifier ordering cannot be asserted. Recorded events: {events}"
    )

    first_verify = min(verify_positions)
    last_work = max(work_positions)
    assert first_verify < last_work, (
        f"_verify_step_inputs({step_name!r}) must be called before the "
        f"step's work executes (issue #1469). verify@{first_verify}, "
        f"work@{last_work}. Recorded events: {events}"
    )
