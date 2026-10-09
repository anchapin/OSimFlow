# AWS Batch worker runtime (issue #1810)

AWS Batch launches the image in the **registered job definition**
(its container image). `OSIMFLOW_CONTAINER` in the job environment is
informational only and cannot change the launched image. The worker image must
therefore contain *both* the pinned OpenStudio/EnergyPlus CLI and OSimFlow on
Python >= 3.12 (the plain `nrel/openstudio` image has no OSimFlow and the
`docker/osimflow-cli` image has no OpenStudio).

`docker/osimflow-worker/Dockerfile` layers Python 3.12 + `osimflow[aws]`
(boto3) onto `nrel/openstudio:<version>` in an isolated venv
(`/opt/osimflow-venv`). The SDK is not rebuilt. Properties:

- Runs as non-root (`uid 10001`), writable scratch at `/scratch` (`TMPDIR`);
  no privileged mode, Docker socket or host mounts.
- No `ENTRYPOINT`; the default `CMD` and the executor's Batch
  `containerOverrides.command` (`python -m osimflow.remote_runner`) run
  verbatim.

## Build and smoke test

```bash
OS_VERSION=3.10.0
docker build -f docker/osimflow-worker/Dockerfile \
  --build-arg OPENSTUDIO_VERSION=$OS_VERSION -t osimflow-worker:$OS_VERSION .
docker run --rm --entrypoint python osimflow-worker:$OS_VERSION \
  /opt/osimflow/smoke_test.py
# Batch-style command override:
docker run --rm osimflow-worker:$OS_VERSION python -m osimflow.remote_runner --negotiate-version
```

The smoke test checks Python >= 3.12, non-root, imports (`osimflow`, `boto3`,
`remote_runner`), `openstudio --version` equals the pinned version, that the
remote runner accepts an HMAC-signed task payload (and rejects a tampered one),
and that a real OSW writes `eplusout.sql` in container scratch.

## Publish to ECR and record the digest

```bash
REPO=<account>.dkr.ecr.<region>.amazonaws.com/osimflow-worker
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin ${REPO%%/*}
docker tag osimflow-worker:$OS_VERSION $REPO:$OS_VERSION-osimflow-$(python -c 'import osimflow;print(getattr(osimflow,"__version__","dev"))')
docker push $REPO:$OS_VERSION-osimflow-<osimflow-version>
DIGEST=$(aws ecr describe-images --repository-name osimflow-worker \
  --image-ids imageTag=$OS_VERSION-osimflow-<osimflow-version> \
  --query 'imageDetails[0].imageDigest' --output text)
echo "$REPO@$DIGEST  openstudio=$OS_VERSION osimflow=<osimflow-version>" >> worker-images.txt
```

Record the digest with the OpenStudio and OSimFlow versions. Register the job
definition with the digest-pinned image — in Terraform set
`worker_image = "$REPO@$DIGEST"` (`infra/aws/terraform`).

## Image / version selection

The job definition is authoritative. When a run pins an image
(`--container-digest` or `--ecr-repository`), the
executor calls `describe_job_definitions` before `submit_job` and fails with an
actionable message unless the job definition's image matches (exact reference,
or the same `sha256:` digest). To use a different OpenStudio version or image,
register another job definition and pass it with `--aws-batch-job-definition`.

Not run in CI by default: ECR publishing and live AWS Batch execution (need AWS
credentials).
