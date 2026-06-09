# Contributing to OSimFlow

> **Status:** Active. The day-to-day developer workflow lives in
> [`DEVELOPMENT.md`](DEVELOPMENT.md). This file is the contributor
> onboarding, governance entry point, and PR-review checklist.

OSimFlow welcomes contributions from OpenStudio users, energy modelers,
researchers, and the broader Python+building-energy community. This
document covers how to propose, develop, and review a change.

---

## 1. Development environment

Quick setup (5 min):

```bash
git clone https://github.com/anchapin/OSimFlow.git
cd OSimFlow
python -m pip install -e ".[dev,aws,slurm]"
pre-commit install
```

Detailed commands and the day-to-day workflow live in
[`DEVELOPMENT.md`](DEVELOPMENT.md). The TL;DR:

```bash
make lint       # ruff check
make format     # ruff format + black
make typecheck  # mypy --strict osimflow/
make test       # full pytest suite
make contract   # tools/check_agents_contract.py + tools/check_docs_sync.py
make precommit  # the pre-push safety net
```

---

## 2. Coding standards

Mirror the rules in [`../AGENTS.md`](../AGENTS.md) §6. Highlights:

- Python 3.11+; **type hints everywhere** on public functions.
- Use `pathlib.Path` over `os.path`. Use `logging`, not `print`.
- Catch, log with `exc_info=True`, **re-raise**. Never swallow.
- CLI entry points use `argparse` subcommands.
- For OpenStudio Python bindings, isolate all `import openstudio` calls
  behind a `try/except` with a clear error message.

The lint, format, and typecheck are enforced by CI. Local pre-commit
runs the same checks on every commit.

---

## 3. Branch & commit conventions

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

## 4. Pull request process

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

---

## 5. Adding a new OpenStudio CLI version

When a new OpenStudio CLI version is released, the project
(`openstudio_cli_image:<version>`) image needs to be added to the
build matrix. Today this is a manual process:

1. Update `.github/workflows/openstudio-cli-image.yml` to add the new
   version to the build matrix.
2. Add the version to the supported-versions table in
   [`docs/OSimFlow.md`](OSimFlow.md).
3. Smoke-test the new image with `osimflow run --executor local
   --openstudio_version <new>` against a known-good template package.

The container-build workflow is currently a stub; see the comment at
the top of that file.

---

## 6. Community channels

- **GitHub Issues** — bugs, feature requests, design proposals.
- **GitHub Discussions** — questions, ideas, show-and-tell.
- **Monthly call** — *to be scheduled*. TBD per
  [`GOVERNANCE.md`](GOVERNANCE.md).

---

## 7. License

By contributing, you agree that your contributions will be licensed
under the [MIT License](../LICENSE).
