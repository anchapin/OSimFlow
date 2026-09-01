<!-- docs-skip -->
# Secret Management

> **Audience:** Engineers and administrators running OSimFlow campaigns across local, HPC, and cloud environments. Covers credential handling for every executor backend, Vault/Secrets Manager integration patterns, and API server authentication.

## Overview

OSimFlow's security model is built on one principle: **credentials come from the execution environment, never from config files or source code** (AGENTS.md §10). Concretely:

- **No `.env` files in the repository.** The `.gitignore` excludes them. The `.env.example` in the repo root exists only for local Docker Compose development ports — it contains no real secrets and must never be copied to `.env` and committed.
- **No IAM access keys in code.** The `AWSBatchExecutor` constructor does **not** accept `aws_access_key_id` / `aws_secret_access_key` (see `osimflow/executors/__init__.py`). Long-lived keys are a common attack vector.
- **No bind-mounted secrets in Singularity containers.** On shared HPC, secrets pass via environment variables or `submitit`'s `update_parameters`, never as container mounts.
- **BYOS scripts are untrusted.** When a user supplies a custom script, it is loaded via `importlib.util` with function-signature validation. Resource limits can be applied via `--byos-resource-limits` to bound blast radius.

OSimFlow does **not** ship a built-in secret store. Instead, each executor sources credentials from its native trust boundary:

| Executor | Credential source | Secret rotation |
|---|---|---|
| `LocalExecutor` | Environment variables | Manual |
| `SlurmExecutor` | Environment via `submitit`, SSH keys | Manual / cluster policy |
| `AWSBatchExecutor` | IAM task role on compute environment | Automatic (AWS-managed) |
| `AzureBatchExecutor` | Azure managed identity / service principal | Azure-managed |
| `GoogleBatchExecutor` | GCP service account | GCP-managed |
| `KubernetesExecutor` | Service account + K8s Secrets | K8s-managed |
| `NomadExecutor` | `NOMAD_TOKEN` env var | ACL token lifecycle |

For scenarios that require external secret stores (HashiCorp Vault, AWS Secrets Manager, cloud-native secret managers), OSimFlow provides two integration points: the `--init-script` flag (runs before the campaign starts) and BYOS scripts (run per-step). Both are documented below.

---

## AWS Batch — IAM Roles

On AWS Batch, **no long-lived credentials are needed**. The IAM role attached to the Batch compute environment provides temporary credentials that AWS rotates automatically. This is the deliberate security decision documented in PRD §6 *Cloud Security Practices* and AGENTS.md §10.

### How credentials flow

```
┌──────────────────────────────────────────────────┐
│  AWS Batch compute environment (EC2)              │
│                                                   │
│  EC2 instance profile                             │
│  → IAM instance role                              │
│    → AmazonEC2ContainerServiceforEC2Role          │
│                                                   │
│  Batch task container                             │
│  → IAM task role                                  │
│    → S3 (campaign bucket only)                    │
│    → CloudWatch Logs (batch log group only)       │
│                                                   │
│  ECS agent                                        │
│  → IAM task-execution role                        │
│    → ECR pull (AmazonECSTaskExecutionRolePolicy)  │
└──────────────────────────────────────────────────┘
```

The Terraform module in `infra/aws/terraform/iam.tf` provisions all four roles with least-privilege policies. The task role is scoped to a single S3 bucket and a single CloudWatch log group — it cannot access other accounts' resources.

### Provisioning roles

Use the Terraform module (recommended):

```bash
cd infra/aws/terraform
terraform init
terraform plan
terraform apply
```

After apply, the Batch job definition is pre-configured with the correct task role ARN. See [`docs/aws-batch-terraform.md`](aws-batch-terraform.md) for the full deployment guide.

### Adding Secrets Manager / KMS access

If your BYOS scripts need to retrieve secrets from AWS Secrets Manager, add an inline policy to the task role. Extend `infra/aws/terraform/iam.tf`:

```hcl
data "aws_iam_policy_document" "task_secrets" {
  statement {
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      # Scope to specific secret ARNs — never use "*"
      "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:osimflow/*",
    ]
  }
}

resource "aws_iam_role_policy" "task_secrets" {
  name   = "${local.name_prefix}-task-secrets"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_secrets.json
}
```

