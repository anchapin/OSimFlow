# client.hcl — Nomad client configuration for multi-node E2E (issue #137)
#
# Shared by both client nodes. The clients join the server cluster
# via retry_join and mount the Docker socket (done in docker-compose)
# so the Docker task driver can pull and run containers.

datacenter = "dc1"
region     = "global"
data_dir   = "/opt/nomad/data"

client {
  enabled = true
  server_join {
    retry_join = ["nomad-server-1", "nomad-server-2", "nomad-server-3"]
  }
  meta = {
    "osimflow.node" = "true"
  }
}

acl {
  enabled = false
}

plugin "docker" {
  config {
    allow_privileged = false
    volumes {
      enabled = true
    }
  }
}
