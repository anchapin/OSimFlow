"""Subprocess utilities shared across OSimFlow modules.

This module exists to break circular dependencies between work.py and
executors/__init__.py. Both modules need run_subprocess, but the
architecture forbids work.py from importing from the executors layer.

Issue #1538: the helper now tracks every in-flight ``Popen`` in a
thread-keyed registry so :func:`terminate_active_subprocesses` can
kill the work subprocesses backing a running local campaign during
graceful shutdown — previously a cancelled LocalExecutor campaign left
every in-flight ``openstudio.cli`` (or work-script) subprocess running
to completion in the background.
"""

import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

# In-flight subprocess registry (issue #1538): thread-ident -> Popen.
# ``run_subprocess`` blocks its calling thread for the child's lifetime,
# so at most one child is registered per thread ident. Populated only
# while the child is alive; the ``finally`` in ``run_subprocess``
# removes the entry, including on timeout-kill and cancel-kill paths.
_ACTIVE_SUBPROCESSES: dict[int, subprocess.Popen[str]] = {}
_ACTIVE_SUBPROCESSES_LOCK = threading.Lock()


def _register_active_subprocess(proc: subprocess.Popen[str]) -> None:
    ident = threading.get_ident()
    with _ACTIVE_SUBPROCESSES_LOCK:
        _ACTIVE_SUBPROCESSES[ident] = proc


def _unregister_active_subprocess() -> None:
    ident = threading.get_ident()
    with _ACTIVE_SUBPROCESSES_LOCK:
        _ACTIVE_SUBPROCESSES.pop(ident, None)


def terminate_active_subprocesses() -> int:
    """Terminate every in-flight ``run_subprocess`` child (issue #1538).

    Sends SIGTERM (``Popen.terminate``) to each registered child and
    returns the number of signals issued. The terminated children
    unblock their ``run_subprocess`` callers, which in turn unblock the
    LocalExecutor pool threads parked on the work functions — the local
    analogue of TerminateJob / scancel / allocation stop. Safe to call
    when nothing is running (returns 0) and idempotent (a terminated
    child is unregistered by its ``run_subprocess`` finally block, or
    the second signal is a harmless no-op on a dying process).
    """
    with _ACTIVE_SUBPROCESSES_LOCK:
        procs = list(_ACTIVE_SUBPROCESSES.values())
    killed = 0
    for proc in procs:
        if proc.poll() is not None:
            # Already exited; the owning thread will unregister it.
            continue
        try:
            proc.terminate()
            killed += 1
        except Exception:  # noqa: BLE001 — best-effort kill sweep
            continue
    return killed


def run_subprocess(
    cmd: Sequence[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and redirect stdout/stderr to per-sample log files.

    This is the LocalExecutor-side analogue of what the Slurm and AWS
    Batch executors will do at the substrate level: capture the process
    output into `${outdir}/work/sim/<sample_id>/{stdout,stderr}.log` so
    the user can `cat` the files to debug a failed sample without
    re-running the campaign.

    Both files are created (possibly empty) before the subprocess is
    invoked, so the paths exist on disk even when the process is killed
    before flushing its output buffers. The `text=True` flag decodes
    output as UTF-8; the `errors="replace"` policy keeps us from
    crashing on a stray non-UTF-8 byte in an EnergyPlus log.

    Returns the `CompletedProcess`. The `stdout` / `stderr` attributes of
    the return value are empty strings because the output went to disk
    (use `stdout_path.read_text()` to recover the captured output).

    The function does NOT raise on non-zero exit when `check=False` (the
    default); the caller decides how to surface failures. The Campaign
    inspects the return code and writes the per-sample status.

    Implementation note (issue #1538): the child is spawned with
    ``Popen`` and reaped with the exact ``communicate``/``wait`` dance
    ``subprocess.run`` performs internally, so the observable contract
    (returncode, ``TimeoutExpired`` after kill, exception surface) is
    byte-identical to the previous ``subprocess.run`` call — the only
    addition is the registry bookkeeping that makes the child
    terminate-able by :func:`terminate_active_subprocesses`.
    """
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        stdout_path.open("w", encoding="utf-8", errors="replace") as out_f,
        stderr_path.open("w", encoding="utf-8", errors="replace") as err_f,
    ):
        proc = subprocess.Popen(  # nosec  # caller owns the argv
            list(cmd),
            stdout=out_f,
            stderr=err_f,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            text=True,
            shell=False,
        )
        _register_active_subprocess(proc)
        try:
            try:
                # communicate() waits for the child and closes the pipes;
                # with file redirection the returned pair is (None, None).
                proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                # Mirror subprocess.run's non-captured timeout path:
                # wait() for the (killed) direct child only. communicate()
                # here would block on stdout EOF, which an orphaned
                # grandchild (e.g. ``sh -c "...; sleep 30"`` whose shell
                # was killed but whose ``sleep`` inherited the stdout fd)
                # never sends.
                proc.wait()
                raise
            except BaseException:
                # Mirror subprocess.run: any other exception during the
                # wait still kills the child before propagating.
                proc.kill()
                proc.wait()
                raise
            retcode = proc.wait()
        finally:
            _unregister_active_subprocess()
        if check and retcode != 0:
            raise subprocess.CalledProcessError(retcode, proc.args, None, None)
        return subprocess.CompletedProcess(proc.args, retcode)
