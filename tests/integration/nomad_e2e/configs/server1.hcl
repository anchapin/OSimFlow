# server1.hcl — Nomad server 1 configuration for multi-node E2E (issue #137)
#
# Test-only config: ACLs are disabled to avoid token bootstrapping in CI.
# The Raft quorum is established via retry_join pointing at all three
# server hostnames.

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
  enabled = false
}

telemetry {
  publish_allocation_metrics = true
  publish_node_metrics       = true
}
