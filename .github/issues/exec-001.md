## Gap ID
EXEC-001

## Source
gap-analysis-execution-backend

## Description
The AzureBatchExecutor and GoogleBatchExecutor are placeholder implementations that will silently fail to run simulations correctly. The Azure executor uses `sleep infinity` as its command, and the Google executor uses a synchronous client in an async context.

## Evidence
- `osimflow/executors/azure_batch_executor.py` — uses `sleep infinity` as command
- `osimflow/executors/google_batch_executor.py` — synchronous client in async context

## Severity
Critical

## Recommended Mitigation
Either implement them properly or remove them from the public API to prevent user confusion. Priority: implement Azure Batch first (most commonly requested), then Google Cloud Batch.

## Labels
gap-analysis, executor, azure, google-cloud, critical
