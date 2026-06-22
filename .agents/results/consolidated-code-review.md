# OSimFlow Code Review — Consolidated Findings

**Date:** 2026-06-21
**Scope:** Full codebase review across 5 specialized perspectives
**Sources:** Architecture review, Bug/Error review, Security review, Docs-Contract review, BEM review

---

## Executive Summary

| Category | HIGH | MEDIUM | LOW | Total |
|---|---|---|---|---|
| Architecture & Logic | 3 | 6 | 6 | 15 |
| Bug & Error Handling | 3 | 4 | 7 | 14 |
| Security & Quality | 2 | 4 | 7 | 13 |
| Docs-Contract Drift | 8 | 12 | 0 | 20 |
| BEM Integration | 2 | 2 | 2 | 6 |
| **TOTAL** | **18** | **28** | **22** | **68** |

**Critical action items:** 3 (security-related shell injection, preflight severe-error regex, non-atomic data point transitions)

---

## CRITICAL Issues (Require Immediate Attention)

### CR-1: Preflight severe-error regex misses `** Severe` format
**Severity:** CRITICAL | **Category:** BEM | **File:** `osimflow/work.py:1553–1562`

```python
def _extract_severe_error(output: str) -> str:
    for line in output.splitlines():
        if re.search(r"^\s*\d+\s+\*+\s*Severe", line, re.IGNORECASE):
            return line.strip()
    return ""
```

**Problem:** The regex requires a digit prefix (`\d+`) and multiple asterisks (`\*+`). EnergyPlus emits two Severe formats:
1. `"  * Severe"` — single asterisk (the regex misses this)
2. `"  1 ** Severe"` — digit + double asterisk

The preflight check silently returns `""` for the common single-asterisk format, allowing campaigns with severe errors to proceed to cloud spend.

**Compare with** `aggregate_results.py:340` which correctly handles both:
```python
if "  * Severe" in line or "** Severe" in line:
```

**Fix:** Change regex to `r"\*{1,2}\s*Severe"` or use the same pattern as `aggregate_results.py`.

---

### CR-2: Non-atomic DataPoint state transitions — race condition
**Severity:** CRITICAL | **Category:** Architecture | **File:** `osimflow/data_point_manager.py`

```python
current = self._get_status(sample_id)
if current not in (DataPointStatus.COMPLETED, DataPointStatus.FAILED):
    raise ValueError(...)
self._update_status(sample_id, DataPointStatus.MARKED_FOR_REANALYSIS)
```

**Problem:** Read-then-write is non-atomic. Between the read and write, another concurrent process could update the state, causing a lost update. This violates issue #420's reanalysis lifecycle contract.

**Additional:** JSON file writes in `DataPointManager` have no file locking. On shared filesystems (NFS, HPC), concurrent writes corrupt or lose updates.

**Fix:** Use `fcntl.flock` or `portalocker` for atomic file updates, or use a database transaction.

---

### CR-3: `BaseExecutor.submit()` not `@abc.abstractmethod`
**Severity:** HIGH | **Category:** Architecture | **File:** `osimflow/executors/base.py`

**Problem:** `BaseExecutor.submit()` is a concrete method that raises `NotImplementedError`. A subclass that forgets to override `submit()` will fail at runtime, not at class construction time. No static analysis or type checking catches this.

**Fix:** Decorate with `@abc.abstractmethod` to catch missing overrides at class definition time.

---

## HIGH Severity Issues

### H-1: BYOS inprocess mode runs user code in orchestrator process
**Severity:** HIGH | **Category:** Architecture | **File:** `osimflow/campaign.py`

When `--byos-trust-level inprocess` is set, user-supplied `apply_fn`/`extract_fn` run in the same Python interpreter as the orchestrator. The functions can mutate internal `Campaign` state (`_sample_results`, `_obs`, `_cache`).

While documented as a known risk in AGENTS.md §10, there is no runtime guard preventing state mutation.

---

### H-2: `run_with_retry` severs exception chain with `from None`
**Severity:** HIGH | **Category:** Bug | **File:** `osimflow/work.py:~250`

```python
if last_exc is not None:
    raise last_exc from None   # chain severed
```

**Problem:** `from None` removes the original traceback. A `TransientError` from a stale heartbeat and a `RuntimeError` from a missing `workflow.osw` surface identically. Debugging requires manual chain reconstruction.

**Fix:** Use `raise last_exc` (implicit chain) or `raise last_exc from last_exc`.

---

### H-3: Work functions replace `CalledProcessError` with generic `RuntimeError`, losing stdout/stderr
**Severity:** HIGH | **Category:** Bug | **Files:** `osimflow/work.py:~930, ~960`

