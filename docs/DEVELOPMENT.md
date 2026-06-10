# OSimFlow — Day-to-day development

> **Audience:** contributors with the dev environment already set up
> (see [`CONTRIBUTING.md`](CONTRIBUTING.md) §1 for the install steps).
> This file is the cheat sheet.

The `Makefile` is the canonical day-to-day interface. Every CI job has
a `make` equivalent.

---

## 1. One-time setup

```bash
# Create the project virtualenv and editable-install with all dev deps.
# `make install` is equivalent to `pip install -e ".[dev,aws,slurm]"`
# but it always uses the project venv (`.venv/`), which is what every
# other target (`make test`, `make lint`, `make typecheck`, ...) also
# uses. Without the venv, system `pytest` will resolve first on $PATH
# and fail with `ModuleNotFoundError: submitit / boto3 / types-PyYAML`.
make install

# Install pre-commit hooks (runs on every git commit)
.venv/bin/pre-commit install

# Optional: install `act` to mirror CI locally (https://github.com/nektos/act)
#   macOS:   brew install act
#   Linux:   curl -fsSL https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash
```

> **Why the venv matters.** The Makefile hard-codes every tool
> invocation (`.venv/bin/pytest`, `.venv/bin/mypy`, ...) so that
> there is exactly one supported way to run the project. If you skip
> `make install` and try to use a global `pytest` / `mypy` / `ruff`,
> those binaries will either be missing or point at a different
> Python that lacks the `[dev,aws,slurm]` extras. Stick to `make`
> targets or invoke the tools through `.venv/bin/` explicitly.

`pre-commit install` registers hooks for: ruff, black, mypy, gitleaks,
the AGENTS.md contract check, the docs sync check, and the unit +
contract test gate. **It will block `git commit` if anything is
broken.** This is the pre-push safety net; do not skip it.

---

## 2. Day-to-day commands

| Goal                          | Command                       |
| ----------------------------- | ----------------------------- |
| Lint (read-only)              | `make lint`                   |
| Auto-format (ruff)            | `make format`                 |
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

6. **Wait for CI.** The `ci` workflow is split into five parallel jobs
   (issue #76) so wall-clock time is dominated by the slowest single
   job, not the sum of all checks. The jobs are:
   - `lint` (ruff check + format check) — ~30s.
   - `typecheck` (mypy --strict on `osimflow/`) — ~60s.
   - `test` (pytest with 85% coverage gate, runs after lint + typecheck).
   - `contract` (AGENTS.md / docs drift check) — ~10s.
   - `security` (`pip-audit` against the dependency set) — ~30s.
   - `bench` (push to main + manual dispatch only; not a PR gate).
   A green check on every required job is the gate to merge.
   The `docs` workflow runs separately on docs-only path filters.
   Lint-only fast feedback (pre-`ci.yml` split) used to live in
   `.github/workflows/lint.yml`; it has been folded into the
   consolidated `lint` job in `ci.yml`.

7. **Address review comments** and re-push; the same CI jobs re-run.

---

## 4. Using `act` to mirror CI locally

If you have [nektos/act](https://github.com/nektos/act) installed, the
local CI mirror is one command:

```bash
make act
```

This runs the `lint`, `unit`, and `agents-contract` jobs against your
local checkout. The full `ci` matrix (Python 3.12) is more
expensive; run it with:

```bash
act -j ci
```

**Known limitations of `act` (relevant for OSimFlow):**

- Container-based jobs (`container.yml`) may not run on macOS due to
  Docker limitations.
- `act` runs the GitHub Actions runner in a container; if your local
  environment differs from `ubuntu-latest`, expect some flakiness.
- OSimFlow has no project-owned OpenStudio image build pipeline; the
  weekly `openstudio-image-availability` workflow checks the
  upstream `nrel/openstudio` tag set on Docker Hub. See
  [`docs/openstudio-image-distribution.md`](openstudio-image-distribution.md).

**Using Podman instead of Docker Desktop.** Docker Desktop requires a
paid license for organizations with 250+ employees. Podman is a free,
OCI-compatible drop-in replacement. See
[`docs/podman-guide.md`](podman-guide.md) for installation instructions
and platform-specific configuration.

---

## 5. Branch protection rules (recommended)

The `main` branch on `anchapin/OSimFlow` is protected. Maintainers
should configure the following in **Settings → Branches → Branch
protection rules → `main`** so a green CI is a hard gate to merge:

| Setting | Value | Why |
| --- | --- | --- |
| **Require a pull request before merging** | ✅ | No direct pushes to `main`; everything goes through a reviewed PR. |
| **Require approvals** | 1 | At least one other maintainer reviews the change. |
| **Dismiss stale pull request approvals** | ✅ when new commits are pushed | Forces re-review on force-push. |
| **Require status checks to pass before merging** | ✅ | Enforce CI as the merge gate. |
| **Required status checks** (search-and-pick the exact names) | `lint`, `typecheck`, `test`, `contract`, `security` | Every job in `.github/workflows/ci.yml` is required. |
| **Require linear history** | ✅ | Enforces rebase/merge, keeps `git log` clean. |
| **Do not allow bypassing the above settings** | ✅ | Even admins must follow the rules. |
| **Allow force pushes** | ❌ | Force pushes to `main` rewrite history for everyone. |
| **Allow deletions** | ❌ | The branch should never be deleted. |
| **Block creation of new branches matching `v*`** | ❌ (allow) | `release.yml` creates annotated tags; we don't block branches. |

Optional but recommended:

- **Require signed commits** — most contributors use SSH-signed
  commits; this catches a compromised local key quickly.
- **Include administrators** in the "do not bypass" rule so an
  emergency hot-push still has to be reviewed out-of-band.
- **Auto-merge after CI** via [Mergify](https://mergify.com/) or
  `gh pr merge --auto --squash` after an approval lands.

---

## 6. Troubleshooting

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

## 7. When you get stuck

- Search existing issues: <https://github.com/anchapin/OSimFlow/issues>
- Open a new issue with the `question` label.
- See [`CONTRIBUTING.md`](CONTRIBUTING.md) §6 for community channels.
