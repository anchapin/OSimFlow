# Nomad Deployment

OSimFlow supports running simulation campaigns on **HashiCorp Nomad** clusters for on-premise and edge deployments.

## Quick start

See the production deployment guide:

- [`docs/nomad-production.md`](../../docs/nomad-production.md)

## Infrastructure

All Nomad HCL configs and scripts live in the **main `infra/` tree**:

| Component | Path |
|---|---|
| HA cluster Docker Compose | [`infra/nomad/examples/ha/`](../../infra/nomad/examples/ha/) |
| Server HCL configs | [`infra/nomad/examples/ha/server*.hcl`](../../infra/nomad/examples/ha/) |
| Client HCL config | [`infra/nomad/examples/ha/client.hcl`](../../infra/nomad/examples/ha/client.hcl) |
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

## High-availability setup

The `infra/nomad/examples/ha/` directory provides a Docker Compose-based HA cluster for testing:

- 3 Nomad servers with `bootstrap_expect=3`
- 2 Nomad clients with Docker task driver
- ACL bootstrap with management and worker tokens

```bash
cd infra/nomad/examples/ha
docker compose up -d
./bootstrap.sh
```

## Security

- ACL-enabled by default with least-privilege worker policy.
- TLS recommended for production (see [`docs/nomad-production.md`](../../docs/nomad-production.md)).
- No bind-mounted secrets; use environment variables or Nomad native Vault integration.

## See also

- [Nomad production guide](../../docs/nomad-production.md)
- [OSimFlow PRD](../../docs/OSimFlow.md)
- [osimflow-deploy README](../README.md)
