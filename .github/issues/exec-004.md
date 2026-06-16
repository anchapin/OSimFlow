## Gap ID
EXEC-004

## Source
gap-analysis-execution-backend

## Description
OSimFlow's SlurmExecutor only supports Slurm via submitit. There is no PBS (Torque) or LSF support. openstudio-server supports all three via Docker Swarm abstraction.

## Evidence
- `osimflow/executors/slurm_executor.py` — only Slurm
- No PBS executor
- No LSF executor

## Severity
Major

## Recommended Mitigation
- Phase 1: Add PBSExecutor using submitit-compatible interface
- Phase 2: Add LSFExecutor
- Document that dask-jobqueue supports PBS/LSF natively

## Labels
gap-analysis, executor, hpc, pbs, lsf, major
