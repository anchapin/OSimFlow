# client.hcl — Nomad client configuration (issue #123)
#
# Shared by both client nodes. The clients join the server cluster
# via retry_join and mount the Docker socket (done in docker-compose)
# so the Docker task driver can pull and run containers — the same
# driver the NomadExecutor uses to dispatch OpenStudio jobs.

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
  enabled = true
}

plugin "docker" {
  config {
    allow_privileged = false
    volumes {
      enabled = true
    }
  }
}
