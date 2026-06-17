## Gap ID
EXEC-006

## Source
gap-analysis-execution-backend

## Description
Spot/preemptible instance handling is only implemented in AWSBatchExecutor. AzureBatchExecutor and GoogleBatchExecutor stubs have no spot interruption handling at all.

## Evidence
- `osimflow/executors/aws_batch_executor.py` — spot handling exists
- `osimflow/executors/azure_batch_executor.py` — no spot handling
- `osimflow/executors/google_batch_executor.py` — no spot handling

## Severity
Major

## Recommended Mitigation
Implement spot interruption handling for Azure and Google Batch executors following the same pattern as AWSBatchExecutor. Use Azure Spot VMs and Google Preemptible VMs respectively.

## Labels
gap-analysis, executor, spot, azure, google-cloud, major
