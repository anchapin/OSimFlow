# server3.hcl — Nomad server 3 configuration (issue #123)

datacenter = "dc1"
region     = "global"
data_dir   = "/opt/nomad/data"
bind_addr  = "0.0.0.0"
name       = "nomad-server-3"

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
