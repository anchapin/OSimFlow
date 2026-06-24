# ---------------------------------------------------------------------------
# osimflow_worker.hcl — Parameterized Nomad job spec for OSimFlow (issue #135)
#
# A parameterized batch job that the NomadExecutor dispatches once per
# sample. The executor calls ``POST /v1/job/osimflow-worker/dispatch``
# with per-sample meta vars; Nomad creates a child job, schedules it on
# a client, and the executor polls the resulting allocation.
#
# Security:
#   * privileged = false  (no host-level access)
#   * Memory capped at 4096 MB default (overridable at dispatch time)
#   * CPU capped at 2000 MHz (2 logical CPUs) default
#   * No host network, no bind mounts
#
# Prerequisites:
#   * Nomad cluster with Docker task driver enabled
#   * ACL policy ``osimflow-worker`` (infra/nomad/acl/policies/worker.hcl)
#   * ``NOMAD_TOKEN`` env var set to a token carrying that policy
#   * ``NOMAD_ADDR`` env var pointing to a Nomad server
#
# Usage:
#   nomad job run osimflow_worker.hcl                  # register the job
#   nomad job dispatch -meta sample_id=0 osimflow-worker  # dispatch a sample
# ---------------------------------------------------------------------------

variable "container_image" {
  type    = string
  default = "nrel/openstudio:3.11.0"
}

job "osimflow-worker" {
  type = "batch"
  # ``parameterized`` enables dispatch with per-invocation meta vars.
  parameterized {
    # Required meta vars — the executor must supply these on every dispatch.
    meta_required = [
      "sample_id",
    ]
    # Optional meta vars — defaults are consumed from the job's Meta block.
    meta_optional = [
      "variables_json",
      "openstudio_version",
      "container_image",
    ]
  }

  # Restrict to a single datacentre (override at dispatch time if needed).
  datacenters = ["dc1"]

  # Default meta values (used when the dispatcher omits the optional metas).
  meta = {
    variables_json     = "{}"
    openstudio_version = "3.11.0"
    container_image    = "nrel/openstudio:3.11.0"
  }

  group "osimflow" {
    # Hard kill after the task exceeds the time budget. Nomad's
    # ``kill_timeout`` is a Go duration; 4h covers most energy sims.
    kill_timeout = "4h"

    # Spread allocations across clients for resilience.
    spread {
      attribute = "${node.unique.id}"
      weight    = 100
    }

    task "simulate" {
      driver = "docker"

      config {
        # The image tag is resolved at dispatch time from the meta var
        # ``container_image`` (or the default ``openstudio_version``).
        image = var.container_image

        # Security: never run as privileged.
        privileged = false

        # Command: invoke the OSimFlow work script. In production the
        # container image includes the ``osimflow`` package; the entry
        # point reads ``NOMAD_META_*`` env vars to discover sample
        # parameters.
        command = "/bin/sh"
        args = [
          "-c",
          "python -m osimflow.remote_runner",
        ]

        # Logging: stdout/stderr are captured by the Nomad client and
        # available via ``nomad alloc logs <alloc_id>``.
      }

      # Resource limits: 2 CPU (2000 MHz), 4 GB memory, 1 GB ephemeral disk.
      resources {
        cpu    = 2000  # MHz
        memory = 4096  # MB
        disk   = 1024  # MB
      }

      # Do not restart failed tasks — the Campaign handles retries.
      restart_policy {
        attempts = 0
        mode     = "fail"
      }

      # Environment variables passed through to the container.
      env {
        OSIMFLOW_STUB_SIM = "${NOMAD_META_sample_id != "" ? "0" : "1"}"
        OSIMFLOW_OUTDIR   = "/local/osimflow/out"
        OSIMFLOW_SAMPLE   = "${NOMAD_META_sample_id}"
        OSIMFLOW_OS_VERSION = "${NOMAD_META_openstudio_version}"
      }

      # Artifact stanza: fetch the template_sim_package before the task
      # starts. Supports S3, HTTP, or local file sources.
      # Uncomment and adjust the source URL for your deployment.
      #
      # artifact {
      #   source      = "s3::https://s3.amazonaws.com/my-bucket/sim-package.tar.gz"
      #   destination = "local/sim-package"
      #   mode        = "file"
      # }
    }
  }
}
