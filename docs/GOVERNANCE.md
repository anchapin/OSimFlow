# OSimFlow Governance

This document defines how the OSimFlow project is governed: who makes
decisions, how they are made, and how the community participates.

---

## 1. Vision and mission

**Vision.** A world where every building-energy modeler can run
large-scale parametric simulation campaigns without writing bespoke
orchestration glue.

**Mission.** OSimFlow wraps the OpenStudio CLI to provide a
reproducible, extensible framework for parametric building-energy
studies — from a handful of local runs to thousands of cloud or HPC
jobs — with transparent caching, monitoring, and resume semantics.

---

## 2. Maintainer roles

| Role | Responsibilities |
|---|---|
| **Maintainer** | Merge PRs, triage issues, cut releases, enforce the code of conduct, update AGENTS.md and docs. |
| **CI Steward** | Maintain `.github/workflows/`, the `Makefile`, and the pre-commit config. Ensure CI mirrors local checks. |
| **Release Manager** | Bump `pyproject.toml` version, tag the release, publish to PyPI (post-MVP), write the changelog. |
| **Docs Lead** | Keep `docs/` in sync with the codebase. Own the contract checks (`tools/check_docs_sync.py`). |
| **OpenStudio Liaison** | Track upstream `nrel/openstudio` releases, update the supported-versions table, test new versions. |

In the current phase (pre-MVP), all roles are held by the initial
maintainer. As the contributor base grows, roles will be delegated.

### Decision authority

| Scope | Who decides |
|---|---|
| Bug fixes, docs, test additions | Any maintainer (single approval). |
| New features, new executors, new DAG steps | Maintainer consensus (lazy consensus, see §3). |
| Breaking API changes, dropping an OpenStudio version | RFC process (see §3). |

---

## 3. Decision-making process

### Lazy consensus (routine changes)

For most PRs — bug fixes, documentation, internal refactors, new tests
— a single maintainer approval is sufficient. If no maintainer objects
within 3 working days, the PR can proceed.

### RFC process (major changes)

For large or potentially controversial changes, use the RFC workflow:

1. **Open an issue** with the `rfc` label describing:
   - The problem or opportunity.
   - The proposed solution.
   - Alternatives considered.
   - Impact on the public API, executors, and DAG.
2. **Discuss.** Maintainers and community members comment on the issue.
   Allow at least 7 days for feedback.
3. **Decide.** A maintainer summarises the outcome. For truly
   contentious changes, a supermajority of maintainers (2/3) must
   agree.
4. **Implement.** One or more contributors implement the accepted
   proposal.

Examples of changes that require an RFC:

- Adding a new executor (e.g., Google Cloud Batch).
- Adding or removing a DAG step.
- Changing the cache key schema in a backward-incompatible way.
- Dropping support for an OpenStudio major version.

### Conflict resolution

If consensus cannot be reached:

1. The maintainers discuss in a dedicated issue or call.
2. If still unresolved, the initial project maintainer (repository
   owner) makes the final decision and documents the reasoning in the
   issue.

---

## 4. Code review requirements

All changes go through pull request review:

- **One approval** is required for most PRs.
- **Two approvals** are required for changes to the executor layer
  (`osimflow/executors/`) or the public API (`osimflow/__init__.py`).
- All CI checks must be green: lint, typecheck, test (85% coverage
  gate), contract, and security audit.
- The author is responsible for rebasing onto `main` and resolving
  merge conflicts.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) §5 for the full PR checklist.

---

## 5. Release process

OSimFlow follows [Semantic Versioning](https://semver.org/):

- **Major (X.0.0):** Breaking API changes.
- **Minor (0.X.0):** New features, new executors, new DAG steps.
  Backward-compatible.
- **Patch (0.0.X):** Bug fixes, docs updates, dependency bumps.

### Release cadence

There is no fixed cadence during the pre-MVP phase. The first
official release will be **v0.1.0**, targeted per PRD §5.2.

Post-MVP, the project aims for a minor release every 4–6 weeks.

### Release checklist

1. Ensure `main` is green on CI.
2. Update the version in `pyproject.toml`.
3. Update `CHANGELOG.md` (to be created post-MVP).
4. Commit: `chore(release): bump version to X.Y.Z`.
5. Tag: `git tag vX.Y.Z && git push --tags`.
6. Publish to PyPI (post-MVP).
7. Create a GitHub Release with the changelog entry.

---

## 6. Community guidelines

OSimFlow is committed to providing a welcoming, inclusive, and
harassment-free experience for everyone.

### Code of Conduct

The project adopts the [Contributor Covenant Code of Conduct
v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
A formal `CODE_OF_CONDUCT.md` file will be added to the repository
root as the community grows. In the meantime, all participants are
expected to:

- Be respectful and constructive.
- Focus on what is best for the community.
- Show empathy toward other community members.

### Enforcement

Reports of conduct violations should be directed to the repository
maintainer via a private GitHub message or email. All reports will be
treated confidentially.

---

## 7. Becoming a maintainer

OSimFlow uses a contribution ladder:

1. **Contributor** — anyone who opens a PR or issue.
2. **Recurring contributor** — someone with 3+ merged PRs who
   participates in code review and discussion.
3. **Maintainer** — a recurring contributor invited by the existing
   maintainers to join the team.

### How to become a maintainer

There is no formal election process during the pre-MVP phase. The
criteria are:

- A track record of high-quality contributions (code, docs, or review).
- Demonstrated understanding of the project architecture (see
  [`../AGENTS.md`](../AGENTS.md)).
- Willingness to perform maintainer duties: triage issues, review PRs,
  and keep docs in sync.

If you are interested, express your interest in a GitHub Discussion or
to an existing maintainer. The maintainers will discuss and extend an
invitation by lazy consensus.

---

## 8. Open questions

The following items are deferred to future community discussion:

- **Monthly community call** — schedule and format to be determined.
- **Formal Code of Conduct file** — to be added as the community grows.
- **PyPI publishing automation** — to be set up as part of the v0.1.0
  release.
- **Governance migration** — if the project joins a foundation (e.g.,
  OpenBuildingSoftware), this document will be updated accordingly.
