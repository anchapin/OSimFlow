"""Unit tests for osimflow.executors.KubernetesExecutor (issue #254, #996).

Covers:
  - KubernetesExecutor: submit, _submit_job, _wait_for_terminal, _get_pod_status
  - _KubernetesHandle: result, done
  - Ephemeral-runner wiring (issue #996): remote_runner command,
    OSIMFLOW_TASK_PAYLOAD + OSIMFLOW_RESULT_* env propagation,
    remote_command override, and object-storage result materialization
    against a mocked storage backend.
  - Pod hardening (issue #1383): strict vs. relaxed SecurityContext.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Detect the optional `kubernetes` SDK BEFORE importing the executor module
# so that the skip marker below is authoritative and — critically — a future
# non-lazy ``import kubernetes`` inside ``osimflow.executors.kubernetes_executor``
# cannot crash test *collection* with ModuleNotFoundError before the guard is
# evaluated (issue #623). pytest's ``skipif`` only suppresses test execution,
# not import-time failures, so the check must win the race against any import.
try:
    from kubernetes import client  # noqa: F401

    _HAS_KUBERNETES = True
except ImportError:
    _HAS_KUBERNETES = False

# Imported after the guard above. ``noqa: E402`` because the SDK detection
# block intentionally precedes these imports (see comment above).
from osimflow.executors import KubernetesExecutor  # noqa: E402
from osimflow.executors.kubernetes_executor import _KubernetesHandle  # noqa: E402
from osimflow.task_payload_hmac import (  # noqa: E402
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SIG_ENV,
    sign_task_payload,
)

# Whole-module skip when the SDK is absent. The executor's heavy tests
# (submit / _submit_job / _wait_for_terminal) construct real Kubernetes
# client types, and a module-level marker — rather than a single class
# marker — guarantees collection can never crash on ``ModuleNotFoundError``
# even if the production import later becomes non-lazy (issue #623).
pytestmark = pytest.mark.skipif(not _HAS_KUBERNETES, reason="kubernetes SDK not installed")


class TestKubernetesExecutor:
    """KubernetesExecutor wraps the K8s BatchV1Api."""

    def _make_mock_client(self) -> MagicMock:
        mock_client = MagicMock()
        mock_client.create_namespaced_job.return_value = None
        return mock_client

    def test_submit_returns_handle(self) -> None:
        mock_client = self._make_mock_client()
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        # Native Job controls (issue #997) — defaults preserve the
        # pre-#997 manifest byte-for-byte.
        ex.backoff_limit = 0
        ex.ttl_seconds_after_finished = None
        ex.queue_name = None
        # Pod hardening (issue #1383) — default is strict.
        ex.security_context_strict = True
        # Issue #1331: short-circuit the version-check pod (see helper).
        ex._negotiated_versions = ["1.0.0"]
        ex._negotiated_image = "nrel/openstudio:latest"
        ex._container_digest = None
        with patch.object(
            ex,
            "_wait_for_terminal",
            return_value={"status": {"phase": "Succeeded"}},
        ):
            handle = ex.submit(lambda: None, name="test")
        assert hasattr(handle, "result")
        assert hasattr(handle, "job_id")
        assert handle.job_id == "osimflow-test"

    def test_submit_with_resources(self) -> None:
        mock_client = self._make_mock_client()
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        ex.backoff_limit = 0
        ex.ttl_seconds_after_finished = None
        ex.queue_name = None
        ex.security_context_strict = True
        ex._negotiated_versions = ["1.0.0"]
        ex._negotiated_image = "nrel/openstudio:latest"
        ex._container_digest = None
        with patch.object(
            ex,
            "_wait_for_terminal",
            return_value={"status": {"phase": "Succeeded"}},
        ):
            handle = ex.submit(lambda: None, name="heavy", cpus=4, memory_mb=8192)
        assert handle.job_id == "osimflow-heavy"

    def test_submit_with_container(self) -> None:
        mock_client = self._make_mock_client()
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        ex.backoff_limit = 0
        ex.ttl_seconds_after_finished = None
        ex.queue_name = None
        ex.security_context_strict = True
        ex._negotiated_versions = ["1.0.0"]
        ex._negotiated_image = "nrel/openstudio:3.11.0"
        ex._container_digest = None
        with patch.object(
            ex,
            "_wait_for_terminal",
            return_value={"status": {"phase": "Succeeded"}},
        ):
            handle = ex.submit(
                lambda: None,
                name="test",
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
            )
        assert handle.job_id == "osimflow-test"
        mock_client.create_namespaced_job.assert_called_once()
        call_kwargs = mock_client.create_namespaced_job.call_args
        job = call_kwargs.kwargs["body"]
        container = job.spec.template.spec.containers[0]
        assert container.image == "nrel/openstudio:3.11.0"
        env = {e.name: e.value for e in container.env}
        assert env["OSIMFLOW_OS_VERSION"] == "3.11.0"
        assert env["OSIMFLOW_CONTAINER"] == "nrel/openstudio:3.11.0"

    # ------------------------------------------------------------------
    # Ephemeral-runner wiring (issue #996)
    # ------------------------------------------------------------------
    def _make_executor(self) -> tuple[MagicMock, KubernetesExecutor]:
        mock_client = self._make_mock_client()
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        # Native Job controls (issue #997) — defaults preserve the
        # pre-#997 manifest byte-for-byte. Override per-test to exercise
        # non-default behaviour.
        ex.backoff_limit = 0
        ex.ttl_seconds_after_finished = None
        ex.queue_name = None
        # Pod hardening (issue #1383) — default is strict; override per
        # test to exercise the relaxed (legacy) path.
        ex.security_context_strict = True
        # Issue #1331: ``submit`` calls ``_check_contract_version_compatibility``
        # which builds and submits a real version-check Pod. The
        # ``__new__``-style helpers bypass the constructor (so the
        # cached ``_negotiated_versions`` attribute is missing) and the
        # default ``_get_core_api()`` would try to talk to a real
        # cluster. Short-circuit by stubbing the negotiation to a
        # compatible version list. Individual tests that need to
        # exercise the negotiation path can override the patch.
        ex._negotiated_versions = ["1.0.0"]
        ex._negotiated_image = "nrel/openstudio:latest"
        ex._container_digest = None
        return mock_client, ex

    def _make_executor_with(
        self,
        *,
        backoff_limit: int = 0,
        ttl_seconds_after_finished: int | None = None,
        queue_name: str | None = None,
        security_context_strict: bool = True,
    ) -> tuple[MagicMock, KubernetesExecutor]:
        """Build an executor with the native Job controls pre-set.

        Helper for issue #997 / #1383 — used by the parametrised tests
        that exercise the new fields. The default values still match
        the pre-#997 manifest exactly (except for the issue #1383
        SecurityContext fields, which are now the strict default), so
        callers only need to override the fields they care about.
        """
        mock_client, ex = self._make_executor()
        ex.backoff_limit = backoff_limit
        ex.ttl_seconds_after_finished = ttl_seconds_after_finished
        ex.queue_name = queue_name
        ex.security_context_strict = security_context_strict
        return mock_client, ex

    @staticmethod
    def _submitted_container(mock_client: MagicMock) -> Any:
        """Return the V1Container from the single create_namespaced_job call."""
        mock_client.create_namespaced_job.assert_called_once()
        job = mock_client.create_namespaced_job.call_args.kwargs["body"]
        return job.spec.template.spec.containers[0]

    def test_submit_default_command_runs_remote_runner(self) -> None:
        """Jobs must execute the ephemeral runner, not sleep forever (issue #996)."""
        mock_client, ex = self._make_executor()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        container = self._submitted_container(mock_client)
        assert container.command == ["python", "-m", "osimflow.remote_runner"]

    def test_submit_remote_command_override(self) -> None:
        """An explicit remote_command must be honored via /bin/sh -c."""
        mock_client, ex = self._make_executor()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(
                lambda: None,
                name="sim_s0",
                remote_command="python -m osimflow.remote_runner --step sim",
            )
        container = self._submitted_container(mock_client)
        assert container.command == [
            "/bin/sh",
            "-c",
            "python -m osimflow.remote_runner --step sim",
        ]

    def test_submit_propagates_task_payload_env(self) -> None:
        """OSIMFLOW_TASK_PAYLOAD must carry the Nomad-compatible serialization."""
        mock_client, ex = self._make_executor()
        hint = Path("/campaign/out/work/sim/s0")
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(
                lambda: None,
                Path("/campaign/out/work/apply/s0"),
                {"r_value": 2.5},
                name="sim_s0",
                result_hint=hint,
            )
        container = self._submitted_container(mock_client)
        env = {e.name: e.value for e in container.env}
        assert "OSIMFLOW_TASK_PAYLOAD" in env
        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["schema_version"] == 1
        assert payload["name"] == "sim_s0"
        assert payload["step"] == "sim"
        # Paths are encoded with the transport path marker (same as Nomad).
        assert payload["args"][0] == {
            "__osimflow_type__": "path",
            "value": "/campaign/out/work/apply/s0",
        }
        assert payload["args"][1] == {"r_value": 2.5}
        assert payload["result_hint"] == {
            "__osimflow_type__": "path",
            "value": "/campaign/out/work/sim/s0",
        }

    def test_submit_propagates_task_payload_signature_env_when_secret_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #1177: the job env must carry the HMAC over the exact payload bytes."""
        secret = "k8s-shared-secret"
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, secret)
        mock_client, ex = self._make_executor()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        container = self._submitted_container(mock_client)
        env = {e.name: e.value for e in container.env}
        assert env[TASK_PAYLOAD_SECRET_ENV] == secret
        assert env[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(env["OSIMFLOW_TASK_PAYLOAD"], secret)

    def test_submit_omits_task_payload_signature_env_when_no_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy unsigned mode must leave the env byte-identical (issue #1177)."""
        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        monkeypatch.delenv(TASK_PAYLOAD_SIG_ENV, raising=False)
        mock_client, ex = self._make_executor()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        container = self._submitted_container(mock_client)
        env = {e.name: e.value for e in container.env}
        assert "OSIMFLOW_TASK_PAYLOAD" in env
        assert TASK_PAYLOAD_SIG_ENV not in env
        assert TASK_PAYLOAD_SECRET_ENV not in env

    def test_submit_propagates_result_storage_env(self) -> None:
        """The OSIMFLOW_RESULT_* transport contract must reach the container."""
        mock_client, ex = self._make_executor()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(
                lambda: None,
                name="sim_s0",
                result_transport_mode="object_storage",
                result_storage_backend="s3",
                result_storage_bucket="osimflow-results",
                result_storage_prefix="out",
                result_storage_endpoint="http://minio:9000",
            )
        container = self._submitted_container(mock_client)
        env = {e.name: e.value for e in container.env}
        assert env["OSIMFLOW_RESULT_TRANSPORT_MODE"] == "object_storage"
        assert env["OSIMFLOW_RESULT_STORAGE_BACKEND"] == "s3"
        assert env["OSIMFLOW_RESULT_STORAGE_BUCKET"] == "osimflow-results"
        assert env["OSIMFLOW_RESULT_STORAGE_PREFIX"] == "out"
        assert env["OSIMFLOW_RESULT_STORAGE_ENDPOINT"] == "http://minio:9000"

    def test_submit_omits_result_storage_env_when_unset(self) -> None:
        mock_client, ex = self._make_executor()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0", result_transport_mode="shared_fs")
        container = self._submitted_container(mock_client)
        env = {e.name: e.value for e in container.env}
        assert env["OSIMFLOW_RESULT_TRANSPORT_MODE"] == "shared_fs"
        for var in (
            "OSIMFLOW_RESULT_STORAGE_BACKEND",
            "OSIMFLOW_RESULT_STORAGE_BUCKET",
            "OSIMFLOW_RESULT_STORAGE_PREFIX",
            "OSIMFLOW_RESULT_STORAGE_ENDPOINT",
        ):
            assert var not in env

    def test_submit_propagates_stub_sim_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OSIMFLOW_STUB_SIM must be forwarded so pods honour stub-vs-real CLI."""
        monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
        mock_client, ex = self._make_executor()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        container = self._submitted_container(mock_client)
        env = {e.name: e.value for e in container.env}
        assert env["OSIMFLOW_STUB_SIM"] == "1"

    def test_submit_preserves_resource_mapping(self) -> None:
        """Requests/limits and activeDeadlineSeconds survive the wiring change."""
        mock_client, ex = self._make_executor()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0", cpus=4, memory_mb=8192, time_min=240)
        mock_client.create_namespaced_job.assert_called_once()
        job = mock_client.create_namespaced_job.call_args.kwargs["body"]
        container = job.spec.template.spec.containers[0]
        assert container.resources.requests == {"cpu": "4", "memory": "8192Mi"}
        assert container.resources.limits == {"cpu": "4", "memory": "8192Mi"}
        assert job.spec.active_deadline_seconds == 240 * 60
        assert job.spec.backoff_limit == 0

    # ------------------------------------------------------------------
    # Native Job controls (issue #997)
    # ------------------------------------------------------------------
    @staticmethod
    def _submitted_job(mock_client: MagicMock) -> Any:
        """Return the V1Job from the single create_namespaced_job call."""
        mock_client.create_namespaced_job.assert_called_once()
        return mock_client.create_namespaced_job.call_args.kwargs["body"]

    def test_default_manifest_matches_pre_997_byte_identical(self) -> None:
        """Defaults produce a byte-identical manifest to the pre-#997 version.

        Verifies the acceptance criterion: no extra keys on the spec
        (no ``ttl_seconds_after_finished``), no extra keys on the
        metadata (no ``labels``), and ``backoff_limit`` is 0.
        """
        mock_client, ex = self._make_executor_with()  # all defaults
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        job = self._submitted_job(mock_client)
        # Spec: backoff_limit is 0, ttl_seconds_after_finished is absent.
        assert job.spec.backoff_limit == 0
        assert job.spec.ttl_seconds_after_finished is None
        # Metadata: no labels keyword passed.
        assert job.metadata.labels is None
        # Name is set, nothing else.
        assert job.metadata.name == "osimflow-sim-s0"
        # The spec only carries the fields we explicitly set.
        assert isinstance(job.spec.template, object)

    def test_default_manifest_dist_to_pre_1383_is_no_new_keys(self) -> None:
        """Compare the K8s API payload of the default manifest to the
        pre-#1383 baseline.

        The K8s Python client's ``ApiClient.sanitize_for_serialization``
        is what the wire serializer uses to build the JSON payload sent
        to the API server (it strips ``None`` values). This is the
        authoritative, byte-level test that defaults produce the same
        payload as the baseline — only the SecurityContext / runAsUser
        / automountServiceAccountToken keys from issue #1383 are
        allowed to appear under the strict default.
        """
        import json
        from copy import deepcopy

        from kubernetes.client import ApiClient, V1Job, V1JobSpec, V1ObjectMeta

        # --- Post-#1383 manifest from the executor with defaults ---
        mock_client, ex = self._make_executor_with()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        post_job = self._submitted_job(mock_client)

        # --- Pre-#1383 baseline (pre-strict) ---
        # Mirror the post-#997 manifest with the pre-#1383 SecurityContext
        # profile so any structural drift introduced by the strict
        # defaults surfaces as a diff here. Issue #1383 added the strict
        # pod-level ``securityContext``, the container-level
        # ``securityContext``, ``runAsUser``, and
        # ``automountServiceAccountToken``; the baseline reuses the
        # actual values from the executor (not hard-coded mirrors) so
        # the diff cannot be masked by a hard-coded reference drift.
        # Deep-copy so the post-#1383 manifest keeps its SecurityContext
        # keys for the acceptance-criterion assertions below.
        pre_template = deepcopy(post_job.spec.template)
        pre_template.spec.security_context = None
        pre_template.spec.automount_service_account_token = None
        for c in pre_template.spec.containers:
            c.security_context = None

        pre_spec = V1JobSpec(
            template=pre_template,
            backoff_limit=0,
            active_deadline_seconds=post_job.spec.active_deadline_seconds,
        )
        pre_metadata = V1ObjectMeta(name=post_job.metadata.name)
        # We compare the serializer-output leaves, not the live objects.
        api_client = ApiClient()
        post_payload = api_client.sanitize_for_serialization(post_job)
        # Build a pre-#1383 V1Job strawman to serialize identically.
        pre_job = V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=pre_metadata,
            spec=pre_spec,
        )
        pre_payload = api_client.sanitize_for_serialization(pre_job)

        # The pre-#1383 and post-#1383 manifests must agree on every
        # field EXCEPT the strict hardening keys added by #1383. Compare
        # the pre payload against a copy of the post payload with the
        # strict keys stripped, so the test still flags any structural
        # drift introduced outside the documented hardening fields.
        post_minus_strict = deepcopy(post_payload)
        post_minus_strict["spec"]["template"]["spec"].pop("securityContext", None)
        post_minus_strict["spec"]["template"]["spec"].pop("automountServiceAccountToken", None)
        for c in post_minus_strict["spec"]["template"]["spec"]["containers"]:
            c.pop("securityContext", None)
        assert json.dumps(pre_payload, sort_keys=True) == json.dumps(
            post_minus_strict, sort_keys=True
        )

        # Final acceptance-criterion summary: the #997-era fields are
        # absent from the post-#1383 payload (they would appear if a
        # non-default value were set), and the #1383 SecurityContext
        # fields ARE present.
        assert "ttlSecondsAfterFinished" not in post_payload["spec"]
        assert "labels" not in post_payload["metadata"]
        assert "securityContext" in post_payload["spec"]["template"]["spec"]
        assert "automountServiceAccountToken" in post_payload["spec"]["template"]["spec"]
        assert post_payload["spec"]["template"]["spec"]["automountServiceAccountToken"] is False

    def test_backoff_limit_set_on_spec(self) -> None:
        """Non-zero ``backoff_limit`` is reflected on the V1JobSpec."""
        mock_client, ex = self._make_executor_with(backoff_limit=3)
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        job = self._submitted_job(mock_client)
        assert job.spec.backoff_limit == 3
        # Defensive: explicit int type — K8s API rejects strings.
        assert isinstance(job.spec.backoff_limit, int)

    def test_ttl_seconds_after_finished_set_on_spec(self) -> None:
        """``ttl_seconds_after_finished`` is reflected on the V1JobSpec
        when set, and is an int (K8s API rejects non-int values).
        """
        mock_client, ex = self._make_executor_with(ttl_seconds_after_finished=3600)
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        job = self._submitted_job(mock_client)
        assert job.spec.ttl_seconds_after_finished == 3600
        assert isinstance(job.spec.ttl_seconds_after_finished, int)

    def test_queue_name_set_on_metadata_label(self) -> None:
        """A ``queue_name`` is applied as the Kueue label on V1ObjectMeta."""
        mock_client, ex = self._make_executor_with(queue_name="team-a-cpu")
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        job = self._submitted_job(mock_client)
        assert job.metadata.labels == {"kueue.x-k8s.io/queue-name": "team-a-cpu"}

    def test_all_three_native_controls_together(self) -> None:
        """All three fields are independently controllable."""
        mock_client, ex = self._make_executor_with(
            backoff_limit=5,
            ttl_seconds_after_finished=7200,
            queue_name="team-b-gpu",
        )
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        job = self._submitted_job(mock_client)
        assert job.spec.backoff_limit == 5
        assert job.spec.ttl_seconds_after_finished == 7200
        assert job.metadata.labels == {"kueue.x-k8s.io/queue-name": "team-b-gpu"}

    def test_init_coerces_backoff_limit_to_int(self) -> None:
        """The constructor accepts an int (or numerically-coercible) value."""
        ex = KubernetesExecutor(backoff_limit=4)
        assert ex.backoff_limit == 4
        assert isinstance(ex.backoff_limit, int)

    def test_init_coerces_ttl_seconds_to_int(self) -> None:
        """The constructor accepts an int for ttl_seconds_after_finished."""
        ex = KubernetesExecutor(ttl_seconds_after_finished=600)
        assert ex.ttl_seconds_after_finished == 600
        assert isinstance(ex.ttl_seconds_after_finished, int)

    def test_init_defaults_preserve_pre_997_manifest(self) -> None:
        """Default init produces a KubernetesExecutor whose defaults are
        byte-identical to the pre-#997 executor."""
        ex = KubernetesExecutor()
        assert ex.backoff_limit == 0
        assert ex.ttl_seconds_after_finished is None
        assert ex.queue_name is None

    # ------------------------------------------------------------------
    # Pod hardening (issue #1383)
    # ------------------------------------------------------------------
    def test_init_security_context_strict_defaults_to_true(self) -> None:
        """The constructor default for ``security_context_strict`` is True."""
        ex = KubernetesExecutor()
        assert ex.security_context_strict is True

    def test_init_security_context_strict_can_be_disabled(self) -> None:
        """``security_context_strict=False`` is honored and coerced to bool."""
        ex = KubernetesExecutor(security_context_strict=False)
        assert ex.security_context_strict is False
        # Coerced through ``bool(...)`` — passing truthy non-bool
        # values still yields True.
        ex2 = KubernetesExecutor(security_context_strict=1)  # type: ignore[arg-type]
        assert ex2.security_context_strict is True

    @staticmethod
    def _submitted_pod_spec(mock_client: MagicMock) -> Any:
        """Return the V1PodSpec from the single create_namespaced_job call."""
        mock_client.create_namespaced_job.assert_called_once()
        job = mock_client.create_namespaced_job.call_args.kwargs["body"]
        return job.spec.template.spec

    def test_default_security_context_emits_all_strict_fields(self) -> None:
        """Strict default (issue #1383) emits every required hardening field.

        Required fields:
          - runAsNonRoot: true  (both pod-level and container-level)
          - readOnlyRootFilesystem: true
          - allowPrivilegeEscalation: false
          - capabilities.drop: ["ALL"]
          - automountServiceAccountToken: false
          - runAsUser: non-zero (1000)
        """
        mock_client, ex = self._make_executor_with()  # default = strict
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        pod_spec = self._submitted_pod_spec(mock_client)
        container = pod_spec.containers[0]

        # Pod-level V1PodSecurityContext.
        pod_sc = pod_spec.security_context
        assert pod_sc is not None
        assert pod_sc.run_as_non_root is True
        assert pod_sc.run_as_user is not None and pod_sc.run_as_user > 0

        # Container-level V1SecurityContext — all four required flags.
        container_sc = container.security_context
        assert container_sc is not None
        assert container_sc.run_as_non_root is True
        assert container_sc.read_only_root_filesystem is True
        assert container_sc.allow_privilege_escalation is False
        assert container_sc.capabilities is not None
        assert container_sc.capabilities.drop == ["ALL"]

        # automountServiceAccountToken on the V1PodSpec itself.
        assert pod_spec.automount_service_account_token is False

    def test_relaxed_security_context_emits_no_security_context(self) -> None:
        """``security_context_strict=False`` omits all hardening fields.

        Relaxed mode is the documented legacy escape hatch for clusters
        that reject the strict admission profile. The manifest must
        carry no pod-level ``securityContext``, no container-level
        ``securityContext``, and ``automountServiceAccountToken`` is
        left unset (cluster default) so the legacy clusters that
        require the SA token mount keep working.
        """
        mock_client, ex = self._make_executor_with(security_context_strict=False)
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        pod_spec = self._submitted_pod_spec(mock_client)
        container = pod_spec.containers[0]

        # Pod-level security context: not present.
        assert pod_spec.security_context is None
        # Container-level security context: not present.
        assert container.security_context is None
        # automountServiceAccountToken: not present (cluster default).
        # The K8s Python client represents an unset field as the
        # ``sentinel`` singleton; equality with None catches the
        # ``__init__``-not-called branch.
        assert pod_spec.automount_service_account_token is None

    def test_strict_run_as_user_is_nonzero_uid(self) -> None:
        """The strict default pins ``runAsUser`` to a documented non-zero UID.

        Kubernetes rejects UID 0 when ``runAsNonRoot: true`` is set,
        so the strict profile must commit to a specific non-zero UID.
        1000 is the standard first-user UID on the Debian/Ubuntu base
        images used by ``nrel/openstudio``.
        """
        mock_client, ex = self._make_executor_with()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        pod_spec = self._submitted_pod_spec(mock_client)
        pod_sc = pod_spec.security_context
        assert pod_sc is not None
        assert pod_sc.run_as_user == 1000
        assert pod_sc.run_as_user > 0

    def test_strict_payload_serialization_contains_required_keys(self) -> None:
        """The wire-serialized payload carries the #1383 hardening keys.

        This is the byte-level acceptance check: the actual JSON sent
        to the API server contains ``runAsNonRoot``, ``readOnlyRootFilesystem``,
        ``allowPrivilegeEscalation``, ``capabilities.drop: ["ALL"]``,
        ``automountServiceAccountToken: false``, and ``runAsUser: 1000``.
        """
        from kubernetes.client import ApiClient

        mock_client, ex = self._make_executor_with()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        job = self._submitted_job(mock_client)
        payload = ApiClient().sanitize_for_serialization(job)
        pod_spec_payload = payload["spec"]["template"]["spec"]
        container_payload = pod_spec_payload["containers"][0]
        pod_sc_payload = pod_spec_payload["securityContext"]
        container_sc_payload = container_payload["securityContext"]
        caps_payload = container_sc_payload["capabilities"]

        # Pod-level.
        assert pod_sc_payload["runAsNonRoot"] is True
        assert pod_sc_payload["runAsUser"] == 1000
        assert pod_spec_payload["automountServiceAccountToken"] is False
        # Container-level.
        assert container_sc_payload["runAsNonRoot"] is True
        assert container_sc_payload["readOnlyRootFilesystem"] is True
        assert container_sc_payload["allowPrivilegeEscalation"] is False
        assert caps_payload["drop"] == ["ALL"]

    def test_relaxed_payload_serialization_omits_hardening_keys(self) -> None:
        """The wire-serialized payload for the relaxed profile omits
        every #1383 hardening key.

        Mirrors the strict test above; when the legacy escape hatch
        is engaged, the wire payload must look like the pre-#1383
        baseline (no ``securityContext`` anywhere, no
        ``automountServiceAccountToken`` override).
        """
        from kubernetes.client import ApiClient

        mock_client, ex = self._make_executor_with(security_context_strict=False)
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        job = self._submitted_job(mock_client)
        payload = ApiClient().sanitize_for_serialization(job)
        pod_spec_payload = payload["spec"]["template"]["spec"]
        container_payload = pod_spec_payload["containers"][0]

        # sanitize_for_serialization drops None values, so the only
        # safe assertion is that no hardening key is present in the
        # wire payload.
        assert "securityContext" not in pod_spec_payload
        assert "automountServiceAccountToken" not in pod_spec_payload
        assert "securityContext" not in container_payload

    def test_infer_step_name_mapping(self) -> None:
        assert KubernetesExecutor._infer_step_name("apply_s0") == "apply"  # noqa: SLF001
        assert KubernetesExecutor._infer_step_name("sim_s0") == "sim"  # noqa: SLF001
        assert KubernetesExecutor._infer_step_name("kpi_s0") == "extract"  # noqa: SLF001
        assert KubernetesExecutor._infer_step_name("aggregate") == "aggregate"  # noqa: SLF001
        assert KubernetesExecutor._infer_step_name("plots") == "plots"  # noqa: SLF001
        assert KubernetesExecutor._infer_step_name("preflight") == "unknown"  # noqa: SLF001

    def test_name_attribute(self) -> None:
        assert KubernetesExecutor.__new__(KubernetesExecutor).name == "kubernetes"  # noqa: SLF001

    def test_namespace_default(self) -> None:
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex.namespace = "default"
        assert ex.namespace == "default"

    def test_wait_for_terminal_succeeds(self) -> None:
        mock_client = self._make_mock_client()
        mock_pod = MagicMock()
        mock_pod.to_dict.return_value = {"status": {"phase": "Succeeded"}}
        mock_client.list_namespaced_pod.return_value.items = [mock_pod]
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 0.1
        ex.max_poll_interval_s = 1.0
        with patch("osimflow.testing.patch_targets.time.sleep"):
            result = ex._wait_for_terminal("osimflow-test")
        assert result["status"]["phase"] == "Succeeded"

    def test_wait_for_terminal_fails(self) -> None:
        mock_client = self._make_mock_client()
        mock_pod = MagicMock()
        mock_pod.to_dict.return_value = {"status": {"phase": "Failed"}}
        mock_client.list_namespaced_pod.return_value.items = [mock_pod]
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 0.1
        ex.max_poll_interval_s = 1.0
        with patch("osimflow.testing.patch_targets.time.sleep"):
            result = ex._wait_for_terminal("osimflow-test")
        assert result["status"]["phase"] == "Failed"

    def test_get_pod_status_no_pods(self) -> None:
        mock_client = self._make_mock_client()
        mock_client.list_namespaced_pod.return_value.items = []
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        result = ex._get_pod_status("osimflow-test")
        assert result == {"status": {"phase": "Pending"}}

    def test_get_pod_status_with_pods(self) -> None:
        mock_client = self._make_mock_client()
        mock_pod = MagicMock()
        mock_pod.to_dict.return_value = {"status": {"phase": "Running"}}
        mock_client.list_namespaced_pod.return_value.items = [mock_pod]
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        result = ex._get_pod_status("osimflow-test")
        assert result["status"]["phase"] == "Running"

    def test_build_job_name(self) -> None:
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex.namespace = "default"
        assert ex._build_job_name("test") == "osimflow-test"
        assert ex._build_job_name("sample_0") == "osimflow-sample-0"
        assert ex._build_job_name("Heavy_Simulation") == "osimflow-heavy-simulation"

    def test_build_environment(self) -> None:
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex.namespace = "default"
        env = ex._build_environment(
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
        )
        env_dict = {e["name"]: e["value"] for e in env}
        assert env_dict["OSIMFLOW_OS_VERSION"] == "3.11.0"
        assert env_dict["OSIMFLOW_CONTAINER"] == "nrel/openstudio:3.11.0"

    def test_build_environment_defaults(self) -> None:
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex.namespace = "default"
        env = ex._build_environment(container=None, openstudio_version=None)
        env_dict = {e["name"]: e["value"] for e in env}
        assert env_dict["OSIMFLOW_CONTAINER"] == "nrel/openstudio:latest"


class TestKubernetesHandle:
    """_KubernetesHandle polls K8s Job on .result() and .done()."""

    def test_result_returns_none_on_succeeded(self) -> None:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {"status": {"phase": "Succeeded"}}
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        result = handle.result()
        assert result is None

    def test_result_returns_result_hint_on_succeeded(self) -> None:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {"status": {"phase": "Succeeded"}}
        hint = Path("/tmp/osimflow/plots")
        handle = _KubernetesHandle(
            job_name="test",
            executor=mock_ex,
            submit_params={},
            result_hint=hint,
        )
        assert handle.result() == hint

    def test_result_raises_on_failed(self) -> None:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {
            "status": {"phase": "Failed"},
        }
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        with pytest.raises(RuntimeError, match="Failed"):
            handle.result()

    def test_done_true_when_succeeded(self) -> None:
        mock_ex = MagicMock()
        mock_ex._get_pod_status.return_value = {"status": {"phase": "Succeeded"}}
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        assert handle.done() is True

    def test_done_false_when_running(self) -> None:
        mock_ex = MagicMock()
        mock_ex._get_pod_status.return_value = {"status": {"phase": "Running"}}
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        assert handle.done() is False

    def test_worker_fields(self) -> None:
        mock_ex = MagicMock()
        mock_ex.namespace = "default"
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        assert handle.worker_id == "test"
        assert handle.worker_region is None

    def test_extract_failure_reason_terminated(self) -> None:
        mock_ex = MagicMock()
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        pod_status = {
            "status": {
                "containerStatuses": [
                    {
                        "state": {
                            "terminated": {
                                "exitCode": 1,
                                "reason": "NonZeroExit",
                            }
                        }
                    }
                ]
            }
        }
        reason = handle._extract_failure_reason(pod_status)
        assert "exit code 1" in reason

    def test_extract_failure_reason_waiting(self) -> None:
        mock_ex = MagicMock()
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        pod_status = {
            "status": {
                "containerStatuses": [
                    {
                        "state": {
                            "waiting": {
                                "reason": "ImagePullBackOff",
                                "message": "rpc error",
                            }
                        }
                    }
                ]
            }
        }
        reason = handle._extract_failure_reason(pod_status)
        assert "ImagePullBackOff" in reason

    # ------------------------------------------------------------------
    # Object-storage result materialization (issue #996) — mocked backend
    # ------------------------------------------------------------------
    @staticmethod
    def _make_mock_storage() -> MagicMock:
        storage = MagicMock()

        def fake_download(key: str, local: Path) -> None:
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(f"content for {key}")

        storage.download_file.side_effect = fake_download
        return storage

    @staticmethod
    def _succeeded_executor() -> MagicMock:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {"status": {"phase": "Succeeded"}}
        return mock_ex

    def test_result_materializes_object_storage_single_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """object_storage mode downloads the hinted artifact to its local path."""
        storage = self._make_mock_storage()
        monkeypatch.setattr(
            "osimflow.executors.transport.build_result_storage",
            lambda **_kwargs: storage,
        )
        handle = _KubernetesHandle(
            job_name="test",
            executor=self._succeeded_executor(),
            submit_params={},
            result_hint=tmp_path / "out" / "work" / "kpis" / "kpi_s0.json",
            result_transport_mode="object_storage",
            result_storage_backend="s3",
            result_storage_bucket="osimflow-results",
            result_storage_prefix="out",
        )
        result = handle.result()
        assert result == tmp_path / "out" / "work" / "kpis" / "kpi_s0.json"
        storage.download_file.assert_called_once_with(
            "work/kpis/kpi_s0.json",
            tmp_path / "out" / "work" / "kpis" / "kpi_s0.json",
        )
        assert (tmp_path / "out" / "work" / "kpis" / "kpi_s0.json").is_file()

    def test_result_materializes_object_storage_dict_of_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Aggregate-style dict hints materialize every leaf path."""
        storage = self._make_mock_storage()
        monkeypatch.setattr(
            "osimflow.executors.transport.build_result_storage",
            lambda **_kwargs: storage,
        )
        out = tmp_path / "out"
        hint = {
            "csv": out / "aggregated_results.csv",
            "parquet": out / "aggregated_results.parquet",
            "failed": out / "failed_simulations.csv",
        }
        handle = _KubernetesHandle(
            job_name="test",
            executor=self._succeeded_executor(),
            submit_params={},
            result_hint=hint,
            result_transport_mode="object_storage",
            result_storage_backend="s3",
            result_storage_bucket="osimflow-results",
            result_storage_prefix="out",
        )
        result = handle.result()
        assert result == hint
        downloaded_keys = {call.args[0] for call in storage.download_file.call_args_list}
        assert downloaded_keys == {
            "aggregated_results.csv",
            "aggregated_results.parquet",
            "failed_simulations.csv",
        }
        for path in hint.values():
            assert path.is_file()

    def test_result_object_storage_without_backend_returns_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing backend/bucket degrades to hint-only (warning path)."""
        storage = self._make_mock_storage()
        monkeypatch.setattr(
            "osimflow.executors.transport.build_result_storage",
            lambda **_kwargs: storage,
        )
        hint = Path("/tmp/out/work/kpis/kpi_s0.json")
        handle = _KubernetesHandle(
            job_name="test",
            executor=self._succeeded_executor(),
            submit_params={},
            result_hint=hint,
            result_transport_mode="object_storage",
            result_storage_backend=None,
            result_storage_bucket=None,
        )
        assert handle.result() == hint
        storage.download_file.assert_not_called()

    def test_result_shared_fs_decodes_encoded_hint(self) -> None:
        """shared_fs mode decodes tagged path payloads without touching storage."""
        storage = self._make_mock_storage()
        with patch(
            "osimflow.executors.transport.build_result_storage",
            return_value=storage,
        ) as mock_build:
            handle = _KubernetesHandle(
                job_name="test",
                executor=self._succeeded_executor(),
                submit_params={},
                result_hint={
                    "__osimflow_type__": "path",
                    "value": "/campaign/out/work/sim/s0",
                },
                result_transport_mode="shared_fs",
            )
            result = handle.result()
        assert result == Path("/campaign/out/work/sim/s0")
        mock_build.assert_not_called()

    def test_result_failure_surfaces_exit_code(self) -> None:
        """A Failed pod re-raises with the extracted exit-code reason."""
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {
            "status": {
                "phase": "Failed",
                "containerStatuses": [
                    {
                        "state": {
                            "terminated": {
                                "exitCode": 3,
                                "reason": "NonZeroExit",
                            }
                        }
                    }
                ],
            },
        }
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        with pytest.raises(RuntimeError, match="exit code 3"):
            handle.result()
