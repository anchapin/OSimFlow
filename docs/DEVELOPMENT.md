# OSimFlow — Day-to-day development

> **Audience:** contributors with the dev environment already set up
> (see [`CONTRIBUTING.md`](CONTRIBUTING.md) §1 for the install steps).
> This file is the cheat sheet.

The `Makefile` is the canonical day-to-day interface. Every CI job has
a `make` equivalent.

---

## 1. One-time setup

```bash
# Editable install with all dev deps
python -m pip install -e ".[dev,aws,slurm]"

# Install pre-commit hooks (runs on every git commit)
pre-commit install

# Optional: install `act` to mirror CI locally (https://github.com/nektos/act)
#   macOS:   brew install act
#   Linux:   curl -fsSL https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash
```

`pre-commit install` registers hooks for: ruff, black, mypy, gitleaks,
the AGENTS.md contract check, the docs sync check, and the unit +
contract test gate. **It will block `git commit` if anything is
broken.** This is the pre-push safety net; do not skip it.

---

## 2. Day-to-day commands

| Goal                          | Command                       |
| ----------------------------- | ----------------------------- |
| Lint (read-only)              | `make lint`                   |
| Auto-format                   | `make format`                 |
| Type-check (mypy strict)      | `make typecheck`              |
| Run full test suite           | `make test`                   |
| Run tests + coverage gate     | `make test-cov`               |
| Run fast tests only           | `make test-fast`              |
| AGENTS.md contract check      | `make agents-contract`        |
| docs/ sync check              | `make docs-sync`              |
| Both contract checks          | `make contract`               |
| pre-commit run on all files   | `make precommit`              |
| Local CI mirror (via `act`)   | `make act`                    |
| Clean caches                  | `make clean`                  |

`make help` lists every target with its description.

---

## 3. Workflow for a typical PR

1. **Branch off `main`** using one of the prefixes from
   [`CONTRIBUTING.md`](CONTRIBUTING.md) §3:

   ```bash
   git checkout main
   git pull
   git checkout -b feat/42-thing
   ```

2. **Write tests first** (TDD, vertical slice). One test → one impl
   step → repeat. The project's testing style is documented in
   [`../AGENTS.md`](../AGENTS.md) §5.

3. **Iterate** with the fast loop:

   ```bash
   make test-fast   # unit + contract, ~10s
   ```

4. **Before committing**, run the full local CI mirror:

   ```bash
   make precommit   # or `pre-commit run --all-files`
   ```

5. **Push and open the PR**:

   ```bash
   git push -u origin feat/42-thing
   gh pr create --fill
   ```

6. **Wait for CI.** The four jobs are:
   - `lint` (PR-only, ~30s) — fast style feedback.
   - `ci` (push to main + PR, ~3-5 min) — full lint + typecheck + tests.
   - `agents-contract` (PR-only) — AGENTS.md / docs drift check.
   - `docs` (PR + main, when `mkdocs.yml` lands) — docs build.

7. **Address review comments** and re-push; the same CI jobs re-run.

---

## 4. Using `act` to mirror CI locally

If you have [nektos/act](https://github.com/nektos/act) installed, the
local CI mirror is one command:

```bash
make act
```

This runs the `lint`, `unit`, and `agents-contract` jobs against your
local checkout. The full `ci` matrix (Python 3.11 + 3.12) is more
expensive; run it with:

```bash
act -j ci
```

**Known limitations of `act` (relevant for OSimFlow):**

- Container-based jobs (`container.yml`) may not run on macOS due to
  Docker limitations.
- `act` runs the GitHub Actions runner in a container; if your local
  environment differs from `ubuntu-latest`, expect some flakiness.
- The OpenStudio CLI image build job is **disabled by default** (see
  the comment at the top of `.github/workflows/openstudio-cli-image.yml`).

---

## 5. Troubleshooting

### pre-commit is slow on the first run

`pre-commit` downloads the hook repositories (ruff, black, gitleaks) on
the first invocation. Subsequent runs use the cache. If a hook is stuck
or broken, clear it with:

```bash
pre-commit clean
pre-commit install
```

### mypy complains about a third-party stub

The `pyproject.toml` `[tool.mypy]` block has `ignore_missing_imports = true`
as a safety net, but if you import a new library, add the corresponding
type stubs to `pyproject.toml` under `dev`:

```toml
dev = [
    ...
    "types-PyYAML",     # already added
    # "types-requests", # add when needed
]
```

### Coverage gate fails

The gate is `--cov-fail-under=85` on the `osimflow/` package. To see
which lines are uncovered:

```bash
make test-cov
```

The line-coverage table at the bottom shows the missing lines per file.
Add unit tests for the missing public-API paths; the gate will clear.

### AGENTS.md contract fails

The contract check reports a list of missing symbols/scripts/steps/
flags. Update `AGENTS.md` §3 (Directory map) and §4 (Build & run) to
add the missing entries. Re-run `make contract` to verify.

### docs sync check fails

The check reports a list of stale `bin/*.py` / path references. Either
update the docs to match the current code, or rename the missing
files. To opt a specific docs file out of the check, add
`<!-- docs-skip -->` HTML comment somewhere in the file.

---

## 6. When you get stuck

- Search existing issues: <https://github.com/anchapin/OSimFlow/issues>
- Open a new issue with the `question` label.
- See [`CONTRIBUTING.md`](CONTRIBUTING.md) §6 for community channels.
