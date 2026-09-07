# ADR-0005 — One handle contract; three queue modules with distinct roles (issue #1543)

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** OSimFlow maintainers
**Supersedes:** none
**Superseded by:** none
**Related:** #1543, #1462, #1464, #1465, #263, #1397, ADR-0004, AGENTS.md §5
(`taskqueue.py`, `jobqueue.py`, `distributed_jobqueue.py`,
`executors/base.py`, `campaign.py`)

> **Implementation status (2026-09-06):** Implemented.
> `taskqueue.TaskHandle` subclasses `executors.base.Handle`;
> `campaign.py` references only `Handle` at every fan-out,
> checkpoint, and retry call site (the `TQHandle` alias and the
> `Handle | TQHandle` unions are gone). The semantics of
> `jobqueue.py` and `distributed_jobqueue.py` are unchanged —
> this ADR documents them, it does not modify them.

## Context

Three queue-ish subsystems coexisted in `osimflow/`, each grown to
serve a different need, with overlapping vocabulary and no shared
adapter:

| Module | Grew from | Vocabulary | Owns |
|---|---|---|---|
| `osimflow/taskqueue.py` | ARCH-001/ARCH-004 (dask.distributed integration) | `ProducerQueue` / `ConsumerQueue` / `TaskHandle` / `TaskQueueStatus` | **Work dispatch** — getting per-sample callables executed (opt-in via `--task-queue dask`) |
| `osimflow/jobqueue.py` | issue #263 (crash recovery) | filesystem `JobQueue`, enqueue/mark-completed/mark-failed | **Crash-recovery journal** — which `{sample}_{step}` items were in flight when the process died |
| `osimflow/distributed_jobqueue.py` | issue #1397 (Redis control plane) | `DistributedJobQueue`, Redis pub/sub, own `CircuitBreaker` | **Control-plane broadcast** — cross-worker visibility of job-state transitions |

