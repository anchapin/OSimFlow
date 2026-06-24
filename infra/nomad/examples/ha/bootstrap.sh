#!/usr/bin/env bash
# bootstrap.sh — Nomad HA ACL bootstrap + policy registration (issue #123)
#
# Native host-OS deployment (issue #619). Run this once against an
# already-running native 3-server Nomad quorum (the servers and clients
# are started directly on the host OS, NOT via Docker Compose / `hind`).
# See README.md in this directory for the native bring-up procedure.
#
# This script:
#   1. Waits for Raft quorum (server leader election).
#   2. Bootstraps the ACL system (generates the initial management token).
#   3. Creates the "agent" (read-only) and "worker" (job submit) policies.
#   4. Generates a worker token for the NomadExecutor to use.
#   5. Writes tokens to infra/nomad/acl/tokens/ (git-ignored).
#
# Prerequisites:
#   - curl, jq on PATH
#   - NOMAD_ADDR pointing at a server (default: http://127.0.0.1:4646).
#     For a remote cluster, set this to one of your server endpoints.
#
# Security notes:
#   - The generated tokens are written to a git-ignored directory.
#   - The management token is printed once and stored locally — treat it
#     like a root password and rotate it after initial setup.
#   - The anonymous token is NOT modified — it retains the default deny-all
#     policy. Never grant anonymous access in production.
set -euo pipefail

NOMAD_ADDR="${NOMAD_ADDR:-http://127.0.0.1:4646}"
TOKEN_DIR="$(cd "$(dirname "$0")" && pwd)/../acl/tokens"
POLICY_DIR="$(cd "$(dirname "$0")" && pwd)/../acl/policies"
MAX_WAIT=120  # seconds to wait for quorum
POLL_INTERVAL=2

mkdir -p "${TOKEN_DIR}"

# ── 1. Wait for quorum ──────────────────────────────────────────
echo "Waiting for Nomad leader election (${MAX_WAIT}s timeout)..."
elapsed=0
while [ "${elapsed}" -lt "${MAX_WAIT}" ]; do
    leader=$(curl -sf "${NOMAD_ADDR}/v1/status/leader" 2>/dev/null || echo "")
    if [ -n "${leader}" ] && [ "${leader}" != '""' ]; then
        echo "Leader elected: ${leader}"
        break
    fi
    sleep "${POLL_INTERVAL}"
    elapsed=$((elapsed + POLL_INTERVAL))
done

if [ "${elapsed}" -ge "${MAX_WAIT}" ]; then
    echo "ERROR: Timed out waiting for Nomad leader" >&2
    exit 1
fi

# ── 2. Bootstrap ACL ────────────────────────────────────────────
echo "Bootstrapping ACL system..."
bootstrap_response=$(curl -sf -X POST "${NOMAD_ADDR}/v1/acl/bootstrap")
management_token=$(echo "${bootstrap_response}" | jq -r '.SecretID')

if [ -z "${management_token}" ] || [ "${management_token}" = "null" ]; then
    echo "ERROR: ACL bootstrap failed (already bootstrapped?)" >&2
    echo "If re-running, use the existing management token from ${TOKEN_DIR}/management.json"
    exit 1
fi

echo "${bootstrap_response}" | jq '.' > "${TOKEN_DIR}/management.json"
chmod 600 "${TOKEN_DIR}/management.json"
echo "Management token saved to ${TOKEN_DIR}/management.json"
echo ""
echo "⚠  IMPORTANT: This is the root management token. Store it securely."
echo "   It is git-ignored but will NOT be encrypted at rest."
echo ""

# ── 3. Register policies ────────────────────────────────────────
echo "Registering ACL policies..."

# Agent policy (read-only for operators)
curl -sf -X PUT \
    -H "X-Nomad-Token: ${management_token}" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg rules "$(cat "${POLICY_DIR}/agent.hcl")" '{ "Name": "agent", "Description": "Read-only agent/node access for operators", "Rules": $rules }')" \
    "${NOMAD_ADDR}/v1/acl/policy/agent" > /dev/null
echo "  ✓ agent policy registered"

# Worker policy (job submission for OSimFlow)
curl -sf -X PUT \
    -H "X-Nomad-Token: ${management_token}" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg rules "$(cat "${POLICY_DIR}/worker.hcl")" '{ "Name": "worker", "Description": "Least-privilege job submission for OSimFlow", "Rules": $rules }')" \
    "${NOMAD_ADDR}/v1/acl/policy/worker" > /dev/null
echo "  ✓ worker policy registered"

# ── 4. Generate worker token ────────────────────────────────────
echo "Generating worker token for NomadExecutor..."
worker_response=$(curl -sf -X POST \
    -H "X-Nomad-Token: ${management_token}" \
    -H "Content-Type: application/json" \
    -d '{
        "Name": "osimflow-worker",
        "Type": "client",
        "Policies": ["worker"],
        "ExpiryTTL": "720h"
    }' \
    "${NOMAD_ADDR}/v1/acl/token")

worker_token=$(echo "${worker_response}" | jq -r '.SecretID')
echo "${worker_response}" | jq '.' > "${TOKEN_DIR}/worker.json"
chmod 600 "${TOKEN_DIR}/worker.json"
echo "  ✓ Worker token saved to ${TOKEN_DIR}/worker.json"

# ── 5. Summary ──────────────────────────────────────────────────
echo ""
echo "Bootstrap complete."
echo ""
echo "To use with OSimFlow:"
echo "  export NOMAD_ADDR=${NOMAD_ADDR}"
echo "  export NOMAD_TOKEN=${worker_token}"
echo ""
echo "To tear down the cluster (native, per node):"
echo "  sudo systemctl stop nomad   # on each server and client node"