```python
except subprocess.CalledProcessError as e:
    log.error("extract_kpis failed: %s", e.stderr)
    raise RuntimeError(f"extract_kpis failed for {sample_id}") from e
```

**Problem:** `stdout`/`stderr` with diagnostic output are discarded. Operator must manually inspect per-sample log files.

**Fix:** Include `e.stdout` and `e.stderr` in the raised exception message.

---

### H-4: Campaign top-level exception not recorded in `run.json`
**Severity:** HIGH | **Category:** Bug | **File:** `osimflow/campaign.py:1880–1882`

```python
except Exception:
    log.exception("campaign failed")
    raise
```

**Problem:** `log.exception()` correctly preserves traceback in logs, but `run.json` structured artifact does NOT capture exception type/message. Operator must rely on log files instead of structured monitoring.

**Fix:** Call `_record_failure` with exception type and message before re-raising.

---

### H-5: `shell=True` in stub simulation mode
**Severity:** HIGH | **Category:** Security | **File:** `osimflow/work.py:646–655`

```python
cmd = f"python -c \"...{sample_id}...{openstudio_version}...\""
run_subprocess(cmd, shell=True, ...)
```

**Problem:** While `sample_id` and `version` are framework-controlled (not user raw input), the `shell=True` pattern is active when `openstudio.cli` is absent and `OSIMFLOW_STUB_SIM` is not set. Future refactoring could introduce injection risk.

**Fix:** Use list argv form without `shell=True`.

---

### H-6: Cache key separator `"|"` not escaped — potential collision
**Severity:** MEDIUM→HIGH | **Category:** Architecture | **File:** `osimflow/cache.py`

Cache keys are constructed as `"|".join(key_components)`. If any component contains `"|"`, key collision/collation could be unpredictable.

**Fix:** Use a separator that cannot appear in key components, or SHA-256 hash of the tuple.

---

### H-7: `_update_sample_state_safely` silently catches all exceptions forever
**Severity:** MEDIUM→HIGH | **Category:** Bug | **File:** `osimflow/campaign.py:1717–1721`

```python
except Exception as exc:
    log.warning("failed to persist sample state: %s", exc)
    # silently continues forever
```

**Problem:** If JSON serialization persistently fails, the function loops infinitely without alerting the operator. No data loss indication until campaign end.

**Fix:** After N consecutive failures (e.g., 3), emit error-level log and optionally abort.

---

### H-8: `cwd=sim_out` may break OpenStudio workflow relative paths
**Severity:** HIGH | **Category:** BEM | **File:** `osimflow/work.py:879–884`

```python
run_subprocess(cmd, cwd=sim_out, ...)
```

**Problem:** OpenStudio CLI resolves relative paths in `.osw` relative to `cwd`, not relative to the workflow file's directory. If the workflow contains `"files": ["model osm/building.osm"]`, the CLI looks in `sim_out/model osm/` not `modified_sim_package/`.

**Fix:** Pass `cwd=modified_sim_package` (directory containing the workflow and all model files).

---

### H-9: Container image no digest pinning
**Severity:** MEDIUM | **Category:** Architecture | **File:** `osimflow/campaign.py`

`container_digest` is computed from the tag format string, not the actual image digest. If Docker Hub re-tags `nrel/openstudio:3.11.0` with a newer image, the campaign will not detect the change and will reuse cached results.

**Fix:** Add `--container-digest` option to pin to a specific image SHA256.

---

### H-10: `manifest._SEVERE_RE` misses `** Severe` format
**Severity:** HIGH | **Category:** BEM | **File:** `osimflow/manifest.py:66`

```python
_SEVERE_RE = re.compile(r"^[ \t]{2}\*[ \t]+Severe[^\n]*", re.MULTILINE)
```

Only matches `"  * Severe"` (single asterisk). `first_severe_error` returns `None` even when severe errors are present in `** Severe` format.

**Fix:** Update regex to `r"[ \t]{2}\*{1,2}[ \t]+Severe"` or match `aggregate_results.py` pattern.

---

### H-11: `LocalExecutor.submit()` has no exception wrapping
**Severity:** MEDIUM | **Category:** Bug | **File:** `osimflow/executors/__init__.py:157–201`

When a simulation fails in `LocalExecutor`, the traceback shows thread-pool dispatcher context, not logical campaign context (sample_id, step).

**Fix:** Wrap `self._pool.submit()` in try/except and transform exception to include task name.

---

### H-12: Registry failure is warning-only — silent fallback to untracked execution
**Severity:** MEDIUM | **Category:** Bug | **File:** `osimflow/campaign.py:302–305`

