#!/bin/bash
# run_optimization.sh - Multi-Objective Optimization Runner
#
# Usage: ./run_optimization.sh [n_samples] [max_generations] [openstudio_version]
#
# Defaults:
#   n_samples: 100 (population size per generation)
#   max_generations: 50
#   openstudio_version: 3.11.0

set -euo pipefail

# Configuration
N_SAMPLES="${1:-100}"
MAX_GENERATIONS="${2:-50}"
OPENSTUDIO_VERSION="${3:-3.11.0}"
OUTDIR="./optimization_results"

# Get directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARIABLES="${SCRIPT_DIR}/variables.yml"
TEMPLATE="${SCRIPT_DIR}/template_sim_package"

echo "=========================================="
echo "OSimFlow Multi-Objective Optimization"
echo "=========================================="
echo "Algorithm: NSGA-II"
echo "Population Size: ${N_SAMPLES}"
echo "Max Generations: ${MAX_GENERATIONS}"
echo "OpenStudio Version: ${OPENSTUDIO_VERSION}"
echo "Output Directory: ${OUTDIR}"
echo "=========================================="

# Run the optimization campaign
osimflow run \
  --executor slurm \
  --algorithm nsga2 \
  --input_variables "${VARIABLES}" \
  --template_sim_package "${TEMPLATE}" \
  --n_samples "${N_SAMPLES}" \
  --max-generations "${MAX_GENERATIONS}" \
  --outdir "${OUTDIR}" \
  --openstudio_version "${OPENSTUDIO_VERSION}"

echo ""
echo "=========================================="
echo "Optimization Complete!"
echo "=========================================="
echo "Results saved to: ${OUTDIR}"
echo ""
echo "Pareto front saved to:"
echo "  ${OUTDIR}/pareto/"
echo ""
echo "View Pareto front:"
echo "  cat ${OUTDIR}/pareto/pareto_front.json | python -m json.tool"
echo ""
echo "Analyze trade-offs:"
echo "  python -c \"import pandas as pd; df=pd.read_csv('${OUTDIR}/aggregated_results.csv'); print(df[['eui','construction_cost']].describe())\""