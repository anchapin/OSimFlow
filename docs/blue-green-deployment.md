<!-- docs-skip -->
# Blue/Green Deployment Guide (issue #402)

This guide explains how to deploy the OSimFlow API server
(`osimflow serve`) using a blue/green strategy for zero-downtime
updates. OSimFlow campaigns can run for hours or days; a deployment
that interrupts a running campaign wastes compute time and delays
results. Blue/green deployment lets you ship new versions without
dropping in-flight requests.

**Audience:** DevOps engineers or IT administrators responsible for
keeping the OSimFlow API server available during upgrades. The guide
covers four platforms: Docker Compose (single host), Kubernetes,
Nomad, and AWS (ALB + ECS/Fargate).

> **Scope note:** This guide applies blue/green to the **API server**
> (`osimflow serve`) — the HTTP process that monitors and controls
> campaigns. The **campaign coordinator** (the `Campaign` class) is a
> single-instance process by design; see
> [ADR-0003](../.agents/results/architecture/0003-coordinator-high-availability.md)
> for coordinator HA patterns.

---

## 1. Overview

### What is blue/green deployment?

Blue/green deployment maintains two identical production environments:

- **Blue** — the currently live environment serving traffic.
- **Green** — the new version, staged and health-checked before it
  receives traffic.

A load balancer (or router) sits in front of both environments. At
switchover time, the router redirects all traffic from Blue to Green.
If Green misbehaves, you flip the router back to Blue — the previous
version is still running and healthy.

### Why it matters for OSimFlow

The OSimFlow API server is the monitoring surface for long-running
campaigns. A typical campaign (500–1000 samples) takes **hours to
days** on real infrastructure. Killing the API server mid-campaign
means:

- **SSE clients lose their live event stream** (`GET /api/v1/events`).
- **Monitoring dashboards go dark** until a new server starts.
- **Operators lose real-time visibility** into per-sample progress.

Blue/green ensures that at no point during a deployment is the API
server unavailable. The old environment keeps serving until the new one
is verified healthy.

---

## 2. Architecture

```
                    ┌──────────────────┐
                    │   Load Balancer   │
                    │  (nginx / ALB /   │
                    │   Traefik / etc)  │
                    └────────┬─────────┘
                             │
              ┌──────────────┴──────────────┐
              │                              │
              ▼                              ▼
    ┌──────────────────┐           ┌──────────────────┐
    │  BLUE (live)      │           │  GREEN (staging)  │
    │  osimflow serve   │           │  osimflow serve   │
    │  v1.2.0           │           │  v1.3.0           │
    │                   │           │                   │
    │  /health → 200    │           │  /health → 200    │
    │  /ready  → 200    │           │  /ready  → 200    │
    └────────┬──────────┘           └────────┬──────────┘
             │                               │
             └─────────────┬─────────────────┘
                           │
                    ┌──────▼──────┐
                    │ Shared Volume │
                    │  (outdir/)    │
                    │  run.json     │
                    │  cache.db     │
                    │  work/queue/  │
                    └──────────────┘
```

Both environments share the same campaign `outdir/` on a networked
volume so that the green server can read the same `run.json` and
campaign artifacts. Switchover is instant: the load balancer simply
updates its upstream target.

---

## 3. Docker Compose (Single Host)

For single-host deployments, nginx acts as the load balancer in front
of two `osimflow serve` containers.

### 3.1. docker-compose.yml

```yaml
version: "3.8"

services:
  osimflow-blue:
    image: ghcr.io/anchapin/scientific_python_image:latest
    command: >
      osimflow serve
      --outdir /data/campaigns
      --host 0.0.0.0
      --port 8000
      --read-write
    volumes:
      - campaign-data:/data/campaigns
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 5s
    restart: unless-stopped

  osimflow-green:
    image: ghcr.io/anchapin/scientific_python_image:latest
    # Override the image tag when deploying a new version:
    #   docker compose up -d --no-deps osimflow-green
    command: >
      osimflow serve
      --outdir /data/campaigns
      --host 0.0.0.0
      --port 8000
      --read-write
    volumes:
      - campaign-data:/data/campaigns
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 5s
    restart: unless-stopped

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      osimflow-blue:
        condition: service_healthy
    restart: unless-stopped

volumes:
  campaign-data:
```

