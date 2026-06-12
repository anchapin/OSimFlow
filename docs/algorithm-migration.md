# Algorithm Migration Guide

This document maps deprecated or out-of-scope OSS (OpenStudio Studio)
algorithms to their OSimFlow equivalents, with migration guidance.

---

## RGENOUD

**OSS mechanism:** R/Rserve-based genetic algorithm with gradient approximation.

**OSimFlow equivalent:** [`DifferentialEvolutionAlgorithm`](../osimflow/algorithms/de.py)
(`osimflow.algorithms.de`, registered as `"de"`).

**Why:** DE is a pure-Python evolutionary algorithm (no R/Rserve dependency).
It uses the same differential mutation/crossover schema as RGENOUD and
supports box constraints natively. Both are steady-state evolutionary
algorithms that do not require gradient information.

**Migration:**

```yaml
# variables.yml — no change to variable definitions
variables:
  wall_r_value:
    distribution: uniform
    min: 20
    max: 50

# Campaign config — replace algorithm name
# Old (OSS):
#   algorithm: rgenoud
# New (OSimFlow):
osimflow run \
  --algorithm de \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

**Limitations vs. RGENOUD:**
- DE does not perform gradient approximation; if your workflow requires
  gradient-based search, consider a scipy optimizer wrapped as a custom
  BYOS script.
- Multi-objective RGENOUD use-cases should use NSGA-II or SPEA-II instead.

---

## OPT-UNCOBJ-COMPASS

**OSS mechanism:** R/Rserve-based single-objective pattern-search
(Compass search) algorithm.

**OSimFlow equivalents:**

| Goal | OSimFlow algorithm | Registration name |
|------|-------------------|-------------------|
| Single-objective optimisation | `DifferentialEvolutionAlgorithm` | `"de"` |
| Multi-objective optimisation | `NSGA2Algorithm` | `"nsga2"` |
| Multi-objective optimisation | `SPEA2Algorithm` | `"spea2"` |

**Why:** COMPASS is a coordinate-pattern search for single-objective
problems. OSimFlow's `de` covers the same use-case with a more robust
evolutionary approach. For multi-objective problems, NSGA-II and SPEA-II
provide true Pareto-front generation.

**Migration (single-objective):**

```bash
osimflow run \
  --algorithm de \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

**Migration (multi-objective):**

```bash
# NSGA-II
osimflow run \
  --algorithm nsga2 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results

# SPEA-II (Strength Pareto Evolutionary Algorithm)
osimflow run \
  --algorithm spea2 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

---

## SPEA-II

**OSS mechanism:** R/Rserve-based Strength Pareto Evolutionary Algorithm 2.

**OSimFlow equivalent:** [`SPEA2Algorithm`](../osimflow/algorithms/spea2.py)
(`osimflow.algorithms.spea2`, registered as `"spea2"`).

**Status:** Available in this PR (issue #271). No migration needed —
use directly:

```bash
osimflow run \
  --algorithm spea2 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

**Algorithm notes:**
- SPEA-II maintains an external Pareto archive and uses k-nearest-neighbor
  crowding to preserve diversity.
- Hypervolume is used as the convergence criterion (same as NSGA-II).
- Configure convergence tolerance via the `hv_tol` parameter if needed.

---

## Summary table

| OSS algorithm | OSimFlow equivalent | Notes |
|---|---|---|
| RGENOUD | `de` | Pure Python, no R/Rserve |
| OPT-UNCOBJ-COMPASS | `de` (single-obj) or `nsga2`/`spea2` (multi-obj) | Compass-style pattern search replaced by evolutionary search |
| SPEA-II | `spea2` | Available natively in OSimFlow |
