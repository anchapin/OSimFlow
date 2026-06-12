# ===========================================================================
# OSimFlow CLI — Dockerfile
# Multi-stage build that produces a slim runtime image with the osimflow
# CLI entry point and the bin/ work-layer scripts.
#
# Build (from repo root):
#   docker build -f docker/osimflow-cli/Dockerfile -t anchapin/osimflow:latest .
#
# Run:
#   docker run --rm anchapin/osimflow:latest --help
#   docker run --rm -v $(pwd)/results:/results anchapin/osimflow:latest \
#     run --executor local --n_samples 5 --outdir /results ...
#
# See docs/container-image-strategy.md for the CI/CD pipeline.
# ===========================================================================

# ---- Stage 1: build dependencies ------------------------------------------
FROM python:3.12-slim AS builder

# System deps required to compile numpy / pandas / scipy wheels
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Install osimflow from source with the extras most users need.
# The aws extra brings boto3; slurm brings submitit.
COPY . .
RUN pip install --no-cache-dir --prefix=/install /src[aws,slurm]

# ---- Stage 2: slim runtime ------------------------------------------------
FROM python:3.12-slim AS runtime

# Copy the installed Python packages and CLI entry point from the builder.
COPY --from=builder /install /usr/local

# The bin/ scripts are invoked by the work layer (osimflow/work.py) via
# subprocess, so they must be present at a known path inside the image.
COPY --from=builder /src/bin /opt/osimflow/bin

# Ensure the entry point is on PATH
ENV PATH="/usr/local/bin:${PATH}"

# Default non-root user for safety
RUN useradd --create-home osimflow
USER osimflow

ENTRYPOINT ["osimflow"]
CMD ["--help"]