### 3.2. nginx.conf

The nginx config uses an upstream variable so the switchover script can
toggle between blue and green without rewriting the server block:

```nginx
upstream osimflow_backend {
    server osimflow-blue:8000;
}

server {
    listen 80;

    location / {
        proxy_pass http://osimflow_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support (issue #143)
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
    }

    # Health check passthrough (no auth required)
    location = /health {
        proxy_pass http://osimflow_backend/health;
        access_log off;
    }

    location = /ready {
        proxy_pass http://osimflow_backend/ready;
        access_log off;
    }
}
```

### 3.3. Switchover script

```bash
#!/usr/bin/env bash
set -euo pipefail

# scripts/blue-green-switch.sh — toggle nginx upstream between blue and green.

ACTIVE="blue"
INACTIVE="green"
NGINX_CONF="nginx.conf"

# Detect which upstream is currently active.
if grep -q "osimflow-green:8000" "$NGINX_CONF"; then
    ACTIVE="green"
    INACTIVE="blue"
fi

echo "Active: $ACTIVE → Switching to: $INACTIVE"

# Point nginx at the inactive (new) environment.
sed -i "s/server osimflow-${ACTIVE}:8000;/server osimflow-${INACTIVE}:8000;/" "$NGINX_CONF"

# Validate that the new upstream is healthy before reloading nginx.
HEALTH_URL="http://osimflow-${INACTIVE}:8000/health"
if ! curl -sf "$HEALTH_URL" | grep -q '"alive"'; then
    echo "ERROR: $INACTIVE failed health check at $HEALTH_URL" >&2
    echo "Reverting nginx config..." >&2
    sed -i "s/server osimflow-${INACTIVE}:8000;/server osimflow-${ACTIVE}:8000;/" "$NGINX_CONF"
    exit 1
fi

# Reload nginx gracefully (zero-downtime).
docker compose exec nginx nginx -s reload

echo "Switch complete. $INACTIVE is now live."
echo "Previous version ($ACTIVE) is still running for rollback."
```

### 3.4. Deploying a new version

```bash
set -euo pipefail

# 1. Build or pull the new image.
docker compose pull osimflow-green

# 2. Recreate only the green container (blue stays live).
docker compose up -d --no-deps osimflow-green

# 3. Wait for green to pass health checks.
until curl -sf http://osimflow-green:8000/ready | grep -q '"ready"'; do
    echo "Waiting for green to become ready..."
    sleep 2
done

# 4. Switch traffic.
./scripts/blue-green-switch.sh

# 5. (Optional) Shut down the old blue container after observation.
# docker compose stop osimflow-blue
```

---

## 4. Kubernetes

Kubernetes supports rolling updates natively via the `Deployment`
strategy. Setting `maxSurge: 1` and `maxUnavailable: 0` ensures that a
new pod is started and becomes ready **before** any old pod is
terminated — effectively a blue/green cutover.

### 4.1. Deployment manifest

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: osimflow-api
  namespace: osimflow
  labels:
    app: osimflow-api
spec:
  replicas: 2
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1        # Start the new pod before removing any old pod.
      maxUnavailable: 0  # Never go below the desired replica count.
  selector:
    matchLabels:
      app: osimflow-api
  template:
    metadata:
      labels:
        app: osimflow-api
    spec:
      containers:
        - name: osimflow-api
          image: ghcr.io/anchapin/scientific_python_image:latest
          command: ["osimflow", "serve"]
          args:
            - --outdir
            - /data/campaigns
            - --host
            - "0.0.0.0"
            - --port
            - "8000"
            - --read-write
          ports:
            - containerPort: 8000
          # Liveness: is the process alive?
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
            failureThreshold: 3
          # Readiness: can the server serve requests?
          readinessProbe:
            httpGet:
              path: /ready
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 5
            failureThreshold: 3
          volumeMounts:
            - name: campaign-data
              mountPath: /data/campaigns
      volumes:
        - name: campaign-data
          persistentVolumeClaim:
            claimName: campaign-pvc
```

### 4.2. Service + Ingress

```yaml
apiVersion: v1
kind: Service
metadata:
  name: osimflow-api
  namespace: osimflow
