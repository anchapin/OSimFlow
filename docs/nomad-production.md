# Nomad Production Deployment (issue #123)

This guide documents OSimFlow's **native** production-grade Nomad HA
deployment: cluster topology, ACL policies, native mTLS, and the bootstrap
recipe. It complements the `NomadExecutor` in
`osimflow/executors/__init__.py`.

> **Native host-OS topology (issue #619).** The Nomad control plane —
> servers and client agents — runs **directly on the host OS** (bare metal
> or VMs). The previous nested-containerization approach (a `hind` /
> Hashistack-in-Docker `docker-compose.yml` that ran the Nomad servers and
> client agents *inside* Docker containers, then bind-mounted the Docker
> socket so those containers could launch further containers) has been
> **removed**. OpenStudio simulations are highly compute-intensive, and
> Docker-in-Docker introduced significant, unnecessary performance overhead
> and a convoluted trust boundary. Isolation is now provided by strict ACL
> policies and native mTLS rather than nested containerization boundaries.
> The OpenStudio **workloads** still run as unprivileged Docker tasks via
> Nomad's Docker task driver — that is the normal Nomad execution model and
> is *not* nested containerization.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Nomad HA Cluster (native host OS)              │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │  Server 1    │◄─┤  Server 2    │◄─┤  Server 3    │       │
│  │ nomad agent  │──►│ nomad agent  │──►│ nomad agent  │ Raft │
│  │  (leader)    │   │ (follower)   │   │ (follower)   │ 3/3  │
│  └──────┬───────┘   └──────────────┘   └──────────────┘       │
│         │ HTTP API (:4646) — mTLS (issue #344)                │
│  ┌──────┴───────────────────────────────────┐                │
│  │                                          │                │
│  ▼                                          ▼                │
│  ┌──────────────┐                   ┌──────────────┐         │
│  │  Client 1    │                   │  Client 2    │         │
│  │ nomad agent  │                   │ nomad agent  │         │
│  │ (host OS)    │                   │ (host OS)    │         │
│  │ ──────────── │                   │ ──────────── │         │
│  │ Docker task  │                   │ Docker task  │         │
│  │ driver       │                   │ driver       │         │
│  │ (unpriv.)    │                   │ (unpriv.)    │         │
│  └──────────────┘                   └──────────────┘         │
│                                                             │
│  Each client agent runs natively on the host. Only the      │
│  OpenStudio *workload* containers are launched by the       │
│  unprivileged Docker task driver (privileged = false).      │
└─────────────────────────────────────────────────────────────┘
```

## Quick start (native bring-up)

The full native bring-up procedure — including a systemd unit, per-node
identity flags, and the join-list configuration — lives in
`infra/nomad/examples/ha/README.md`. In summary:

```bash
# 1. On each of 3 server hosts (edit retry_join IPs in server.hcl first):
nomad agent -config=server.hcl -node=nomad-server-1 -bind=10.0.0.11

# 2. On each compute (client) host:
nomad agent -config=client.hcl -node=<this-node> -bind=<this-node-ip>

# 3. Bootstrap ACLs + policies against any server:
export NOMAD_ADDR=http://10.0.0.11:4646
cd infra/nomad/examples/ha
./bootstrap.sh
```

After bootstrap, set the environment variables the NomadExecutor reads:

```bash
export NOMAD_ADDR=http://10.0.0.11:4646
export NOMAD_TOKEN=<worker token from bootstrap output>
```

## ACL model

The cluster runs with ACL enabled (deny-by-default). Three token types:

| Token type | Policy | Use case |
|---|---|---|
| **Management** | Full access (generated at bootstrap) | Initial setup, policy management. Rotate after setup. |
| **Worker** (`osimflow-worker`) | `infra/nomad/acl/policies/worker.hcl` — submit/read/dispatch jobs in `default` namespace | Used by `NomadExecutor` to submit OpenStudio simulation jobs. Expires after 720h. |
| **Agent** (optional) | `infra/nomad/acl/policies/agent.hcl` — read-only agent/node metadata | Monitoring dashboards, health checks. No job submission. |

### Anonymous access

Anonymous tokens are **not** granted any capabilities. The default
deny-all policy is never relaxed. In production, ensure no policy
is attached to the anonymous token.

## Security checklist

- [x] ACL enabled on all server and client nodes
- [x] No long-lived tokens in source code or config files
- [x] Worker token scoped to `default` namespace with minimum capabilities
- [x] Anonymous token retains deny-all default
- [x] `NOMAD_TOKEN` sourced from environment (same pattern as AWS IAM roles)
- [x] Control plane runs natively on the host OS — no nested containerization (issue #619)
- [x] Docker task driver `allow_privileged = false` on client nodes; workload jobs `privileged = false`
- [x] Docker socket consumed locally by the agent — not shared across a nested container boundary
- [x] **Production**: TLS enabled for HTTP API (issue #344)
- [x] **Production**: `NomadExecutor` fails closed when `NOMAD_TOKEN` is configured for a non-local address without TLS — the campaign refuses to start rather than transmit the ACL token in cleartext (SEC-009, issue #1450). Dev/test override: `--nomad-allow-insecure-token`.
- [x] **Production**: native mTLS is the primary isolation/trust boundary (replaces nested containerization)
- [ ] **Production**: Enable Gossip encryption with a pre-shared key
- [ ] **Production**: Store management token in Vault or a secrets manager
- [ ] **Production**: Set worker token `ExpiryTTL` to match your rotation schedule

## Executor integration

The `NomadExecutor` in `osimflow/executors/__init__.py` reads:

- `NOMAD_ADDR` — the cluster HTTP endpoint (default `http://127.0.0.1:4646`)
- `NOMAD_TOKEN` — the ACL token (required when ACL is enabled)
- `OSIMFLOW_PYTHON_CONTAINER_IMAGE` — optional override for the Python
  post-processing container used by APPLY/KPI/AGGREGATE/PLOTS steps.
  Use this when GHCR access is restricted on Nomad clients (for example,
  point to a mirrored/private registry image or a preloaded local tag).

Nomad result handling is **remote-first**. The default CLI behavior
(`--nomad-remote-results-only`) keeps result resolution on remote artifacts
(shared filesystem result hints or object-storage materialization when
`result_storage_backend` is configured). The legacy local-callable compatibility
mode remains available via `--no-nomad-remote-results-only` but is deprecated
and retained for only one minor release to support migration.

### OpenStack preload path (local-tag strategy)

If your Nomad workers cannot pull GHCR images directly, preload the Python
image and force OSimFlow to use a local tag:

```bash
# On each Nomad node
export PYTHON_IMAGE_SOURCE=ghcr.io/anchapin/scientific_python_image:latest
export PYTHON_IMAGE_LOCAL_TAG=scientific_python_image:local
export GHCR_USERNAME=<ghcr-username>        # optional if image is public to your env
export GHCR_TOKEN=<ghcr-read-token>         # optional if image is public to your env

./scripts/setup_nomad_vm.sh
```

The setup script verifies the local tag is runnable and writes:

```bash
/etc/profile.d/osimflow_nomad_env.sh
```

which exports `OSIMFLOW_PYTHON_CONTAINER_IMAGE=scientific_python_image:local`
for campaign runs on that node.

No constructor kwarg accepts a token — this matches the security
model of `AWSBatchExecutor` (IAM role) and `SlurmExecutor` (SSH key):
credentials come from the environment, not from code.

## Scale hardening playbook (Nomad campaigns)

OSimFlow now includes built-in Nomad scale controls for dispatch mode,
fan-out pacing/chunking, and coordinator sharding.

### Dispatch policy at scale

`--nomad-dispatch-policy` controls whether OSimFlow uses Nomad parameterized
dispatch jobs:

- `keep_manual` (default): keep direct job submission unless explicitly
  requested elsewhere.
- `force_dispatch`: always use dispatch mode.
- `auto_prefer_dispatch`: auto-switch to dispatch when estimated run size
  crosses the executor threshold (chunk-size driven; defaults to dispatching
  for larger fan-outs).

For large campaigns (thousands of samples), prefer `force_dispatch` to avoid
registering many near-identical job specs.

### 10k sample recommendation: shard first

For 10k-scale campaigns, run multiple coordinators with built-in sharding
instead of a single coordinator:

- Partition mode: `--shard-count N --shard-index K`
- Range mode: `--shard-start A --shard-end B`

This keeps each coordinator’s active fan-out, polling, and result staging
bounded.

### Staged load ramp (recommended)

Before launching full production scale, ramp in stages:

1. 500 samples
2. 2,000 samples
3. 5,000 samples
4. 10,000 samples

At each stage, validate queue depth, submission latency, allocation resolution
latency, and object-storage upload latency/error rate before increasing load.

### Operational considerations (polling, submission, storage)

- **Polling pressure:** tune `--nomad-poll-interval-s` and
  `--nomad-max-poll-interval-s` to reduce API thundering at high concurrency.
- **Submission backpressure:** use `--nomad-fanout-submit-chunk-size` and
  `--nomad-fanout-submit-rate-per-sec` to bound burst size and smooth submit
  rate.
- **Allocation resolution timeout:** adjust
  `--nomad-allocation-resolution-timeout-s` for busy clusters with delayed
  scheduling.
- **Result-storage backpressure:** keep remote storage enabled and monitor
  upload queue/retry behavior (bounded queue + retry with exponential
  backoff) so storage slowdowns do not destabilize the coordinator.

For cost optimization on the underlying compute (instance-type selection
and idle-compute auto-shutdown patterns that apply to any executor,
including Nomad client fleets), see the
[Cost Estimation & Optimization Guide](cost-estimation.md#cost-reduction-strategies).

## Files

| Path | Purpose |
|---|---|
| `infra/nomad/examples/ha/server.hcl` | Native server config template (shared by all 3 quorum nodes; per-node identity via `-node`/`-bind` flags) |
| `infra/nomad/examples/ha/client.hcl` | Native client config — unprivileged Docker task driver, ACL, mTLS template |
| `infra/nomad/examples/ha/bootstrap.sh` | ACL bootstrap + policy/token registration (cluster-agnostic) |
| `infra/nomad/examples/ha/README.md` | Native bring-up procedure, systemd unit, and migration path from the removed `hind` setup |
| `infra/nomad/osimflow_worker.hcl` | Parameterized OpenStudio workload job spec (`privileged = false`) |
| `infra/nomad/acl/policies/agent.hcl` | Read-only agent/node policy |
| `infra/nomad/acl/policies/worker.hcl` | Least-privilege job submission policy |
| `infra/nomad/acl/tokens/` | Generated tokens (git-ignored) |

## Production TLS (issue #344)

Nomad supports TLS for all HTTP, RPC, and Serf communications. For production
deployments, enable TLS with mTLS (mutual TLS) so that both the client and
server present certificates to each other. **Native mTLS is the primary
isolation and trust boundary for the cluster** — it replaces the boundary
that nested containerization (`hind`) attempted to provide.

### Certificate generation

Generate certificates using HashiCorp Consul's TLS cert commands:

```bash
# Create the CA
consul tls ca create

# Create server certificates (for each server)
consul tls cert create -dc=dc1 -server -domain=nomad

# Create client certificates (for the NomadExecutor)
consul tls cert create -dc=dc1 -client -domain=nomad
```

Alternatively, use your organization's internal PKI.

### Server configuration

Uncomment and configure the `tls {}` block in
`infra/nomad/examples/ha/server.hcl` (the shared server template; applies to
all three quorum nodes):

```hcl
tls {
  http = true
  rpc  = true

  ca_file   = "/etc/nomad/tls/nomad-ca.pem"
  cert_file = "/etc/nomad/tls/nomad-server.pem"
  key_file  = "/etc/nomad/tls/nomad-server-key.pem"

  verify_server_hostname = true
  verify_https_client    = true
}
```

For clients, use client certificates instead of server certificates in
`infra/nomad/examples/ha/client.hcl`:

```hcl
tls {
  http = true
  rpc  = true

  ca_file   = "/etc/nomad/tls/nomad-ca.pem"
  cert_file = "/etc/nomad/tls/nomad-client.pem"
  key_file  = "/etc/nomad/tls/nomad-client-key.pem"

  verify_server_hostname = true
  verify_https_client    = true
}
```

### NomadExecutor TLS configuration

The `NomadExecutor` supports TLS with the following CLI flags:

| Flag | Description |
|---|---|
| `--nomad-tls` | Enable TLS for the Nomad connection |
| `--nomad-tls-verify` | Enable/disable certificate verification (default: true) |
| `--nomad-cert` | Path to client certificate PEM file (for mTLS) |
| `--nomad-key` | Path to client private key PEM file (for mTLS) |
| `--nomad-ca-cert` | Path to CA certificate PEM file (to verify server cert) |
| `--nomad-allow-insecure-token` | Explicit opt-out permitting `NOMAD_TOKEN` over plaintext to a non-local address (dev/test only; issue #1450) |

#### Fail-closed token guard (issue #1450)

When `NOMAD_TOKEN` is set and the resolved Nomad address is **non-local**
(anything other than `localhost` / `127.0.0.1` / `::1`) **without TLS**,
`NomadExecutor` raises `ValueError` at construction — the campaign never
starts, so a misconfigured `--nomad-address` can no longer silently degrade
to plaintext token transmission. This mirrors the storage-endpoint guard
from issue #1386 (`--allow-insecure-storage-endpoint`).

To run a non-TLS cluster anyway (dev/test only), pass
`--nomad-allow-insecure-token`; the executor then proceeds with a loud
warning on both the warnings channel and the logger. Loopback addresses
remain exempt because loopback traffic never leaves the host.

Example usage with mTLS:

```bash
osimflow run \
  --executor nomad \
  --nomad-address https://nomad.example.com:4646 \
  --nomad-tls \
  --nomad-cert /path/to/nomad-client.pem \
  --nomad-key /path/to/nomad-client-key.pem \
  --nomad-ca-cert /path/to/nomad-ca.pem \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

For development with self-signed certificates, disable verification:

```bash
osimflow run \
  --executor nomad \
  --nomad-address https://localhost:4646 \
  --nomad-tls \
  --nomad-tls-verify=false \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./results
```

### Security notes

- **mTLS protects NOMAD_TOKEN**: When TLS is enabled, the ACL token is encrypted in transit, preventing interception (SEC-009)
- **Certificate verification**: Always verify server certificates in production to prevent MITM attacks
- **Client certificates**: For high-security environments, use mTLS so both client and server authenticate each other

## Coordinator High Availability

The Nomad cluster itself is highly available (3-server Raft quorum handles
server-side failures automatically). However, the OSimFlow **coordinator**
(`Campaign` class) is a single-instance process. See
[ADR-0003](../.agents/results/architecture/0003-coordinator-high-availability.md)
for the full analysis and supported HA patterns.

Two patterns are supported for coordinator HA with Nomad:

**Pattern 1 — Shared filesystem**: Mount a shared network storage (NFS,
etc.) as the `--outdir` on all Nomad client nodes running coordinator
jobs. The `JobQueue.recover()` mechanism handles crash recovery
automatically. See ADR-0003 §Pattern 1 for details.

**Pattern 2 — Campaign-per-worker (recommended)**: Launch multiple
coordinator jobs via Nomad, each processing a disjoint subset of samples
with its own `--outdir`. The Nomad job spec can use `spread` to distribute
coordinator jobs across availability zones. Sample partitioning is handled
by an external script or Airflow DAG that submits N coordinator jobs with
non-overlapping sample ranges. See ADR-0003 §Pattern 2 for details.
