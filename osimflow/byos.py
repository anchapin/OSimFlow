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
import inspect
import json
import logging
import os
import resource
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from osimflow._byos_runner_generated import _SUBPROCESS_RUNNER
from osimflow.byos_contract import _BYOS_CONTRACT

log = logging.getLogger("osimflow.byos")


class ByosContractError(Exception):
    """Raised when a BYOS user function does not match the documented contract.

    Issue #1048: AGENTS.md §6 / §10 promises that BYOS user functions are
    loaded via ``importlib.util`` AND signature-validated with
    ``inspect.signature``. Previously only the first half of that promise
    was kept; this exception surfaces signature mismatches at load time
    instead of silently accepting wrong-arity functions and failing late
    in the campaign run with a confusing ``TypeError`` from the orchestrator.
    """


# Re-export for backwards compatibility (issue #1061: the table moved to
# ``osimflow.byos_contract``; the original import path is preserved so
# downstream callers continue to work).
__all__ = [
    "ByosContractError",
    "ByosTrustLevel",
    "load_user_function",
    "validate_trust_level",
]


def _validate_byos_signature(fn: Callable[..., Any], function_name: str) -> None:
    """Validate that *fn* matches the documented BYOS contract for *function_name*.

    Raises :class:`ByosContractError` with a clear message if the function
    has the wrong number of required positional parameters. Names are
    checked best-effort (the orchestrator forwards positional args, so a
    wrong name with the right arity would still misbehave at call time —
    but we surface this as a contract violation too).

    Issue #1048: a BYOS user writing

    .. code-block:: python

        def apply_parameters(template, params):  # wrong arity
            ...

    would silently receive the orchestrator's positional args
    (``sim_dir, resolved_params``) which is incorrect. With this check
    the loader fails fast at BYOS discovery time with a precise error
    message naming the expected signature.
    """
    spec = _BYOS_CONTRACT.get(function_name)
    if spec is None:
        # Unknown function name — let it through. The orchestrator will
        # still get a clear ``AttributeError`` from the loader when the
        # function is invoked, since the function name must already be
        # in ``_CANDIDATE_NAMES`` to be discovered.
        return

    expected_count = spec.required_positional
    expected_names = spec.param_names
    accepts_kwargs = spec.accepts_kwargs

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise ByosContractError(
            f"BYOS function '{function_name}' is not introspectable: {exc}"
        ) from exc

    # Count required (no default) positional parameters.
    required_positional = [
        p
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]

    if len(required_positional) != expected_count:
        actual = (
            f"{len(required_positional)} required positional"
            if len(required_positional) != 1
            else "1 required positional"
        )
        expected = (
            f"{expected_count} required positional"
            if expected_count != 1
            else "1 required positional"
        )
        expected_list = ", ".join(expected_names)
        raise ByosContractError(
            f"BYOS function '{function_name}' has {actual} parameter(s); "
            f"expected {expected} ({expected_list}). "
            f"Got signature: {sig}. "
            f"See the BYOS contract docs in user_scripts/README.md and "
            f"AGENTS.md §6."
        )

    # Best-effort param-name check. Allow the function to accept extra
    # kwargs (extract_kpis accepts openstudio_version etc.).
    if not accepts_kwargs and any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    ):
        log.warning(
            "BYOS function '%s' accepts **kwargs but the contract "
            "documents a fixed signature; kwargs will be ignored.",
            function_name,
        )

    # If the function has the right arity but the param names look wrong
    # (e.g. user wrote ``def apply_parameters(a, b, c, d):``), warn — we
    # do not raise, because positional callers still work, but the
    # contract docs use the canonical names.
    actual_names = tuple(p.name for p in required_positional)
    if actual_names != expected_names:
        log.warning(
            "BYOS function '%s' parameter names %s differ from the "
            "documented contract %s. The orchestrator forwards positional "
            "args, so this is a soft warning — but renaming for clarity "
            "is recommended.",
            function_name,
            actual_names,
            expected_names,
        )


