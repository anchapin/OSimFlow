#!/usr/bin/env bash
# smoke_test.sh — Verify an osimflow binary/install works correctly.
#
# Usage:
#   ./packaging/scripts/smoke_test.sh [/path/to/osimflow]
#
# If no argument is given, assumes 'osimflow' is on PATH.
# Exit code 0 = all checks pass, non-zero = failure.
set -euo pipefail

OSIMFLOW="${1:-osimflow}"
FAILED=0

echo "=== OSimFlow Smoke Test ==="
echo "Binary: ${OSIMFLOW}"
echo ""

# Check 1: --help exits 0
echo -n "Check 1: osimflow --help ... "
if "${OSIMFLOW}" --help > /dev/null 2>&1; then
    echo "PASS"
else
    echo "FAIL (exit code $?)"
    FAILED=1
fi

# Check 2: --version exits 0
echo -n "Check 2: osimflow --version ... "
if "${OSIMFLOW}" --version > /dev/null 2>&1; then
    echo "PASS"
else
    echo "FAIL (exit code $?)"
    FAILED=1
fi

# Check 3: dry-run 1-sample campaign exits 0
# Requires a minimal template_sim_package. We create a temp one.
echo -n "Check 3: osimflow run --dry-run (1 sample) ... "
TMPDIR=$(mktemp -d)
trap 'rm -rf "${TMPDIR}"' EXIT

# Minimal template package
mkdir -p "${TMPDIR}/template"
echo '{"seed": true}' > "${TMPDIR}/template/workflow.osw"

# Minimal variables.yml
cat > "${TMPDIR}/variables.yml" <<'EOF'
variables:
  - name: wall_r_value
    distribution: uniform
    min: 2.0
    max: 10.0
EOF

mkdir -p "${TMPDIR}/results"

if "${OSIMFLOW}" run \
    --executor local \
    --dry-run \
    --input_variables "${TMPDIR}/variables.yml" \
    --template_sim_package "${TMPDIR}/template" \
    --n_samples 1 \
    --outdir "${TMPDIR}/results" \
    > /dev/null 2>&1; then
    echo "PASS"
else
    echo "FAIL (exit code $?)"
    FAILED=1
fi

# Check 4 (only for onedir builds): unpacked size ≤ 220 MB
if [ -d "dist/osimflow" ]; then
    SIZE_KB=$(du -sk dist/osimflow | cut -f1)
    SIZE_MB=$((SIZE_KB / 1024))
    echo -n "Check 4: bundle size ≤ 220 MB (${SIZE_MB} MB) ... "
    if [ "${SIZE_MB}" -le 220 ]; then
        echo "PASS"
    else
        echo "FAIL (size ${SIZE_MB} MB exceeds 220 MB budget)"
        FAILED=1
    fi
elif [ -f "dist/osimflow.exe" ]; then
    SIZE_BYTES=$(stat -f%z dist/osimflow.exe 2>/dev/null || stat -c%s dist/osimflow.exe 2>/dev/null || echo 0)
    SIZE_MB=$((SIZE_BYTES / 1048576))
    echo -n "Check 4: exe size (${SIZE_MB} MB) ... "
    echo "INFO (onefile, no hard limit)"
fi

echo ""
if [ "${FAILED}" -eq 0 ]; then
    echo "=== All checks PASSED ==="
else
    echo "=== Some checks FAILED ==="
fi
exit "${FAILED}"
