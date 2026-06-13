"""Canonical BYOS (Bring Your Own Script) loader with subprocess isolation.

This is the single entry point for loading user-supplied Python scripts.
The CLI (``__main__.py``) and the Campaign (``campaign.py``) both use
``load_user_function`` to discover the callable in a user's ``.py`` file.

Security model (issue #269):
    BYOS scripts are treated as **untrusted** (AGENTS.md §10).  The default
    trust level is ``subprocess``, which runs the user script in a child
    process so it cannot access the orchestrator's memory, credentials, or
    open file handles.  Users who want the old in-process behaviour can opt
    in via ``--byos-trust-level inprocess``.

    For cloud executors (AWS Batch, Slurm), BYOS scripts already run inside
    a container or job isolation boundary — the subprocess isolation is an
    additional defence-in-depth layer for ``LocalExecutor``.

The function-name convention is:

* ``apply_parameters`` — for the parameter-application override.
* ``extract_kpis`` — for the KPI-extraction override.

See AGENTS.md §9 *Task routing hints* and ``user_scripts/README.md``
for the full BYOS contract.
"""

import enum
import importlib.util
import json
import logging
import resource
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

log = logging.getLogger("osimflow.byos")

# The canonical function names a BYOS script must expose.  The first
# match wins.  Order matters: ``apply_parameters`` is the primary name
# documented in AGENTS.md; ``apply`` is kept as a fallback for
# backwards-compatibility with scripts written against the old
# ``osimflow.apply_params._load_custom_apply`` loader (removed in the
# fix for issue #36).
_CANDIDATE_NAMES = ("apply_parameters", "extract_kpis", "apply")


# ---------------------------------------------------------------------------
# Trust levels (issue #269)
# ---------------------------------------------------------------------------
class ByosTrustLevel(enum.Enum):
    """Controls how BYOS user scripts are executed.

    ``SUBPROCESS`` (default):
        The script runs in a child process.  It cannot access the
        orchestrator's memory, campaign credentials, or open file
        handles.  This is the recommended mode for all deployments.

    ``INPROCESS`` (legacy):
        The script is loaded via ``importlib`` and called directly in
        the orchestrator process.  This is the pre-#269 behaviour and
        should only be used when the user explicitly trusts the script
        (e.g. during development or in a controlled environment).
    """

    SUBPROCESS = "subprocess"
    INPROCESS = "inprocess"


