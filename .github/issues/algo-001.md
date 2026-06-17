## Gap ID
ALGO-001

## Source
gap-analysis-algorithms

## Description
OSimFlow has no native Genetic Algorithm (GA) implementation. It has Differential Evolution and Dual Annealing, but not a classical GA with crossover, mutation, and selection operators.

openstudio-server supports GA via its `analysis_library`.

## Evidence
- `osimflow/algorithms/` — no ga.py file
- `AlgorithmRegistry` has no GA registered
- scipy.optimize has no GA

## Severity
Major

## Recommended Mitigation
Implement a `GeneticAlgorithm` class in `osimflow/algorithms/ga.py` using DEAP library (which provides canonical GA operators). Register it with `AlgorithmRegistry`.

## Labels
gap-analysis, algorithms, genetic-algorithm, major
