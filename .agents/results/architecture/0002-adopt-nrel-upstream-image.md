# ADR-0002 — Adopt `nrel/openstudio` upstream; decommission the OSimFlow-built image pipeline

- **Status:** Accepted
- **Date:** 2026-06-09
- **Closes:** [#9](https://github.com/anchapin/OSimFlow/issues/9) (with maintainer ratification)
- **Supersedes:** the planned build pipeline in `docs/openstudio-image-distribution.md` (not yet written)
- **Related:** `osimflow/campaign.py:53` (`CONTAINER_OS` constant), `.github/workflows/openstudio-cli-image.yml` (now deleted), AGENTS.md §2/§11, PRD §1.4, PRD §5.2

## Context

Issue #9 was opened on 2026-06-09 with the stated premise that the project
could not implement an `openstudio_cli_image` build pipeline because the
NREL OpenStudio installer is proprietary and the project's license-aware
redistribution design was undecided. The issue lays out four options
(build at CI, build locally and mirror, have NREL publish to ghcr.io, or
use a different distribution mechanism) and gates everything on
"Part 1: License + distribution decision."

The premise is no longer accurate. As of 2026-06-09, NREL maintains
the `nrel/openstudio` image on Docker Hub with **192 published tags**,
including every stable release from 3.6.1 through 3.11.0 (the most
recent stable, pushed 2026-01-15), pre-release tags, a `develop`
alias, and Ubuntu-LTS-suffixed variants. The image is updated by
NREL staff (`@tijcolem`); pulls are observed daily. NREL is the
upstream maintainer of both the installer and the image, so the
license posture of consuming the public Docker Hub artifact is
identical to the license posture of building the image from the
installer in CI.

This collapses the four-option matrix in #9 into a single
decision: **adopt the upstream image and treat image maintenance as
NREL's responsibility.**

## Decision

1. **Default image registry:** `docker.io/nrel/openstudio` (the
   registry-neutral form `nrel/openstudio` is also acceptable; pin a
   registry explicitly to avoid resolver drift).
2. **Project-owned image pipeline:** none. Delete the stub at
   `.github/workflows/openstudio-cli-image.yml`. The cache-key
   machinery that previously hashed project-built image content is
   reduced to hashing the pinned image digest.
3. **Version pin contract:** the project advertises a list of
   versions known to be available upstream and supported by
   `--openstudio_version`. A weekly cron job verifies each pinned
   version still resolves; alerts (and the issue tracker) get a
   ticket on miss.
4. **Documentation:** write `docs/openstudio-image-distribution.md`
   (per the original ask in #9) describing the new posture, linking
   to NREL's license page, and documenting the upstream-dependency
   break-glass plan.

## What changes in the codebase

- `osimflow/campaign.py:53` — `CONTAINER_OS` becomes
  `"docker.io/nrel/openstudio:{version}"`. The dynamic `{version}`
  shape is unchanged.
- `.github/workflows/openstudio-cli-image.yml` — **deleted**; the
  no-op stub is no longer useful documentation once the design
  choice is documented elsewhere.
- `.github/workflows/openstudio-image-availability.yml` — **new**;
  weekly cron + `workflow_dispatch`, iterates over the supported
  version matrix, runs `docker manifest inspect`, fails on miss.
- `osimflow/campaign.py:_compute_code_hashes` — include the pinned
  image digest in the cache key for the `RUN_OPENSTUDIO_SIM` step
  (already trivially derivable from `CONTAINER_OS` + the resolved
  digest; no project-built image content to hash).
- `AGENTS.md` §2 / §11 — update the registry row and remove
  references to `ghcr.io/anchapin/openstudio_cli_image`. Add a
  cross-reference to `docs/openstudio-image-distribution.md`.
- `docs/openstudio-image-distribution.md` — **new**; the
  distribution-decision document the issue asked for, but
  describing adoption rather than build.

## Why this option over the alternatives

| Option | Implementation | Operational | Team | Future change |
|---|---|---|---|---|
| **Adopt upstream (this ADR)** | hours | near-zero | none new | one-line version bump |
| Build at CI from NREL installer (issue as written) | 1–2 weeks | high (CI minutes, 1 GB cache, registry storage, signing, version-matrix updates) | new maintainer surface | Dockerfile + workflow + sign + matrix on every NREL release |
| Mirror to `ghcr.io` only | hours (cron pull/re-tag/push) | medium (1 GB mirror × N versions) | none new | cron + retention policy |
| Have NREL publish to `ghcr.io` | coordination request, not code | — | — | — |
| Conda / Spack / tarball | out of PRD scope | — | — | — |

"Build at CI from installer" duplicates NREL's already-published
artifact and pays the cost forever for no user-visible benefit. The
mirror-only option is a defensible compromise if the PRD's literal
wording requires `ghcr.io`; it is much cheaper than a build, and a
one-day fallback if the maintainer rejects the "consume from
Docker Hub" interpretation of PRD §1.4.

## Risks

- **NREL deprecates a tag.** Mitigation: weekly availability check.
  Fallback: a one-day Dockerfile that builds from the NREL public
  installer (we keep the break-glass plan in
  `docs/openstudio-image-distribution.md` and *do not* write the
  Dockerfile now).
- **License terms change.** Mitigation: the docs file links to
  NREL's license page and the project depends on the same terms
  that would apply to a project-built image, so no new exposure.
- **PRD §1.4 literally requires `ghcr.io`.** If the maintainer
  enforces the strict reading, the mirror-only path is the
  fallback. Document the interpretation chosen in the ADR's
  supersession note.
- **NREL image lacks OSimFlow wrapper layers.** The current
  `work.py:run_openstudio_sim` invokes `openstudio.cli` directly
  inside the container, so no wrapper layer is needed. If the
  project later wants in-container `bin/*.py` wrappers, switch to
  a `FROM nrel/openstudio:X` multi-stage build — still simpler
  than building from the installer.

## Validation

1. `docker run --rm nrel/openstudio:3.10.0 openstudio --version` emits
   `3.10.0`.
2. `docker run --rm nrel/openstudio:3.10.0 openstudio run -w
   /tmp/empty.osw` exits 0 on a no-op workflow.
3. `osimflow run --executor local --openstudio_version 3.10.0
   --n_samples 1 ...` produces a non-empty `eplusout.sql` end to
   end.
4. The new `openstudio-image-availability` workflow passes for the
   pinned version matrix.
5. Existing `tests/integration/test_cache_invalidation.py` still
   passes after `CONTAINER_OS` is changed (the cache key now
   includes the upstream image digest).

## Acceptance criteria (replacing #9's)

- [x] `CONTAINER_OS` points at `docker.io/nrel/openstudio` (or the
      registry-neutral form), with the existing `{version}` shape
      preserved. — `osimflow/campaign.py:56`.
- [x] The stub workflow `.github/workflows/openstudio-cli-image.yml`
      is deleted.
- [x] The availability-check workflow
      `.github/workflows/openstudio-image-availability.yml` is in
      place and green.
- [x] `docs/openstudio-image-distribution.md` exists and is linked
      from `AGENTS.md` §11.
- [x] AGENTS.md §2 and §11 no longer reference
      `ghcr.io/anchapin/openstudio_cli_image`.
- [ ] The full test suite (`pytest`) and the existing
      `test_cache_invalidation` integration tests pass. — *see PR
      description for CI run.*
- [ ] One end-to-end local run with `--openstudio_version 3.10.0`
      produces the four required artifacts
      (`aggregated_results.csv`, `failed_simulations.csv`, KPI
      JSONs, plot files). — *follow-up issue, not gating for #9.*

## Out of scope

- Multi-arch (`amd64` + `arm64`) builds — NREL is `amd64`-only
  today; defer until NREL publishes arm64 or the project needs it.
- Building the `scientific_python_image` — unrelated.
- Migrating `nrel/openstudio-cli` (the legacy 2019 `nllong`-era
  image) — that repo is dead; do not consume from it.
