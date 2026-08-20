# OpenStudio Compatibility Matrix

> **Audience:** OSimFlow users and contributors who need to know which
> OpenStudio versions are tested and supported.

OSimFlow consumes the [`nrel/openstudio`](https://hub.docker.com/r/nrel/openstudio/tags)
image directly from Docker Hub. No project-owned build is required.

## Compatibility Matrix

This matrix reflects the **currently-released** OSimFlow line and the
OpenStudio versions it has been tested against. Future OSimFlow lines
(0.2.x, 0.3.x) are listed in the [PRD](OSimFlow.md) once they
exist — speculative rows have been removed from this table.

| OSimFlow | OpenStudio | Status | Notes |
|----------|------------|--------|-------|
| 0.1.x    | 3.7.0      | Supported | Ubuntu 20.04 base; floor version |
| 0.1.x    | 3.7.0-2204 | Supported | Ubuntu 22.04 base |
| 0.1.x    | 3.8.0      | Supported | |
| 0.1.x    | 3.9.0      | Supported | |
| 0.1.x    | 3.10.0     | Supported | |
| 0.1.x    | 3.11.0     | Supported | latest stable |

NREL also publishes older tags on Docker Hub (down to `3.1.0`); OSimFlow
does not test or advertise them. Pinning to a non-listed version will
fall through to the default container path but is unsupported per the
[Version Support Policy](#version-support-policy) below.

## Status Definitions

- **Supported**: Full test coverage in CI. All core workflows verified.
- **Beta**: Community-reported as working. May lack full CI coverage.
- **Deprecated**: Security fixes only. Migration to a newer version recommended.
- **EOL**: No longer maintained. Not recommended for new campaigns.

## Selecting an OpenStudio Version

Pass `--openstudio_version` on the CLI:

```bash
osimflow run \
  --executor local \
  --openstudio_version 3.11.0 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results
```

The selected version is embedded in the simulation step cache key. Changing
`--openstudio_version` invalidates only the `RUN_OPENSTUDIO_SIM` step cache.

## Adding a New OpenStudio Version

1. Open a PR against the availability check workflow:
   `.github/workflows/openstudio-image-availability.yml`
2. Add the new version to the `SUPPORTED_OPENSTUDIO_VERSIONS` list.
3. Update this matrix with the new row.
4. No code change is required beyond those two files.

## Version Support Policy

- OSimFlow maintains support for the **two most recent stable OpenStudio releases**.
- The `nrel/openstudio` image availability is validated weekly via
  `.github/workflows/openstudio-image-availability.yml`.
- When NREL publishes a new stable version, a maintainer should update
  this matrix within 14 days.

## References

- [`docs/openstudio-image-distribution.md`](openstudio-image-distribution.md) — upstream image provenance
- [NREL OpenStudio on Docker Hub](https://hub.docker.com/r/nrel/openstudio/tags)
- [ADR-0002: Adopt `nrel/openstudio` upstream](../.agents/results/architecture/0002-adopt-nrel-upstream-image.md)