# Whitelist of environment variables that BYOS subprocesses may receive.
# This prevents accidental credential leakage (AWS keys, etc.) from the
# parent process environment.  Each entry may be a bare variable name
# (checked for presence) or a "VAR=default" string (used if VAR is absent).
# See issue #764.
_SAFE_ENV_WHITELIST = [
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "LANG",
    "LC_ALL",
    "USER",
    "USERNAME",
    "OSIMFLOW_STUB_SIM",
]

# The canonical function names a BYOS script must expose.  The first
# match wins.  Order matters: ``apply_parameters`` is the primary name
# documented in AGENTS.md; ``apply`` is kept as a fallback for
# backwards-compatibility with scripts written against the old
# ``osimflow.apply_params._load_custom_apply`` loader (removed in the
# fix for issue #36).
_CANDIDATE_NAMES = ("apply_parameters", "extract_kpis", "apply")


def _sanitize_env() -> dict[str, str]:
    """Return a sanitized environment for BYOS subprocesses.

    Builds a clean env dict containing only the whitelisted variables from
    the current environment.  This prevents accidental credential leakage
    (AWS keys, secrets, etc.) from the parent process.  See issue #764.
    """
    clean: dict[str, str] = {}
    for name in _SAFE_ENV_WHITELIST:
        if "=" in name:
            var, default = name.split("=", 1)
            clean[var] = os.environ.get(var, default)
        elif name in os.environ:
            clean[name] = os.environ[name]
    return clean


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


def validate_trust_level(
    trust_level: ByosTrustLevel,
    require_trusted_scripts: bool,
) -> None:
    """Reject the ``INPROCESS`` trust level when trusted scripts are required.

    Production-hardening guard for issue #908.  When a deployment sets
    ``--require-trusted-scripts``, BYOS scripts must run in the isolated
    ``SUBPROCESS`` mode.  This helper centralises the rejection so the
    CLI, the REST API, and any other entry point share a single rule.

    Args:
        trust_level: The BYOS trust level requested by the caller.
        require_trusted_scripts: Whether the surrounding deployment
            mandates isolated script execution.  When ``True``, the
            ``INPROCESS`` trust level is rejected.

    Raises:
        ValueError: ``require_trusted_scripts`` is ``True`` *and*
            ``trust_level`` is :attr:`ByosTrustLevel.INPROCESS`.
    """
    if require_trusted_scripts and trust_level is ByosTrustLevel.INPROCESS:
        raise ValueError(
            "BYOS trust level 'inprocess' is not allowed when "
            "--require-trusted-scripts is set. In-process execution loads "
            "user scripts directly into the orchestrator with full access to "
            "memory, credentials, and the filesystem. Re-run with "
            "--byos-trust-level=subprocess (the default) or drop "
            "--require-trusted-scripts to allow in-process execution in a "
            "trusted development environment (issue #908)."
        )


def _discover_function_name(path: Path) -> str:
    """Discover the BYOS function name in a user script in an isolated subprocess.

    Issue #1005: previously this called ``spec.loader.exec_module(mod)``
    *inside the orchestrator process*, so a malicious BYOS file with
    module-level code such as ``import os; os._exit(42)`` would terminate
    the orchestrator before any subprocess sandbox was ever created.  The
    fix moves ``exec_module`` into a child process: malicious module-level
    code dies in the child, and the orchestrator survives to surface a
    clear ``RuntimeError`` to the caller.

    Returns the name of the first matching callable found.

    Raises:
        ImportError: the file cannot be loaded as a Python module.
        AttributeError: no callable with a recognised name was found.
        RuntimeError: the discovery subprocess crashed (e.g. a malicious
            ``os._exit`` at module level).  The orchestrator itself
            remains running — only this call raises.
    """
    function_name = _discover_in_subprocess(path)
    if function_name == "apply":
        warnings.warn(
            f"User script {path} uses the deprecated function name "
            f"'apply'. Rename it to 'apply_parameters' for forward "
            f"compatibility. Support for 'apply' will be removed in a "
            f"future release.",
            DeprecationWarning,
            stacklevel=2,
        )
    return function_name