Then `terraform apply` to grant the task role access. The BYOS script (see [AWS Secrets Manager](#aws-secrets-manager) below) uses `boto3` to retrieve secrets at runtime — `boto3` picks up the IAM role credentials automatically, with no code changes.

### What the executor does not accept

The `AWSBatchExecutor` constructor signature is intentionally minimal:

```python
AWSBatchExecutor(
    job_queue="osimflow-batch-queue",
    job_definition="osimflow-openstudio-job-def",
    # Optional:
    region_name="us-east-1",
    max_spot_price_usd=None,
    fallback_to_on_demand=False,
    max_retries=3,
)
```

There is no `aws_access_key_id`, `aws_secret_access_key`, or `aws_session_token` parameter. The boto3 client resolves credentials from the default credential chain (environment → instance metadata → container credentials), which on Batch is always the task role.

> **Never** put AWS credentials in `variables.yml`, the job definition container environment, or any file tracked in git.

> Related: `--result-storage-endpoint` / `--s3-artifact-endpoint` must use `https://` for non-loopback hosts unless `--allow-insecure-storage-endpoint` is set (issue #1386) — plaintext HTTP exposes SigV4 signing material in transit. See [user-guide.md](user-guide.md#result-storage--cost-tracking).

---

## Slurm / HPC — Environment Variables

On Slurm and other HPC schedulers, OSimFlow uses `submitit.AutoExecutor` (wrapped in `SlurmExecutor`). Secrets are passed as environment variables, **not** as Singularity container bind-mounts.

### Passing secrets via `--init-script`

The `--init-script` flag runs a shell script before the first campaign step. It receives several environment variables (`OSIMFLOW_OUTDIR`, `OSIMFLOW_N_SAMPLES`, `OSIMFLOW_EXECUTOR`, `OSIMFLOW_ALGORITHM`) and can export additional variables that the campaign inherits.

```bash
# user_scripts/slurm_init.sh
#!/usr/bin/env bash
set -euo pipefail

# Source the cluster's Vault auth module (example)
module load vault

# Retrieve secrets and export them for the campaign
export DATABASE_URL="$(vault kv get -field=url secret/osimflow/database)"
export API_TOKEN="$(vault kv get -field=token secret/osimflow/api)"

echo "Secrets loaded successfully"
```

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --init-script user_scripts/slurm_init.sh \
  --input_variables variables.yml \
  --n_samples 500 \
  --outdir ./results
```

### `submitit` environment propagation

`SlurmExecutor` wraps `submitit.AutoExecutor`, which passes environment variables to Slurm jobs via `sbatch --export=`. The `--init-script` runs in the orchestrator process, so its exported variables are inherited by the executor and propagated to each Slurm task.

For per-task environment variables that must be set on the Slurm side (not the orchestrator), use submitit's `update_parameters` through the `setup` mechanism. This is the documented pattern for passing secrets without bind-mounting files:

```python
# In a BYOS script that customises the executor (advanced)
# The 'setup' lines run inside each Slurm task before the work function.
executor._auto_executor.update_parameters(
    setup=[
        "export DATABASE_URL=$(/opt/vault/bin/vault kv get -field=url secret/osimflow/database)",
    ],
)
```

> **Singularity rule:** never bind-mount a secrets file (e.g., `--bind secrets.env:/run/secrets.env`) into a Singularity container on a shared cluster. Other users on the same node may read the bind-mounted file. Use environment variables instead — `submitit` sets them per-task and they are not world-readable on `/proc`.

### PBS / Torque

The `PBSExecutor` (issue #351) follows the same pattern. Secrets are passed as environment variables propagated through submitit's PBS backend. No bind-mounting.

---

## HashiCorp Vault Integration

OSimFlow does not bundle a Vault client. Instead, the `vault` CLI is invoked from an init script or a BYOS script, which fetches secrets and exports them as environment variables. This keeps OSimFlow's dependency surface small while supporting any Vault deployment.

### Pattern 1 — Init script with `vault` CLI

This is the simplest pattern. The init script authenticates to Vault, retrieves secrets, and exports them before the campaign starts.

**`user_scripts/vault_init.sh`:**

```bash
#!/usr/bin/env bash
set -euo pipefail

# --- Configuration -----------------------------------------------------------
# The Vault address and auth method should be set by the cluster environment
# or your CI/CD pipeline — never hardcoded.
export VAULT_ADDR="${VAULT_ADDR:-https://vault.internal:8200}"

# Authenticate. Common methods:
#   approle:  VAULT_ROLE_ID + VAULT_SECRET_ID env vars (set by scheduler)
#   k8s:      vault login -path=kubernetes -method=kubernetes role=osimflow
#   ldap:     vault login -method=ldap username=$USER
#
# This example uses AppRole. The role/secret ID must come from the environment,
# NOT from this script.
if [[ -z "${VAULT_ROLE_ID:-}" || -z "${VAULT_SECRET_ID:-}" ]]; then
  echo "ERROR: VAULT_ROLE_ID and VAULT_SECRET_ID must be set in the environment" >&2
  exit 1
fi
vault login -method=approle \
  role_id="$VAULT_ROLE_ID" \
  secret_id="$VAULT_SECRET_ID" \
  >/dev/null

# --- Retrieve secrets --------------------------------------------------------
# Each secret is fetched and exported as an environment variable.
# Use field-level extraction so the value is clean (no JSON wrapper).
export S3_ACCESS_KEY="$(vault kv get -field=access_key secret/osimflow/s3)"
export S3_SECRET_KEY="$(vault kv get -field=secret_key secret/osimflow/s3)"
export API_TOKEN="$(vault kv get -field=token secret/osimflow/external-api)"

# --- Verify ------------------------------------------------------------------
if [[ -z "$S3_ACCESS_KEY" || -z "$S3_SECRET_KEY" || -z "$API_TOKEN" ]]; then
  echo "ERROR: one or more Vault secrets are empty" >&2
  exit 1
fi

echo "Vault secrets retrieved successfully"
```

Run the campaign:

```bash
# The approle credentials are set by your scheduler / CI — not by hand.
export VAULT_ROLE_ID="<your-role-id>"
export VAULT_SECRET_ID="<your-secret-id>"

osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition compute \
  --init-script user_scripts/vault_init.sh \
  --input_variables variables.yml \
  --n_samples 500 \
  --outdir ./results
```

The init script must exit `0` or the campaign aborts (see `osimflow/__main__.py` `--init-script` help text). This is a fail-fast safety net: if Vault is down, the campaign does not start with missing credentials.

### Pattern 2 — BYOS script with `hvac` library

For BYOS scripts that need secrets mid-campaign (e.g., a custom KPI extractor that pushes to an authenticated API), use the `hvac` Python library inside a BYOS script.

**`user_scripts/vault_kpi_extractor.py`:**

```python
"""BYOS KPI extractor that retrieves a Vault secret at runtime."""
from __future__ import annotations

import os


def extract_kpis(sim_dir, sample_id):
    """Extract KPIs and optionally push to an external API."""
    # Lazy import so the dependency is only needed when this script runs.
    try:
        import hvac
    except ImportError:
        raise ImportError(
            "hvac is required for Vault integration. "
            "Install with: pip install hvac"
        )

    # Read KPIs from the simulation output (standard OSimFlow contract)
    # ... (your extraction logic here) ...

    # Optionally push to an external API using a Vault-retrieved token
    vault_addr = os.environ.get("VAULT_ADDR", "https://vault.internal:8200")
    client = hvac.Client(url=vault_addr)
    client.auth.approle.login(
        role_id=os.environ["VAULT_ROLE_ID"],
        secret_id=os.environ["VAULT_SECRET_ID"],
    )
    secret = client.secrets.kv.v2.read_secret_version(
        path="osimflow/external-api",
    )
    api_token = secret["data"]["data"]["token"]

    # Use the token (never log it)
    # ...

    return {"eui_kwh_m2_yr": 120.5}
```

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --init-script user_scripts/vault_init.sh \
  --custom_kpi_extractor user_scripts/vault_kpi_extractor.py \
  --input_variables variables.yml \
  --n_samples 100 \
  --outdir ./results
```

### Pattern 3 — Vault Agent sidecar (long-running campaigns)

For multi-day campaigns on a static cluster, the Vault Agent sidecar pattern keeps secrets fresh without restarting the campaign. The Vault Agent runs as a separate process, polls Vault for secret rotations, and writes the latest values to a file or template.

```
┌──────────────────────────────────────────────────┐
│  Compute node                                     │
│                                                   │
│  ┌──────────────┐     ┌─────────────────────┐    │
│  │ Vault Agent   │────▶│ /vault/secrets/env  │    │
│  │ (sidecar)     │     │ (auto-rotated)      │    │
│  └──────────────┘     └──────┬──────────────┘    │
│         ▲                     │ source            │
│         │ Vault API           ▼                   │
│  ┌──────┴──────┐     ┌─────────────────────┐    │
│  │ Vault server │     │ OSimFlow init script │    │
│  └─────────────┘     │ source /vault/...     │    │
│                       └─────────────────────┘    │
└──────────────────────────────────────────────────┘
```

**Vault Agent config (`vault-agent.hcl`):**

```hcl
vault {
  address = "https://vault.internal:8200"
}

auto_auth {
  method "approle" {
    config = {
      role_id_file_path = "/vault/role-id"
      secret_id_file_path = "/vault/secret-id"
    }
  }

  sink "file" {
    config = {
      path = "/vault/token"
    }
  }
}

# Template that renders secrets as a sourceable env file
template {
  source      = "/vault/templates/env.ctmpl"
  destination = "/vault/secrets/osimflow.env"
  command     = "pkill -HUP osimflow || true"
}
```

**Template file (`env.ctmpl`):**

```liquid
{{ with secret "secret/osimflow/s3" }}
export S3_ACCESS_KEY="{{ .Data.access_key }}"
export S3_SECRET_KEY="{{ .Data.secret_key }}"
{{ end }}
{{ with secret "secret/osimflow/external-api" }}
export API_TOKEN="{{ .Data.token }}"
{{ end }}
```

**Init script that sources the rendered file:**

```bash
#!/usr/bin/env bash
set -euo pipefail
# Vault Agent keeps this file fresh — just source it.
source /vault/secrets/osimflow.env
echo "Secrets loaded from Vault Agent"
```

This pattern is recommended for production Slurm deployments where secrets rotate every few hours and you cannot afford to restart long-running campaigns.

---

## AWS Secrets Manager

AWS Secrets Manager is the recommended secret store for AWS Batch campaigns. Secrets are retrieved at runtime via `boto3`, which picks up the IAM task role credentials automatically.

### BYOS script using boto3

**`user_scripts/sm_kpi_extractor.py`:**

```python
"""BYOS script that retrieves a secret from AWS Secrets Manager."""
from __future__ import annotations

import json
import os


def _get_secret(secret_name: str) -> dict:
    """Retrieve and parse a secret from AWS Secrets Manager.

    Uses the IAM role credentials — no hardcoded keys.
    """
    import boto3  # noqa: PLC0415

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def extract_kpis(sim_dir, sample_id):
    """Extract KPIs, using a Secrets Manager value for an external push."""
    # ... (your KPI extraction logic here) ...

    # Retrieve the API token from Secrets Manager
    secret = _get_secret("osimflow/external-api")
    api_token = secret["token"]

    # Use the token — never log it
    # ...

    return {"eui_kwh_m2_yr": 120.5}
```

### IAM policy for Secrets Manager access

Add this policy to the Batch task role (extend `infra/aws/terraform/iam.tf`):

```hcl
data "aws_iam_policy_document" "task_secrets_manager" {
  statement {
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    # Scope to specific secrets — never use "*"
    resources = [
      "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:osimflow/*",
    ]
  }

  # KMS decrypt permission if the secret is encrypted with a CMK
  statement {
    effect = "Allow"
    actions = ["kms:Decrypt"]
    resources = [
      "arn:aws:kms:${var.region}:${data.aws_caller_identity.current.account_id}:key/<your-cmk-key-id>",
    ]
  }
}

resource "aws_iam_role_policy" "task_secrets_manager" {
  name   = "${local.name_prefix}-task-secrets-manager"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_secrets_manager.json
}
```

> **Always scope Secrets Manager access to specific secret ARNs.** Granting `secretsmanager:GetSecretValue` on `"*"` allows the task to read every secret in the account, violating least-privilege.

### Using Secrets Manager with the init script

For secrets that the entire campaign needs (not just one BYOS script), retrieve them in the init script:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Requires aws CLI v2 on the container image.
# Credentials come from the IAM task role — no aws configure needed.
export API_TOKEN="$(aws secretsmanager get-secret-value \
  --secret-id osimflow/external-api \
  --query SecretString --output text | jq -r .token)"

echo "Secret retrieved from AWS Secrets Manager"
```

```bash
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --init-script user_scripts/sm_init.sh \
  --input_variables variables.yml \
  --n_samples 1000 \
  --outdir ./results
```

> **Note:** the `nrel/openstudio` container image does not include the `aws` CLI or `jq`. If you use the init script on AWS Batch, either bake a custom image with these tools, or use the BYOS Python pattern with `boto3` (which is included in the `scientific_python_image`).

---

## Kubernetes Secrets

The `KubernetesExecutor` runs each simulation sample as a Kubernetes Job. Secrets are managed using native Kubernetes Secrets, which can be mounted as environment variables or files.

### Creating a Kubernetes Secret

```bash
# Create a generic secret from literal values
kubectl create secret generic osimflow-secrets \
  --namespace osimflow \
  --from-literal=database-url='<your-database-url>' \
  --from-literal=api-token='<your-api-token>'

# Or from a file
kubectl create secret generic osimflow-secrets \
  --namespace osimflow \
  --from-file=secrets.json=path/to/secrets.json
```

For production, use an external secret operator (External Secrets Operator, Sealed Secrets, or cloud-specific providers like AWS Secrets Store CSI driver) so that secrets are not created manually via `kubectl`.

### Referencing secrets in the KubernetesExecutor

The `KubernetesExecutor` maps resource directives to Kubernetes requests/limits. Secrets that need to be available inside simulation pods should be configured at the cluster level. The executor sets `OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` as environment variables on each job — additional environment variables can be injected through the service account or a mutating admission webhook.

For secrets that the BYOS scripts need, the recommended pattern is:

1. Create the Kubernetes Secret in the target namespace (above).
2. Use an `--init-script` that reads the secret (if the orchestrator pod has access via its service account):

```bash
#!/usr/bin/env bash
set -euo pipefail
# If the orchestrator pod has the secret mounted at /var/run/secrets
export API_TOKEN="$(cat /var/run/secrets/osimflow-secrets/api-token)"
echo "Secret loaded from Kubernetes Secret"
```

3. For per-sample secrets that must be available in each simulation pod, configure a `Secret` + `ServiceAccount` at the cluster level so the executor's jobs inherit them. See the [Kubernetes Deployment Guide](kubernetes-deployment.md) for RBAC and service account configuration.

### External Secrets Operator (recommended for production)

```yaml
# external-secret.yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: osimflow-secrets
  namespace: osimflow
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: SecretStore
  target:
    name: osimflow-secrets
    type: Opaque
  data:
    - secretKey: api-token
      remoteRef:
        key: osimflow/external-api
        property: token
```

This syncs secrets from Vault/AWS Secrets Manager/GCP Secret Manager into native K8s Secrets automatically, with rotation support.

---

## HMAC Task-Payload Signing (remote executors)

Issue #1177 added HMAC-SHA256 signing of the remote-runner task payload. Every
remote-runner executor — `NomadExecutor`, `KubernetesExecutor`,
`AWSBatchExecutor`, `AzureBatchExecutor`, `GoogleBatchExecutor`, and
`DockerSwarmExecutor` — signs the payload at submission time via
`osimflow.task_payload_hmac.build_signature_env`, and the worker process
(`python -m osimflow.remote_runner`) verifies the signature **before decoding
or executing anything**.

### Threat model

`OSIMFLOW_TASK_PAYLOAD` is a JSON-serialized step call that travels through the
job environment (or Nomad dispatch meta) in plain sight. Anything that can
modify that value between the orchestrator's submission and the worker's
execution can otherwise redirect a simulation job into *arbitrary step calls* —
the entire `remote_runner` step-call surface. The signature defends against
**writers who do not know the shared secret**: an attacker who can rewrite the
payload but cannot read the orchestrator-side secret cannot produce a matching
`OSIMFLOW_TASK_PAYLOAD_SIG`, and the runner refuses to execute. Constant-time
comparison (`hmac.compare_digest`) prevents signature-oracle timing leaks.

### The three environment variables

Defined in `osimflow/task_payload_hmac.py`:

| Env Var | Purpose |
|---|---|
| `OSIMFLOW_TASK_PAYLOAD` | Serialized step call (`schema_version`, `name`, `step`, encoded `args`/`kwargs`, `result_hint`) |
| `OSIMFLOW_TASK_PAYLOAD_SIG` | Hex HMAC-SHA256 digest computed over the **exact payload string bytes** (UTF-8) as submitted |
| `OSIMFLOW_TASK_PAYLOAD_SECRET` | Shared secret used for signing (orchestrator side) and verification (worker side) |

On Nomad dispatch jobs the same values travel as dispatch meta keys
(`task_payload`, `task_payload_sig`, `task_payload_secret`), exposed inside the
task as `NOMAD_META_task_payload`, `NOMAD_META_task_payload_sig`, and
`NOMAD_META_task_payload_secret`. The worker resolves the secret env-first,
then the Nomad meta fallback (`resolve_payload_secret`).

### Provisioning the secret

Set `OSIMFLOW_TASK_PAYLOAD_SECRET` in the **orchestrator** environment before
launching a campaign. `build_signature_env` picks it up and ships the
signature + secret pair alongside each job's payload. Without it the runner
rejects every payload (see below), so it is effectively **required** for
Nomad/Kubernetes/AWS Batch/Azure Batch/Google Batch/Docker Swarm campaigns.

```bash
# Generate a fresh secret (any 256-bit random value works)
export OSIMFLOW_TASK_PAYLOAD_SECRET="$(openssl rand -hex 32)"
osimflow run --executor nomad ...
```

Currently the secret is provisioned as a **plain environment variable** on both
sides. The hardening direction (issue #1449) is per-substrate secret stores:
inject `OSIMFLOW_TASK_PAYLOAD_SECRET` into worker pods via a Kubernetes Secret
(and the orchestrator via IRSA/External Secrets — see
[Kubernetes Secrets](#kubernetes-secrets) above), and via a Nomad Vault
template stanza on Nomad, rather than a literal orchestrator environment
variable. Note that because the secret travels in the same job env as the
payload today, anything that can read the full job env (for example
`kubectl get pod -o yaml` or `nomad alloc status` output on an insecure
cluster) also sees it — mTLS + ACL hardening of the substrate (see the
[Nomad Production Guide](nomad-production.md)) is part of the same defense.

### Fail-closed verification semantics

`osimflow.remote_runner` verifies the signature **before** `json.loads` or any
step execution, and fails closed in every ambiguous case:

1. **No secret configured** on the worker → `RuntimeError` — unsigned payloads
   are rejected by design (issue #1205).
2. **Missing or empty `OSIMFLOW_TASK_PAYLOAD_SIG`** → `RuntimeError`.
3. **Signature mismatch** (tampered payload, wrong secret) → `RuntimeError`
   via `hmac.compare_digest` (constant-time).

The runner only proceeds to decode and execute after
`verify_task_payload` returns True.

### Rotation

The signature and secret are bound to a job **at submission time** — a job
already in flight carries its own `SIG` + `SECRET` pair and keeps verifying
against it even after the orchestrator rotates. To rotate:

1. Generate a new secret (`openssl rand -hex 32`).
2. Update the orchestrator environment (restart the coordinator process).
3. Revoke the old secret in your secret store once no in-flight campaigns
   remain. Jobs submitted before the rotation complete normally; new
   submissions use the new secret.

### Work-script env scrub (issue #1388)

`OSIMFLOW_TASK_PAYLOAD_SECRET` and `OSIMFLOW_TASK_PAYLOAD_SIG` are
**deliberately absent** from the work-script subprocess environment.
`osimflow/work.py:_sanitize_env` uses an explicit per-name allowlist (the
legacy `OSIMFLOW_*` prefix wildcard was removed in commit 8470449), so the
HMAC secret/signature pair never reaches `bin/*.py`, BYOS scripts, or
`openstudio.cli` child processes. The only legitimate consumer is
`osimflow.remote_runner`, whose environment comes directly from the substrate
job spec — not through the work-script allowlist. This prevents a compromised
work script from reading the signing secret and forging payloads.

### See also

- [Kubernetes Deployment Guide](kubernetes-deployment.md) — job env vars and
  RBAC setup for the runner Jobs
- [Nomad Production Guide](nomad-production.md) — ACL model, mTLS, and
  dispatch-mode behavior

---

## API Server Authentication

The REST API server (`osimflow serve`) supports API key authentication for single-user and multi-user deployments.

### Single-key mode

Use `--api-key` for a single API key. When `--enable-writes` or `--read-write` is set, an API key is **required** (auto-generated and logged if not provided):

```bash
# Auto-generated key (logged at startup)
osimflow serve --outdir ./results --read-write

# Explicit key
osimflow serve --outdir ./results --read-write --api-key '<your-api-key>'
```

Clients authenticate via the `X-API-Key` header or the `api_key` query parameter:

```bash
curl -H "X-API-Key: <your-api-key>" http://localhost:8000/api/v1/campaign
```

### Multi-user mode (`--api-keys-file`)

For multi-user deployments (issue #395), use `--api-keys-file` to specify a JSON file with per-user keys and roles. When set, `--api-key` is ignored.

**`api_keys.json`:**

```json
{
  "users": [
    {"key": "<alice-api-key>", "user_id": "alice", "role": "admin"},
    {"key": "<bob-api-key>", "user_id": "bob", "role": "readwrite"},
    {"key": "<carol-api-key>", "user_id": "carol", "role": "readonly"}
  ]
}
```

```bash
osimflow serve \
  --outdir ./results \
  --read-write \
  --api-keys-file ./api_keys.json \
  --tls-cert /path/to/cert.pem \
  --tls-key /path/to/key.pem \
  --host 0.0.0.0
```

### Permission levels

Roles are hierarchical (`admin > readwrite > readonly`), defined in `osimflow/api/auth.py`:

| Role | Can read | Can write (POST/PUT/DELETE) | Can manage keys |
|---|---|---|---|
| `readonly` | ✅ | ❌ | ❌ |
| `readwrite` | ✅ | ✅ | ❌ |
| `admin` | ✅ | ✅ | ✅ |

API key validation uses `secrets.compare_digest` (constant-time comparison) to prevent timing attacks. See `osimflow/api/auth.py:validate_api_key`.

### Generating secure API keys

Use the built-in generator or any secure random source:

```bash
# Python (matches the OSimFlow internal generator)
python -c "import secrets; print(secrets.token_urlsafe(32))"

# openssl
openssl rand -base64 32
```

> **TLS is required for API key authentication.** Without TLS, API keys are transmitted in clear text and are vulnerable to interception. Always use `--tls-cert` and `--tls-key` for network-accessible deployments. See [`docs/api.md`](api.md) §TLS.

### Protecting the API keys file

- **Never commit `api_keys.json` to git.** Add it to `.gitignore`.
- Store the keys file outside the `--outdir` campaign directory so it is not included in archived results.
- For containerised deployments, mount the keys file as a read-only volume.
- **Set restrictive file mode (`chmod 0600`).** The server refuses to
  load a file that is group or world readable at startup (issue #1480,
  mirrors the `--result-storage-endpoint` HTTPS rule from issue #1386).
  On a shared HPC login node or multi-tenant host, a permissive mode
  hands every local account every API key — including admin-role keys.
  Override the check with `--allow-insecure-api-keys-file` (dev/test only).
- Rotate keys regularly — remove a user's entry and restart the server to revoke access immediately.

---

## Best Practices

### Rotate secrets

- **AWS IAM roles:** AWS rotates temporary credentials automatically (typically every 6 hours). No action needed.
- **AWS Secrets Manager:** Enable automatic rotation with a Lambda rotation function. Set the rotation interval to match your organisation's policy (e.g., 30/60/90 days).
- **Vault:** Use dynamic secrets (short-lived, machine-generated) wherever possible. For static secrets, set a rotation schedule in the Vault policy.
- **API keys:** Rotate on a fixed schedule. The multi-user store supports instant revocation — remove the entry and restart `osimflow serve`.
- **Slurm environment variables:** Rotate by re-running the init script with fresh credentials. For long campaigns, use the Vault Agent sidecar pattern (above).

### Least-privilege IAM

- Scope IAM policies to specific resource ARNs. Never use `"Resource": "*"` for application permissions.
- The Terraform module's task role is scoped to one S3 bucket and one CloudWatch log group by default. Extend it with additional scoped policies (Secrets Manager, KMS) only when needed.
- For CI/CD, use GitHub OIDC (`aws-actions/configure-aws-credentials`) instead of long-lived access keys. See the nightly E2E workflow in `.github/workflows/aws-batch-e2e.yml`.
- Review IAM policies with tools like `aws iam simulate-principal-policy` or `parliament` (AWS IAM linting).

### Never log secrets

OSimFlow's structured JSON logger (`osimflow/logging.py:JSONFormatter`) outputs machine-parseable JSON to stdout and rotating log files. It does **not** redact secret values automatically — if a secret value appears in a `log.info()` call, it will be in the JSON output.

Follow these rules:

1. **Never pass secret values to `log.*()` calls.** Log metadata (e.g., "retrieved secret from Vault") not values.
2. **Never `print()` secrets.** The Campaign captures stdout/stderr from init scripts and BYOS scripts into `run.json` and per-sample log files.
3. **Avoid echoing env vars in shell scripts.** An init script that runs `env` or `set` will dump all secrets into the log. Use `echo "Secrets loaded"` instead of `echo $API_TOKEN`.
4. **Use constant-time comparison for API key validation.** OSimFlow already does this internally (`secrets.compare_digest` in `osimflow/api/auth.py`). If you write custom auth code, follow the same pattern.

### Redaction patterns

While OSimFlow's `JSONFormatter` does not have built-in field redaction, you can pre-filter secrets before logging:

```python
import os
import re

_SENSITIVE_KEYS = frozenset({
    "password", "secret", "token", "api_key", "access_key", "private_key",
})

def redact(data: dict) -> dict:
    """Return a copy of *data* with sensitive values replaced."""
    return {
        k: ("***REDACTED***" if k.lower() in _SENSITIVE_KEYS else v)
        for k, v in data.items()
    }
```

Apply this to any dict before passing it to `log.info("config: %s", redact(config_dict))`.

### BYOS script isolation

- BYOS scripts loaded with `--byos-trust-level subprocess` (the default, issue #269) run in an isolated child process, limiting the blast radius of a malicious script.
- Use `--byos-resource-limits` (issue #343) to set CPU/memory limits on BYOS subprocess wrappers.
- Treat user-supplied scripts as untrusted code. Review BYOS scripts before running them, especially on shared clusters.

### Filesystem hygiene

- **Never commit** `.osm`, `.osw`, `.idf`, `.epw`, `eplusout.*` files — the `.gitignore` excludes them.
- **Never commit** `api_keys.json`, `.env`, `*.pem`, `*.key`, or `id_rsa*` files.
- Use **`git-lfs`** for large inputs that must be tracked — don't bypass the gitignore.
- Run `make precommit` before pushing — it runs `gitleaks` for secrets detection (see `docs/DEVELOPMENT.md`).

### Secret-adjacent files

The following files must never contain real secrets:

| File | What goes there | What must NOT go there |
|---|---|---|
| `.env.example` | Port numbers, non-sensitive defaults | Real passwords, tokens, keys |
| `variables.yml` | Sampling distributions, measure args | Credentials, API keys |
| `api_keys.json` | (not tracked — `.gitignore`'d) | (must never be committed) |
| `infra/aws/terraform/*.tfvars` | Region, project name, vCPU counts | AWS access keys |
| `docker-compose.yml` | Service definitions, port mappings | Hardcoded passwords (use env refs) |

## Quick Reference — Secret Sources by Executor

| Need | LocalExecutor | SlurmExecutor | AWSBatchExecutor | KubernetesExecutor |
|---|---|---|---|---|
| **AWS credentials** | `AWS_PROFILE` env var | `AWS_PROFILE` / env vars | IAM task role (automatic) | IRSA / EKS pod identity |
| **Database token** | Init script | Init script + Vault | Secrets Manager + IAM | K8s Secret + External Secrets |
| **API token** | Env var | `submitit` setup | Secrets Manager / SSM Parameter Store | K8s Secret as env var |
| **Custom CA cert** | `REQUESTS_CA_BUNDLE` | Init script `export` | Job def env var | ConfigMap mount |
| **TLS cert/key** | File path flag | Init script + file perms | ACM + job def | K8s TLS Secret + volume mount |

## See Also

- [AGENTS.md §10](../AGENTS.md) — security policy (IAM roles, no access keys, no bind-mounted secrets)
- [AWS Batch Terraform Guide](aws-batch-terraform.md) — IAM role provisioning and least-privilege policies
- [AWS Batch Deployment Guide](deployment/aws-batch.md) — manual IAM role creation and security checklist
- [API Reference](api.md) — REST API endpoints, TLS configuration, and API key auth
- [Kubernetes Deployment Guide](kubernetes-deployment.md) — K8s RBAC and service account setup
- [Nomad Production Guide](nomad-production.md) — ACL model and token management
- [Observability Guide](observability.md) — CloudWatch/Prometheus/OpenTelemetry backend configuration (uses IAM roles)
