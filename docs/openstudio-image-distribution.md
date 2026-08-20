# OpenStudio image distribution

> **Audience:** OSimFlow maintainers and contributors. Anyone asking
> "where does the OpenStudio CLI container come from?" should land here.

## TL;DR

OSimFlow **consumes** `nrel/openstudio` directly from Docker Hub.
OSimFlow does **not** build, publish, or sign a project-owned copy of
the OpenStudio CLI image. The image is maintained by NREL.

- **Registry:** `docker.io/nrel/openstudio`
- **Tag contract:** any stable release NREL publishes (3.7.0 →
  current), selected via `--openstudio_version`
- **What OSimFlow owns:** nothing on the registry side. A weekly CI
  check (`.github/workflows/openstudio-image-availability.yml`) that
  alerts if a pinned version disappears upstream.

This document supersedes the four-option license-gated design that was
originally proposed in
[issue #9](https://github.com/anchapin/OSimFlow/issues/9). See
[`../.agents/results/architecture/0002-adopt-nrel-upstream-image.md`](../.agents/results/architecture/0002-adopt-nrel-upstream-image.md)
for the full decision record (ADR-0002).

## Why we don't build it ourselves

The NREL OpenStudio installer is proprietary. Until 2026, that fact
implied OSimFlow could not legally redistribute a project-owned
container image, and an in-CI build seemed impractical.

That premise no longer holds. NREL — the upstream maintainer of both
the installer and the published image — has been publishing
`nrel/openstudio` to Docker Hub continuously since 2018, with **192
tags** as of 2026-06-09, including every stable release from 3.6.1
through 3.11.0, pre-release tags, a `develop` alias, and Ubuntu-LTS
suffixed variants. Active pulls are observed daily.

The license posture of consuming the public Docker Hub artifact is
identical to the license posture of building the image from the
installer in CI: in both cases, OSimFlow depends on NREL's published
license terms for the OpenStudio distribution. Building a parallel
private fork would not avoid the license; it would only duplicate
NREL's work and create a maintenance liability that nobody needs.

## Supported versions

These are the versions OSimFlow advertises as supported. The list is
hand-maintained, mirrored in
[`.github/workflows/openstudio-image-availability.yml`](../.github/workflows/openstudio-image-availability.yml)
(`SUPPORTED_OPENSTUDIO_VERSIONS`), and validated weekly by the
availability check. NREL publishes older tags on Docker Hub (down to
`3.1.0`); the floor of `3.7.0` reflects the lowest version OSimFlow
has integration tests against, not the limit of NREL's image set.

| Version | Tag on Docker Hub | Notes |
|---|---|---|
| 3.7.0 | `docker.io/nrel/openstudio:3.7.0` | LTS-compatible (Ubuntu 20.04 base) |
| 3.7.0 | `docker.io/nrel/openstudio:3.7.0-2204` | Ubuntu 22.04 base |
| 3.8.0 | `docker.io/nrel/openstudio:3.8.0` | |
| 3.9.0 | `docker.io/nrel/openstudio:3.9.0` | |
| 3.10.0 | `docker.io/nrel/openstudio:3.10.0` | |
| 3.11.0 | `docker.io/nrel/openstudio:3.11.0` | latest stable |

**Adding a version** is a two-step PR:

1. Add the new tag to the matrix in
   `.github/workflows/openstudio-image-availability.yml`.
2. Update the table above.

No code change is required: the campaign reads
`docker.io/nrel/openstudio:{version}` directly from the
`--openstudio_version` flag.

**Removing a version** is the same two steps in reverse. The
availability check will auto-open a GitHub Issue if NREL deletes a
tag we still advertise, so a missed removal is loud, not silent.

## How the cache key reflects the image

`osimflow/campaign.py` defines:

```python
CONTAINER_OS = "docker.io/nrel/openstudio:{version}"
```

The `RUN_OPENSTUDIO_SIM` step includes `CONTAINER_OS.format(version=...)`
in the cache key (`container_digest` column in `cache_entries`). Two
campaign runs with the same `--openstudio_version` therefore share
the same cache entry, and bumping the version correctly invalidates
only the simulation step (not LHS, apply, extract, aggregate).
This matches the invalidation rules documented in
[`../osimflow/cache.py`](../osimflow/cache.py).

## What "consume" means in practice

The image is the *runtime* for the OpenStudio CLI. The OSimFlow
`work.py:run_openstudio_sim` step wraps the standard
`openstudio.cli run -w workflow.osw` invocation. There is no
in-container `bin/*.py` shim — the Python orchestration lives in
the *driver* container, and the driver calls into the OpenStudio
container per sample. The campaign passes the resolved image string
as the executor's `container=` argument
(`osimflow/campaign.py:step_run_openstudio_sim`).

If a future feature needs the OSimFlow Python layer inside the
OpenStudio container, switch to a `FROM nrel/openstudio:X`
multi-stage build — still simpler than building from the installer.

## Break-glass: if `nrel/openstudio` goes away

We treat the upstream image as a soft dependency. If NREL ever
deprecates the entire `nrel/openstudio` repository:

1. **Day 0** — the weekly availability check fails and opens an
   issue. Local smoke runs continue to work as long as Docker
   caches the previously-pulled images.
2. **Day 1–2** — a maintainer writes `docker/openstudio_cli/Dockerfile`
   using NREL's public installer (the installer URL and license
   mechanism are documented at
   <https://www.openstudio.net/>). The Dockerfile is a 30-line
   `FROM ubuntu:22.04` + `RUN curl … | bash` recipe; the OSimFlow
   layer is empty because the campaign is the driver, not the
   container.
3. **Day 2–3** — the new Dockerfile is built manually and pushed
   to a registry (Docker Hub personal account is fine for the
   transition; promotion to a project-owned namespace can wait).
4. **Day 3+** — `CONTAINER_OS` is repointed at the new location
   in a one-line change. Existing cache entries invalidate because
   the cache key includes the container string.

The break-glass plan is intentionally a *plan*, not a pre-built
artifact. Pre-building it would re-introduce the maintenance
liability the adoption decision removed.

## Supply-chain trust model

OSimFlow trusts the `nrel/openstudio` image via **digest pinning**, not
tag-based references. This establishes an auditable, reproducible link
between the declared version and the exact bytes that run in production.

### Digest-pinned pull

The ECR sync script
([`infra/aws/scripts/sync-openstudio-to-ecr.sh`](https://github.com/anchapin/OSimFlow/blob/main/infra/aws/scripts/sync-openstudio-to-ecr.sh))
resolves the tag to a content-addressable digest before any pull:

```bash
DIGEST=$(docker manifest inspect "${SOURCE_IMAGE}" | jq -r '.[0].digest')
docker pull "${SOURCE_IMAGE}@${DIGEST}"
```

- If the digest cannot be resolved (e.g., network failure, manifest
  unavailable), the script exits immediately with a clear error — it
  does **not** fall back to a bare tag pull.
- The resolved digest is logged and embedded in the ECR tag, so a
  future re-sync of the same version tag can be compared against the
  previously-pinned digest.

### Cosign signature verification

If [`cosign`](https://github.com/sigstore/cosign) is installed **and**
NREL publishes a Sigstore attestation for the image, the sync script
runs:

```bash
cosign verify --certificate-identity "https://github.com/NREL/OpenStudio" \
  "${SOURCE_IMAGE}@${DIGEST}"
```

- **NREL currently does not publish cosign signatures** for
  `nrel/openstudio`. In this case, the verification step is skipped
  with an explicit logged warning:

  ```
  WARNING: Skipping cosign verification — NREL does not publish image
  signatures (trusted via digest pin)
  ```

- If cosign is not installed at all, a similar informational message is
  logged. Neither case is a hard failure.
- If NREL begins publishing signatures and the verification fails, the
  sync script exits with an error rather than pushing an unverified
  image to ECR.

### Failure behaviour

| Condition | Behaviour |
|---|---|
| Digest resolution fails | Hard fail — sync aborts, no image pushed |
| cosign available + NREL signs + verify fails | Hard fail — sync aborts |
| cosign available + NREL does not sign | Warning logged, sync continues |
| cosign not available | Info logged, sync continues |

### Trust assumptions

OSimFlow's current trust posture for `nrel/openstudio` is:

1. **NREL's Docker Hub namespace** is the authoritative source.
2. The **digest pin** guarantees bit-for-bit reproducibility across
   sync runs, even if a tag is later re-tagged by NREL.
3. **No third-party signature** is verified today because NREL does
   not publish one.
4. If NREL adds Sigstore signing, the existing cosign verification
   block activates automatically (no script changes required).

## References

- [ADR-0002: Adopt `nrel/openstudio` upstream](../.agents/results/architecture/0002-adopt-nrel-upstream-image.md)
- [Issue #9 on GitHub](https://github.com/anchapin/OSimFlow/issues/9)
- [NREL OpenStudio on Docker Hub](https://hub.docker.com/r/nrel/openstudio/tags)
- [OpenStudio license terms](https://www.openstudio.net/) (consult
  the linked EULA; OSimFlow inherits the same terms whether we
  consume from Docker Hub or build from the installer)
