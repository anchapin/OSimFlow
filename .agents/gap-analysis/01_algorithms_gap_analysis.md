# Algorithms & Sampling Methods Gap Analysis

## OSimFlow vs openstudio-server (OSAF)

**Date:** 2026-06-16
**Focus Area:** Algorithms & Sampling Methods
**Reference:** [Algorithm Migration Guide](../../docs/algorithm-migration.md), [Migration Guide](../../docs/migration-openstudio-server.md)

---

## 1. Feature Comparison Table

### 1.1 Optimization Algorithms

| Algorithm | openstudio-server | OSimFlow | Status | Implementation |
|-----------|-------------------|----------|--------|----------------|
| **NSGA-II** (Multi-objective) | ✅ `nsga_nrel` | ✅ `nsga2` | **Parity** | `pymoo.algorithms.moo.nsga2` |
| **R-NSGA-II** (Reference-based NSGA-II) | ✅ | ❌ | **Gap** | Not implemented |
| **SPEA2** (Strength Pareto) | ✅ | ✅ `spea2` | **Parity** | `pymoo.algorithms.moo.spea2` |
| **PSO** (Particle Swarm) | ✅ | ✅ `pso` | **Parity** | `pymoo.algorithms.moo.pso` |
| **DOE/GA** (Differential Evolution) | ✅ | ✅ `de` | **Parity** | `scipy.optimize.differential_evolution` |
| **Dual Annealing** | ❌ | ✅ `da` | **OSimFlow-only** | `scipy.optimize.dual_annealing` |
| **Genetic Algorithm (DEAP)** | ❌ | ✅ `ga` | **OSimFlow-only** | `deap` library |
| **RGENOUD** (Genetic + Gradient) | ✅ | ⚠️ Partial | **Gap** | DE lacks gradient approximation |

### 1.2 Calibration Algorithms

| Algorithm | openstudio-server | OSimFlow | Status | Notes |
|-----------|-------------------|----------|--------|-------|
| **BM25** (Energy calibration) | ✅ | ❌ | **Critical Gap** | Minimizes error vs measured data |
| **Other calibration** | ✅ | ❌ | **Critical Gap** | Auto-calibration to utility bills |

### 1.3 DOE / Sampling Methods

| Method | openstudio-server | OSimFlow | Status | Implementation |
|--------|-------------------|----------|--------|----------------|
| **Latin Hypercube (LHS)** | ✅ `lhs` | ✅ `lhs` | **Parity** | `scipy.stats.qmc.LatinHypercube` |
| **Sobol Sequence** | ✅ | ✅ `sobol` | **Parity** | `scipy.stats.qmc.Sobol` |
| **Halton Sequence** | ✅ | ✅ `halton` | **Parity** | `scipy.stats.qmc.Halton` |
| **Monte Carlo (Random)** | ✅ | ✅ `random` | **Parity** | `RandomSamplingAlgorithm` |
| **Full Factorial** | ✅ | ✅ `full_factorial` | **Parity** | `FullFactorialAlgorithm` |
| **Fractional Factorial** | ✅ | ❌ | **Gap** | Subset of full factorial combinations |
| **Repeat All** | ❌ | ✅ `repeat_all` | **OSimFlow-only** | Stochastic analysis |
| **Parameter Study** | ✅ | ❌ | **Gap** | Single-value or range sweep |

### 1.4 Sensitivity Analysis Methods

| Method | openstudio-server | OSimFlow | Status | Implementation |
|--------|-------------------|----------|--------|----------------|
| **Morris Method** | ✅ | ✅ `morris` | **Parity** | `SALib.analyze.morris` |
| **FAST99** (Fourier Amplitude) | ✅ | ✅ `fast99` | **Parity** | `SALib.analyze.fast99` |
| **Sobol Indices** | ✅ | ✅ `sobol` | **Parity** | `SALib.analyze.sobol` |
| **DGSM** (Derivative-based Global) | ✅ | ❌ | **Gap** | SALib supports but not wrapped |
| **PAWN** | ✅ | ❌ | **Gap** | Emerging sensitivity method |

### 1.5 Uncertainty Quantification

