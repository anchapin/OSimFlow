# Nomad Production Deployment (issue #123)

This directory contains the production-grade Nomad HA bootstrap recipe
for OSimFlow. It complements the basic `NomadExecutor` in
`osimflow/executors/__init__.py` with cluster topology, ACL policies,
and a bootstrap script.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Nomad HA Cluster                         │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                 │
│  │ Server 1 │◄─┤ Server 2 │◄─┤ Server 3 │   Raft quorum   │
│  │ (leader) │──►│(follower)│──►│(follower)│   (3/3)        │
│  └────┬─────┘  └──────────┘  └──────────┘                 │
│       │ HTTP API (:4646)                                     │
│  ┌────┴──────────────────────────────────┐                  │
│  │                                       │                  │
│  ▼                                       ▼                  │
│  ┌──────────┐                     ┌──────────┐             │
│  │ Client 1 │                     │ Client 2 │             │
│  │ (docker) │                     │ (docker) │             │
│  └──────────┘                     └──────────┘             │
│                                                             │
│  Each client mounts /var/run/docker.sock                    │
│  for the Docker task driver.                                │
└─────────────────────────────────────────────────────────────┘
```

## Quick start (local development)

```bash
cd infra/nomad/examples/ha
docker compose up -d
./bootstrap.sh
```

After bootstrap, set the environment variables the NomadExecutor reads:

```bash
export NOMAD_ADDR=http://localhost:4646
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
- [x] Docker socket mounted read-write only on client nodes (not servers)
- [x] `allow_privileged = false` in Docker plugin config
- [ ] **Production**: Enable TLS for HTTP/RPC/Serf (not done in Docker Compose example)
- [ ] **Production**: Enable Gossip encryption with a pre-shared key
- [ ] **Production**: Store management token in Vault or a secrets manager
- [ ] **Production**: Set worker token `ExpiryTTL` to match your rotation schedule

## Executor integration

The `NomadExecutor` in `osimflow/executors/__init__.py` reads:

- `NOMAD_ADDR` — the cluster HTTP endpoint (default `http://127.0.0.1:4646`)
- `NOMAD_TOKEN` — the ACL token (required when ACL is enabled)

No constructor kwarg accepts a token — this matches the security
model of `AWSBatchExecutor` (IAM role) and `SlurmExecutor` (SSH key):
credentials come from the environment, not from code.

## Files

| Path | Purpose |
|---|---|
| `infra/nomad/examples/ha/docker-compose.yml` | 3-server + 2-client Docker Compose |
| `infra/nomad/examples/ha/server.hcl` | Server 1 config |
| `infra/nomad/examples/ha/server2.hcl` | Server 2 config |
| `infra/nomad/examples/ha/server3.hcl` | Server 3 config |
| `infra/nomad/examples/ha/client.hcl` | Client config (shared) |
| `infra/nomad/examples/ha/bootstrap.sh` | ACL bootstrap + policy registration |
| `infra/nomad/acl/policies/agent.hcl` | Read-only agent/node policy |
| `infra/nomad/acl/policies/worker.hcl` | Least-privilege job submission policy |
| `infra/nomad/acl/tokens/` | Generated tokens (git-ignored) |

## Production TLS (out of scope for this recipe)

For real deployments, add to each `server*.hcl` and `infra/nomad/examples/ha/client.hcl`: <!-- docs-skip -->

```hcl
tls {
  http = true
  rpc  = true

  ca_file   = "/etc/nomad/tls/ca.pem"
  cert_file = "/etc/nomad/tls/server.pem"
  key_file  = "/etc/nomad/tls/server-key.pem"

  verify_server_hostname = true
  verify_https_client    = true
}
```

Generate certificates with `consul tls create` or your organization's PKI.
