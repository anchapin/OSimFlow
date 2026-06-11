# =============================================================================
# OSimFlow Nomad Job Spec — parameterized batch worker (issue #135)
# =============================================================================
#
# This is a Nomad parameterized job that acts as a template for per-sample
# OpenStudio simulation dispatch. The Campaign orchestrator registers this
# job spec once per campaign, then dispatches one instance per sample via
# the Nomad dispatch API (POST /v1/job/<job_id>/dispatch).
#
# Usage:
#   nomad job run osimflow_worker.hcl           # register the parameterized job
#   nomad job dispatch                           \
#     -meta SAMPLE_ID="sample-0"                 \
#     -meta OUTDIR="/data/campaigns/run-001"     \
#     -meta OPENSTUDIO_VERSION="3.5.0"           \
#     osimflow-worker
#
# Security constraints (PRD §6):
#   - No privileged containers
#   - Memory limited to 4096 MB
#   - CPU limited to 2000 MHz (2 logical cores)
#   - Template package mounted read-only
#   - Restart policy: fail fast (attempts=1, mode="fail")
#
# See docs/openstudio-image-distribution.md for the container image source.
# =============================================================================

job "osimflow-worker" {
  region      = "global"
  datacenters = ["dc1"]
  type        = "batch"

  # Parameterized dispatch — one dispatch per sample
  parameterized {
    payload       = "optional"
    meta_required = ["SAMPLE_ID", "OUTDIR"]
    meta_optional = ["OPENSTUDIO_VERSION", "GENERATION"]
  }

  group "sim" {
    count = 1

    restart_policy {
      attempts = 1
      interval = "5m"
      delay    = "10s"
      mode     = "fail"
    }

    task "openstudio" {
      driver = "docker"

      config {
        image      = "nrel/openstudio:${OPENSTUDIO_VERSION}"
        command    = "openstudio"
        args       = ["cli", "run", "-w", "/local/workflow.osw"]
        privileged = false  # Security: never privileged

        # Mount template package read-only
        volumes = [
          "local/input:/local/input:ro"
        ]

        # Resource limits
        memory_mb = 4096
        cpu       = 2000
      }

      # Meta params from dispatch
      meta {
        SAMPLE_ID = "${NOMAD_META_SAMPLE_ID}"
        OUTDIR    = "${NOMAD_META_OUTDIR}"
      }

      # Security: no privileged, memory limited
      resources {
        cpu    = 2000
        memory = 4096
      }

      logs {
        max_files     = 2
        max_file_size = 50
      }
    }
  }
}
