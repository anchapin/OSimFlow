# Contributing to OSimFlow

> **Status:** Active. The day-to-day developer workflow lives in
> [`DEVELOPMENT.md`](DEVELOPMENT.md). This file is the contributor
> onboarding, governance entry point, and PR-review checklist.

OSimFlow welcomes contributions from OpenStudio users, energy modelers,
researchers, and the broader Python+building-energy community. Whether
you are fixing a typo, adding a new executor, or proposing a new
simulation workflow, we are glad to have you.

**Project philosophy.** OSimFlow is a community-driven framework. We
prioritise reproducibility, transparent orchestration, and a clean
separation between the campaign driver and the per-sample work. Every
change should make it easier for the next person to run a parametric
study without writing bespoke glue code.

---

## 1. Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.12+ | The package uses 3.12-only syntax. |
| git | 2.x | For version control and worktree-based workflows. |
| Docker | optional | Needed only if you want to test against the real `openstudio.cli` inside the `nrel/openstudio` container. Most development uses the built-in stub mode. |
| make | GNU or BSD | The `Makefile` is the canonical developer entry point. |

---

## 2. Development environment

Quick setup (5 min):

```bash
# Fork the repo on GitHub, then:
git clone https://github.com/<your-username>/OSimFlow.git
cd OSimFlow
python -m pip install -e ".[dev,aws,slurm]"
pre-commit install
```

For the minimal install (local executor only, no Slurm/Boto3):

```bash
pip install -e .
```

For optional MLflow integration:

```bash
pip install -e ".[mlflow]"
```

Detailed commands and the day-to-day workflow live in
[`DEVELOPMENT.md`](DEVELOPMENT.md). The TL;DR:

```bash
make help       # list all targets
make install    # pip install -e ".[dev,aws,slurm]"
make lint       # ruff check
make format     # ruff format
make typecheck  # mypy --strict osimflow/
make test       # full pytest suite
make test-cov   # pytest with 85% coverage gate
make test-fast  # contract + unit only (pre-commit mirror)
make contract   # tools/check_agents_contract.py + tools/check_docs_sync.py
make precommit  # the pre-push safety net
```

---

## 3. Coding standards

Mirror the rules in [`../AGENTS.md`](../AGENTS.md) §6. Highlights:

- Python 3.12+; **type hints everywhere** on public functions.
- Use `pathlib.Path` over `os.path`. Use `logging`, not `print`.
- Catch, log with `exc_info=True`, **re-raise**. Never swallow.
- CLI entry points use `argparse` subcommands.
- For OpenStudio Python bindings, isolate all `import openstudio` calls
  behind a `try/except` with a clear error message.

The lint, format, and typecheck are enforced by CI. Local pre-commit
runs the same checks on every commit.

---

## 4. Branch & commit conventions

### Branch naming

`<type>/<short-description>-<issue-number>`

| Type     | Used for                                    |
| -------- | ------------------------------------------- |
| `feat`   | New user-facing capability.                 |
| `fix`    | Bug fix.                                    |
| `chore`  | Tooling, infra, docs, deps.                 |
| `refactor` | Internal cleanup, no behavior change.     |
| `test`   | Test-only changes.                          |

Examples: `feat/15-developer-best-practices`, `fix/cache-stale-output`,
`chore/upgrade-submitit`.

### Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

<body — what and why, not how>

