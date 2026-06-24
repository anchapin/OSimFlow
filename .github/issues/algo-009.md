## Gap ID
ALGO-009

## Source
gap-analysis-algorithms

## Description
The SobolAlgorithm generates quasi-random Sobol samples but computes no sensitivity indices. openstudio-server's `sobol` analysis type automatically computes first-order and total-effect indices from the sample results.

## Evidence
- `osimflow/algorithms/sobol.py` — only sample generation
- No sensitivity index computation
- No post-processing step for Sobol results

## Severity
Major

## Recommended Mitigation
Add a post-processing step to SobolAlgorithm that computes and stores sensitivity indices. Use SALib's `sobol.analyze()` function after KPI extraction.

## Labels
gap-analysis, algorithms, sensitivity, sobol, major
