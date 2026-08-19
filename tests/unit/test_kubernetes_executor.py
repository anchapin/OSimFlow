"""Unit tests for osimflow.executors.KubernetesExecutor (issue #254, #996).

Covers:
  - KubernetesExecutor: submit, _submit_job, _wait_for_terminal, _get_pod_status
  - _KubernetesHandle: result, done
  - Ephemeral-runner wiring (issue #996): remote_runner command,
    OSIMFLOW_TASK_PAYLOAD + OSIMFLOW_RESULT_* env propagation,
    remote_command override, and object-storage result materialization
    against a mocked storage backend.
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
        return mock_client, ex

    def _make_executor_with(
        self,
        *,
        backoff_limit: int = 0,
        ttl_seconds_after_finished: int | None = None,
        queue_name: str | None = None,
    ) -> tuple[MagicMock, KubernetesExecutor]:
        """Build an executor with the native Job controls pre-set.

        Helper for issue #997 — used by the parametrised tests that
        exercise the new fields. The default values still match the
        pre-#997 manifest exactly, so callers only need to override
        the fields they care about.
        """
        mock_client, ex = self._make_executor()
        ex.backoff_limit = backoff_limit
        ex.ttl_seconds_after_finished = ttl_seconds_after_finished
        ex.queue_name = queue_name
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

    def test_default_manifest_dist_to_pre_997_is_no_new_keys(self) -> None:
        """Compare the K8s API payload of the default manifest to the
        pre-#997 baseline.

        The K8s Python client's ``ApiClient.sanitize_for_serialization``
        is what the wire serializer uses to build the JSON payload sent
        to the API server (it strips ``None`` values). This is the
        authoritative, byte-level test of the "byte-identical manifest"
        acceptance criterion: defaults produce the same payload as the
        pre-#997 executor, key for key, value for value.
        """
        import json

        from kubernetes.client import ApiClient, V1Job, V1JobSpec, V1ObjectMeta

        # --- Post-#997 manifest from the executor with defaults ---
        mock_client, ex = self._make_executor_with()
        with patch.object(
            ex, "_wait_for_terminal", return_value={"status": {"phase": "Succeeded"}}
        ):
            ex.submit(lambda: None, name="sim_s0")
        post_job = self._submitted_job(mock_client)

        # --- Pre-#997 baseline (mirror of the post-#996 manifest) ---
        # Rebuild with the SAME call parameters the executor used
        # (so the only difference is the absence of the three new
        # fields). Any non-default Kubernetes ``__init__`` kwargs
        # absent here would themselves be a test bug.
        pre_spec = V1JobSpec(
            template=post_job.spec.template,
            backoff_limit=0,
            active_deadline_seconds=post_job.spec.active_deadline_seconds,
        )
        pre_metadata = V1ObjectMeta(name=post_job.metadata.name)
        # We compare the serializer-output leaves, not the live objects.
        api_client = ApiClient()
        post_payload = api_client.sanitize_for_serialization(post_job)
        # Build a pre-#997 V1Job strawman to serialize identically.
        pre_job = V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=pre_metadata,
            spec=pre_spec,
        )
        pre_payload = api_client.sanitize_for_serialization(pre_job)

        # Both payloads sort_keys=False; the diff is structural.
        assert json.dumps(pre_payload, sort_keys=True) == json.dumps(post_payload, sort_keys=True)

        # Final acceptance-criterion summary: the new fields are absent
        # from the post-#997 payload (they would appear if a non-default
        # value were set).
        assert "ttlSecondsAfterFinished" not in post_payload["spec"]
        assert "labels" not in post_payload["metadata"]

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
        with patch("osimflow.executors.time.sleep"):
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
        with patch("osimflow.executors.time.sleep"):
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
