## Gap ID
EXEC-003

## Source
gap-analysis-execution-backend

## Description
`SlurmExecutor` defaults to `debug=True`, meaning jobs run locally via `submitit.DebugExecutor` unless explicitly set to `debug=False`. This is the documented submitit pattern but can cause confusion in production.

## Evidence
- `osimflow/executors/slurm_executor.py` — debug=True default
- AGENTS.md §8 Gotcha #10 confirms this
- No warning when running in debug mode

## Severity
Major

## Recommended Mitigation
1. Add warning log when debug=True
2. Consider renaming to --slurm-debug flag for clarity
3. Document the debug/production distinction clearly

## Labels
gap-analysis, executor, slurm, major