| Capability | openstudio-server | OSimFlow | Status | Notes |
|------------|-------------------|----------|--------|-------|
| **Uncertainty Propagation** | ✅ | ❌ | **Gap** | Monte Carlo with distribution propagation |
| **Probability of Failure** | ✅ | ❌ | **Gap** | Reliability analysis |
| **Confidence Intervals** | ✅ | ❌ | **Gap** | Statistical output analysis |

---

## 2. Identified Gaps

### 2.1 Critical Gaps

#### GAP-ALGO-001: No Calibration Algorithms
- **Gap Name:** Energy Calibration / BM25 Algorithm
- **Description:** openstudio-server provides calibration algorithms (BM25, etc.) that minimize error between simulated and measured energy end uses using utility bill data. OSimFlow has no equivalent.
- **Severity:** Critical
- **openstudio-server implementation:** R/Rserve-based calibration using OpenStudio resources + statistical matching
- **Impact:** Users cannot perform auto-calibration workflows that match simulation outputs to actual utility data without custom BYOS scripts.
- **Affected Use Cases:** Model calibration to measured data, inverse modeling, ASHRAE 14-tier compliance

#### GAP-ALGO-002: No R-NSGA-II (Reference-based NSGA-II)
- **Gap Name:** R-NSGA-II Multi-objective Optimizer
- **Description:** R-NSGA-II uses reference points/parego decomposition for multi-objective optimization. It is the preferred algorithm in many PAT workflows.
- **Severity:** Critical
- **openstudio-server implementation:** R-based `nsga2r` algorithm with adaptive reference point generation
- **Impact:** Multi-objective optimization users relying on R-NSGA-II must migrate to NSGA-II or SPEA2.
- **Workaround:** Use `nsga2` or `spea2` (pymoo-based) — both are production-quality but lack reference point adaptation.

#### GAP-ALGO-003: No Uncertainty Quantification Framework
- **Gap Name:** Uncertainty Propagation and UQ
- **Description:** openstudio-server provides explicit uncertainty quantification including probability of failure, confidence intervals, and distribution propagation analysis.
- **Severity:** Critical
- **openstudio-server implementation:** R-based UQ engine with Monte Carlo propagation
- **Impact:** Users requiring probabilistic risk analysis must implement custom solutions.
- **Note:** While OSimFlow's SALib-based sensitivity analysis (Morris, FAST99, Sobol) provides some UQ capabilities, there is no unified UQ framework or probability-of-failure analysis.

### 2.2 Major Gaps

#### GAP-ALGO-004: No Fractional Factorial Sampling
- **Gap Name:** Fractional Factorial DOE
- **Description:** Fractional factorial is a statistical DOE method that tests a subset of full factorial combinations, useful when full factorial is computationally infeasible.
- **Severity:** Major
- **openstudio-server implementation:** R-based `fractional_factorial` algorithm
- **Impact:** DOE users with many variables cannot use statistically-sound subset sampling.
- **Workaround:** FullFactorialAlgorithm with manual subsetting of levels

#### GAP-ALGO-005: No Parameter Study / Single-point Analysis
- **Gap Name:** Parameter Study Algorithm
- **Description:** A parameter study sweeps one or more variables across a defined range or set of values, evaluating all combinations. This is distinct from random/DOE sampling.
- **Severity:** Major
- **openstudio-server implementation:** R-based `parameter_study` algorithm
- **Impact:** Users wanting to sweep specific values (e.g., "test R-values from 20 to 50 in steps of 5") must use full factorial with discrete levels or custom BYOS scripts.

#### GAP-ALGO-006: RGENOUD — Gradient Approximation Not Available
- **Gap Name:** Genetic Algorithm with Gradient Approximation
- **Description:** openstudio-server's RGENOUD performs gradient approximation within the genetic algorithm, enabling hybrid search. OSimFlow's DifferentialEvolution lacks this.
- **Severity:** Major
- **openstudio-server implementation:** R-based `rgenoud` with gradient estimation
- **Impact:** Complex optimization problems requiring gradient-guided search have no native equivalent.
- **Workaround:** Custom BYOS script wrapping scipy optimizers with gradient support

