"""Shared transport/result helpers for remote executors.

Defines a small executor-agnostic contract for result references so
remote handles can return stable callback-facing values.

Executor participation matrix (issue #1333)
-------------------------------------------

``result_transport_mode="object_storage"`` must behave identically on
every remote executor: the handle resolves the result hint through
:func:`resolve_result_for_callback` and then downloads artifacts via
:func:`materialize_object_storage_result` so Campaign callbacks receive
**local paths**, never object-storage keys.

Transport capability matrix (issue #1473)
-----------------------------------------

:data:`TRANSPORT_CAPABILITIES` is the single source of truth for which
executors support which result-transport modes.  ``"auto"`` and
``"shared_fs"`` are universally supported (results return in-band via
the handle's future — trivially true for in-process, submitit, and
shared-filesystem executors alike).  ``"object_storage"`` requires a
handle-side resolve + materialize implementation.

======================  ==================================================
Executor                Declared transport modes
======================  ==================================================
``local``               ``auto``, ``shared_fs``
``slurm``               ``auto``, ``shared_fs``
``dask_jobqueue``       ``auto``, ``shared_fs``
``aws_batch``           ``auto``, ``shared_fs``, ``object_storage``
``azure_batch``         ``auto``, ``shared_fs``, ``object_storage``
``google_batch``        ``auto``, ``shared_fs``, ``object_storage``
``kubernetes``          ``auto``, ``shared_fs``, ``object_storage``
``nomad``               ``auto``, ``shared_fs``, ``object_storage``
``pbs``                 ``auto``, ``shared_fs``, ``object_storage``
``docker_swarm``        ``auto``, ``shared_fs``, ``object_storage``
(other / plugins)       ``auto``, ``shared_fs`` (in-band default)
======================  ==================================================

:func:`validate_transport_mode` enforces the matrix at submit time
(``BaseExecutor.submit_request`` plus the in-band executors' ``submit``
paths) so an unsupported ``object_storage`` configuration raises a
clear :class:`ValueError` instead of being silently discarded
(issue #1473).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from osimflow.storage import build_result_storage

type ResultTransportMode = Literal["auto", "shared_fs", "object_storage"]

#: Modes available to every executor (issue #1473).  ``"auto"`` is the
#: executor default; ``"shared_fs"`` means results come back in-band via
#: the handle's future, which every executor satisfies by construction.
IN_BAND_TRANSPORT_MODES: frozenset[ResultTransportMode] = frozenset({"auto", "shared_fs"})

#: Modes that additionally ship results through object storage and
#: materialize them back to local paths in the handle (resolve +
#: materialize, issue #1333).
OBJECT_STORAGE_TRANSPORT_MODES: frozenset[ResultTransportMode] = IN_BAND_TRANSPORT_MODES | {
    "object_storage"
}

#: Per-executor transport capability matrix (issue #1473).  Executors
#: not listed here (including third-party plugin executors) default to
#: :data:`IN_BAND_TRANSPORT_MODES`.
TRANSPORT_CAPABILITIES: dict[str, frozenset[ResultTransportMode]] = {
    "local": IN_BAND_TRANSPORT_MODES,
    "slurm": IN_BAND_TRANSPORT_MODES,
    "dask_jobqueue": IN_BAND_TRANSPORT_MODES,
    "aws_batch": OBJECT_STORAGE_TRANSPORT_MODES,
    "azure_batch": OBJECT_STORAGE_TRANSPORT_MODES,
    "google_batch": OBJECT_STORAGE_TRANSPORT_MODES,
    "kubernetes": OBJECT_STORAGE_TRANSPORT_MODES,
    "nomad": OBJECT_STORAGE_TRANSPORT_MODES,
    "pbs": OBJECT_STORAGE_TRANSPORT_MODES,
    "docker_swarm": OBJECT_STORAGE_TRANSPORT_MODES,
}

#: Capability set applied to any executor name missing from
#: :data:`TRANSPORT_CAPABILITIES`.
DEFAULT_TRANSPORT_CAPABILITIES: frozenset[ResultTransportMode] = IN_BAND_TRANSPORT_MODES

#: Raw strings that :func:`coerce_transport_mode` recognizes.
_RECOGNIZED_MODE_STRINGS: frozenset[str] = frozenset(
    {
        "",
        "auto",
        "shared_fs",
        "shared-fs",
        "sharedfs",
        "object_storage",
        "object-storage",
        "objectstorage",
    }
)

_PATH_MARKER_KEY = "__osimflow_type__"
_PATH_MARKER_VALUE = "path"
_PATH_VALUE_KEY = "value"

log = logging.getLogger("osimflow.executors.transport")


def coerce_transport_mode(raw: str | None) -> ResultTransportMode:
    """Normalize raw transport mode strings to the contract literal set."""
    if raw is None:
        return "auto"
    normalized = raw.strip().lower()
    if normalized in {"shared_fs", "shared-fs", "sharedfs"}:
        return "shared_fs"
    if normalized in {"object_storage", "object-storage", "objectstorage"}:
        return "object_storage"
    return "auto"


def validate_transport_mode(executor_name: str, mode: str | None) -> ResultTransportMode:
    """Validate *mode* against the executor's declared capability set.

    Raises
    ------
    ValueError
        If *mode* is not a recognized transport mode string, or if the
        coerced mode is not in the executor's capability set from
        :data:`TRANSPORT_CAPABILITIES`.  This replaces the historic
        silent-discard behaviour where e.g. LocalExecutor accepted
        ``object_storage`` and then ignored it (issue #1473).
    """
    if mode is not None and mode.strip().lower() not in _RECOGNIZED_MODE_STRINGS:
        raise ValueError(
            f"unknown result_transport_mode={mode!r} for executor {executor_name!r}; "
            f"expected one of 'auto', 'shared_fs', 'object_storage'"
        )
    normalized = coerce_transport_mode(mode)
    capabilities = TRANSPORT_CAPABILITIES.get(executor_name, DEFAULT_TRANSPORT_CAPABILITIES)
    if normalized not in capabilities:
        raise ValueError(
            f"executor {executor_name!r} does not support result_transport_mode="
            f"{normalized!r}; supported modes: "
            f"{sorted(capabilities)}. Use one of the object-storage-capable "
            "executors (aws_batch, azure_batch, google_batch, kubernetes, "
            "nomad, pbs, docker_swarm) or an in-band mode ('auto', "
            "'shared_fs')."
        )
    return normalized


def encode_transport_value(value: Any) -> Any:  # noqa: ANN401
    """Encode Python values for transport-safe JSON payloads."""
    if isinstance(value, Path):
        return {_PATH_MARKER_KEY: _PATH_MARKER_VALUE, _PATH_VALUE_KEY: str(value)}
    if isinstance(value, dict):
        return {str(k): encode_transport_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [encode_transport_value(v) for v in value]
    if isinstance(value, tuple):
        return [encode_transport_value(v) for v in value]
    return value


def decode_transport_value(value: Any) -> Any:  # noqa: ANN401
    """Decode transport payload values to callback-facing Python objects."""
    if isinstance(value, dict):
        marker = value.get(_PATH_MARKER_KEY)
        if marker == _PATH_MARKER_VALUE:
            raw = value.get(_PATH_VALUE_KEY)
            return Path(str(raw))
        return {str(k): decode_transport_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_transport_value(v) for v in value]
    return value


def resolve_result_for_callback(
    result_hint: Any,  # noqa: ANN401
    *,
    default: Any = None,  # noqa: ANN401
    transport_mode: str = "auto",
) -> Any:  # noqa: ANN401
    """Return callback-facing result value for remote handles.

    If ``result_hint`` is set, it is decoded through the transport value
    decoder to normalize tagged path payloads. Otherwise ``default`` is
    returned to preserve existing executor behavior.

    When ``transport_mode`` is ``"object_storage"``, the result hint
    contains placeholder paths that will be resolved by
    ``materialize_object_storage_result``; no decoding is applied in that
    case to avoid double-translating the payload.
    """
    if result_hint is None:
        return default
    mode = coerce_transport_mode(transport_mode)
    if mode == "object_storage":
        return result_hint
    return decode_transport_value(result_hint)


def local_path_to_storage_key(path: Path, root_marker: str | None) -> str:
    """Convert a local callback path to a storage-relative remote key."""
    marker = (root_marker or "").strip("/")
    if marker:
        parts = list(path.parts)
        for idx, part in enumerate(parts):
            if part == marker:
                remainder = parts[idx + 1 :]
                if remainder:
                    return Path(*remainder).as_posix()
                break
    return path.name


def _collect_path_leaves(value: Any) -> list[Path]:  # noqa: ANN401
    if isinstance(value, Path):
        return [value]
    if isinstance(value, dict):
        out: list[Path] = []
        for nested in value.values():
            out.extend(_collect_path_leaves(nested))
        return out
    if isinstance(value, list):
        out = []
        for nested in value:
            out.extend(_collect_path_leaves(nested))
        return out
    return []


def _download_directory(*, path: Path, remote_prefix: str, storage: Any) -> None:  # noqa: ANN401
    prefix = remote_prefix.rstrip("/")
    objects = storage.list_results(prefix)
    if not objects:
        raise FileNotFoundError(f"no objects found for remote directory prefix={prefix!r}")

    local_base = prefix + "/"
    for obj in objects:
        if obj == prefix:
            continue
        if not obj.startswith(local_base):
            continue
        rel = obj[len(local_base) :]
        if not rel:
            continue
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        storage.download_file(obj, target)


def materialize_object_storage_result(
    callback_result: Any,  # noqa: ANN401
    *,
    transport_mode: str,
    result_storage_backend: str | None,
    result_storage_bucket: str | None,
    result_storage_prefix: str | None,
    result_storage_endpoint: str | None = None,
    allow_insecure_storage_endpoint: bool = False,
) -> Any:  # noqa: ANN401
    """Download object-storage artifacts so callbacks can consume local paths."""
    mode = coerce_transport_mode(transport_mode)
    if mode != "object_storage":
        return callback_result
    if callback_result is None:
        return callback_result
    if not result_storage_backend or not result_storage_bucket:
        log.warning(
            "object_storage transport requested but backend/bucket missing; returning hints only"
        )
        return callback_result

    storage = build_result_storage(
        backend=result_storage_backend,
        bucket=result_storage_bucket,
        prefix=str(result_storage_prefix or ""),
        endpoint_url=result_storage_endpoint,
        allow_insecure_endpoint=allow_insecure_storage_endpoint,
    )

    downloaded: set[str] = set()
    root_marker = str(result_storage_prefix or "")
    for path in _collect_path_leaves(callback_result):
        key = local_path_to_storage_key(path, root_marker)
        if not key:
            continue
        if str(path) in downloaded:
            continue
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            storage.download_file(key, path)
            downloaded.add(str(path))
            continue
        path.mkdir(parents=True, exist_ok=True)
        _download_directory(path=path, remote_prefix=key, storage=storage)
        downloaded.add(str(path))

    return callback_result
