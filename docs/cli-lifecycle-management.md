# CLI Lifecycle Management

OSimFlow invokes the OpenStudio CLI directly rather than through a server-managed process supervisor. This guide covers the current implementation, the process-supervision gap, and patterns for adding restart-on-failure behaviour across all executors.

---

## 1. Overview

### The Gap

openstudio-server owns CLI process lifecycle — it starts the CLI, monitors its health, and restarts it on failure. OSimFlow currently **invokes the CLI as a one-shot subprocess** and relies on the Campaign's retry logic at the *step* level, not the *process* level. There is no per-CLI process supervision, no automatic restart on crash, and no health monitoring within a running simulation.

### What OSimFlow Does Instead

The Campaign's `run_with_retry` helper (`osimflow/work.py`) wraps each step call and retries on transient errors. However, this retry logic operates *between* CLI invocations — if the CLI crashes mid-run with a non-transient exit code, the retry applies to the whole step, not to a self-healing CLI process.

### Why It Matters

- A simulation that runs for 30 minutes before crashing must restart from scratch unless the error is classified as transient.
- CLI crashes can leave `eplusout.sql` in an inconsistent state.
- Long-running HPC jobs on Slurm or cloud substrates may be terminated by the scheduler before the CLI finishes.

---

## 2. Current Implementation

### `run_openstudio_sim`

The primary CLI invocation lives in `osimflow/work.py:_run_openstudio_sim_impl` (called via the public `run_openstudio_sim` wrapper):

```python
# osimflow/work.py  (lines 390–460)

def _run_openstudio_sim_impl(
    modified_sim_package: Path,
    sample_id: str,
    openstudio_version: str,
    out: Path,
    simulate_work_s: float = 2.0,
    *,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    max_retries: int = 3,
    worker_id: str = "local",
) -> Path:
    sim_out = out / sample_id
    sim_out.mkdir(parents=True, exist_ok=True)

    # Check whether eplusout.sql already exists (skip if already run)
    if (sim_out / "eplusout.sql").is_file():
        return sim_out

    use_real_cli = _is_openstudio_available() and not _is_stub_mode()

    if use_real_cli:
        return _run_real_openstudio(
            modified_sim_package=modified_sim_package,
            sample_id=sample_id,
            sim_out=sim_out,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    # Stub path: simulate work with a sleep
    cmd = [
        sys.executable, "-c",
        f"import sys, time; print('openstudio CLI stub'); "
        f"time.sleep({simulate_work_s}); sys.exit(0)"
    ]
    run_subprocess(cmd, stdout_path=stdout_path, stderr_path=stderr_path, cwd=sim_out)
    (sim_out / "eplusout.sql").write_text("-- placeholder sql")
    return sim_out
```

### Real CLI Invocation

`_run_real_openstudio` (`osimflow/work.py`, lines 568–638) invokes the CLI directly:

```python
cmd: list[str] = [
    "openstudio.cli",
    "run",
    "-w",
    str(workflow_path),
]
run_subprocess(
    cmd,
    stdout_path=stdout_path,
    stderr_path=stderr_path,
    cwd=sim_out,
)
```

### stdout / stderr Capture

`run_subprocess` (`osimflow/executors/__init__.py`, lines 94–144) is the shared helper that redirects output to per-sample log files:

```python
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
    """Run a subprocess and redirect stdout/stderr to per-sample log files."""
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        stdout_path.open("w", encoding="utf-8", errors="replace") as out_f,
        stderr_path.open("w", encoding="utf-8", errors="replace") as err_f,
    ):
        return subprocess.run(
            list(cmd),
            stdout=out_f,
            stderr=err_f,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            check=check,
            timeout=timeout,
            text=True,
        )
```

Per-sample logs land at `${outdir}/work/sim/<sample_id>/{stdout,stderr}.log`.

### Stub Mode

When `openstudio.cli` is not on PATH, or when `OSIMFLOW_STUB_SIM=1` is set in the environment, the work function uses a stub that sleeps and writes placeholder output:

```python
def _is_openstudio_available() -> bool:
    return shutil.which("openstudio.cli") is not None

def _is_stub_mode() -> bool:
    return os.environ.get("OSIMFLOW_STUB_SIM") == "1"
```

The stub is also used by integration tests in CI so they run without a real OpenStudio installation.

### Timeout Handling

`run_subprocess` accepts a `timeout` argument that is passed directly to `subprocess.run`. The Campaign sets per-step timeouts via `time_min` on the executor (enforced by the substrate scheduler on Slurm/Batch/Nomad). Within the CLI invocation itself, no per-process timeout is set — the substrate-level timeout is the enforcement point.

