"""Regression test for issue #1392 — DAG dispatcher consults
``DAGStep.inputs_signature`` / ``outputs_signature`` instead of a
hardcoded if/elif chain.

Acceptance criterion (issue #1392, body):

    Add a regression test that adds a no-op step via
    ``_STEP_DEPENDENCIES`` only (without touching
    ``_run_one_generation``) and asserts it runs.

This file pins that contract: a step declared only in
``_STEP_DEPENDENCIES`` (and providing its own
``inputs_signature``/``outputs_signature`` as needed) is invoked by the
dispatcher without any edit to ``Campaign._run_one_generation``.  This
is the conservative extension over the prior if/elif chain — new steps
plug into the table, no dispatcher surgery required.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.campaign import (
    _STEP_DEPENDENCIES as PRODUCTION_DAG,
)
from osimflow.campaign import (
    DAGStep,
    StepInputs,
    StepOutputs,
)
from osimflow.executors import LocalExecutor

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


@pytest.fixture
def cfg(workdir: Path, template_pkg: Path, outdir: Path) -> CampaignConfig:
    return CampaignConfig(
        input_variables=EXAMPLE_PKG / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=1,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        skip_preflight=True,
    )


@pytest.fixture
def campaign(cfg: CampaignConfig) -> Campaign:
    return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))


def _install_dag_with_test_step(
    monkeypatch: pytest.MonkeyPatch,
    test_step_name: str,
    test_step: DAGStep,
    insert_after: str = "GENERATE_LHS_SAMPLES",
) -> dict[str, DAGStep]:
    """Insert ``test_step`` into a copy of the production DAG.

    Returns the inserted-after name so the caller can verify position.
    The returned dict is a fresh copy so the production
    ``_STEP_DEPENDENCIES`` table is untouched.
    """
    table: dict[str, DAGStep] = {}
    inserted = False
    for name, step in PRODUCTION_DAG.items():
        table[name] = step
        if name == insert_after and not inserted:
            table[test_step_name] = test_step
            inserted = True
    if not inserted:
        # insert_after not found — append at the end as a safety net.
        table[test_step_name] = test_step
    monkeypatch.setattr(
        "osimflow.campaign._STEP_DEPENDENCIES",
        table,
        raising=False,
    )
    return table


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_op_step_declared_only_in_dag_table_runs(
    campaign: Campaign,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step declared only in ``_STEP_DEPENDENCIES`` runs end-to-end.

    Regression test for issue #1392 acceptance criterion: registering
    a new step in the DAG table — without editing
    ``Campaign._run_one_generation`` — must be enough to wire it into
    the per-generation dispatch loop.  The dispatcher consults
    ``DAGStep.inputs_signature`` / ``outputs_signature`` and invokes
    the step method with the declared arg tuple.

    The no-op step:

    * method: a unique sentinel method installed on ``Campaign`` via
      ``monkeypatch.setattr`` so the dispatcher resolves ``getattr``
      to it.  The only call site for this method is the dispatcher's
      ``step_method(*args)`` invocation.
    * inputs_signature: returns the positional arg tuple ``()``.
    * outputs_signature: ``None`` — return value ignored.

    The test asserts both:

    1. The dispatcher's invocation list contains our sentinel name.
    2. The per-generation work_dir contains the marker file the
       no-op wrote — confirming the dispatcher actually ran the
       step (not just dispatched it).
    """
    invocations: list[str] = []
    marker_path = workdir / "work" / "issue_1392_noop_marker.txt"

    def step_no_op_1392(self: Campaign) -> None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("issue-1392-noop-step-ran")
        invocations.append("step_no_op_1392")

    monkeypatch.setattr(Campaign, "step_no_op_1392", step_no_op_1392, raising=False)

    noop_step = DAGStep(
        inputs=StepInputs(),
        outputs=StepOutputs(produced=("issue_1392_noop_marker.txt",)),
        method="step_no_op_1392",
        # Issue #1392: this Callable is the entire contract for the
        # dispatcher's ``step_method(*args)`` invocation.  Without it
        # the dispatcher would call ``step_method()`` with no args
        # (the ``None`` fallback).  We declare an explicit signature
        # for documentation and to verify the dispatcher passes it
        # through verbatim.
        inputs_signature=lambda state, campaign, algo, generation: (),
    )
    _install_dag_with_test_step(
        monkeypatch,
        test_step_name="ISSUE_1392_NO_OP",
        test_step=noop_step,
        insert_after="GENERATE_LHS_SAMPLES",
    )

    # End-to-end run.  The campaign writes ``samples.json`` during
    # ``GENERATE_LHS_SAMPLES``; our no-op runs immediately after, so
    # downstream fan-out steps find the samples.json they expect.
    campaign.run()

    assert invocations == ["step_no_op_1392"], (
        f"expected exactly one call to step_no_op_1392, got {invocations!r}; "
        f"the dispatcher did not invoke the step declared in _STEP_DEPENDENCIES."
    )
    assert marker_path.is_file(), (
        f"no-op marker missing at {marker_path}; the step was declared but did not run end-to-end."
    )
    assert marker_path.read_text() == "issue-1392-noop-step-ran"


