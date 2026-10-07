"""Container smoke test for the OSimFlow AWS Batch worker runtime (issue #1810).

Run inside the image (as the non-root image user, read-only of host, no mounts):

    docker run --rm --entrypoint python osimflow-worker:3.11.0 \
        /opt/osimflow/smoke_test.py

Checks: Python >= 3.12, non-root, imports, OpenStudio CLI version matches the
pinned version, the remote runner executes a *signed* task payload, and a real
OpenStudio workflow produces ``eplusout.sql`` in writable container scratch.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GEMS = Path("/var/oscli/gems/ruby/3.2.0/gems")


def _check(cond: bool, msg: str) -> None:
    if not cond:
        print(f"SMOKE FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"ok: {msg}")


def _find_epw() -> Path:
    matches = sorted(GEMS.glob("openstudio-extension-*/lib/files/*.epw"))
    _check(bool(matches), "bundled test EPW present")
    return matches[0]


def main() -> int:
    _check(sys.version_info >= (3, 12), f"python {sys.version.split()[0]} >= 3.12")
    _check(os.geteuid() != 0, f"running non-root (uid={os.geteuid()})")

    import boto3  # noqa: F401, PLC0415

    import osimflow  # noqa: F401, PLC0415
    import osimflow.remote_runner  # noqa: F401, PLC0415

    expected = os.environ.get("OSIMFLOW_OPENSTUDIO_VERSION", "")
    cli = shutil.which("openstudio")
    _check(cli is not None, "openstudio CLI on PATH")
    version = subprocess.run(
        [str(cli), "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    print(f"openstudio --version: {version}")
    _check(bool(expected) and version.startswith(expected), f"CLI version matches {expected!r}")

    from osimflow.executors.base import BaseExecutor  # noqa: PLC0415
    from osimflow.task_payload_hmac import sign_task_payload  # noqa: PLC0415

    work = Path(tempfile.mkdtemp(prefix="smoke-", dir=os.environ.get("TMPDIR", "/scratch")))
    pkg = work / "pkg"
    pkg.mkdir()
    ruby = (
        "m = OpenStudio::Model::exampleModel; rp = m.getRunPeriod; "
        "rp.setBeginMonth(1); rp.setBeginDayOfMonth(1); "
        "rp.setEndMonth(1); rp.setEndDayOfMonth(2); "
        f"m.save(OpenStudio::Path.new('{pkg / 'model.osm'}'), true)"
    )
    subprocess.run([str(cli), "-e", ruby], check=True, capture_output=True)
    shutil.copy(_find_epw(), pkg / "weather.epw")
    (pkg / "workflow.osw").write_text(
        json.dumps({"seed_file": "model.osm", "weather_file": "weather.epw", "steps": []})
    )
    out = work / "out"
    out.mkdir()

    payload = json.dumps(
        {
            "schema_version": 1,
            "name": "smoke",
            "step": "sim",
            "args": [],
            "kwargs": {
                "modified_sim_package": BaseExecutor._encode_payload_value(pkg),
                "sample_id": "smoke",
                "openstudio_version": expected,
                "out": BaseExecutor._encode_payload_value(out),
            },
            "result_hint": None,
        }
    )
    secret = "smoke-secret"
    env = {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}
    env.update(
        OSIMFLOW_TASK_PAYLOAD=payload,
        OSIMFLOW_TASK_PAYLOAD_SIG=sign_task_payload(payload, secret),
        OSIMFLOW_TASK_PAYLOAD_SECRET=secret,
    )
    cmd = ["python", "-m", "osimflow.remote_runner"]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=work, check=False)
    print(proc.stdout[-2000:])
    print(proc.stderr[-2000:], file=sys.stderr)
    _check(proc.returncode == 0, "remote runner accepted signed payload and exited 0")

    sqls = list(work.rglob("eplusout.sql"))
    _check(bool(sqls) and sqls[0].stat().st_size > 0, "real simulation wrote eplusout.sql")

    # A tampered payload must be rejected.
    env["OSIMFLOW_TASK_PAYLOAD"] = payload + " "
    bad = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=work, check=False)
    _check(bad.returncode != 0, "tampered payload rejected")

    shutil.rmtree(work, ignore_errors=True)
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
