"""Remote step runner for Nomad-dispatched fan-out work.

This module is intentionally small and stdlib-only so it can run in
remote containers without requiring extra orchestration dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .executors.transport import (
    coerce_transport_mode,
    decode_transport_value,
    encode_transport_value,
    local_path_to_storage_key,
)
from .storage import build_result_storage
from .task_payload_hmac import (
    TASK_PAYLOAD_SIG_ENV,
    TASK_PAYLOAD_SIG_META_KEY,
    resolve_payload_secret,
    verify_task_payload,
)
from .work import (
    aggregate_results,
    default_apply_parameters,
    extract_kpis,
    generate_plots,
    run_openstudio_sim,
)

log = logging.getLogger("osimflow.remote_runner")


def _decode_payload_value(value: Any) -> Any:  # noqa: ANN401
    return decode_transport_value(value)


def _verify_payload_signature(raw: str) -> None:
    """Verify the HMAC-SHA256 signature over the raw payload (issue #1177).

    Fail closed: when a shared secret is configured
    (``OSIMFLOW_TASK_PAYLOAD_SECRET`` / ``NOMAD_META_task_payload_secret``)
    a missing or tampered ``OSIMFLOW_TASK_PAYLOAD_SIG`` raises
    ``RuntimeError`` *before* the payload is decoded or executed.

    Rejected unsigned: when no secret is configured the payload is rejected
    (issue #1205) because any process that can read/modify this job's
    environment (e.g. via ``nomad alloc status`` or ``kubectl exec``)
    can inject arbitrary step calls. Set
    ``OSIMFLOW_TASK_PAYLOAD_SECRET`` on the orchestrator to enable
    signature enforcement.
    """
    secret = resolve_payload_secret()
    if not secret:
        raise RuntimeError(
            "OSIMFLOW_TASK_PAYLOAD_SECRET is not configured — "
            "refusing to execute unsigned task payload. "
            "Configure OSIMFLOW_TASK_PAYLOAD_SECRET on the orchestrator "
            "to enable HMAC-SHA256 verification (issue #1205)."
        )
    signature = _get_env_or_nomad_meta(
        env_key=TASK_PAYLOAD_SIG_ENV,
        meta_key=TASK_PAYLOAD_SIG_META_KEY,
    )
    if not verify_task_payload(raw, signature, secret):
        raise RuntimeError(
            "OSIMFLOW_TASK_PAYLOAD_SIG verification failed: task payload "
            "signature is missing or tampered with — refusing to execute "
            "(issue #1177)"
        )
    log.info("task payload signature verified (HMAC-SHA256)")


def _load_payload() -> dict[str, Any]:
    raw = os.environ.get("OSIMFLOW_TASK_PAYLOAD")
    if raw is None:
        # Nomad exposes dispatch meta vars as NOMAD_META_<meta_key>.
        raw = os.environ.get("NOMAD_META_task_payload")  # noqa: SIM112
    if raw is None or raw.strip() == "":
        raise RuntimeError("missing task payload: OSIMFLOW_TASK_PAYLOAD/NOMAD_META_task_payload")
    # Issue #1177: verify the signature over the exact raw payload bytes
    # BEFORE decoding (json.loads) or executing anything.
    _verify_payload_signature(raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid OSIMFLOW task payload JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("invalid OSIMFLOW task payload: expected object")
    return payload


def _resolve_step_fn(step: str) -> Any:  # noqa: ANN401
    if step == "apply":
        return default_apply_parameters
    if step == "sim":
        return run_openstudio_sim
    if step == "extract":
        return extract_kpis
    if step == "aggregate":
        return aggregate_results
    if step == "plots":
        return generate_plots
    raise RuntimeError(f"unsupported remote runner step: {step}")


def _run_payload(payload: dict[str, Any]) -> Any:  # noqa: ANN401
    step = str(payload.get("step", "unknown"))
    fn = _resolve_step_fn(step)
    args_raw = payload.get("args", [])
    kwargs_raw = payload.get("kwargs", {})
    if not isinstance(args_raw, list):
        raise RuntimeError("invalid task payload: args must be a list")
    if not isinstance(kwargs_raw, dict):
        raise RuntimeError("invalid task payload: kwargs must be an object")

    args = [_decode_payload_value(v) for v in args_raw]
    kwargs = {str(k): _decode_payload_value(v) for k, v in kwargs_raw.items()}
    return fn(*args, **kwargs)


def _get_env_or_nomad_meta(*, env_key: str, meta_key: str) -> str | None:
    value = os.environ.get(env_key)
    if value is not None:
        return value
    return os.environ.get(f"NOMAD_META_{meta_key}")


def _collect_paths(value: Any) -> list[Path]:  # noqa: ANN401
    if isinstance(value, Path):
        return [value]
    if isinstance(value, dict):
        out: list[Path] = []
        for nested in value.values():
            out.extend(_collect_paths(nested))
        return out
    if isinstance(value, list):
        out = []
        for nested in value:
            out.extend(_collect_paths(nested))
        return out
    return []


def _upload_artifacts_for_object_storage(result: Any) -> None:  # noqa: ANN401
    mode = coerce_transport_mode(
        _get_env_or_nomad_meta(
            env_key="OSIMFLOW_RESULT_TRANSPORT_MODE",
            meta_key="result_transport_mode",
        )
    )
    if mode != "object_storage":
        return

    backend = _get_env_or_nomad_meta(
        env_key="OSIMFLOW_RESULT_STORAGE_BACKEND",
        meta_key="result_storage_backend",
    )
    bucket = _get_env_or_nomad_meta(
        env_key="OSIMFLOW_RESULT_STORAGE_BUCKET",
        meta_key="result_storage_bucket",
    )
    prefix = _get_env_or_nomad_meta(
        env_key="OSIMFLOW_RESULT_STORAGE_PREFIX",
        meta_key="result_storage_prefix",
    )
    endpoint = _get_env_or_nomad_meta(
        env_key="OSIMFLOW_RESULT_STORAGE_ENDPOINT",
        meta_key="result_storage_endpoint",
    )

    if not backend or not bucket:
        raise RuntimeError(
            "result transport mode object_storage requires result storage backend and bucket metadata"
        )

    storage = build_result_storage(
        backend=backend,
        bucket=bucket,
        prefix=str(prefix or ""),
        endpoint_url=endpoint,
    )

    uploaded: set[str] = set()
    for path in _collect_paths(result):
        key = local_path_to_storage_key(path, prefix)
        if not key or str(path) in uploaded:
            continue
        if path.is_file():
            storage.upload_file(path, key)
            uploaded.add(str(path))
            continue
        if path.is_dir():
            storage.upload_dir(path, key)
            uploaded.add(str(path))
            continue
        log.warning("object-storage upload skipped missing path: %s", path)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        payload = _load_payload()
        result = _run_payload(payload)
        _upload_artifacts_for_object_storage(result)
        result_json = json.dumps({"ok": True, "result": encode_transport_value(result)})
        print(result_json)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("remote runner failed")
        error_json = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        print(error_json, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
