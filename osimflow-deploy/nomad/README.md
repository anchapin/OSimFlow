# Nomad Deployment

OSimFlow supports running simulation campaigns on **HashiCorp Nomad** clusters for on-premise and edge deployments.

## Quick start

See the production deployment guide:

- [`docs/nomad-production.md`](../../docs/nomad-production.md)

## Infrastructure

All Nomad HCL configs and scripts live in the **main `infra/` tree**:

| Component | Path |
|---|---|
| Native HA cluster recipe | [`infra/nomad/examples/ha/`](../../infra/nomad/examples/ha/) |
| Server HCL config (shared template) | [`infra/nomad/examples/ha/server.hcl`](../../infra/nomad/examples/ha/server.hcl) |
| Client HCL config | [`infra/nomad/examples/ha/client.hcl`](../../infra/nomad/examples/ha/client.hcl) |
| Native bring-up & migration guide | [`infra/nomad/examples/ha/README.md`](../../infra/nomad/examples/ha/README.md) |
| ACL bootstrap script | [`infra/nomad/examples/ha/bootstrap.sh`](../../infra/nomad/examples/ha/bootstrap.sh) |
| ACL policies | [`infra/nomad/acl/policies/`](../../infra/nomad/acl/policies/) |

## Running a campaign

```bash
osimflow run \
  --executor nomad \
  --nomad-address http://nomad.example.com:4646 \
  --nomad-datacentre dc1 \
  --input_variables variables.yml \
  --n_samples 500 \
  --outdir ./results
```

## High-availability setup (native host-OS topology)

The `infra/nomad/examples/ha/` directory provides a **native host-OS** HA
cluster recipe — the Nomad control plane runs directly on bare metal / VMs,
not inside containers (issue #619). The previous Docker-in-Docker (`hind`)
template has been removed.

- 3 Nomad servers with `bootstrap_expect=3` (Raft quorum, tolerates 1 failure)
- N Nomad clients with the **unprivileged** Docker task driver
- ACL bootstrap with management and worker tokens

```bash
# Edit retry_join IPs in server.hcl / client.hcl, then on each host:
nomad agent -config=server.hcl -node=nomad-server-1 -bind=10.0.0.11   # servers
nomad agent -config=client.hcl -node=<node> -bind=<ip>                # clients

# Then bootstrap ACLs against any server:
export NOMAD_ADDR=http://10.0.0.11:4646
cd infra/nomad/examples/ha
./bootstrap.sh
```

See [`infra/nomad/examples/ha/README.md`](../../infra/nomad/examples/ha/README.md)
for the full procedure, including a systemd unit and the migration path from
the removed Docker Compose setup.

## Security

- ACL-enabled by default with least-privilege worker policy.
- Native mTLS is the primary isolation/trust boundary for production (issue #619).
- Docker task driver runs `privileged = false`; only OpenStudio workloads are containerized.
- No bind-mounted secrets; use environment variables or Nomad native Vault integration.

## See also

- [Nomad production guide](../../docs/nomad-production.md)
- [OSimFlow PRD](../../docs/OSimFlow.md)
- [osimflow-deploy README](../README.md)