### Transient Error Retry

`run_with_retry` (`osimflow/work.py`, lines 78–143) wraps each step call and retries on transient failures. Transient markers include timeout, network, and resource-busy conditions, plus specific exit codes:

```python
_TRANSIENT_EXIT_CODES = frozenset([-1, 2, 4, 5, 6, 11, 12, 15, 24, 25, 26, 27, 28])

def _is_transient_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    transient_markers = (
        "timeout", "timed out", "connection", "network",
        "resource busy", "temporary failure", "refused",
        "too many open files", "disk full", "io error",
    )
    if any(m in msg for m in transient_markers):
        return True
    return (
        isinstance(exc, subprocess.CalledProcessError)
        and exc.returncode in _TRANSIENT_EXIT_CODES
    )
```

---

## 3. Process Supervision Patterns

### Using `supervisord` Inside Containers

`supervisord` can manage the OpenStudio CLI lifecycle inside the `nrel/openstudio` container. Add a `supervisord.conf` to the container entrypoint:

```ini
# /etc/supervisord.conf
[supervisord]
nodaemon=true
logfile=/var/log/supervisor/supervisord.log
pidfile=/var/run/supervisord.pid

[program:openstudio-cli]
command=/usr/local/bin/openstudio.cli run -w /workflow.osw
directory=/workdir
stdout_logfile=/workdir/supervisor-stdout.log
stderr_logfile=/workdir/supervisor-stderr.log
autorestart=true
startretries=3
exitcodes=0
```

Then run the container with a custom entrypoint:

```bash
docker run --rm \
  nrel/openstudio:3.11.0 \
  /usr/bin/supervisord -c /etc/supervisord.conf
```

**Pros:** Automatic restart on crash, log aggregation, single PID 1 management.
**Cons:** Adds a process supervisor to the container; restart loops can mask root-cause failures.

### Running CLI as a Monitored Subprocess

Wrap the CLI invocation in a Python process that monitors it and restarts on failure:

```python
# cli_wrapper.py
import subprocess
import sys
import time
import pathlib

MAX_RETRIES = 3
RETRY_DELAY = 10.0  # seconds

def run_with_supervision(cmd: list[str], cwd: pathlib.Path) -> None:
    for attempt in range(MAX_RETRIES + 1):
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = proc.communicate()
        if proc.returncode == 0:
            return
        if attempt < MAX_RETRIES:
            print(f"CLI exited with {proc.returncode}, retrying in {RETRY_DELAY}s", flush=True)
            time.sleep(RETRY_DELAY)
        else:
            print(f"CLI failed after {MAX_RETRIES} retries", flush=True)
            sys.exit(proc.returncode)

if __name__ == "__main__":
    run_with_supervision(
        cmd=["openstudio.cli", "run", "-w", sys.argv[1]],
        cwd=pathlib.Path(sys.argv[2]),
    )
```

Invoke as:

```bash
python cli_wrapper.py workflow.osw /workdir
```

### Docker `HEALTHCHECK` Directive

Add a health check to the `Dockerfile` that validates the CLI is responsive:

```dockerfile
# Part of a custom Dockerfile extending nrel/openstudio
FROM nrel/openstudio:3.11.0

HEALTHCHECK --interval=60s --timeout=30s --start-period=120s --retries=3 \
  CMD openstudio.cli openstudio --version || exit 1
```

The `HEALTHCHECK` makes Docker monitor the container's health. When combined with `--restart=on-failure` in `docker run`, Docker will restart containers that fail health checks:

```bash
docker run --rm \
  --restart=on-failure:3 \
  -v $(pwd):/workdir \
  nrel/openstudio:3.11.0 \
  openstudio.cli run -w /workdir/workflow.osw
```

On Kubernetes, use `livenessProbe` instead:

```yaml
livenessProbe:
  exec:
    command: ["openstudio.cli", "openstudio", "--version"]
  initialDelaySeconds: 120
  periodSeconds: 60
  timeoutSeconds: 30
  failureThreshold: 3
```

---

## 4. Restart on Failure

### Custom Wrapper Script for `run_openstudio_sim`

Integrate a restart-on-failure wrapper directly into the `run_openstudio_sim` stub. When the CLI exits non-zero, the wrapper re-runs it up to `max_retries` times before propagating the failure:

