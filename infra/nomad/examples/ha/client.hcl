# client.hcl — Nomad client configuration (native host-OS deployment)
#
# Runs natively on each compute node (bare metal / VM). The client agent
# itself is NOT containerized — only the OpenStudio *workloads* it
# dispatches run as unprivileged Docker tasks (see osimflow_worker.hcl).
# This is the correct separation: the control plane / agents live on the
# host OS, while the compute-intensive simulation jobs run in containers
# via Nomad's Docker task driver with `privileged = false`. See issue #619
# and docs/nomad-production.md for the rationale (deprecation of nested
# containerization / `hind`).
#
# Start on each compute node:
#   nomad agent -config=client.hcl -node=<this-node> -bind=<this-node-ip>

datacenter = "dc1"
region     = "global"
data_dir   = "/opt/nomad/data"

client {
  enabled = true
  server_join {
    # Replace with the real addresses of your server nodes (or a cluster
    # DNS name / cloud auto-join expression).
    retry_join = ["10.0.0.11", "10.0.0.12", "10.0.0.13"]
  }
  meta = {
    "osimflow.node" = "true"
  }
}

# ACLs are mandatory on client nodes as well.
acl {
  enabled = true
}

# Docker task driver for OpenStudio *workload* containers only.
# Security posture (issue #619):
#   * allow_privileged = false — workload containers never get host access
#   * no host network, no bind mounts of host secrets (see osimflow_worker.hcl)
# The Docker socket is consumed locally on the compute node by the agent;
# it is NOT shared across a nested container boundary.
plugin "docker" {
  config {
    allow_privileged = false
    volumes {
      enabled = true
    }
  }
}

# ── Native mTLS (issue #344) ──────────────────────────────────────────
# Production deployments MUST enable TLS. Use client (not server)
# certificates here. mTLS replaces the trust boundary that nested
# containerization attempted to provide and protects NOMAD_TOKEN in transit.
#
# tls {
#   http = true
#   rpc  = true
#
#   ca_file   = "/etc/nomad/tls/nomad-ca.pem"
#   cert_file = "/etc/nomad/tls/nomad-client.pem"
#   key_file  = "/etc/nomad/tls/nomad-client-key.pem"
#
#   verify_server_hostname = true
#   verify_https_client    = true
# }