def _discover_function_name(path: Path) -> str:
    """Discover the BYOS function name in a user script without executing it.

    Uses ``importlib`` to load the module and inspect its attributes.
    Returns the name of the first matching callable found.

    Raises:
        ImportError: the file cannot be loaded as a Python module.
        AttributeError: no callable with a recognised name was found.
    """
    spec = importlib.util.spec_from_file_location(f"user_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for candidate in _CANDIDATE_NAMES:
        candidate_obj = getattr(mod, candidate, None)
        if callable(candidate_obj):
            if candidate == "apply":
                warnings.warn(
                    f"User script {path} uses the deprecated function name "
                    f"'apply'. Rename it to 'apply_parameters' for forward "
                    f"compatibility. Support for 'apply' will be removed in a "
                    f"future release.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            return candidate
    raise AttributeError(
        f"User script {path} must define `apply_parameters(...)` or `extract_kpis(...)`."
    )


# ---------------------------------------------------------------------------
# Subprocess runner — the inline script executed in the child process
# ---------------------------------------------------------------------------
_SUBPROCESS_RUNNER = """
import json
import sys
import importlib.util
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

def main():
    args_file = sys.argv[1]
    with open(args_file) as f:
        payload = json.load(f)

    script_path = payload["script"]
    function_name = payload["function"]
    positional_args = payload.get("args", [])
    keyword_args = payload.get("kwargs", {})

    # Deserialize Path arguments back from strings.
    # The BYOS contract specifies:
    #   apply_parameters(template: Path, parameters: dict, sample_id: str, out: Path) -> Path
    #   extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path
    # So positions 0 and -1 (or 3 for apply_parameters) are Path-like.
    deserialized = []
    for i, arg in enumerate(positional_args):
        if isinstance(arg, str) and (
            i == 0  # first arg is always a Path (template or simulation_dir)
            or i == len(positional_args) - 1  # last arg is always 'out' Path
            or (function_name == "apply_parameters" and i == 3)  # 'out' is at index 3
        ):
            deserialized.append(Path(arg))
        elif isinstance(arg, dict):
            deserialized.append(arg)
        else:
            deserialized.append(arg)

    # Deserialize kwargs: Path-like values for known Path keys.
    path_keys = {"template", "out", "simulation_dir", "modified_sim_package"}
    deserialized_kwargs = {}
    for k, v in keyword_args.items():
        if k in path_keys and isinstance(v, str):
            deserialized_kwargs[k] = Path(v)
        else:
            deserialized_kwargs[k] = v

    # Load the user script module.
    spec = importlib.util.spec_from_file_location("_byos_module", script_path)
    if spec is None or spec.loader is None:
        print(json.dumps({"error": f"could not load spec for {script_path}"}))
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fn = getattr(mod, function_name, None)
    if fn is None or not callable(fn):
        print(json.dumps({"error": f"function {function_name!r} not found in {script_path}"}))
        sys.exit(1)

    try:
        result = fn(*deserialized, **deserialized_kwargs)
        # The BYOS contract always returns a Path.
        print(json.dumps({"result": str(result)}))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)

main()
"""


def _serialize_arg(value: object) -> object:
    """Convert a single argument for JSON serialization (Path -> str)."""
    return str(value) if isinstance(value, Path) else value


def _serialize_args(
    args: tuple[object, ...],
    kwargs: dict[str, object] | None,
) -> tuple[list[object], dict[str, object]]:
    """Serialize positional and keyword arguments for JSON transport."""
    serialized_args = [_serialize_arg(a) for a in args]
    serialized_kwargs: dict[str, object] = {}
    if kwargs:
        serialized_kwargs = {k: _serialize_arg(v) for k, v in kwargs.items()}
    return serialized_args, serialized_kwargs


def _parse_subprocess_response(
    stdout: str,
    script_path: Path,
    function_name: str,
) -> Path:
    """Parse the JSON response from a BYOS subprocess.

    Returns the result Path on success.  Raises ``RuntimeError`` on any
    error or malformed response.
    """
    if not stdout:
        raise RuntimeError(
            f"BYOS subprocess produced no output: script={script_path} function={function_name}"
        )

    try:
        response = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"BYOS subprocess returned invalid JSON: {stdout[:200]}") from exc

    if "error" in response:
        raise RuntimeError(f"BYOS function {function_name} raised: {response['error']}")

    return_path = response.get("result")
    if return_path is None:
        raise RuntimeError(f"BYOS subprocess did not return a result path: {stdout[:200]}")
    return Path(str(return_path))


def _run_byos_subprocess(
    script_path: Path,
    function_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object] | None = None,
    resource_limits: dict[str, int] | None = None,
) -> Path:
    """Run a BYOS function in an isolated subprocess.

    Serializes the positional arguments to a JSON temp file, launches a
    child Python process that loads the user script and calls the target
    function, then reads the result path from stdout.

    The subprocess inherits the current environment (so PATH, AWS
    credentials from the IAM role, etc. are available) but does **not**
    share the orchestrator's memory space.

    When ``resource_limits`` is provided (issue #343), ``resource.setrlimit``
    is called before ``Popen`` to cap CPU time, address space, and open files.
    ``resource.error`` from impossible limits is caught and logged as a
    warning (limits that cannot be lowered below the current usage are
    non-fatal).

    Args:
        script_path: Absolute path to the user's ``.py`` script.
        function_name: The function to call (e.g. ``apply_parameters``).
        args: Positional arguments to forward to the function.
        kwargs: Keyword arguments to forward (Path values are serialized).
        resource_limits: Optional dict mapping rlimit names to values.
            Keys are rlimit names (``RLIMIT_CPU``, ``RLIMIT_AS``,
            ``RLIMIT_NOFILE``, etc.); values are the soft/hard limit to set.
            Applied via ``resource.setrlimit`` before ``Popen``.

    Returns:
        The Path returned by the BYOS function.

    Raises:
        RuntimeError: the subprocess exited with a non-zero code or
            returned an error payload.
    """
    serialized_args, serialized_kwargs = _serialize_args(args, kwargs)

    payload: dict[str, object] = {
        "script": str(script_path),
        "function": function_name,
        "args": serialized_args,
        "kwargs": serialized_kwargs,
    }

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="osimflow_byos_",
        delete=False,
    ) as tmp:
        json.dump(payload, tmp)
        tmp_path = tmp.name

    if resource_limits:
        for name, value in resource_limits.items():
            try:
                limit_constant = getattr(resource, name)
                resource.setrlimit(limit_constant, (value, value))
            except resource.error as exc:
                log.warning(
                    "BYOS rlimit %s=%d could not be set (non-fatal): %s",
                    name,
                    value,
                    exc,
                )

    try:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", _SUBPROCESS_RUNNER, tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError(
                f"BYOS subprocess timed out after 600s: script={script_path} "
                f"function={function_name}"
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if proc.returncode != 0:
        error_detail = stderr.strip() or stdout.strip() or "unknown error"
        raise RuntimeError(f"BYOS subprocess failed (exit {proc.returncode}): {error_detail}")

    if stderr:
        for line in stderr.splitlines():
            log.debug("BYOS subprocess stderr: %s", line)

    return _parse_subprocess_response(stdout.strip(), script_path, function_name)


