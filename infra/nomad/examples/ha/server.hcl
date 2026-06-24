# server.hcl — Nomad server configuration (native host-OS deployment)
#
# Used by all three nodes of the HA control-plane quorum. OSimFlow's
# production topology runs the Nomad control plane natively on the host
# OS (bare metal / VM) — NOT inside containers — so that compute-intensive
# OpenStudio simulations avoid the overhead of nested containerization
# (the deprecated `hind` / Hashistack-in-Docker pattern; see issue #619
# and docs/nomad-production.md).
#
# All three server nodes share this single template. Per-node identity is
# supplied at process start via CLI flags rather than duplicated files:
#
#   nomad agent -config=server.hcl \
#     -node=nomad-server-2 \
#     -bind=<this-node-ip> \
#     -advertise=<this-node-ip>
#
# (Equivalently, set the `name`/`bind_addr`/`advertise` blocks below in a
# small per-node override file loaded with a second `-config=`.)
#
# The Raft quorum (bootstrap_expect=3) is formed via `retry_join`, which
# must list the real addresses of the three server nodes. Replace the
# placeholder IPs below with your deployment's addresses, or use cloud
# auto-join (e.g. `retry_join = ["provider=aws ..."]`).

datacenter = "dc1"
region     = "global"
data_dir   = "/opt/nomad/data"

# Bind to all interfaces by default; operators should restrict this to the
# cluster network interface via `-bind=<ip>` for least-privilege exposure.
bind_addr = "0.0.0.0"

# Unique per node — override with `-node=` on each server (nomad-server-1,
# nomad-server-2, nomad-server-3). The default keeps single-node bring-up
# working without flags.
name = "nomad-server-1"

server {
  enabled          = true
  # 3-node Raft quorum tolerates the loss of one server (floor((3-1)/2) = 1).
  bootstrap_expect = 3

  # ── Replace these placeholder addresses with your real server IPs ──
  # In production prefer a resolved cluster-DNS name or cloud auto-join so
  # the list does not drift as nodes are replaced.
  retry_join = ["10.0.0.11", "10.0.0.12", "10.0.0.13"]
}

# ACLs are mandatory: deny-by-default, tokens sourced from the environment
# (same trust model as the AWS IAM role / Slurm SSH-key executors).
acl {
  enabled = true
}

telemetry {
  publish_allocation_metrics = true
  publish_node_metrics       = true
}

# ── Native mTLS (issue #344) ──────────────────────────────────────────
# Production deployments MUST enable TLS for HTTP, RPC, and Serf so that
# both the client and server present certificates to each other (mTLS).
# This protects NOMAD_TOKEN in transit (SEC-009) and replaces the trust
# boundary that nested containerization attempted to provide.
#
# Generate certificates with your organization's internal PKI, or:
#   consul tls ca create
#   consul tls cert create -dc=dc1 -server -domain=nomad
#
# tls {
#   http = true
#   rpc  = true
#
#   ca_file   = "/etc/nomad/tls/nomad-ca.pem"
#   cert_file = "/etc/nomad/tls/nomad-server.pem"
#   key_file  = "/etc/nomad/tls/nomad-server-key.pem"
#
#   verify_server_hostname = true
#   verify_https_client    = true
# }