```python
except Exception as exc:
    log.warning("could not open campaign registry: %s (continuing without)", exc)
    self._registry = None
```

Campaign proceeds untracked. Operator may not realize until `osimflow list` shows nothing.

**Fix:** Log at error level; emit `_registry_available: False` in `run.json`.

---

### H-13: `bin/*.py` argument signatures not validated against actual scripts
**Severity:** MEDIUM | **Category:** Architecture | **File:** `osimflow/work.py`

Argument signatures are hardcoded in `work.py`. If a `bin/*.py` script changes argument names, the campaign silently passes wrong arguments.

**Fix:** Add `--dry-run` verification that parses `bin/*.py --help` and validates signatures.

---

## MEDIUM Severity Issues

### M-1: BYOS temp file uses `delete=False` — crash could leave JSON on disk
**Severity:** MEDIUM | **Category:** Security | **File:** `osimflow/byos.py:275–282`

Temp file created with `NamedTemporaryFile(delete=False)` could survive a crash between creation and deletion, exposing resolved paths in JSON payload.

**Fix:** Use `delete=True` and a derivate approach, or use ` tempfile.mkdtemp()` + explicit cleanup.

---

### M-2: `variables.yml` modified in-place after type coercion
**Severity:** MEDIUM | **Category:** Security | **File:** `osimflow/config.py:307–386`

`_coerce_variables_yml_file` writes normalized YAML back to the source file, changing user input on disk. Breaks idempotency (running same campaign twice produces different file contents).

**Fix:** Write to separate output file or keep coercion in-memory.

---

### M-3: Hardcoded 600-second BYOS subprocess timeout
**Severity:** MEDIUM | **Category:** Security | **File:** `osimflow/work.py:305`

Jobs on constrained hardware may legitimately need longer. No CLI flag to override.

**Fix:** Add `--byos_timeout_s` CLI flag.

---

### M-4: `cancel()` not in `BaseExecutor.Handle` ABC contract
**Severity:** MEDIUM | **Category:** Architecture | **File:** `osimflow/executors/base.py`

`KubernetesExecutor` has `.cancel()` but it's not part of the base contract. Callers cannot reliably cancel across executors without `isinstance` checks.

**Fix:** Add `cancel()` to `Handle` ABC with default `NotImplementedError`.

---

### M-5: No DAG topological validation — manual `await` ordering
**Severity:** MEDIUM | **Category:** Architecture | **File:** `osimflow/campaign.py`

Future steps that should run in parallel (e.g., `step_generate_plots` with `step_extract_kpis`) require manual reordering of `await` calls. No mechanism enforces step N does not depend on step N+1.

**Fix:** Consider a dependency graph validation at campaign start.

---

### M-6: Unmet-hours column/row names may not match EnergyPlus schema
**Severity:** MEDIUM | **Category:** BEM | **File:** `osimflow/_work_scripts/extract_kpis.py:273–350`

Row/column positions may be **swapped** for several candidates. `"During Occupied Heating"` may not exist as a column header in standard EnergyPlus output.

**Fix:** Verify against actual EnergyPlus output schema for relevant versions.

---

### M-7: EUI unit assumption (MJ vs kBtu) unverified
**Severity:** MEDIUM | **Category:** BEM | **File:** `osimflow/_work_scripts/extract_kpis.py:121–132`

Code assumes `"Energy Per Total Building Area"` is in MJ/m²/yr and converts with `_MJ_TO_KWH = 1/3.6`. US projects typically use kBtu/ft²/yr, making the conversion completely wrong.

**Fix:** Detect or specify unit system explicitly before conversion.

---

### M-8: Only `stderr` logged on subprocess failure, `stdout` discarded
**Severity:** MEDIUM | **Category:** Bug | **File:** `osimflow/work.py:~930, ~960`

If work script writes error to stdout (or both), operator only sees stderr portion.

**Fix:** Log both `e.stdout` and `e.stderr`.

---

### M-9: Spot price parsing from `statusReason` is fragile
**Severity:** LOW→MEDIUM | **Category:** Architecture | **File:** `osimflow/executors/aws_batch.py`

If AWS changes `statusReason` format, spot detection silently fails.

---

### M-10: Stub mode not distinguished in `run.json`
**Severity:** LOW | **Category:** Architecture | **File:** `osimflow/work.py`

Campaign that ran in stub mode could be mistaken for successful real run.

---

### M-11: Hit rate computation racy under concurrent access
**Severity:** LOW | **Category:** Architecture | **File:** `osimflow/cache.py`

Hit/miss counters could race under concurrent LocalExecutor workers.

---

