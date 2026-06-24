# Release Process

> **Audience:** OSimFlow maintainers and contributors who cut releases.

## Version Numbering

OSimFlow follows [Semantic Versioning 2.0.0](https://semver.org/):

```
MAJOR.MINOR.PATCH[-prerelease]
```

| Component | When to bump | Example |
|-----------|-------------|---------|
| `MAJOR` | Breaking changes to the public API or CLI interface | `0.1.0 → 1.0.0` |
| `MINOR` | New features, backward-compatible | `0.1.0 → 0.2.0` |
| `PATCH` | Bug fixes, no API change | `0.1.0 → 0.1.1` |
| `prerelease` | Alpha/beta/rc, e.g. `0.1.0-alpha.1` | |

The current version is defined in `pyproject.toml` under `[project].version`.
Development snapshots carry the `-dev` suffix (e.g., `0.1.0-dev`).

## Release Cadence

- **Feature releases** (MINOR bump): every 6–8 weeks.
- **Patch releases**: as needed for critical bug fixes, at maintainer discretion.
- **Major releases**: when a breaking change is warranted, no fixed schedule.

## Release Checklist

1. **Changelog review**: Ensure `CHANGELOG.md` is up to date with all changes
   since the last release. Categorize entries per [Conventional Commits](#conventional-commits).
2. **Version bump**: Update `pyproject.toml` `[project].version` (remove `-dev`
   suffix for stable releases, add `-rc.N` for pre-releases).
3. **CI green**: All required CI checks must pass on `main` before tagging.
4. **Tag**: Annotated tag with `git tag -a v<version> -m "Release <version>"`.
5. **Push the tag**: `git push origin v<version>` (pushing `main` is not
   required — the `release.yml` workflow triggers on the `v*` tag push alone).
6. **GitHub Release**: created **automatically** by the `release.yml` workflow
   via `softprops/action-gh-release@v3` with `generate_release_notes: true`.
   The maintainer does **not** create the release manually; just monitor
   https://github.com/anchapin/OSimFlow/actions/workflows/release.yml and
   verify the release appears at https://github.com/anchapin/OSimFlow/releases
   with wheel + sdist + `.sigstore` bundle + CycloneDX SBOM attached.
7. **Announce**: Post a brief note in the project's discussion forum / mailing list.

### CI-Produced Release Artifacts (issue #954)

The `release` workflow (`.github/workflows/release.yml`) runs automatically on
every `v*` tag push and produces the following artifacts. **All of these must
appear on the GitHub Release** (the workflow attaches them automatically — do
not delete any when editing the release):

| Artifact | Purpose | Mandatory |
|----------|---------|:---------:|
| `osimflow-<ver>.whl` + `.tar.gz` | Built wheel + sdist (also published to PyPI) | ✅ |
| `*.sigstore` | Sigstore keyless signature bundle for each signed artifact | ✅ |
| `osimflow-<ver>.cdx.json` | **CycloneDX 1.5 Software Bill of Materials** for the wheel | ✅ |

The CycloneDX SBOM is generated with `cyclonedx-py environment` from the freshly
built wheel **before** Sigstore signing, so the SBOM itself is signed. Downstream
build-energy teams that pin `osimflow` in reproducible campaigns can use the SBOM
to audit the transitive dependency surface (PRD §6 #5–6, supply-chain hygiene).

## Changelog Process

The `CHANGELOG.md` is a **running document** — it is updated in every PR.
Do not wait until release time to write changelog entries.

### Conventional Commits

Commit messages must follow the [Conventional Commits](https://www.conventionalcommits.org/)
format so changelog entries can be auto-generated:

```
<type>(<scope>): <description>

[optional body]

[optional footer(s)]
```

**Types:**

| Type | Changelog section |
|------|-------------------|
| `feat` | Features |
| `fix` | Bug Fixes |
| `docs` | Documentation |
| `refactor` | (not in changelog) |
| `test` | (not in changelog) |
| `chore` | (not in changelog) |
| `perf` | Performance |
| `ci` | CI/CD |
| `BREAKING CHANGE` | Breaking Changes |

**Example:**

```
feat(cli): add --openstudio_version flag for image selection

The flag is forwarded to the executor container argument and included
in the simulation step cache key.

Closes #260
```

When merging, the squash commit message becomes the canonical changelog entry.

### Changelog Structure

```markdown
# Changelog

All notable changes to this project will be documented in this file.

## [0.3.0] — YYYY-MM-DD

### Features
- ...

### Bug Fixes
- ...

### Documentation
- ...

### Performance
- ...

### Breaking Changes
- ...

## [0.2.0] — YYYY-MM-DD
...
```

## Deprecation Policy

- Deprecated features remain functional for at least **two MINOR releases**.
- The deprecation must be announced in the changelog entry when introduced.
- Deprecated CLI flags or behaviors emit a warning (not an error) at runtime.

## Long-Term Support (LTS)

OSimFlow does **not** currently designate LTS versions. This policy will be
re-evaluated when the project reaches version `1.0.0`.

When LTS is adopted:
- LTS branches receive bug-fix patches for **12 months**.
- Non-LTS versions receive only critical security patches.
- At least one LTS version is always maintained.

## Release Notes Template

When drafting a GitHub Release, use this structure:

```markdown
## What's Changed

<!-- auto-generated from conventional commits between tags -->

## New Contributors

<!-- list of first-time contributors in this release -->

## Full Changelog

https://github.com/anchapin/OSimFlow/compare/v0.2.0...v0.3.0
```

## References

- [Semantic Versioning 2.0.0](https://semver.org/)
- [Conventional Commits](https://www.conventionalcommits.org/)
- [Keep a Changelog](https://keepachangelog.com/)