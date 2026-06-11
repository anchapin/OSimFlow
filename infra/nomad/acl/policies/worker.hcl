# worker.hcl — Least-privilege policy for OSimFlow job submission (issue #123)
#
# Used by the NomadExecutor when dispatching OpenStudio simulation
# jobs. The token carrying this policy can list and submit jobs in
# the default namespace and read job status — exactly what the
# executor needs and nothing more.
#
# Explicitly omitted:
#   - "policy" = "write" at the top level (would grant global write)
#   - "alloc-lifecycle" (not needed for batch submit+poll)
#   - "scale" (not needed — OSimFlow does not use Nomad autoscaling)

job {
  policy = "read"
}

namespace "default" {
  policy       = "write"
  capabilities = ["list-jobs", "dispatch-job", "read-job", "submit-job"]
}
