# ===========================================================================
# OSimFlow AWS Batch worker runtime (issue #1810)
#
# Layers Python 3.12 + osimflow (with boto3) onto the upstream
# nrel/openstudio image so ONE image has the pinned OpenStudio/EnergyPlus CLI
# and the OSimFlow remote runner. No SDK rebuild.
#
# Build (from repo root):
#   docker build -f docker/osimflow-worker/Dockerfile \
#     --build-arg OPENSTUDIO_VERSION=3.11.0 \
#     -t osimflow-worker:3.11.0 .
#
# AWS Batch sets containerOverrides.command = ["python", "-m",
# "osimflow.remote_runner"]; there is deliberately NO ENTRYPOINT so that
# command runs verbatim. Runs as non-root with a writable /scratch; needs no
# privileged mode, Docker socket or host mounts.
#
# See docs/aws-batch-worker-runtime.md (ECR publish, digest recording).
# ===========================================================================
ARG OPENSTUDIO_VERSION=3.11.0
FROM nrel/openstudio:${OPENSTUDIO_VERSION}

ARG OPENSTUDIO_VERSION
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    OSIMFLOW_OPENSTUDIO_VERSION=${OPENSTUDIO_VERSION}

USER root

# The base image is Ubuntu 24.04 (system Python 3.12) without venv/pip; install
# them and isolate osimflow in its own venv so base-image Python is untouched.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.12 python3.12-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3.12 -m venv /opt/osimflow-venv

WORKDIR /src
COPY pyproject.toml README.md ./
COPY osimflow ./osimflow
COPY bin /opt/osimflow/bin
RUN /opt/osimflow-venv/bin/pip install --no-cache-dir ".[aws]" \
    && rm -rf /src

ENV PATH="/opt/osimflow-venv/bin:${PATH}"

RUN useradd --create-home --uid 10001 osimflow \
    && mkdir -p /scratch \
    && chown osimflow:osimflow /scratch \
    && chmod -R a+rX /opt/osimflow-venv /opt/osimflow

COPY docker/osimflow-worker/smoke_test.py /opt/osimflow/smoke_test.py

USER osimflow
WORKDIR /scratch
ENV TMPDIR=/scratch HOME=/home/osimflow

# Intentionally no ENTRYPOINT: Batch's command override must run as given.
CMD ["python", "-m", "osimflow.remote_runner"]