```python
# Part of osimflow/work.py  —  replaces the run_subprocess call in _run_real_openstudio

import subprocess

_MAX_CLI_RESTARTS = 3
_RESTART_DELAY = 10.0  # seconds

def _run_cli_with_restart(
    cmd: list[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    cwd: Path,
) -> None:
    """Run the CLI, restarting on non-zero exit up to _MAX_CLI_RESTARTS times."""
    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(_MAX_CLI_RESTARTS + 1):
        try:
            run_subprocess(
                cmd,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                cwd=cwd,
                check=True,   # raises CalledProcessError on non-zero
            )
            return  # success
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            if attempt < _MAX_CLI_RESTARTS:
                log.warning(
                    "CLI exited %d (attempt %d/%d), restarting in %.1fs",
                    exc.returncode,
                    attempt + 1,
                    _MAX_CLI_RESTARTS,
                    _RESTART_DELAY,
                )
                time.sleep(_RESTART_DELAY)
            else:
                break
    if last_exc is not None:
        raise last_exc from None
```

### Integration with Campaign Retry

The Campaign-level `run_with_retry` and the process-level restart are independent layers:

| Layer | Trigger | Action |
|---|---|---|
| Campaign `run_with_retry` | Step raises `TransientError` | Re-run the entire step function |
| CLI wrapper `_run_cli_with_restart` | CLI exits non-zero | Re-run the CLI in-place |
| Substrate scheduler (Slurm/Batch/Nomad) | Job times out or is killed | Reschedules the whole job |

Both the CLI wrapper and the Campaign retry should be configured so they do not compound excessively (e.g., set `max_retries=0` in the CLI wrapper if the Campaign already retries the step).

### Detecting CLI Crashes vs. Normal Completion

The CLI writes `eplusout.sql` on success. If the process crashes, the file may be missing or truncated. A wrapper can verify output integrity before declaring success:

```python
def _run_cli_with_verification(
    cmd: list[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    cwd: Path,
) -> None:
    run_cli_with_restart(cmd, stdout_path=stdout_path, stderr_path=stderr_path, cwd=cwd)
    sql_path = cwd / "eplusout.sql"
    if not sql_path.is_file():
        raise RuntimeError(
            f"CLI exited 0 but eplusout.sql is missing in {cwd}. "
            "The simulation may have crashed without reporting an error."
        )
    # Optionally verify the SQLite file is readable
    try:
        import sqlite3
        sqlite3.connect(sql_path).close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"eplusout.sql is corrupted: {exc}") from exc
```

---

## 5. Long-Running Campaign Considerations

### The 6-Step DAG

Each step in the Campaign is independent and stateless between steps:

```
GENERATE_LHS_SAMPLES  (single-shot, no fan-out)
       ↓
PREFLIGHT_RUN_MODEL   (single-shot, validates seed model)
       ↓
APPLY_PARAMETERS      (fan-out over N samples)
       ↓
RUN_OPENSTUDIO_SIM   (fan-out, heavy — the CLI lifecycle matters most here)
       ↓
EXTRACT_KPIS         (fan-out over N samples)
       ↓
AGGREGATE_RESULTS    (single-shot)
       ↓
GENERATE_BASIC_PLOTS  (single-shot)
```

### What This Means for CLI Lifecycle

- **Steps 1–2 and 6–7** are single-shot and short-lived. Process supervision adds little value here.
- **Steps 3 and 5** (fan-out, per-sample) invoke the CLI or a work script per sample. Supervision at the CLI level applies.
- **Step 4** is the primary long-running concern. Simulations run 5 minutes to 4 hours. CLI crashes in step 4 waste the most compute.

### Cache Key and Restart Behaviour

The Campaign uses SHA-256 hashes of `bin/*.py` files in the cache key (`osimflow/campaign.py:_compute_code_hashes`). Editing a wrapper script invalidates the cache for that step, forcing a clean re-run. This means a CLI wrapper that crashes and restarts will correctly re-run the full step on retry.

### Sample-Level Independence

