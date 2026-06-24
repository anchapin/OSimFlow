# Air-Gapped / Offline Deployment

> **Audience:** OSimFlow operators who need to run campaigns in
> network-isolated environments (HPC clusters, government facilities,
> secure enclaves, factories with no internet egress).

## TL;DR

OSimFlow normally requires internet access for three things:

1. **Docker Hub image pulls** — `nrel/openstudio:<version>` fetched at job
   submission time.
2. **pip package installs** — the Python environment is built from PyPI.
3. **Weather file downloads** — `.epw` files fetched from the EnergyPlus
   website or a URL in `variables.yml`.

Air-gapped deployment bypasses all three by pre-bundling every required
asset into a single offline tarball that can be transferred via USB
stick, NFS mount, or an approved data-transfer mechanism.

---

## What to bundle

An air-gapped deployment needs **four** asset categories:

| Category | Contents | How to bundle |
|---|---|---|
| **Python packages** | All `pip` dependencies + OSimFlow itself as a wheel | `scripts/bundle_offline.py --pip` |
| **Container images** | `nrel/openstudio:<version>` + scientific Python image | `scripts/bundle_offline.py --docker` |
| **Weather files** | `.epw` files referenced in `variables.yml` | `scripts/bundle_offline.py --weather` |
| **Campaign inputs** | `template_sim_package/`, `variables.yml` | Manual copy — these are user-supplied |

The bundle script (`scripts/bundle_offline.py`) downloads all four into
a versioned tarball named `osimflow-offline-<date>.tar.gz`.

---

## Step 1 — Create the offline bundle (online machine)

On a machine **with** internet access, run:

```bash
# Install OSimFlow with all extras first
pip install -e ".[dev,aws,slurm,mlflow,sensitivity,optimization,api,tui]"

# Bundle everything
python scripts/bundle_offline.py \
    --openstudio-version 3.11.0 \
    --pip-packages "osimflow[dev,aws,slurm]" \
    --output /tmp/osimflow-offline.tar.gz
```

The script downloads:

- All pip wheels for the requested extras into `offline/pip/`.
- The `nrel/openstudio:3.11.0` Docker image as a tar archive into
  `offline/docker/`.
- The scientific Python image (`ghcr.io/anchapin/scientific_python_image:latest`)
  as a tar archive into `offline/docker/`.
- Any `.epw` files referenced in bundled `variables.yml` files into
  `offline/weather/`.

**Bundle size estimate:** ~8 GB (OpenStudio image is ~5 GB).

---

## Step 2 — Transfer to air-gapped environment

Copy the tarball to the air-gapped machine via the approved mechanism
(USB, NFS, SCP via jump host, etc.):

```bash
scp -P 2222 /tmp/osimflow-offline.tar.gz airgap-user@host:/data/osimflow/
```

---

## Step 3 — Extract and configure on air-gapped machine

```bash
# Extract the bundle
tar -xzf /data/osimflow/osimflow-offline.tar.gz -C /opt/osimflow/
cd /opt/osimflow/

# Load Docker images from tar files
docker load -i offline/docker/nrel-openstudio-3.11.0.tar
docker load -i offline/docker/scientific-python-image.tar

# Install pip packages from local wheels (no PyPI access needed)
pip install --no-index --find-links=offline/pip/ osimflow

# Verify offline mode is active
osimflow run --offline --help   # should show --offline flag
```

---

## Step 4 — Run a campaign in offline mode

```bash
osimflow run \
    --offline \
    --offline-bundle /opt/osimflow/offline \
    --executor local \
    --input_variables /data/models/variables.yml \
    --template_sim_package /data/models/example_package \
    --n_samples 50 \
    --outdir /data/results/run01 \
    --openstudio_version 3.11.0
```

When `--offline` is set, OSimFlow:

- Uses locally-loaded Docker images instead of pulling from Docker Hub.
- Uses `--find-links` pip wheels instead of PyPI.
- Skips weather file URL downloads; only reads from `--offline-bundle`.
- Skips version-check pings to PyPI / Docker Hub.
- Uses the local ECR/repository if `--ecr-repository` points to a
  pre-loaded local registry.

---

## Offline bundle directory structure

```
offline/
├── pip/
│   ├── osimflow-0.1.0-py3-none-any.whl
│   ├── numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl
│   └── ... (all pip wheels)
├── docker/
│   ├── nrel-openstudio-3.11.0.tar
│   └── scientific-python-image.tar
├── weather/
│   └── USA_CA_San.Fransisco.Intl.AP.724940_TMY3.epw
└── bundle_manifest.json     # metadata: versions, checksums, created date
```

---

## Creating a local pip mirror (optional, for large teams)

For teams with many air-gapped machines, serve the `pip/` directory over
an internal HTTP server:

```bash
cd /opt/osimflow/offline/pip/
python -m http.server 8080 &
```

Then on each air-gapped machine, configure pip to use the internal mirror:

```bash
# pip.conf on air-gapped machine
[global]
find-links = http://internal-pip-mirror:8080/
extra-index-url = http://internal-pip-mirror:8080/
```

OSimFlow's `--offline-bundle` flag automatically detects this and passes
`--no-index --find-links` to pip.

---

## Singularity on HPC (air-gapped)

On HPC systems that run Singularity instead of Docker:

```bash
# Convert Docker tar -> Singularity image on the online machine
singularity pull docker-archive://nrel-openstudio-3.11.0.tar sif://nrel-openstudio-3.11.0.sif

# Transfer the .sif file and .tar.gz bundle together
rsync -avP offline/ airgap-hpc:/opt/osimflow/offline/

# On the HPC login node (air-gapped)
module load singularity
singularity exec --bind /data/osimflow:/data osimflow-offline.sif \
    osimflow run --offline ...
```

The `--offline-bundle` path is passed through the `SINGULARITY_BINDPATH`
environment variable automatically by the `SlurmExecutor` when it detects
Singularity as the container runtime.

---

## Troubleshooting

### "image not found" when running in offline mode

The Docker image was not loaded into the local registry. Run:

```bash
docker load -i /opt/osimflow/offline/docker/nrel-openstudio-3.11.0.tar
docker images | grep openstudio
```

### pip install fails with "index.html not found"

The `--offline-bundle` path is wrong or the pip directory is empty.
Verify:

```bash
ls /opt/osimflow/offline/pip/*.whl | head -5
```

### Weather file missing

The bundle did not include the `.epw` file. Re-run the bundle script
with the correct `--weather-dir` pointing to the directory that contains
your `.epw` files:

```bash
python scripts/bundle_offline.py \
    --weather-dir /data/models/example_package/weather \
    --output /tmp/osimflow-offline.tar.gz
```

---

## References

- [`scripts/bundle_offline.py`](../scripts/bundle_offline.py) — the bundle creation script
- [`infra/offline/`](.infra/offline/) — Docker / Singularity configuration for offline use
- [OpenStudio image distribution](openstudio-image-distribution.md) — image source and versioning
- [Issue #261](https://github.com/anchapin/OSimFlow/issues/261) — upstream tracking issue
