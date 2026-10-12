"""Moved from ``osimflow.work`` (issue #1890); re-exported there."""

import logging
import os

log = logging.getLogger("osimflow.work")

# ---------------------------------------------------------------------------
# Subprocess env sanitization (issue #1027)
#
# Without ``env=`` on ``subprocess.run(...)``, the child inherits the
# orchestrator's full environment — including any AWS / GitHub / Redis
# secrets the orchestrator itself was handed.  Every subprocess call site
# in this module passes ``env=_sanitize_env()`` so those secrets cannot
# leak to ``openstudio.cli`` or the Python work scripts.
#
# The allowlist here is intentionally narrower than
# ``osimflow.byos._SAFE_ENV_WHITELIST`` (issue #1007 owns BYOS semantics).
# ``openstudio.cli`` is a trusted binary that needs PATH and the Python
# interpreter variables to start; it does not need user-shell vars like
# ``USER`` / ``USERNAME`` and we don't want any framework flags that are
# only meaningful to the orchestrator itself.
#
# SECURITY — HMAC task-payload secret (issue #1388):
#   ``OSIMFLOW_TASK_PAYLOAD_SECRET`` and ``OSIMFLOW_TASK_PAYLOAD_SIG``
#   are the HMAC shared secret and signature that gate which cloud-side
#   workloads the orchestrator may submit (issue #1177).  Compromise of
#   the secret lets an attacker forge ``OSIMFLOW_TASK_PAYLOAD`` +
#   ``OSIMFLOW_TASK_PAYLOAD_SIG`` and pivot into arbitrary
#   ``python -m osimflow.remote_runner`` invocations on Nomad /
#   Kubernetes — that is, into the entire step-call surface.  The
#   legitimate consumer of these vars is *only* ``osimflow.remote_runner``,
#   which runs inside a Nomad / Kubernetes Job that receives its env
#   directly from the substrate's Job spec (set by the executor via
#   :func:`osimflow.task_payload_hmac.build_signature_env`) — it never
#   reads its env through this allowlist.  Therefore we ship an explicit
#   allowlist of *named* vars and refuse to forward ``OSIMFLOW_*``
#   wildcards: every byte that reaches ``bin/*.py`` or the local stub
#   subprocess must be justified by name.
# ---------------------------------------------------------------------------
_WORK_SUBPROCESS_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        # POSIX basics the child needs to locate binaries, locale data,
        # and the temp dir.
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        # Python interpreter variables.  An explicit list rather than the
        # legacy ``PYTHON*`` prefix keeps the surface tight — a future
        # ``PYTHON_TEMP_SECRET`` style mistake cannot leak by accident.
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "PYTHONHASHSEED",
        # Legitimate OSIMFLOW_* flags consumed by work scripts.
        # ``OSIMFLOW_STUB_SIM`` is the testing / development escape
        # hatch (see :func:`_is_stub_mode`); ``OSIMFLOW_RUN_ID`` lets
        # child logs correlate with the parent campaign;
        # ``OSIMFLOW_LOG_LEVEL`` lets the CLI surface log-verbosity
        # overrides.  ``OSIMFLOW_TASK_PAYLOAD_SECRET`` and
        # ``OSIMFLOW_TASK_PAYLOAD_SIG`` are **deliberately absent** —
        # see the SECURITY block above (issue #1388).
        "OSIMFLOW_STUB_SIM",
        "OSIMFLOW_RUN_ID",
        "OSIMFLOW_LOG_LEVEL",
    }
)


def _sanitize_env() -> dict[str, str]:
    """Return a sanitized env for subprocesses spawned from this module (issue #1027).

    Builds a clean ``dict[str, str]`` from an explicit per-call allowlist
    (issue #1388):

    * POSIX basics (``PATH``, ``HOME``, ``LANG``, ``LC_ALL``, ``TMPDIR``,
      ``TEMP``, ``TMP``) — needed to locate binaries, locale data, and
      the temp directory;
    * Python interpreter variables (``PYTHONPATH``, ``PYTHONHOME``,
      ``PYTHONIOENCODING``, ``PYTHONUNBUFFERED``, ``PYTHONHASHSEED``);
    * a short list of legitimate ``OSIMFLOW_*`` framework flags
      (``OSIMFLOW_STUB_SIM``, ``OSIMFLOW_RUN_ID``, ``OSIMFLOW_LOG_LEVEL``).

    Everything else from ``os.environ`` (notably ``AWS_*``, ``GITHUB_TOKEN``,
    ``REDIS_URL``, ``*_SECRET*``, **and the HMAC task-payload secret /
    signature pair** ``OSIMFLOW_TASK_PAYLOAD_SECRET`` /
    ``OSIMFLOW_TASK_PAYLOAD_SIG``) is **dropped**.  This is the single
    defence against accidental credential leakage from the orchestrator
    to ``openstudio.cli run -w ...`` and the bundled ``bin/*.py`` work
    scripts.
    """
    clean: dict[str, str] = {}
    for name, value in os.environ.items():
        if name in _WORK_SUBPROCESS_ENV_ALLOWLIST:
            clean[name] = value
    return clean
