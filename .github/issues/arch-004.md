## Gap ID
ARCH-004

## Source
gap-analysis-architecture

## Description
OSimFlow has no horizontal worker auto-scaling infrastructure. The number of workers is fixed at campaign launch time. There is no mechanism to:
- Add workers dynamically as load increases
- Remove workers when idle
- React to queue depth

openstudio-server uses Docker Swarm's built-in scaling via `docker service scale`.

## Evidence
- `osimflow/executors/` — fixed worker count
- No auto-scaling configuration
- No integration with cluster managers (Kubernetes, Nomad)

## Severity
Major

## Recommended Mitigation
Integrate dask-jobqueue for elastic scaling. This is already in the PRD stack and resolves ARCH-001 and ARCH-004 simultaneously.

## Labels
gap-analysis, architecture, scaling, major
