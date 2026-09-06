"""Contract test: pin the per-substrate real-E2E matrix (issue #1555).

The per-substrate real-E2E matrix in ``docs/substrate-coverage.md`` is
the headline deliverable for issue #1020, but nothing machine-checked
the three-link invariant between the three places every new executor
must touch:

1. A companion test file under ``tests/integration/`` that carries a
   real-substrate gate (a ``pytestmark = pytest.mark.skipif(...)`` on
   a recognized ``OSIMFLOW_*_E2E`` env var, or — for the
   ``nomad_e2e`` marker pattern — a per-test or module-level
   ``@pytest.mark.<marker>`` decorator).
2. A ``.github/workflows/<substrate>-e2e.yml`` runner that references
   the test path in its ``run:`` step.
3. A row in the ``docs/substrate-coverage.md`` matrix that names the
   test path.

Before #1555 this contract was honored by convention. The matrix in
``docs/substrate-coverage.md`` had already drifted: rows 9–11 (PBS,
Dask-JobQueue, Docker Swarm) said *"add ``<substrate>-e2e.yml`` when
a CI runner is provisioned"* even though ``pbs-e2e.yml`` /
``dask-e2e.yml`` / ``docker-swarm-e2e.yml`` had already landed and
were running their ``test_real_*`` files nightly. This contract test
fails CI until every name in :class:`osimflow.executors.ExecutorRegistry`
closes all three links — so the next 11th executor (or a silent
renaming of an existing one) cannot shrink the advertised coverage.

Hermetic: pure file reads + one ``osimflow.executors`` import for the
canonical name list. No subprocesses, no network. Mirrors the pattern
of :mod:`tests.contract.test_workflow_sha_pinning`,
:mod:`tests.contract.test_agents_md_consistency`, and
:mod:`tests.contract.test_cli_flag_config_wiring`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from osimflow.executors import ExecutorRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_INTEGRATION = REPO_ROOT / "tests" / "integration"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
MATRIX_DOC = REPO_ROOT / "docs" / "substrate-coverage.md"

pytestmark = pytest.mark.contract

# ---------------------------------------------------------------------------
# Per-executor mapping: candidate real-E2E test paths
# ---------------------------------------------------------------------------
# Each ExecutorRegistry entry must have AT LEAST ONE of these paths
# present on disk AND carrying a real-substrate gate. A path is
# considered gated when:
#   - The file declares a module-level
#     ``pytestmark = pytest.mark.skipif(...)`` that references a
#     recognized ``OSIMFLOW_*_E2E`` env var (the canonical pattern
#     used by every test under ``tests/integration/test_real_*.py`` /
#     ``tests/integration/test_<substrate>_real*.py``), OR
#   - The file declares a per-test or module-level
#     ``@pytest.mark.<marker>`` decorator (the ``nomad_e2e`` pattern).
#
# Multiple candidates are listed where one workflow drives multiple
# related tests (AWS Batch ships ``test_aws_batch_real.py`` and the
# real-OpenStudio variant ``test_aws_batch_real_openstudio.py``;
# Nomad ships the canonical ``test_real_nomad_ha_campaign.py`` AND
# the Docker-Compose harness tests under ``tests/integration/nomad_e2e/``).
EXECUTOR_TEST_PATHS: dict[str, list[str]] = {
    "local": [
        "tests/integration/test_local_executor.py",
    ],
    "slurm": [
        "tests/integration/test_slurm_real_cluster.py",
    ],
    "aws_batch": [
        "tests/integration/test_aws_batch_real.py",
        "tests/integration/test_aws_batch_real_openstudio.py",
    ],
    "azure_batch": [
        "tests/integration/test_azure_batch_real.py",
    ],
    "google_batch": [
        "tests/integration/test_google_batch_real.py",
    ],
    "kubernetes": [
        "tests/integration/test_kubernetes_executor_real.py",
    ],
    "nomad": [
        "tests/integration/test_real_nomad_ha_campaign.py",
        "tests/integration/nomad_e2e/test_multi_node.py",
        "tests/integration/nomad_e2e/test_single_node.py",
    ],
    "pbs": [
        "tests/integration/test_real_pbs_campaign.py",
    ],
    "dask_jobqueue": [
        "tests/integration/test_real_dask_campaign.py",
    ],
    "docker_swarm": [
        "tests/integration/test_real_docker_swarm_campaign.py",
    ],
}

# Executors in this set legitimately have NO dedicated ``-e2e.yml``
# workflow. Each entry must be paired with a comment in the set below
# explaining why the workflow is missing — a reviewer who wants to
# remove an entry has to read that comment first.
ALLOWLIST_NO_WORKFLOW: dict[str, str] = {
    # ``local`` is the dev runner exercised by every PR via the
    # ``ci.yml`` ``test`` job (no special workflow needed). See row 1
    # of the matrix in docs/substrate-coverage.md.
    "local": "dev runner; covered by ci.yml `test` job",
}

# Per-executor override for the workflow filename. The default rule
# is ``<executor-with-dashes>-e2e.yml`` (e.g. ``aws_batch`` →
# ``aws-batch-e2e.yml``). Executors whose registered name does not
# match the workflow filename list an override here — currently only
# ``dask_jobqueue`` (workflow is ``dask-e2e.yml``, not
# ``dask-jobqueue-e2e.yml``).
EXECUTOR_WORKFLOW_OVERRIDES: dict[str, str] = {
    "dask_jobqueue": "dask-e2e.yml",
}


# Recognized real-substrate gate env vars (issue #1020). A test file
# is considered to carry a real-substrate skipif-gate when its
# ``pytestmark = pytest.mark.skipif(...)`` clause references at least
# one of these. The leading ``OSIMFLOW_`` and trailing ``_E2E`` are
# the canonical naming convention — see ``docs/substrate-coverage.md``
# §"Skip-gate contract" for the full enumeration.
GATE_ENV_VAR_RE = re.compile(r"os\.environ\.get\(\s*['\"]OSIMFLOW_[A-Z0-9_]+_E2E['\"]")
# Per-test or module-level ``pytestmark = pytest.mark.<marker>``.
PYTESTMARK_ASSIGN_RE = re.compile(r"pytestmark\s*=\s*pytest\.mark\.(\w+)")
PYTESTMARK_DECORATOR_RE = re.compile(r"@pytest\.mark\.(\w+)")


def _workflow_path_for(executor_name: str) -> Path:
    """Return the canonical workflow path for *executor_name*.

    The default mapping is the simple
    ``<name-with-dashes>-e2e.yml`` rule used by most workflows
    (``aws_batch`` → ``aws-batch-e2e.yml``, ``docker_swarm`` →
    ``docker-swarm-e2e.yml``). Executors whose registered name does
    not match the workflow filename list an explicit override in
    :data:`EXECUTOR_WORKFLOW_OVERRIDES`.
    """
    override = EXECUTOR_WORKFLOW_OVERRIDES.get(executor_name)
    if override is not None:
        return WORKFLOWS_DIR / override
    return WORKFLOWS_DIR / (executor_name.replace("_", "-") + "-e2e.yml")


def _has_real_substrate_gate(test_path: Path) -> tuple[bool, str]:
    """Return ``(has_gate, evidence)`` for the test file's gate.

    Gates come in two flavors:

    1. The canonical ``pytestmark = pytest.mark.skipif(...)`` form
       that reads an ``OSIMFLOW_<SUBSTRATE>_E2E`` env var — see
       :data:`GATE_ENV_VAR_RE`. Every test under
       ``tests/integration/test_real_*.py`` and
       ``tests/integration/test_<substrate>_real*.py`` follows it.
    2. The ``@pytest.mark.<marker>`` decorator (per-test or
       module-level) used by the ``nomad_e2e`` tests under
       ``tests/integration/nomad_e2e/`` — see
       ``tests/integration/nomad_e2e/test_multi_node.py``.

    Returns the (bool, evidence) pair so callers can surface the
    matched pattern in failure messages instead of forcing a reviewer
    to dig for it.
    """
    text = test_path.read_text(encoding="utf-8")
    module_marks = PYTESTMARK_ASSIGN_RE.findall(text)
    has_skipif = "skipif" in module_marks
    if has_skipif and GATE_ENV_VAR_RE.search(text):
        return True, "pytestmark=skipif on OSIMFLOW_*_E2E env var"
    has_per_test_marker = bool(PYTESTMARK_DECORATOR_RE.search(text))
    has_other_module_marker = any(m != "skipif" for m in module_marks)
    if has_per_test_marker or has_other_module_marker:
        return True, "pytest.mark.<substrate> decorator or pytestmark"
    return False, "no skipif(OSIMFLOW_*_E2E) and no pytest.mark.<substrate>"


def _select_matching_test_paths(executor_name: str) -> list[tuple[Path, str]]:
    """Return ``[(abs_path, evidence), ...]`` for every gated candidate.

    Filters :data:`EXECUTOR_TEST_PATHS[executor_name]` down to the
    files that actually exist on disk AND carry a real-substrate gate.
    An empty result is the contract failure signal.
    """
    matched: list[tuple[Path, str]] = []
    for rel in EXECUTOR_TEST_PATHS[executor_name]:
        test_path = REPO_ROOT / rel
        if not test_path.is_file():
            continue
        has_gate, evidence = _has_real_substrate_gate(test_path)
        if has_gate:
            matched.append((test_path, evidence))
    return matched


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def matrix_doc_text() -> str:
    """Cached ``docs/substrate-coverage.md`` content (read once per module)."""
    assert MATRIX_DOC.is_file(), (
        f"docs/substrate-coverage.md missing at {MATRIX_DOC}. "
        "Substrate coverage matrix is a contract-tested artifact."
    )
    return MATRIX_DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("executor_name", sorted(EXECUTOR_TEST_PATHS))
def test_substrate_has_companion_test(executor_name: str) -> None:
    """Every ExecutorRegistry entry has at least one gated real-substrate test.

    A "gated" test is one whose ``pytestmark = pytest.mark.skipif(...)``
    references a recognized ``OSIMFLOW_*_E2E`` env var, OR whose
    ``@pytest.mark.<marker>`` decorators (per-test or module-level)
    are present. See :func:`_has_real_substrate_gate` for the regex
    details. The same gated test pattern is asserted to match in the
    companion `tests/integration/test_*_real*.py` files — see the
    "Skip-gate contract" section of ``docs/substrate-coverage.md``.
    """
    matched = _select_matching_test_paths(executor_name)
    candidates = EXECUTOR_TEST_PATHS[executor_name]
    assert matched, (
        f"Executor '{executor_name}': no candidate real-substrate test "
        f"found with a gate. Candidates checked: {candidates}. Either the "
        f"file is missing, the pytestmark/skipif pattern drifted, or "
        f"EXECUTOR_TEST_PATHS['{executor_name}'] is out of date."
    )


@pytest.mark.parametrize("executor_name", sorted(EXECUTOR_TEST_PATHS))
def test_substrate_has_workflow_referencing_test(executor_name: str) -> None:
    """Every ExecutorRegistry entry has a CI workflow that drives its test.

    The workflow file path is the simple
    ``<executor-with-dashes>-e2e.yml`` rule (e.g. ``dask_jobqueue`` →
    ``dask-e2e.yml``). The workflow must reference at least one of
    the executor's candidate test paths in its ``run:`` step.

    ``ALLOWLIST_NO_WORKFLOW`` lists executors that legitimately have
    no dedicated workflow — currently ``local`` (the dev runner,
    covered by ``ci.yml`` test job). A new entry must be paired with
    a justifying comment.
    """
    if executor_name in ALLOWLIST_NO_WORKFLOW:
        reason = ALLOWLIST_NO_WORKFLOW[executor_name]
        pytest.skip(
            f"Executor '{executor_name}' is in ALLOWLIST_NO_WORKFLOW — "
            f"no dedicated e2e workflow. Justification: {reason}."
        )

    candidates = EXECUTOR_TEST_PATHS[executor_name]
    workflow_path = _workflow_path_for(executor_name)
    assert workflow_path.is_file(), (
        f"Executor '{executor_name}': workflow file missing at "
        f"{workflow_path.relative_to(REPO_ROOT)}. Either add the "
        f"workflow, or — if the executor does not need a dedicated "
        f"e2e workflow — move '{executor_name}' to "
        f"ALLOWLIST_NO_WORKFLOW with a justifying comment."
    )

    workflow_text = workflow_path.read_text(encoding="utf-8")
    referenced = [rel for rel in candidates if rel in workflow_text]
    assert referenced, (
        f"Executor '{executor_name}': workflow "
        f"{workflow_path.relative_to(REPO_ROOT)} does not reference any "
        f"candidate test path. Candidates: {candidates}. Either fix the "
        f"workflow `run:` step to invoke one of these paths, or update "
        f"EXECUTOR_TEST_PATHS['{executor_name}'] to match the path the "
        f"workflow actually drives."
    )


@pytest.mark.parametrize("executor_name", sorted(EXECUTOR_TEST_PATHS))
def test_substrate_appears_in_matrix(executor_name: str, matrix_doc_text: str) -> None:
    """Every ExecutorRegistry entry has a row in the substrate-coverage matrix.

    The matrix is the user-facing artifact that issue #1020 promised
    to maintain — a test that fails when a row goes stale (the
    symptom issue #1555 was opened against) closes the loop. The
    check is intentionally lenient (substring match against any
    candidate test path) so a future re-naming of the matrix
    filename still resolves here.
    """
    candidates = EXECUTOR_TEST_PATHS[executor_name]
    mentioned = [rel for rel in candidates if rel in matrix_doc_text]
    assert mentioned, (
        f"Executor '{executor_name}': docs/substrate-coverage.md does not "
        f"mention any candidate test path. Candidates: {candidates}. "
        f"Add or fix the matrix row for {executor_name} (the matrix is "
        f"the source of truth for the per-substrate real-E2E story)."
    )


def test_executor_registry_matches_contract_mapping() -> None:
    """Every ExecutorRegistry name must appear in :data:`EXECUTOR_TEST_PATHS`.

    The contract maps *executor → candidate test paths* statically. A
    new executor that lands in the registry without a corresponding
    entry here will fail every per-executor test (no candidates →
    "no candidate real-substrate test found") — this test makes the
    failure mode obvious in the parameterize list instead of a
    generic per-executor error.
    """
    registry_names = set(ExecutorRegistry.list_available())
    mapped_names = set(EXECUTOR_TEST_PATHS)
    missing_from_map = registry_names - mapped_names
    extra_in_map = mapped_names - registry_names
    assert not missing_from_map, (
        "ExecutorRegistry has names missing from EXECUTOR_TEST_PATHS: "
        f"{sorted(missing_from_map)}. Add an entry so the per-executor "
        "contract tests above cover the new substrate."
    )
    assert not extra_in_map, (
        "EXECUTOR_TEST_PATHS has entries not in ExecutorRegistry: "
        f"{sorted(extra_in_map)}. Either remove the stale entries or "
        "register the corresponding executor class."
    )


def test_allowlist_entries_are_documented() -> None:
    """Every entry in :data:`ALLOWLIST_NO_WORKFLOW` must carry a justification.

    The allowlist is the only escape hatch from the workflow check;
    keeping each entry paired with a one-line comment makes future
    reviewers question it instead of inheriting it silently.
    """
    undocumented = [
        name
        for name, justification in ALLOWLIST_NO_WORKFLOW.items()
        if not justification or not justification.strip()
    ]
    assert not undocumented, (
        f"ALLOWLIST_NO_WORKFLOW entries without a justification comment: "
        f"{undocumented}. Each entry must explain WHY the workflow is "
        f"missing — reviewers should not have to dig."
    )