spec:
  selector:
    app: osimflow-api
  ports:
    - port: 80
      targetPort: 8000
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: osimflow-api
  namespace: osimflow
spec:
  rules:
    - host: osimflow.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: osimflow-api
                port:
                  number: 80
```

### 4.3. Triggering a rollout

```bash
set -euo pipefail

# Update the image tag, then apply.
kubectl set image deployment/osimflow-api \
    osimflow-api=ghcr.io/anchapin/scientific_python_image:v1.3.0 \
    -n osimflow

# Watch the rollout. The new pod must pass readiness checks
# (GET /ready → 200) before traffic is routed to it.
kubectl rollout status deployment/osimflow-api -n osimflow
```

### 4.4. Integration with the KubernetesExecutor

The API server deployment above is for the **monitoring/control**
plane. Campaign simulation work is dispatched separately via the
`KubernetesExecutor`:

```bash
osimflow run \
    --executor kubernetes \
    --kubernetes-namespace osimflow \
    --input_variables variables.yml \
    --n_samples 500 \
    --outdir /data/campaigns/run-001 \
    --openstudio_version 3.11.0
```

The API server and the executor share the same `--outdir` via the
PersistentVolume. See the [Kubernetes Deployment
Guide](kubernetes-deployment.md) for executor-specific configuration.

---

## 5. Nomad

Nomad supports blue/green deployments via the `canary` stanza in the
`update` block. When `canary` is set, Nomad creates the new allocation
without stopping the old one — the operator promotes the canary to cut
over traffic.

### 5.1. Job specification

```hcl
job "osimflow-api" {
  datacenters = ["dc1"]

  update {
    max_parallel      = 1
    health_check      = "checks"
    min_healthy_time  = "10s"
    healthy_deadline  = "5m"
    progress_deadline = "10m"
    auto_revert       = true
    canary            = 1   # Create a canary allocation alongside the stable one.
  }

  group "api" {
    count = 1

    network {
      port "http" {
        to = 8000
      }
    }

    service {
      name = "osimflow-api"
      port = "http"

      check {
        type     = "http"
        path     = "/health"
        interval = "10s"
        timeout  = "5s"
      }

      check {
        type     = "http"
        path     = "/ready"
        interval = "10s"
        timeout  = "5s"
      }

      # Tag the canary so the load balancer can route to it selectively.
      tag = "stable"
    }

    task "server" {
      driver = "docker"

      config {
        image = "ghcr.io/anchapin/scientific_python_image:latest"
        ports = ["http"]

        args = [
          "osimflow", "serve",
          "--outdir", "/data/campaigns",
          "--host", "0.0.0.0",
          "--port", "8000",
          "--read-write",
        ]
      }

      # Mount the shared campaign volume (see nomad-production.md §Pattern 1).
      volume_mount {
        volume      = "campaign-data"
        destination = "/data/campaigns"
      }
    }
  }
}
```

### 5.2. Performing the deployment

```bash
set -euo pipefail

# 1. Submit the updated job (new image tag). Nomad creates a canary
#    allocation alongside the existing stable one.
nomad job run osimflow-api.nomad

# 2. Verify the canary passes health checks (/health and /ready).
nomad alloc status -stats <canary-alloc-id>

# 3. Promote the canary — this stops the old allocation and routes
#    all traffic to the new one.
nomad job promote osimflow-api

# 4. (If something goes wrong) The update block has auto_revert = true,
#    so Nomad will automatically roll back if the canary fails its
#    health check within healthy_deadline.
```

### 5.3. Reference

The cluster topology, ACL model, and TLS configuration for Nomad are
documented in the [Nomad Production Deployment
Guide](nomad-production.md). The job spec above extends the existing
`infra/nomad/` infrastructure with the `canary` stanza for the API
server role.

---

## 6. AWS (ALB + ECS/Fargate)

On AWS, the Application Load Balancer (ALB) routes traffic to target
groups. Blue/green is achieved by maintaining two target groups (blue
and green) and swapping the listener rule at switchover time.

### 6.1. Architecture

```
                     ┌──────────────────┐
                     │       ALB         │
                     │  listener :443    │
                     └────────┬──────────┘
                              │
                    ┌─────────┴──────────┐
                    │                    │
                    ▼                    ▼
          ┌─────────────────┐  ┌─────────────────┐
          │  TG-Blue         │  │  TG-Green        │
          │  (port 8000)     │  │  (port 8000)     │
          └────────┬─────────┘  └────────┬─────────┘
                   │                      │
                   ▼                      ▼
          ┌─────────────────┐  ┌─────────────────┐
          │  ECS Service     │  │  ECS Service     │
          │  (task rev 3)    │  │  (task rev 4)    │
          │  Fargate          │  │  Fargate          │
          └─────────────────┘  └─────────────────┘
