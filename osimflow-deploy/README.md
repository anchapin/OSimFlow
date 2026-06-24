# osimflow-deploy

Cloud and infrastructure deployment recipes for **OSimFlow**.

This directory is a **sub-monorepo** within the main OSimFlow repository. It provides organizational scaffolding, documentation, and change tracking for all deployment-related artefacts. The actual IaC code lives in the existing [`infra/`](../infra/) directory tree — `osimflow-deploy/` does **not** duplicate or move those files.

## Available platforms

| Platform | IaC location | Documentation |
|---|---|---|
| **AWS Batch** | [`infra/aws/terraform/`](../infra/aws/terraform/) | [`aws/README.md`](aws/README.md) · [`docs/aws-batch-terraform.md`](../docs/aws-batch-terraform.md) |
| **Nomad** | [`infra/nomad/`](../infra/nomad/) | [`nomad/README.md`](nomad/README.md) · [`docs/nomad-production.md`](../docs/nomad-production.md) |
| **Docker** | Upstream `nrel/openstudio` image | [`docker/README.md`](docker/README.md) · [`docs/container-image-strategy.md`](../docs/container-image-strategy.md) |

## Versioning

Deployment recipes are versioned independently from OSimFlow core using **git tag prefixes**:

```
osimflow-deploy-v0.1.0
osimflow-deploy-v0.2.0
```

See [`CHANGELOG.md`](CHANGELOG.md) for the release history.

## Structure

```
osimflow-deploy/
├── README.md          ← you are here
├── CHANGELOG.md       ← independent semver
├── CODEOWNERS         ← cloud/IaC review ownership
├── aws/
│   └── README.md      ← AWS Batch deployment guide
├── nomad/
│   ├── README.md      ← Nomad deployment guide
│   └── examples/
│       └── basic/
└── docker/
    └── README.md      ← container image strategy
```

## Contributing deployment recipes

1. Add or modify IaC in the appropriate subdirectory under [`infra/`](../infra/).
2. Add or update the corresponding documentation under `osimflow-deploy/<platform>/`.
3. Update [`CHANGELOG.md`](CHANGELOG.md) with a description of the change.
4. Tag a new release: `git tag osimflow-deploy-vX.Y.Z`.
5. Open a PR — the `CODEOWNERS` file will request a review from the cloud/IaC maintainers.

## Relationship to `infra/`

`osimflow-deploy/` is purely documentation and organisational scaffolding. All Terraform modules, Nomad HCL configs, shell scripts, and ECR/Sync tooling remain in [`infra/`](../infra/) and are **not** moved or duplicated here. The README files in each platform subdirectory link back to the actual IaC paths.

## See also

- [OSimFlow PRD](../docs/OSimFlow.md)
- [AWS Batch Terraform guide](../docs/aws-batch-terraform.md)
- [Nomad production guide](../docs/nomad-production.md)
- [Container image strategy](../docs/container-image-strategy.md)
- [Contributing guide](../docs/CONTRIBUTING.md)