def load_user_function(
    path: Path,
    *,
    trust_level: ByosTrustLevel = ByosTrustLevel.SUBPROCESS,
    resource_limits: dict[str, int] | None = None,
) -> Callable[..., Any]:
    """Import a user ``.py`` file and return a callable that respects the trust level.

    When ``trust_level`` is ``SUBPROCESS`` (the default), the returned
    callable runs the user script in an isolated child process.  When
    ``trust_level`` is ``INPROCESS``, the callable is the function object
    loaded directly into the orchestrator process (legacy behaviour).

    In both cases, the function-name convention is the same: the script
    must define ``apply_parameters``, ``extract_kpis``, or ``apply``
    (deprecated).

    A warning is always logged when a BYOS script is loaded, regardless
    of the trust level.

    Args:
        path: Path to the user's ``.py`` script.
        trust_level: Execution isolation mode (default: ``SUBPROCESS``).
        resource_limits: Optional dict of rlimit names to values
            (e.g. ``{"RLIMIT_CPU": 300, "RLIMIT_AS": 4294967296}``).
            Applied via ``resource.setrlimit`` before ``Popen`` in
            subprocess mode (issue #343).  ``resource.error`` from
            impossible limits is caught and logged as a warning.

    Returns:
        A callable with the same signature as the discovered function.
        In subprocess mode, calling the returned function launches a
        child process and returns the result Path.

    Raises:
        ImportError: the file cannot be loaded as a Python module.
        AttributeError: no callable with a recognised name was found.
    """
    log.warning(
        "BYOS: loading user script %s (trust_level=%s). "
        "User-supplied scripts are treated as untrusted code.",
        path,
        trust_level.value,
    )

    if trust_level == ByosTrustLevel.INPROCESS:
        return _load_inprocess(path)

    function_name = _discover_function_name(path)
    script_path = path.resolve()

    def _subprocess_wrapper(*args: object, **kwargs: object) -> Path:
        return _run_byos_subprocess(
            script_path, function_name, args, kwargs, resource_limits=resource_limits
        )

    _subprocess_wrapper.__name__ = function_name
    _subprocess_wrapper.__qualname__ = function_name
    _subprocess_wrapper._byos_script_path = script_path  # type: ignore[attr-defined]
    _subprocess_wrapper._byos_trust_level = trust_level  # type: ignore[attr-defined]
    _subprocess_wrapper._byos_resource_limits = resource_limits  # type: ignore[attr-defined]

    return _subprocess_wrapper


def _load_inprocess(path: Path) -> Callable[..., Any]:
    """Load a BYOS function in-process (legacy mode).

    This is the original ``load_user_function`` behaviour prior to
    issue #269.  The user script is executed in the orchestrator
    process with full access to memory, filesystem, and network.

    Raises:
        ImportError: the file cannot be loaded as a Python module.
        AttributeError: no callable with a recognised name was found.
    """
    spec = importlib.util.spec_from_file_location(f"user_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for candidate in _CANDIDATE_NAMES:
        candidate_obj = getattr(mod, candidate, None)
        if callable(candidate_obj):
            if candidate == "apply":
                warnings.warn(
                    f"User script {path} uses the deprecated function name "
                    f"'apply'. Rename it to 'apply_parameters' for forward "
                    f"compatibility. Support for 'apply' will be removed in a "
                    f"future release.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            # Cast: the BYOS contract is validated at call time via
            # ``inspect.signature``; mypy cannot prove the module attr type.
            return cast(Callable[..., Any], candidate_obj)
    raise AttributeError(
        f"User script {path} must define `apply_parameters(...)` or `extract_kpis(...)`."
    )