```

### 6.2. Target group health checks

Both target groups use the OSimFlow health endpoints:

```bash
set -euo pipefail

# Blue target group
aws elbv2 create-target-group \
    --name osimflow-tg-blue \
    --protocol HTTP \
    --port 8000 \
    --vpc-id vpc-xxxxxxxx \
    --health-check-path /health \
    --health-check-interval-seconds 10 \
    --health-check-timeout-seconds 5 \
    --healthy-threshold-count 2 \
    --unhealthy-threshold-count 3

# Green target group (identical config)
aws elbv2 create-target-group \
    --name osimflow-tg-green \
    --protocol HTTP \
    --port 8000 \
    --vpc-id vpc-xxxxxxxx \
    --health-check-path /health \
    --health-check-interval-seconds 10 \
    --health-check-timeout-seconds 5 \
    --healthy-threshold-count 2 \
    --unhealthy-threshold-count 3
```

### 6.3. ECS task definition

```json
{
  "family": "osimflow-api",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "arn:aws:iam::123456789012:role/osimflow-task-exec",
  "taskRoleArn": "arn:aws:iam::123456789012:role/osimflow-task",
  "containerDefinitions": [
    {
      "name": "osimflow-api",
      "image": "ghcr.io/anchapin/scientific_python_image:latest",
      "portMappings": [
        { "containerPort": 8000, "protocol": "tcp" }
      ],
      "command": [
        "osimflow", "serve",
        "--outdir", "/data/campaigns",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--read-write"
      ],
      "healthCheck": {
        "command": [
          "CMD-SHELL",
          "curl -sf http://localhost:8000/health || exit 1"
        ],
        "interval": 10,
        "timeout": 5,
        "retries": 3
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/osimflow-api",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

### 6.4. Automated switchover with CodeDeploy

AWS CodeDeploy can orchestrate the blue/green cutover for ECS
automatically:

```bash
set -euo pipefail

# Create a CodeDeploy application for ECS.
aws deploy create-application \
    --application-name osimflow-api \
    --compute-platform ECS

# Create a deployment group with blue/green configuration.
# The deployment group references both target groups and the ALB listener.
aws deploy create-deployment-group \
    --application-name osimflow-api \
    --deployment-group-name osimflow-api-dg \
    --deployment-config-name CodeDeployDefault.ECSAllAtOnce \
    --service-role-arn arn:aws:iam::123456789012:role/CodeDeployServiceRole \
    --deployment-style deploymentType=BLUE_GREEN,deploymentOption=WITH_TRAFFIC_CONTROL \
    --blue-green-deployment-configuration \
        '{
          "terminateBlueInstancesOnDeploymentSuccess": {
            "action": "TERMINATE",
            "terminationWaitTimeInMinutes": 30
          },
          "deploymentReadyOption": {
            "action": "CONTINUE_DEPLOYMENT",
            "waitTimeInMinutes": 0
          }
        }' \
    --load-balancer-info \
        '{
          "targetGroupPairInfoList": [{
            "targetGroups": [
              {"name": "osimflow-tg-blue"},
              {"name": "osimflow-tg-green"}
            ],
            "prodTrafficRoute": {
              "listenerArns": ["arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/osimflow-alb/xxx/yyy"]
            }
          }]
        }'

# Deploy a new task definition revision. CodeDeploy will:
#   1. Start the new task (green) and register it in TG-Green.
#   2. Wait for the /health check to pass.
#   3. Shift the ALB listener from TG-Blue → TG-Green.
#   4. Terminate the old task (blue) after the wait period.
aws deploy create-deployment \
    --application-name osimflow-api \
    --deployment-group-name osimflow-api-dg \
    --revision \
        '{
          "revisionType": "AppSpecContent",
          "appSpecContent": {
            "content": "{\"version\": 1, \"Resources\": [{\"TargetService\": {\"Type\": \"AWS::ECS::Service\", \"Properties\": {\"TaskDefinition\": \"osimflow-api:4\", \"LoadBalancerInfo\": {\"ContainerName\": \"osimflow-api\", \"ContainerPort\": 8000}}}}]}"
          }
        }'
```

### 6.5. IAM roles

The IAM roles referenced above follow the least-privilege model from
`infra/aws/terraform/iam.tf`:

| Role | Purpose |
|---|---|
| `osimflow-task-exec` | ECS agent: pull ECR image + write CloudWatch Logs |
| `osimflow-task` | Application: S3 read/write (campaign bucket) + CloudWatch Logs |
| `CodeDeployServiceRole` | CodeDeploy: manage ALB listener rules + ECS service updates |

See the [AWS Batch Terraform Guide](aws-batch-terraform.md) for the
Terraform module that provisions these roles.

---

## 7. Campaign Continuity

A blue/green deployment must not lose in-flight campaign work. OSimFlow
is designed so that running campaigns survive a server restart, provided
both environments share the same `outdir/` volume.

### 7.1. SQLite cache enables resume

The `SQLiteCache` (`osimflow/cache.py`) stores per-step, per-sample
completion records keyed on a content hash:

```
(step, sample_id, openstudio_version, inputs_sha256, code_sha256,
 container_digest)
```

When a campaign is re-run with the same `--outdir`, every step that
already completed is a **cache hit** and is skipped. This is the
foundation of resume semantics. After a blue/green switch, the new
server reads the existing `cache.db` and picks up exactly where the
previous server left off.

```bash
set -euo pipefail

# The campaign coordinator process (osimflow run) is separate from the
# API server (osimflow serve). If the API server is swapped via
# blue/green, the coordinator keeps running — the API server is a
# monitoring surface, not the execution engine.
#
# If the coordinator itself needs to restart (e.g., node failure), just
# re-run with the same --outdir:
osimflow run \
    --executor local \
    --input_variables variables.yml \
    --n_samples 500 \
    --outdir /shared/campaigns/run-001 \
    --openstudio_version 3.11.0

# Output:
#   cache HIT  step=GENERATE_LHS_SAMPLES sample=ALL -> ...
#   cache HIT  step=APPLY_PARAMETERS sample=sample_000 -> ...
#   cache HIT  step=RUN_OPENSTUDIO_SIM sample=sample_000 -> ...
#   ... (only incomplete samples are re-processed)
```

### 7.2. run.json persists across restarts

The monitoring trace (`run.json`) is written incrementally by
`RunTrace.update_sample()` in `osimflow/monitoring.py`. Each completed
sample triggers an atomic write (temp file + rename), so the file is
never in a corrupted state — even if the process is killed mid-campaign.

The new API server reads this `run.json` on startup:

- `GET /ready` checks that `run.json` is accessible.
- `GET /api/v1/campaign` returns campaign metadata from `run.json`.
- `GET /api/v1/steps` returns step timing from `run.json`.
- `GET /api/v1/samples` returns per-sample traces from `run.json`.

Because both blue and green servers point at the same `outdir/`, the
green server has immediate access to the full campaign history.

### 7.3. Job queue crash recovery

The `JobQueue` (`osimflow/jobqueue.py`) is a filesystem-based
persistence layer that tracks the lifecycle of each work item:

```
outdir/work/queue/
    pending/        ← not yet started
    in_progress/    ← currently being processed
    completed/      ← finished successfully
    failed/         ← finished with error
```

If the coordinator process crashes (or is restarted during a
deployment), `JobQueue.recover()` moves all `in_progress` jobs back to
`pending` so they are re-processed on the next run. This happens
automatically at campaign start.

> **Important:** `JobQueue` is **not** a distributed queue. Concurrent
> writers (multiple coordinators pointing at the same `outdir`) are not
> supported. Blue/green applies to the API server, not the coordinator.
> See [ADR-0003](../.agents/results/architecture/0003-coordinator-high-availability.md)
> for coordinator HA constraints.

---

## 8. Rollback Procedure

If the new (green) environment is unhealthy after switchover, roll back
to the previous (blue) environment. The blue container/pod/task is still
running and healthy — rollback is just another traffic switch.

### Docker Compose

```bash
set -euo pipefail

# The blue-green-switch.sh script toggles the nginx upstream.
# Running it again reverts to the previous environment.
./scripts/blue-green-switch.sh

# Verify blue is serving traffic.
curl -sf http://localhost/health | grep '"alive"'
curl -sf http://localhost/ready  | grep '"ready"'
```

### Kubernetes

```bash
set -euo pipefail

# Roll back to the previous Deployment revision.
kubectl rollout undo deployment/osimflow-api -n osimflow

# Verify the rollback completed.
kubectl rollout status deployment/osimflow-api -n osimflow

# Check which image is now live.
kubectl get deployment osimflow-api -n osimflow -o jsonpath='{.spec.template.spec.containers[*].image}'
```

### Nomad

```bash
set -euo pipefail

# If auto_revert did not trigger, manually revert by re-submitting
# the previous job specification or pointing the job at the previous
# image tag.
nomad job run osimflow-api-prev.nomad

# The update block's auto_revert = true should handle this automatically
# if the canary failed health checks within healthy_deadline.
```

### AWS (CodeDeploy)

```bash
set -euo pipefail

# List recent deployments and find the one to roll back.
aws deploy list-deployments \
    --application-name osimflow-api \
    --deployment-group-name osimflow-api-dg \
    --include-only-statusSucceeded

# Roll back by creating a new deployment with the previous task
# definition revision.
aws deploy create-deployment \
    --application-name osimflow-api \
    --deployment-group-name osimflow-api-dg \
    --revision \
        '{
          "revisionType": "AppSpecContent",
          "appSpecContent": {
            "content": "{\"version\": 1, \"Resources\": [{\"TargetService\": {\"Type\": \"AWS::ECS::Service\", \"Properties\": {\"TaskDefinition\": \"osimflow-api:3\", \"LoadBalancerInfo\": {\"ContainerName\": \"osimflow-api\", \"ContainerPort\": 8000}}}}]}"
          }
        }'
```

---

## 9. Health Check Integration

The OSimFlow API server exposes two health endpoints (defined in
`osimflow/api/app.py`) that integrate directly with load balancer probes:

### GET /health — liveness probe

Returns `{"status": "alive"}` with HTTP 200. This endpoint is always
available (no authentication required — it is in the `PUBLIC_PATHS`
set). Use this for:

- **Load balancer health checks** (ALB target group, nginx upstream check)
- **Kubernetes liveness probes** (`livenessProbe`)
- **Nomad service checks** (`check` block)
- **ECS container health checks**

```bash
curl -sf http://localhost:8000/health
# {"status": "alive"}
```

### GET /ready — readiness probe

Returns `{"status": "ready", "campaign_id": "..."}` with HTTP 200 when
the server can read `run.json` from the configured `--outdir`. Returns
`{"status": "not_ready", "reason": "..."}` when `run.json` is not yet
available. Use this for:

- **Kubernetes readiness probes** (`readinessProbe`) — the pod is not
  added to the Service endpoints until it can serve campaign data.
- **Nomad deployment health checks** — the canary allocation must pass
  this check before promotion.
- **Pre-switchover validation** — verify the green environment can read
  campaign data before switching traffic.

```bash
curl -sf http://localhost:8000/ready
# {"status": "ready", "campaign_id": "my-campaign-001"}
```

### Probe configuration summary

| Platform | Liveness | Readiness |
|---|---|---|
| **Docker Compose** | `healthcheck: GET /health` | Pre-switch validation: `GET /ready` |
| **Kubernetes** | `livenessProbe: GET /health` | `readinessProbe: GET /ready` |
| **Nomad** | `check: GET /health` | `check: GET /ready` |
| **AWS ALB** | Target group `health-check-path: /health` | CodeDeploy deployment readiness via `/health` |

> **Note:** The `/health` endpoint is intentionally lightweight (no
> filesystem I/O) so it responds even under heavy load. The `/ready`
> endpoint reads `run.json` from disk, so it adds a small latency cost —
> use it for readiness probes, not for high-frequency liveness checks.

See the [API Reference](api.md) for the full list of endpoints.
