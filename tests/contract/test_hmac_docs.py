"""Contract tests pinning the HMAC task-payload signing docs to code (issue #1459).

Issue #1177 added HMAC-SHA256 signing of ``OSIMFLOW_TASK_PAYLOAD`` (constants
in ``osimflow/task_payload_hmac.py``, fail-closed verification in
``osimflow/remote_runner.py``), and commit 8470449 (issue #1388) dropped the
secret/signature pair from the work-script subprocess env. No doc covered the
env vars, secret provisioning, fail-closed semantics, or the env scrub until
the "HMAC Task-Payload Signing (remote executors)" section was added to
``docs/secret-management.md``.

These tests parse the env-var constant values straight from
``osimflow/task_payload_hmac.py`` and require the exact strings to appear in
the docs, so renaming a variable or changing the section anchor fails here
instead of drifting silently. Pure file reads — hermetic and fast.

  1. secret-management.md names all three env-var constants
     -> test_secret_management_docs_all_env_var_constants
  2. secret-management.md documents fail-closed + compare_digest semantics
     -> test_secret_management_docs_fail_closed_semantics
  3. secret-management.md covers secret generation, rotation, meta fallback,
     and the #1388 env scrub
     -> test_secret_management_docs_provisioning_rotation_and_scrub
  4. both deployment guides reference the new section
     -> test_deployment_guides_reference_hmac_section
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_HMAC_MODULE = REPO_ROOT / "osimflow" / "task_payload_hmac.py"
_SECRET_MGMT = REPO_ROOT / "docs" / "secret-management.md"
_NOMAD_GUIDE = REPO_ROOT / "docs" / "nomad-production.md"
_K8S_GUIDE = REPO_ROOT / "docs" / "kubernetes-deployment.md"

_ENV_VAR_ASSIGN_RE = re.compile(r'^TASK_PAYLOAD(?:_SIG|_SECRET)?_ENV\s*=\s*"([^"]+)"', re.MULTILINE)

_HMAC_SECTION_ANCHOR = "secret-management.md#hmac-task-payload-signing-remote-executors"


@pytest.fixture(scope="module")
def hmac_env_var_constants() -> list[str]:
    source = _HMAC_MODULE.read_text(encoding="utf-8")
    constants = _ENV_VAR_ASSIGN_RE.findall(source)
    assert len(constants) == 3, (
        f"expected exactly three TASK_PAYLOAD*_ENV constants in {_HMAC_MODULE}, "
        f"found {constants!r} — update this test if the module surface changed"
    )
    return constants


def test_secret_management_docs_all_env_var_constants(hmac_env_var_constants: list[str]) -> None:
    doc = _SECRET_MGMT.read_text(encoding="utf-8")
    for constant in hmac_env_var_constants:
        assert constant in doc, (
            f"docs/secret-management.md must mention the env-var constant "
            f"{constant!r} (parsed from task_payload_hmac.py) — see issue #1459"
        )


def test_secret_management_docs_fail_closed_semantics() -> None:
    doc = _SECRET_MGMT.read_text(encoding="utf-8")
    assert "compare_digest" in doc, "doc must name the constant-time comparison primitive"
    assert re.search(r"fail.{0,2}closed", doc, re.IGNORECASE), (
        "doc must state the fail-closed verification semantics "
        "(unsigned/mismatched payloads rejected)"
    )


def test_secret_management_docs_provisioning_rotation_and_scrub(
    hmac_env_var_constants: list[str],
) -> None:
    doc = _SECRET_MGMT.read_text(encoding="utf-8")
    secret_env = hmac_env_var_constants[2]
    assert "openssl rand -hex 32" in doc, "doc must include secret-generation guidance"
    assert "Rotation" in doc or "rotation" in doc, "doc must cover secret rotation"
    assert "NOMAD_META_" in doc, "doc must cover the Nomad dispatch-meta fallback"
    assert secret_env in doc
    assert "8470449" in doc or "#1388" in doc, (
        "doc must reference the #1388 work-script env scrub (commit 8470449)"
    )
    assert "#1449" in doc, "doc must note the substrate-secret-store hardening direction"


@pytest.mark.parametrize(
    ("guide", "name"),
    [(_NOMAD_GUIDE, "nomad-production.md"), (_K8S_GUIDE, "kubernetes-deployment.md")],
)
def test_deployment_guides_reference_hmac_section(guide: Path, name: str) -> None:
    text = guide.read_text(encoding="utf-8")
    assert _HMAC_SECTION_ANCHOR in text, (
        f"docs/{name} must cross-reference the HMAC task-payload signing "
        f"section in docs/secret-management.md — see issue #1459"
    )