#### GAP-ALGO-007: DGSM Sensitivity Not Wrapped
- **Gap Name:** Derivative-based Global Sensitivity Analysis
- **Description:** DGSM (Derivative-based Global Sensitivity Measure) is available in SALib but not wrapped in OSimFlow.
- **Severity:** Major
- **openstudio-server implementation:** R-based DGSM
- **Impact:** Users requiring DGSM must implement custom solutions.

### 2.3 Minor Gaps

#### GAP-ALGO-008: PAWN Sensitivity Method Not Available
- **Gap Name:** PAWN Sensitivity Analysis
- **Description:** PAWN is an emerging non-parametric sensitivity method available in SALib but not wrapped.
- **Severity:** Minor
- **openstudio-server implementation:** R-based PAWN
- **Impact:** Niche sensitivity analysis use cases require custom BYOS scripts.

---

## 3. Recommendations

### 3.1 Priority Recommendations (Phase 1-2)

| Priority | Recommendation | Rationale |
|----------|---------------|-----------|
| **P0** | Implement R-NSGA-II via pymoo | Multi-objective is a core use case; pymoo's nsga2 supports reference point adaptation |
| **P0** | Add CalibrationAlgorithm base class + BM25 implementation | Critical for ASHRAE 14-tier compliance and model calibration workflows |
| **P1** | Add FractionalFactorialAlgorithm | Common DOE need; can leverage existing FullFactorialAlgorithm structure |
| **P1** | Add UncertaintyQuantification class | Probability of failure, confidence intervals via Monte Carlo propagation |

### 3.2 Implementation Guidance

#### R-NSGA-II Implementation
```
# Reference: pymoo.algorithms.moo.nsga2.NSGA2 with reference_direction
# Use pymoo's built-in reference point support if available
# Fallback: Implement reference point adaptation from OSAF's R-NSGA-II specification
```

#### Calibration Algorithm Structure
```python
class CalibrationAlgorithm(BaseAlgorithm):
    """Base class for calibration algorithms."""
    
    def __init__(self, measured_data_path: Path, calibration_metric: str = "bm25"):
        self.measured_data_path = measured_data_path
        self.calibration_metric = calibration_metric
    
    def compute_error(self, simulated: dict, measured: dict) -> float:
        """Compute calibration error (BM25 or other metric)."""
        raise NotImplementedError
```

#### Fractional Factorial Implementation
```python
class FractionalFactorialAlgorithm(BaseAlgorithm):
    """Uses a statistical design generator to select subset of factorial combinations."""
    
    def __init__(self, resolution: int = 3):
        self.resolution = resolution  # III, IV, V standard resolutions
```

### 3.3 Plugin Architecture Extension

The AlgorithmRegistry already supports third-party plugins via `entry_points`. These gaps can be addressed via:

1. **Internal implementation** in `osimflow/algorithms/`
2. **Third-party plugins** declared in `[project.entry-points."osimflow.algorithms"]`
3. **BYOS scripts** for custom algorithm prototyping

---

## 4. Summary Matrix

| Category | openstudio-server | OSimFlow | Parity | Gaps |
|----------|-------------------|----------|--------|------|
| Multi-objective optimization | NSGA-II, R-NSGA-II, SPEA2 | NSGA2, SPEA2, PSO | 67% | R-NSGA-II |
| Single-objective optimization | DOE/GA, RGENOUD | DE, DA, GA | 67% | RGENOUD (gradient) |
| Calibration | BM25, auto-calibration | ❌ | 0% | BM25, calibration |
| DOE/Sampling | Full factorial, fractional, LHS, Monte Carlo | Full factorial, LHS, random, Sobol, Halton | 71% | Fractional factorial |
| Sensitivity analysis | Morris, FAST99, Sobol, DGSM, PAWN | Morris, FAST99, Sobol | 60% | DGSM, PAWN |
| Uncertainty quantification | UQ framework, probability of failure | ❌ | 0% | Full UQ framework |

**Overall Algorithm Parity: ~50%**

---

## 5. References

- Algorithm Migration Guide: `docs/algorithm-migration.md`
- openstudio-server migration: `docs/migration-openstudio-server.md`
- Existing OSimFlow algorithms: `osimflow/algorithms/`
- BaseAlgorithm interface: `osimflow/algorithms/__init__.py`