<footer — Closes #issue, BREAKING CHANGE: ...>
```

Examples:

```
chore(ci): adopt ruff + black + mypy strict (#15)

Adds automated lint, type-check, coverage gate, and AGENTS.md
contract enforcement. Closes #15.
```

```
fix(cache): invalidate on stale output (#42)

The cache.lookup() now returns None when the output file has
been deleted out from under the cache row, instead of returning
a path that points at a non-existent file. The next call will
re-run the step.
```

---

## 5. Pull request process

### Before opening

- [ ] All checks green locally: `make precommit` (or `act` for the CI
      mirror if you have it installed).
- [ ] Full test suite green: `make test`.
- [ ] Coverage gate (85%) still passes: `make test-cov`.
- [ ] If you added a new public symbol to `osimflow/__init__.py`, a new
      `bin/*.py` script, a new file in `osimflow/executors/`, a new
      campaign step, or a new CLI flag, **AGENTS.md is updated in the
      same commit** (the contract check enforces this in CI).
- [ ] If you renamed, removed, or otherwise invalidated a docs
      reference, **the docs are updated in the same commit** (the docs
      sync check enforces this in CI).

### PR template

Open the PR with:

1. **Summary** — 1-3 sentences.
2. **Issue** — `Closes #N` (one issue per PR; split if needed).
3. **Test plan** — what you ran, what you observed.
4. **Risk** — any behavior changes, any backward-incompatible surface.
5. **Checklist** — paste the "Before opening" list above ticked.

If a docs change is intentionally deferred, write `// docs: skip —
<reason>` in the PR body so reviewers can confirm.

### Review

- A maintainer will review within ~3 working days. If you don't hear
  back, ping the issue thread.
- All checks must be green before merge.
- One approval is sufficient for the MVP; two for changes to the
  executor layer (`osimflow/executors/`) or the public API
  (`osimflow/__init__.py`).

### Merge

- Squash-merge by default; preserve the conventional-commit prefix in
  the squashed subject.
- The squash body should reference the issue: `Closes #N`.

### When `--admin` merge is appropriate

`gh pr merge --admin` (or `gh pr merge --admin --squash`) bypasses
branch protection's required-status-checks gate. It is **not** a
substitute for a green build — it is an escape hatch for documented
edge cases only.

Use `--admin` only when **all** of the following are true:

1. **Local verification is fully green** — `make lint`, `make
   typecheck`, and `.venv/bin/pytest` (the full fast suite, no
   coverage gate) all pass on the candidate commit.
2. **The failing CI check is one of the explicitly non-gating
   jobs** — the `slow (@pytest.mark.slow)` job is *intentionally* a
   non-gating diagnostic (see `.github/workflows/ci.yml` lines
   299-308), and the AWS Batch / Nomad / Azure / GCP / Kubernetes /
   Slurm / Docker-Swarm / Dask / OpenStudio-CLI / real-MLflow E2E
   jobs are skip-gated outside their respective `*-e2e.yml`
   workflows. A flake in any of these is not a blocking gate.
3. **The CI queue has been stuck for ≥10 minutes** without the
   failing check reporting back. Branch protection's required
   checks have a built-in timeout; an admin-merge before that
   timeout is *not* appropriate.
4. **The PR is a known fix for the failing check** (e.g. you are
   re-opening a PR that previously failed on the same check) **or**
   the failing check is a heredoc flake (CI runner contention, an
   intermittent external service, a known race that the test author
   has accepted as a flake).

Do **not** use `--admin` to:

- Bypass a real `test (pytest, 83% coverage gate)` failure — that
  check is the primary gate. If it's red, the PR is not mergeable.
- Bypass a `lint`, `typecheck`, or `agents & docs contract` failure.
  These are deterministic and must be green.
- Skip past a queue that has been running for <10 minutes. The CI
  runner may just be slow; let it finish.

When you do use `--admin`, leave a comment on the PR linking the
issue that tracks the underlying flake (e.g. `#1047` for the
`@pytest.mark.slow` flake pattern) so the reason is auditable.

---

## 6. Adding a new OpenStudio CLI version

When a new OpenStudio CLI version is released upstream on Docker Hub
(`nrel/openstudio`), advertise it in OSimFlow. This is a docs +
workflow-matrix change, not a build:

1. Confirm the new tag exists: `docker manifest inspect
   docker.io/nrel/openstudio:<new_version>` returns a digest.
2. Add the new version to the matrix in
   `.github/workflows/openstudio-image-availability.yml`.
3. Add the new version to the supported-versions table in
   [`docs/openstudio-image-distribution.md`](openstudio-image-distribution.md).
4. Smoke-test it with `osimflow run --executor local
   --openstudio_version <new_version> --n_samples 1` against a
   known-good template package; confirm `eplusout.sql` is non-empty.

No image build is required: OSimFlow consumes the upstream
`nrel/openstudio` image directly (see ADR-0002 and
[`docs/openstudio-image-distribution.md`](openstudio-image-distribution.md)).

---

## 7. Reporting issues

Found a bug? Have a question?

1. **Search existing issues** to avoid duplicates.
2. **Open a new issue** at <https://github.com/anchapin/OSimFlow/issues/new>.
3. Use the appropriate template:
   - **Bug report** — include OS, Python version, `osimflow --version`,
     a minimal repro, and the full traceback.
   - **Question / support** — describe what you are trying to do, what
     you expected, and what happened instead.

Good bug reports include the exact CLI invocation, the contents of
`run.json` (if a campaign ran partially), and the relevant
`stdout.log` / `stderr.log` from the failed sample.

---

## 8. Proposing features

Feature ideas are welcome. To propose one:

1. **Open a Discussion** on GitHub (preferred for early-stage ideas) or
   an issue with the `enhancement` label.
2. Describe the **use case** — what building-energy workflow does this
   enable?
3. Outline the **proposed interface** — new CLI flag? New `bin/*.py`
   script? New executor class? See [`../AGENTS.md`](../AGENTS.md) §9
   for the task-routing table.
4. A maintainer will respond within ~5 working days to discuss scope
   and next steps.

For large changes (new executor, new DAG step, breaking API change),
the maintainer may request an RFC-style write-up. See
[`GOVERNANCE.md`](GOVERNANCE.md) for the decision-making process.

---

## 9. Community channels

- **GitHub Issues** — bugs, feature requests, design proposals.
- **GitHub Discussions** — questions, ideas, show-and-tell.
- **Monthly call** — *to be scheduled*. TBD per
  [`GOVERNANCE.md`](GOVERNANCE.md).

---

## 10. AI-Assisted Development

OSimFlow ships configuration files that popular AI coding assistants
auto-discover. These files are intentionally short — they point to
`AGENTS.md` as the canonical source rather than duplicating rules.

### Config files

| File | Auto-discovered by | Purpose |
|---|---|---|
| `AGENTS.md` | All (canonical) | Full project conventions, architecture, task routing |
| `.cursorrules` | Cursor | Points to AGENTS.md; 10-line rule summary |
| `CLAUDE.md` | Claude Code | Points to AGENTS.md; project type + key docs |
| `.github/copilot-instructions.md` | GitHub Copilot | Points to AGENTS.md; paths + commands |
| `.clinerules` | Cline | Points to AGENTS.md; rule summary + commands |

### Keeping them in sync

All four config files are *pointers* — they reference `AGENTS.md` and
do not duplicate substantive rules. When `AGENTS.md` changes (e.g. a new
executor, a new CLI flag), the pointer files rarely need updating. If
you add a new category of rule that should be surfaced in the summaries,
update the relevant file and keep it under 30 lines.

The AGENTS.md contract check (`make contract`) does **not** validate the
pointer files — it validates that `AGENTS.md` itself stays in sync with
the codebase. The pointer files are documentation, not configuration.

---

## 11. License

By contributing, you agree that your contributions will be licensed
under the [MIT License](../LICENSE).
