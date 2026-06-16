## Gap ID
ARCH-005

## Source
gap-analysis-architecture

## Description
OSimFlow has no distributed shared storage model. All workers write to the local `outdir`. For multi-node campaigns, there is no:
- Shared filesystem (NFS, EFS, GCSfuse)
- Distributed result aggregation
- Path consistency across workers

openstudio-server uses Docker volumes with distributed storage backends.

## Evidence
- `osimflow/campaign.py` — local outdir assumption
- No S3/GS/Azure Blob result publication
- Per-sample files written locally

## Severity
Major

## Recommended Mitigation
- Phase 1: Support S3/GS/Azure Blob as result storage backend
- Phase 2: Add shared filesystem mounting guidance for HPC
- Phase 3: Implement result aggregation pipeline

## Labels
gap-analysis, architecture, storage, major
