## Gap ID
ARCH-001

## Source
gap-analysis-architecture

## Description
OSimFlow has no distributed task queue. All job submission goes through the Campaign orchestrator directly to executors. There is no Celery-like queue that can:
- Accept jobs from multiple producers
- Distribute work to multiple workers
- Provide retry, dead-letter, and result persistence

openstudio-server uses Celery + Redis for distributed task queuing.

## Evidence
- `osimflow/campaign.py` — direct executor.submit() calls
- No task queue abstraction
- No worker process separate from orchestrator

## Severity
Critical

## Recommended Mitigation
Integrate dask-jobqueue (already in PRD stack) for distributed task queuing. This resolves ARCH-001 and ARCH-004 (horizontal worker scaling) simultaneously.

## Labels
gap-analysis, architecture, distributed, task-queue, critical