# ---------------------------------------------------------------------------
# Discovery subprocess runner — isolates ``exec_module`` from the orchestrator
# (issue #1005).
# ---------------------------------------------------------------------------
_DISCOVERY_RUNNER = """
import json
import sys
import importlib.util
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Mirror the canonical candidate list from osimflow.byos.
# Duplicated here so the subprocess is self-contained.
_CANDIDATE_NAMES = ("apply_parameters", "extract_kpis", "apply")


def _emit_error(exc, message_override=None):
    \"\"\"Emit a JSON error payload and exit non-zero.

    The ``type`` field carries the exception class name so the parent
    can re-raise the same exception type.
    \"\"\"
    type_name = type(exc).__name__ if exc is not None else "RuntimeError"
    message = message_override if message_override is not None else (str(exc) or type_name)
    print(json.dumps({"error": message, "type": type_name}))
    sys.exit(1)


def main() -> None:
    script_path = Path(sys.argv[1])

    try:
        spec = importlib.util.spec_from_file_location("_byos_discover", script_path)
    except BaseException as exc:  # noqa: BLE001 - propagate any spec failure
        _emit_error(exc, message_override=f"could not load spec for {script_path}: {exc}")
        return  # unreachable, _emit_error exits

    if spec is None or spec.loader is None:
        _emit_error(
            ImportError(f"could not load spec for {script_path}"),
            message_override=f"could not load spec for {script_path}",
        )
        return

    try:
        mod = importlib.util.module_from_spec(spec)
        # ``exec_module`` runs the BYOS script's module-level code.  Any
        # malicious ``os._exit()``, infinite loop, or runtime error here
        # dies inside THIS child process — not in the orchestrator.
        spec.loader.exec_module(mod)
    except SystemExit:
        # Honour user-initiated ``sys.exit(...)`` / ``os._exit(...)`` by
        # letting it propagate out of the subprocess silently.
        raise
    except BaseException as exc:  # noqa: BLE001 - capture any module-level error
        _emit_error(exc)
        return

    for candidate in _CANDIDATE_NAMES:
        candidate_obj = getattr(mod, candidate, None)
        if callable(candidate_obj):
            print(json.dumps({"function": candidate}))
            return

    _emit_error(
        AttributeError(
            f"User script {script_path} must define "
            f"`apply_parameters(...)` or `extract_kpis(...)`."
        )
    )


main()
"""


def _discover_in_subprocess(path: Path) -> str:
    """Run the BYOS discovery path (``exec_module`` + attribute lookup) in a child process.

    The subprocess loads the user's ``.py`` file via ``importlib`` and
    writes a JSON payload of the form:

        ``{"function": "<candidate_name>"}``       — on success
        ``{"error": "<message>"}``                 — on failure (load, missing callable, exception)

    The orchestrator never executes ``exec_module`` itself.  See issue
    #1005 for the security rationale (a malicious module-level
    ``os._exit(42)`` previously killed the orchestrator).

    Args:
        path: Path to the user's ``.py`` script.

    Returns:
        The discovered BYOS function name (e.g. ``"apply_parameters"``).

    Raises:
        AttributeError: no callable with a recognised name was found.
        ImportError: the file cannot be loaded as a Python module.
        RuntimeError: the discovery subprocess exited non-zero (e.g. user
            ``os._exit(42)``).  The orchestrator itself keeps running.
    """
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _DISCOVERY_RUNNER, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_sanitize_env(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"BYOS discovery subprocess timed out after 60s: script={path}") from exc

    if proc.returncode != 0:
        # The subprocess may have produced a parseable JSON error payload
        # before exiting (e.g. missing callable, syntax error, import
        # failure).  Try to extract it; only raise ``RuntimeError`` when
        # no structured response is available, which indicates a hard
        # crash such as a malicious ``os._exit(42)``.
        response = _parse_discovery_response(stdout)
        if response is not None and "error" in response:
            _raise_discovery_error(response["error"], response.get("type", "RuntimeError"))
            raise AssertionError  # unreachable
        # Malicious module-level ``os._exit(42)`` (or any other hard crash)
        # ends the child at a non-zero exit code with no JSON payload.
        error_detail = stderr.strip() or stdout.strip() or "unknown error"
        raise RuntimeError(
            f"BYOS discovery subprocess failed (exit {proc.returncode}): "
            f"the user script likely has module-level side effects or "
            f"premature termination. script={path} detail={error_detail}"
        )

    response = _parse_discovery_response(stdout)
    if response is None:
        raise RuntimeError(f"BYOS discovery subprocess returned invalid JSON: {stdout[:200]}")

    if "error" in response:
        _raise_discovery_error(response["error"], response.get("type", "RuntimeError"))
        raise AssertionError  # unreachable

    function_name = response.get("function")
    if not isinstance(function_name, str) or not function_name:
        raise RuntimeError(f"BYOS discovery subprocess returned no function name: {stdout[:200]}")
    return function_name


