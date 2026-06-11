# server.hcl — Nomad server 1 configuration (issue #123)
#
# This is the primary server config used by all three servers in the
# HA compose setup. Each server's HCL file is identical except for
# the `name` field (used for logging). The Raft quorum is established
# via retry_join pointing at all three server hostnames.

datacenter = "dc1"
region     = "global"
data_dir   = "/opt/nomad/data"
bind_addr  = "0.0.0.0"
name       = "nomad-server-1"

server {
  enabled          = true
  bootstrap_expect = 3
  retry_join       = ["nomad-server-1", "nomad-server-2", "nomad-server-3"]
}

acl {
  enabled = true
}

telemetry {
  publish_allocation_metrics = true
  publish_node_metrics       = true
}
