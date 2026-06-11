# agent.hcl — Read-only agent/node policy for operators (issue #123)
#
# Intended for human operators and monitoring services that need
# cluster visibility but must NOT submit or modify jobs. Attach this
# policy to a "reader" ACL token.

agent {
  policy = "read"
}
node {
  policy = "read"
}
