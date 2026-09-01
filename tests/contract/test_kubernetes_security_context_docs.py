"""Contract tests pinning the Kubernetes SecurityContext docs to the code (issue #1456).

``docs/kubernetes-deployment.md`` gained a "Strict Pod SecurityContext"
section documenting the ``security_context_strict`` constructor flag
(issue #1383, commit 3dea4ae). The flag has no CLI surface, so the docs
are the only discovery path for operators on Pod Security Standards
``restricted`` clusters — silent drift between doc and code would make
the hardening unverifiable.

These tests parse both files with plain regex reads (hermetic, fast):

  1. docs/ mentions ``security_context_strict`` at least once
  2. the doc's emitted-fields list matches kubernetes_executor.py
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_DOCS_DIR = REPO_ROOT / "docs"
_K8S_DOC = _DOCS_DIR / "kubernetes-deployment.md"
_EXECUTOR = REPO_ROOT / "osimflow" / "executors" / "kubernetes_executor.py"

# snake_case identifier in kubernetes_executor.py -> camelCase YAML field
# the docs must show. ``automount_service_account_token`` lives on the
# V1PodSpec (not the V1PodSecurityContext) and ``run_as_user`` is the
# pod-level fixed UID; both belong in the same strict-mode snippet.
_FIELD_MAP = {
    "run_as_non_root": "runAsNonRoot",
    "read_only_root_filesystem": "readOnlyRootFilesystem",
    "allow_privilege_escalation": "allowPrivilegeEscalation",
    "run_as_user": "runAsUser",
    "automount_service_account_token": "automountServiceAccountToken",
}


def test_docs_mention_security_context_strict() -> None:
    """``grep -rn security_context_strict docs/`` has at least one hit."""
    hits = [
        p
        for p in _DOCS_DIR.rglob("*.md")
        if "security_context_strict" in p.read_text(encoding="utf-8")
    ]
    assert hits, (
        "No doc under docs/ mentions security_context_strict (issue #1456 "
        "regression). Document the flag in docs/kubernetes-deployment.md."
    )


def test_doc_emitted_fields_match_executor_code() -> None:
    """The doc's strict-mode fields must match what the executor emits.

    Guards both drift directions: a field renamed/removed from
    kubernetes_executor.py, or a field dropped from the docs snippet.
    """
    executor_src = _EXECUTOR.read_text(encoding="utf-8")
    doc_src = _K8S_DOC.read_text(encoding="utf-8")

    missing_in_code = [
        snake for snake in _FIELD_MAP if not re.search(rf"\b{snake}\b", executor_src)
    ]
    assert not missing_in_code, (
        f"kubernetes_executor.py no longer sets {missing_in_code}; update "
        "_FIELD_MAP here and the manifest snippet in "
        "docs/kubernetes-deployment.md."
    )

    missing_in_docs = [camel for camel in _FIELD_MAP.values() if camel not in doc_src]
    assert not missing_in_docs, (
        f"docs/kubernetes-deployment.md is missing strict-mode fields "
        f"{missing_in_docs}; the emitted-manifest snippet drifted from "
        "osimflow/executors/kubernetes_executor.py."
    )
    assert "capabilities" in doc_src and "ALL" in doc_src, (
        'docs/kubernetes-deployment.md must show capabilities.drop: ["ALL"] '
        "from the strict container security context."
    )


def test_doc_cites_issue_1383() -> None:
    """The docs section must cite issue #1383 as the origin of the flag."""
    assert "#1383" in _K8S_DOC.read_text(encoding="utf-8"), (
        "docs/kubernetes-deployment.md no longer cites issue #1383 for the "
        "security_context_strict flag (issue #1456 acceptance criterion)."
    )