# Mapping from subprocess-reported exception class names to the actual
# exception class.  Used by ``_discover_in_subprocess`` to re-raise the
# same exception type the user script would have raised in-process.
_DISCOVERY_EXCEPTION_TYPES: dict[str, type[BaseException]] = {
    "AttributeError": AttributeError,
    "ImportError": ImportError,
    "ModuleNotFoundError": ImportError,  # subclass of ImportError in 3.10+
    "SyntaxError": SyntaxError,
    "FileNotFoundError": FileNotFoundError,
    "NameError": NameError,
    "TypeError": TypeError,
    "ValueError": ValueError,
    "RuntimeError": RuntimeError,
}


def _parse_discovery_response(stdout: str) -> dict[str, object] | None:
    """Parse the JSON response from the BYOS discovery subprocess.

    Returns the decoded object, or ``None`` if ``stdout`` was empty or
    not valid JSON.
    """
    payload = stdout.strip()
    if not payload:
        return None
    try:
        response = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(response, dict):
        return None
    return response


def _raise_discovery_error(message: object, type_name: object) -> None:
    """Re-raise a subprocess error with the exception type the user script raised.

    Looks up ``type_name`` (the exception class name reported by the
    subprocess) in :data:`_DISCOVERY_EXCEPTION_TYPES` and raises an
    instance of that class with ``str(message)`` as its message.  Falls
    back to :class:`RuntimeError` for unknown / missing type names.
    """
    message_str = str(message)
    exc_class = _DISCOVERY_EXCEPTION_TYPES.get(
        str(type_name) if isinstance(type_name, str) else "",
        RuntimeError,
    )
    raise exc_class(message_str)


# ---------------------------------------------------------------------------
# Subprocess runner — the inline script executed in the child process
#
# The runner script lives in ``osimflow/_byos_runner_generated.py``,
# which is regenerated from ``osimflow.byos_contract._BYOS_CONTRACT`` by
# ``tools/_generate_byos_runner.py`` (issue #1061).  We import it here
# so the constant is exported as ``osimflow.byos._SUBPROCESS_RUNNER`` —
# the rest of the module continues to use the historical name.
# ---------------------------------------------------------------------------


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

    The subprocess receives a **sanitized** environment containing only a
    whitelisted subset of variables (``PATH``, ``HOME``, ``TMPDIR``, etc.).
    Credentials (AWS keys, secrets, etc.) from the parent environment are
    explicitly excluded.  See ``_sanitize_env`` and issue #764.

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
            except OSError as exc:
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
            env=_sanitize_env(),
        )
        try:
            stdout, stderr = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()
            raise RuntimeError(
                f"BYOS subprocess timed out after 600s: script={script_path} "
                f"function={function_name}"
            ) from exc
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
        warnings.warn(
            "BYOS trust level 'inprocess' loads user scripts directly into "
            "the orchestrator process with full access to memory, filesystem, "
            "and network. This is a security risk in production (issue #908). "
            "Only use 'inprocess' in trusted development environments. "
            "The 'subprocess' trust level (default) runs scripts in an isolated "
            "child process and is recommended for all production deployments.",
            UserWarning,
            stacklevel=2,
        )
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
            # Validate the signature against the documented BYOS contract
            # (issue #1048). Raises ``ByosContractError`` on arity mismatch.
            _validate_byos_signature(candidate_obj, candidate)
            # Cast: the BYOS contract is validated above; mypy cannot prove
            # the module attr type.
            return cast(Callable[..., Any], candidate_obj)
    raise AttributeError(
        f"User script {path} must define `apply_parameters(...)` or `extract_kpis(...)`."
    )
