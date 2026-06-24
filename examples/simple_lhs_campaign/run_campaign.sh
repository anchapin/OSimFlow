#!/bin/bash
# run_campaign.sh - Simple LHS Campaign Runner
#
# Usage: ./run_campaign.sh [n_samples] [openstudio_version]
#
# Defaults:
#   n_samples: 20
#   openstudio_version: 3.11.0

set -euo pipefail

# Configuration
N_SAMPLES="${1:-20}"
OPENSTUDIO_VERSION="${2:-3.11.0}"
OUTDIR="./results"

# Get directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARIABLES="${SCRIPT_DIR}/variables.yml"
TEMPLATE="${SCRIPT_DIR}/template_sim_package"

echo "=========================================="
echo "OSimFlow Simple LHS Campaign"
echo "=========================================="
echo "Samples: ${N_SAMPLES}"
echo "OpenStudio Version: ${OPENSTUDIO_VERSION}"
echo "Output Directory: ${OUTDIR}"
echo "=========================================="

# Run the campaign
osimflow run \
  --executor local \
  --input_variables "${VARIABLES}" \
  --template_sim_package "${TEMPLATE}" \
  --n_samples "${N_SAMPLES}" \
  --outdir "${OUTDIR}" \
  --openstudio_version "${OPENSTUDIO_VERSION}"

echo ""
echo "=========================================="
echo "Campaign Complete!"
echo "=========================================="
echo "Results saved to: ${OUTDIR}"
echo ""
echo "Next steps:"
echo "  1. Review results: cat ${OUTDIR}/run.json"
echo "  2. Check KPIs: ls ${OUTDIR}/kpis/"
echo "  3. View plots: ls ${OUTDIR}/plots/"
echo "  4. Compare with baseline:"
echo "     python -c \"import pandas as pd; df=pd.read_csv('${OUTDIR}/aggregated_results.csv'); print(df.describe())\""