def test_custom_step_with_inputs_signature_receives_declared_args(
    campaign: Campaign,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step's ``inputs_signature`` Callable is consulted verbatim.

    Pins that the dispatcher passes the ``inputs_signature`` return
    value straight into ``step_method(*args)`` — no implicit shape
    transformation.  The test step's signature returns ``(algo,
    generation)`` and the step method records both.  After the run,
    the recorded values must match what the dispatcher saw.
    """
    captured: dict[str, object] = {}
    marker_path = workdir / "work" / "issue_1392_args_marker.json"

    def step_capture_args(self: Campaign, algo: object, generation: int) -> None:
        captured["algo_name"] = algo.name()
        captured["generation"] = generation
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(f"{algo.name()}|{generation}")

    monkeypatch.setattr(Campaign, "step_capture_args_1392", step_capture_args, raising=False)

    capture_step = DAGStep(
        inputs=StepInputs(),
        outputs=StepOutputs(produced=("issue_1392_args_marker.json",)),
        method="step_capture_args_1392",
        inputs_signature=lambda state, campaign, algo, generation: (algo, generation),
    )
    _install_dag_with_test_step(
        monkeypatch,
        test_step_name="ISSUE_1392_CAPTURE_ARGS",
        test_step=capture_step,
        insert_after="GENERATE_LHS_SAMPLES",
    )

    campaign.run()

    assert captured.get("algo_name") == "lhs", (
        f"expected algo.name()=='lhs', got {captured.get('algo_name')!r}; "
        f"the inputs_signature args were not forwarded to step_method."
    )
    assert captured.get("generation") == 0, (
        f"expected generation==0, got {captured.get('generation')!r}; "
        f"the inputs_signature args were not forwarded to step_method."
    )
    assert marker_path.is_file()
    assert marker_path.read_text() == "lhs|0"


def test_dag_step_inputs_signature_default_is_none() -> None:
    """``DAGStep.inputs_signature`` defaults to ``None`` for back-compat.

    The legacy ``_verify_step_inputs`` tests in
    ``tests/integration/test_step_input_verification.py`` build
    ``DAGStep(method=...)`` without specifying a signature.  The
    dispatcher's fallback for ``inputs_signature=None`` is to call
    ``method()`` with no args.  This pins that default so a future
    refactor can't silently make it required.
    """
    step = DAGStep(
        inputs=StepInputs(),
        outputs=StepOutputs(),
        method="step_validate_measure_variables",
    )
    assert step.inputs_signature is None
    assert step.outputs_signature is None


# Steps in ``_STEP_DEPENDENCIES`` whose ``condition`` gates them on a
# non-default algorithm (Sobol, UQ).  These are *not* dispatched by the
# main loop — they are invoked explicitly in the post-loop code in
# ``_run_one_generation``.  For them, ``inputs_signature=None`` is the
# documented contract (issue #1392): the step is configured for
# monitoring / configuration but the per-generation loop skips the call.
_NON_DISPATCHED_STEPS: frozenset[str] = frozenset(
    {"COMPUTE_SENSITIVITY_INDICES", "COMPUTE_UQ_INDICES"}
)


def test_production_dag_steps_carry_inputs_signature() -> None:
    """Every dispatched production step carries a non-``None`` ``inputs_signature``.

    Issue #1392 acceptance: the dispatcher consults
    ``inputs_signature`` for every step.  After the fix, each
    production step that the dispatcher actually invokes registers its
    own signature explicitly in ``_STEP_DEPENDENCIES`` — no dispatched
    step relies on the implicit no-args fallback.

    Steps whose ``condition`` is permanently False for the default
    algorithm (``COMPUTE_SENSITIVITY_INDICES``, ``COMPUTE_UQ_INDICES``)
    are excluded: they are invoked explicitly in the post-loop code,
    and ``inputs_signature=None`` is the documented contract for that
    pattern (the dispatcher skips them).
    """
    for name, step in PRODUCTION_DAG.items():
        if name in _NON_DISPATCHED_STEPS:
            assert step.inputs_signature is None, (
                f"non-dispatched step {name!r} should leave "
                f"inputs_signature=None (invoked outside the dispatcher "
                f"loop); got {step.inputs_signature!r}."
            )
            continue
        assert step.inputs_signature is not None, (
            f"production step {name!r} has inputs_signature=None; "
            f"every dispatched step in _STEP_DEPENDENCIES must declare "
            f"its arg tuple explicitly (issue #1392)."
        )