### M-12: No priority escalation for repeated reanalysis failures
**Severity:** LOW | **Category:** Architecture | **File:** `osimflow/data_point_manager.py`

Failed reanalysis doesn't auto-escalate priority.

---

### M-13: blanket `nosec` suppressions bypass bandit checks
**Severity:** MEDIUM | **Category:** Security | **File:** `osimflow/work.py:421, 916, 948, 1236, 1289`

```python
# nosec # sourcery skip: suspicious-subprocess-call
```

Future misuse of these patterns will go undetected.

**Fix:** Use narrow inline `# nosec: [S603]` with explanatory comment.

---

## LOW Severity Issues (Good Patterns / Minor)

### L-1: `CancelledError` suppression — Correct ✅
`contextlib.suppress(Exception, concurrent.futures.CancelledError)` correctly handles `CancelledError` as `BaseException`.

### L-2: `log.exception` + `raise` in `run()` — Correct ✅
Top-level campaign exception handler correctly preserves traceback in logs.

### L-3: `_is_transient_error` — Well-designed ✅
`_TRANSIENT_EXIT_CODES` frozenset is clean and maintainable.

### L-4: `run_subprocess` — Robust ✅
Creates stdout/stderr log files before subprocess; `errors="replace"` prevents crashes on non-UTF-8 bytes.

### L-5: `jobqueue.py` — Atomic JSON moves ✅
Uses atomic file moves for crash recovery.

### L-6: `byos.py` — Proper subprocess isolation ✅
CPU/memory limits, timeout, `preexec_fn=setsid`, signature validation.

### L-7: `_apply_parameters_via_cli` — Correct exception handling ✅
Catches `SubprocessError`, logs with context, re-raises descriptively.

### L-8: `_validate_model_geometry` is best-effort — documented ✅
Non-zero exit from quick parse logs warning but doesn't fail; preflight simulation is the real gate.

---

## Documentation Drift (From result-docs.md)

| # | Issue | Severity |
|---|---|---|
| 1 | ~30 files in `osimflow/` absent from AGENTS.md §3 directory map | HIGH |
| 2 | 14 algorithms registered but not documented in AGENTS.md | HIGH |
| 3 | `--bcl-api-key` documented but not implemented | HIGH |
| 4 | `--validate-measures` documented but not implemented | HIGH |
| 5 | `list-measures` documented but not implemented | HIGH |
| 6 | `warm-cache`, `cancel`, `pause`, `resume` subcommands implemented but undocumented | HIGH |
| 7 | `viz/` module missing from AGENTS.md §3 | HIGH |
| 8 | `docker_swarm_executor` duplicated in AGENTS.md §3 | MEDIUM |
| 9 | PRD says 6-step DAG, AGENTS.md says 7-step, actual is 11+ steps | MEDIUM |
| 10 | `--api-redis-url` implemented but undocumented | MEDIUM |
| 11 | `--project` implemented but undocumented | MEDIUM |
| 12 | AWS Batch marked "future/stub" in §2 but fully implemented | MEDIUM |
| 13 | `NomadExecutor` missing from §2 Stack table | MEDIUM |
| 14 | `KubernetesExecutor` missing from §2 Stack table | MEDIUM |

---

## Consolidated Prioritization

### Immediate (Before Next Release)
1. **CR-1:** Fix preflight severe-error regex to handle both `*` and `**` formats
2. **CR-2:** Add file locking to `DataPointManager` for atomic state transitions
3. **H-2:** Fix `raise last_exc from None` to preserve exception chain
4. **H-3:** Preserve stdout/stderr in `CalledProcessError` → `RuntimeError` conversion
5. **H-8:** Fix `cwd=sim_out` to `cwd=modified_sim_package`

### Soon (Before MVP)
6. **CR-3 / H-3:** Make `BaseExecutor.submit()` abstract
7. **H-4:** Record top-level exception in `run.json`
8. **H-5:** Remove `shell=True` from stub mode
9. **H-7:** Add failure limit to `_update_sample_state_safely`
10. **H-10:** Fix `manifest._SEVERE_RE` to handle both formats
11. **M-13:** Narrow blanket `nosec` suppressions
12. **M-2:** Don't mutate `variables.yml` in-place

### Before Phase 3
13. **H-6:** Fix cache key separator escaping
14. **H-9:** Add container digest pinning
15. **H-11:** Add exception wrapping to `LocalExecutor.submit()`
16. **M-1:** Fix BYOS temp file cleanup
17. **M-6/M-7:** Verify BEM KPI extraction against actual EnergyPlus output
18. All docs drift items from §4 of this report

---

*Report consolidated from: result-architecture.md, result-debug.md, result-docs.md, and security review findings.*
