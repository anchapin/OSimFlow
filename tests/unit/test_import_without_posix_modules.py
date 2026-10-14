"""Issue #2015: ``import osimflow`` must not require POSIX-only modules."""

import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    import sys
    import signal
    import fasteners  # picks its lock backend by sys.platform; load before hiding fcntl
    for name in ("resource", "fcntl", "pwd"):
        sys.modules[name] = None  # makes `import <name>` raise ImportError
    if hasattr(signal, "SIGKILL"):
        delattr(signal, "SIGKILL")  # POSIX-only; absent on Windows
    import osimflow
    import osimflow.byos, osimflow.audit, osimflow.batches
    import osimflow.campaign.lifecycle, osimflow.api.events
    assert osimflow.byos.resource is None
    assert osimflow.audit.cli_actor().startswith("cli:")
    assert 9 in osimflow._work_retry._NON_TRANSIENT_SUBPROCESS_SIGNALS
    print("ok")
    """
)


def test_package_imports_without_posix_only_modules() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