Each sample runs in its own directory (`${outdir}/work/sim/<sample_id>/`). A CLI crash in one sample does not affect others. This is both a strength (blast radius is limited) and a gap (there is no cross-sample coordination for a single sample's CLI process).

---

## 6. Kubernetes / Executor Integration

### LocalExecutor

```python
# osimflow/executors/__init__.py  (lines 147–181)
class LocalExecutor(BaseExecutor):
    name = "local"

    def __init__(self, max_workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="osimflow")

    def submit(self, fn, *args, **kwargs) -> Handle:
        fut = self._pool.submit(fn, *args)
        return Handle(job_id=f"local-{id(fut)}", _future=fut, ...)
```

- **Process lifecycle:** Tasks run as threads in a `ThreadPoolExecutor`. No resource limits are enforced.
- **Restart on failure:** The Campaign's `run_with_retry` retries the whole step function. There is no per-process restart.
- **CLI supervision:** Not available. Use `supervisord` inside the container or a custom wrapper script in the work function.
- **Timeouts:** Not enforced by the executor. The `time_min` parameter is logged but ignored.

### SlurmExecutor

```python
# osimflow/executors/__init__.py  (lines 231–397)
class SlurmExecutor(BaseExecutor):
    name = "slurm"

    def submit(self, fn, *args, name="task", cpus=1, memory_mb=1024,
               time_min=60, container=None, **kwargs) -> Handle:
        # A fresh submitit.AutoExecutor is built per submission with
        # per-call resource overrides rendered into the sbatch header.
        call_ex = self._submitit.AutoExecutor(folder=self._ex.folder, ...)
        _apply_slurm_params(call_ex, partition=self.partition, ...)
        fut = call_ex.submit(_wrapped)   # _wrapped calls fn(*args)
        return Handle(job_id=str(fut.job_id), _future=fut, ...)
```

- **Process lifecycle:** `submitit` wraps each task in a Slurm job. The job is the unit of restart — if the Slurm job is killed (scheduler timeout, node failure), the Campaign retry re-submits it.
- **Restart on failure:** The Campaign's `run_with_retry` handles transient step failures. For non-transient CLI crashes, the Slurm job re-runs the whole step.
- **CLI supervision:** Not available inside the Slurm job. The `osimflow_work.py` process on the compute node is the supervisor.
- **Timeouts:** `time_min` is passed as `#SBATCH --time` and is enforced by the Slurm scheduler. A job that exceeds its time limit is killed.

### AWSBatchExecutor

```python
# osimflow/executors/__init__.py  (lines 547–931)
class AWSBatchExecutor(BaseExecutor):
    name = "aws_batch"

    def submit(self, fn, *args, cpus=1, memory_mb=1024,
              time_min=60, container=None, **kwargs) -> Handle:
        submit_params = dict(name=name, cpus=cpus, memory_mb=memory_mb,
                              time_min=time_min, environment=environment)
        job_id = self._submit_job(**submit_params)
        return _AWSBatchHandle(job_id=job_id, executor=self, submit_params=submit_params)
```

- **Process lifecycle:** One Batch task per step invocation. The task runs the Campaign work function inside the `nrel/openstudio` container.
- **Restart on failure:** Spot interruption retry lives in `_AWSBatchHandle.result()`. On Spot interruption, the handle re-submits up to `max_retries` times before falling back to on-demand. Non-Spot failures are not retried by the executor.
- **CLI supervision:** Not available inside the container. Use `supervisord` in the container entrypoint or the `HEALTHCHECK` directive.
- **Timeouts:** `time_min * 60` is set as `attemptDurationSeconds` on the job. The task is killed when the timeout is reached.

### KubernetesExecutor

```python
# osimflow/executors/kubernetes_executor.py  (lines 110–338)
class KubernetesExecutor(BaseExecutor):
    name = "kubernetes"

    def _submit_job(self, *, name, cpus, memory_mb, time_min, environment):
        container = client.V1Container(
            ...
            command=["/bin/sh", "-c", "sleep infinity"],
            env=env_vars,
            resources=client.V1ResourceRequirements(
                requests={"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
                limits={"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
            ),
        )
        job = client.V1Job(
            ...
            spec=client.V1JobSpec(
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        containers=[container],
                        restart_policy="Never",   # <-- no pod restart
                    ),
                ),
                backoff_limit=0,   # <-- no job-level restart
                active_deadline_seconds=int(time_min) * 60 if time_min > 0 else None,
            ),
        )
        client.create_namespaced_job(namespace=self.namespace, body=job)
```

- **Process lifecycle:** One Kubernetes Job per step. The pod runs `sleep infinity`; the actual work runs as the Campaign work function on the pod's init process.
- **Restart on failure:** `restart_policy = "Never"` and `backoff_limit = 0` means no automatic pod restart. A failed Job is not retried automatically.
- **CLI supervision:** Not available. Add `livenessProbe` or `restartPolicy = OnFailure` to the pod spec to enable in-pod restarts.
- **Timeouts:** `active_deadline_seconds` is set from `time_min`. The job is killed when the deadline is reached.

To enable pod-level restart on CLI crash:

```yaml
# Add to the V1PodSpec in _submit_job
restart_policy: OnFailure   # restart the pod on CLI failure
```

Or use a `livenessProbe` to detect a hung CLI and restart the container:

```yaml
livenessProbe:
  exec:
    command: ["openstudio.cli", "--version"]
  initialDelaySeconds: 60
  periodSeconds: 300
  failureThreshold: 3
  # Kubernetes will restart the container if the probe fails 3 times
```

### NomadExecutor

```python
# osimflow/executors/__init__.py  (lines 1301–1623)
class NomadExecutor(BaseExecutor):
    name = "nomad"

    def _build_job_spec(self, ...):
        return {
            "Job": {
                "Name": _slugify_job_name(f"osimflow-{name}"),
                "Type": "batch",
                "TaskGroups": [{
                    "Tasks": [{
                        "Name": "osimflow",
                        "Driver": "docker",
                        "Config": {
                            "image": image,
                            "command": "/bin/sh",
                            "args": ["-c", "sleep infinity"],
                            "env": env,
                        },
                        "Resources": {"CPU": cpus * 1000, "MemoryMB": memory_mb},
                        "Restart": {
                            "Attempts": 0,   # no Nomad-level restart
                        },
                    }],
                }],
            }
        }
```

- **Process lifecycle:** One Nomad job per step, running `sleep infinity` in Docker.
- **Restart on failure:** `Restart.Attempts = 0` disables Nomad restarts.
- **CLI supervision:** Not available. Use `supervisord` in the container.
- **Timeouts:** Mapped to `KillTimeout` at the task group level.

---

## 7. Best Practices

### Always Set Timeouts

Always set `time_min` on the executor to a value 2–3x the expected simulation runtime. The simulation step (`RUN_OPENSTUDIO_SIM`) defaults to 240 minutes. This is enforced by the substrate scheduler on Slurm, Batch, and Nomad. On the LocalExecutor, it is advisory only.

### Capture stdout / stderr to `${outdir}/work/sim/<sample_id>/`

The `run_subprocess` helper (`osimflow/executors/__init__.py`) redirects output to per-sample log files. Pass `stdout_path` and `stderr_path` explicitly rather than relying on the defaults. This makes debugging a failed simulation a matter of reading two files:

```bash
cat results/work/sim/0001/stdout.log
cat results/work/sim/0001/stderr.log
```

### Use Stub Mode for Testing

Set `OSIMFLOW_STUB_SIM=1` in the environment to use the stub simulation path during development and CI. This avoids the need for a real OpenStudio installation:

```bash
OSIMFLOW_STUB_SIM=1 osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 3 \
  --outdir ./results
```

### Detect and Handle Transient vs. Non-Transient Failures

Classify CLI exit codes correctly. The transient codes in `_TRANSIENT_EXIT_CODES` are retried by `run_with_retry`. A CLI crash that produces a non-transient exit code should not be retried immediately — diagnose the root cause first. Repeated restart attempts on a broken model waste compute budget.

### Verify Simulation Output Before Proceeding

A CLI that exits 0 (success) may still have produced a corrupted or incomplete `eplusout.sql`. Verify the file exists and is readable before marking a sample complete. See the verification snippet in §4.

### Use `supervisord` for Long-Running Containers on HPC

On Slurm or Nomad where the scheduler may pre-empt or kill a job, wrap the CLI invocation in `supervisord` so a single simulation crash does not lose the entire job. The `supervisord.conf` approach from §3 works inside Singularity and Docker containers alike.

### Log the Worker ID for Debugging

Set the `worker_id` field on the handle to a value that lets you correlate logs back to the physical worker. The `SlurmExecutor` uses the Slurm job ID; `AWSBatchExecutor` uses the Batch job ARN; `KubernetesExecutor` uses the pod name. This makes it possible to find the right log stream when a CLI crashes on a remote worker.

---

## Cross-References

- [`osimflow/work.py`](osimflow/work.py) — `run_openstudio_sim`, `run_with_retry`, `_is_stub_mode`
- [`osimflow/executors/__init__.py`](osimflow/executors/__init__.py) — `run_subprocess`, `LocalExecutor`, `SlurmExecutor`, `AWSBatchExecutor`, `NomadExecutor`
- [`osimflow/executors/kubernetes_executor.py`](osimflow/executors/kubernetes_executor.py) — `KubernetesExecutor`, `restart_policy`
- [`osimflow/executors/base.py`](osimflow/executors/base.py) — `BaseExecutor`, `Handle`
- [`osimflow/campaign.py`](osimflow/campaign.py) — Campaign retry logic, cache key construction
- [AGENTS.md §4](../AGENTS.md) — CLI flags, 6-step DAG, DAG step names
- [Resource Allocation Guide](resource-allocation.md) — per-step `time_min` defaults and tuning
- [Observability Guide](observability.md) — `run.json` trace and optional MLflow integration