Meanwhile `Campaign` threaded the union type
`Handle | TaskHandle` (imported as `TQHandle`) through at least ten
call sites: `_submit_and_await_all`'s `submissions` mapping and
`resubmit_callback` signature, the per-step fan-out loops
(`APPLY_PARAMETERS`, `RUN_OPENSTUDIO_SIM`, `EXTRACT_KPIS`), and the
`_on_success` closure's `_handle` capture. Every fan-out, checkpoint,
and retry path had to carry two handle shapes forever, and a future
fix to one (e.g. the deadline semantics of #1465, the poll-retry
state machine of #1464) would silently miss the other. Plug-in
authors faced the same confusion: the executor conformance suite
(issue #1478) verifies the `Handle` contract, but nothing explained
how `DaskTaskQueue` submission interacts with the same fan-out loop.

## Decision

1. **`taskqueue.TaskHandle` is a subclass of
   `executors.base.Handle`.** The work-dispatch queue returns handles
   that *are* executor handles: `result()`, `done()`, `is_failed()`,
   and the identity / worker-attribution fields (`job_id`,
   `worker_id`, `worker_ip`, `worker_region`, `cost_usd`,
   `billed_duration_seconds`, `error`) come from the base;
   queue-specific surface (`task_id`, `status`, `submitted_at`,
   `retry()`) is layered on top. `__post_init__` mirrors `task_id`
   onto `job_id` (whichever identifier was supplied wins) so
   Handle-typed consumers see a meaningful job id.
2. **`Campaign` and the fan-out loop reference only `Handle`.**
   The `TQHandle` import alias and every `Handle | TQHandle` union
   are removed; `task_queue.submit(...)` and `executor.submit(...)`
   flow into the same variable, the same `submissions` mapping, and
   the same `resubmit_callback` signature.
3. **Subclass, not wrap-at-boundary.** Wrapping `TaskHandle` in an
   adapter at `build_task_queue` was considered and rejected: the
   handles are created per-submission inside
   `NoOpTaskQueue` / `DaskTaskQueue.submit`, not by the factory, so
   wrapping would either bifurcate the type again inside the queue
   methods or require every queue method signature to change.
   Direct inheritance keeps `ConsumerQueue.get_result(handle)` /
   `retry(handle)` operating on the concrete `TaskHandle` while the
   orchestrator sees the base type. The import is cycle-free:
   `executors.base`'s import closure (`_rate_limiter`, `transport` →
   `storage`) never touches `taskqueue`, and every existing import
   chain (`osimflow/__init__.py`, `campaign.py`) loads `executors`
   before `taskqueue`.
4. **Deliberate, documented LSP widening.** `Handle._future` is
   typed non-`Optional` (executors always attach a backing future);
   `TaskHandle._future` remains `Future | None` because a queued
   handle may exist before a future is attached. The override of
   `result()` fails closed (`RuntimeError`) on `None` before the
   base implementation could observe it — the single
   `# type: ignore[assignment]` on the field is the honest record
   of that decision. `done()` keeps queue semantics (terminality
   tracked on `status`, not by probing the future) since a PENDING
   dask handle whose future has already resolved is still awaiting
   its state transition.
5. **The three queue modules keep distinct roles.** They are *not*
   converging into one subsystem:
   - **Work dispatch (`taskqueue.py`)** — moves *callables* to where
     compute is. Best-effort result future, opt-in, no durability
     guarantee of its own.
   - **Crash-recovery journal (`jobqueue.py`)** — a *local
     filesystem* record of which `{sample}_{step}` items were
     enqueued / completed / failed, consumed on restart to
     reconstruct what was in flight. It never dispatches work.
   - **Control-plane broadcast (`distributed_jobqueue.py`)** —
     *Redis pub/sub* fan-out of job-state transitions to other
     workers/coordinators, with its own `CircuitBreaker` (#1397,
     ADR-0004 plane 3). It never dispatches work either.
   A future change that needs "a queue" must say which of the three
   roles it is fulfilling; adding a fourth overlapping vocabulary is
   the anti-pattern this ADR closes.

## Alternatives Considered

### Wrap `TaskHandle` in a `Handle` adapter at `build_task_queue`

Rejected — see Decision §3. The factory builds *queues*, not
handles; the wrap point would actually be each queue's
`submit`/`enqueue`, which either reintroduces the two-shape problem
inside the queue or changes every method signature to return the
adapter. Inheritance expresses the intended relationship ("a queued
task's handle *is* a job handle") without an extra layer.

### Merge the three queue modules into one

Rejected. They share only the word "queue". Work dispatch needs
futures and opt-in backends; the journal needs filesystem atomicity
and restart semantics; the broadcast needs Redis pub/sub and a
circuit breaker. Merging them couples three failure modes and three
config surfaces (ADR-0004's four-plane blast-radius lesson, in
miniature).

### Make `Handle` a protocol (structural typing)

Rejected for now. `Handle` is a dataclass carrying defaulted
worker-attribution state that executors mutate after submit; a
Protocol would push field-compatibility onto every implementer
without removing the union at the call sites (both shapes would
still need to satisfy it explicitly). The conformance suite
(#1478) already treats `Handle` as the nominal contract; a protocol
migration can ride on top of this ADR later if a non-dataclass
handle appears.

## Consequences

### Positive

- **One type at ten call sites.** Fan-out, checkpoint, and retry
  paths in `campaign.py` handle a single `Handle` type; the next
  Handle-semantics fix (deadline propagation, worker attribution)
  lands once and reaches task-queue submissions for free.
- **Plug-in clarity.** Third-party executor authors read one handle
  contract (`executors/base.py` + the conformance suite); the dask
  path provably produces the same shape (`issubclass(TaskHandle,
  Handle)` is pinned by unit test).
- **Zero behavior change.** The queue-specific `done()` / `result()`
  semantics are preserved verbatim by overrides; the campaign never
  calls anything on a task handle that it did not call before.

### Negative

- `TaskHandle`'s dataclass `__repr__` / `__eq__` now include the
  inherited fields (all default-`None` / synced for legacy
  constructions, so legacy-equal handles remain equal — but repr
  output is longer).
- The single `type: ignore[assignment]` on `_future` is a standing
  exception that must be re-justified if `Handle._future`'s type
  ever changes.

### Neutral

- `jobqueue.py` and `distributed_jobqueue.py` are untouched;
  their semantics and tests are exactly as before.
- `TaskHandle` remains public API (`osimflow/__init__.py` export
  unchanged); existing keyword construction
  (`TaskHandle(task_id=..., status=..., _future=...)`) is
  source-compatible. Positional construction order changed (base
  fields come first) — no in-tree caller constructs positionally.

## Gap Closure Criteria

This ADR can be superseded when:

1. `Handle` migrates to a structural protocol (then the subclass
   relationship becomes conformance, and this ADR's §3 rationale
   is revisited), or
2. the dask work-dispatch path grows real deadline / retry
   semantics of its own that diverge from `PollingHandle`'s
   (#1464/#1465) — at which point the shared state machine should
   be extracted and both handle families should delegate to it.

## References

- Issue #1543 — this ADR's motivating issue (unify the handle
  contract; document the three queue roles).
- Issue #1464 — `PollingHandle` shared poll-retry-fallback state
  machine (the "fix lands once" motivation).
- Issue #1465 — handle deadline semantics (ditto).
- Issue #263 — filesystem `JobQueue` crash-recovery journal.
- Issue #1397 — `DistributedJobQueue` control-plane circuit breaker.
- Issue #1478 — executor conformance suite (verifies the `Handle`
  contract for plug-in authors).
- ADR-0004 — the four Redis-backed planes (of which
  `distributed_jobqueue.py` is plane 3).
- `osimflow/executors/base.py` — `Handle` definition.
- `osimflow/taskqueue.py` — `TaskHandle(Handle)` + the queue ABCs.
- `tests/unit/test_taskqueue.py` — pins
  `issubclass(TaskHandle, Handle)` and the `job_id`/`task_id`
  mirroring.
