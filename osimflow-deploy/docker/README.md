# Docker

OSimFlow runs simulations inside containerised environments. This directory documents the container image strategy.

## Image strategy

OSimFlow does **not** build its own OpenStudio container image. Instead, it consumes the upstream **`nrel/openstudio`** image from Docker Hub and selects the version at campaign launch time via the `--openstudio_version` flag.

For full details, see:

- [`docs/container-image-strategy.md`](../../docs/container-image-strategy.md)
- [`docs/openstudio-image-distribution.md`](../../docs/openstudio-image-distribution.md)

## Available images

| Image | Registry | Purpose |
|---|---|---|
| `nrel/openstudio:<version>` | Docker Hub | OpenStudio CLI + EnergyPlus simulation engine |
| `scientific_python_image` | `ghcr.io` | Python data processing (pandas, scipy, matplotlib) |

## ECR mirror (production)

For AWS Batch production workloads, mirror the upstream image to ECR to avoid Docker Hub rate limits:

```bash
./infra/aws/scripts/sync-openstudio-to-ecr.sh \
  --repository <account-id>.dkr.ecr.<region>.amazonaws.com/osimflow-openstudio \
  --versions 3.4.0 3.5.0
```

The ECR lifecycle policy keeps the last 5 tagged `3.*` images.

## Singularity (HPC)

On Slurm clusters with Singularity/Apptainer, convert the Docker image:

```bash
singularity pull openstudio-3.5.0.sif docker://nrel/openstudio:3.5.0
```

## Version pinning

The container version is pinned via the `--openstudio_version` CLI flag. This becomes the dynamic container tag passed to the executor:

```bash
osimflow run --openstudio_version 3.5.0 ...
```

## See also

- [Container image strategy](../../docs/container-image-strategy.md)
- [OpenStudio image distribution](../../docs/openstudio-image-distribution.md)
- [AWS deployment guide](../aws/README.md)
- [osimflow-deploy README](../README.md)
