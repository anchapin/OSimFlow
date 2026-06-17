# Plan: Cross-platform CLI installer builds for OSimFlow

## Charter preflight (required by developer policy)

```
CHARTER_CHECK:
- Clarification level: LOW (research-driven; user decisions captured below)
- Task domain: tf-infra / distribution / dev-experience
- Must NOT do:
  1. Modify .agents/ files
  2. Hardcode secrets, signing keys, or PII in any .tf / .spec / .yml
  3. Run destructive operations (no `terraform apply` against a real AWS account, no `gh release delete`)
- Success criteria:
  - pyproject.toml produces a working wheel (the load-bearing prerequisite)
  - bin/*.py relocates into the package (or ships as package_data)
  - PyInstaller onedir builds on all 3 OSes from a single .spec
  - .github/workflows/release-installers.yml chains off release.yml via workflow_run
  - Smoke test passes on every built binary
  - A v0.1.0-rc.1 tag produces downloadable .exe/.dmg/.deb/.rpm/.tar.gz on the GitHub Release
  - docs/installation.md published, README updated
- Assumptions applied (user-confirmed):
  - PyPI Trusted Publishing + Sigstore keyless (existing release.yml model) is preserved
  - Code signing is deferred to 0.3.0 (OSSign) — alpha is shipped unsigned
  - **HYBRID:** `--onedir` for Linux/macOS (wrapped in `.deb`/`.dmg`/`.AppImage`) + `--onefile` for Windows (single `.exe` in zip; user double-clicks; avoids the "binary in folder inside zip inside .dmg" UX)
  - **Size target: 180-220 MB unpacked** (down from baseline ~280 MB) via 5 techniques: `--strip`, exclude `*.tests`, exclude PySide6/PyQt5/PyQt6, exclude matplotlib `sphinxext`/`style`/`tests`, exclude unused scipy submodules
  - Scientific stack (numpy/scipy/pandas/matplotlib/pyarrow/seaborn) IS bundled in the installer
  - Optional extras (boto3, submitit, mlflow, SALib, pymoo) are NOT bundled; user runs `pip install osimflow[aws]` etc. post-install
  - **`getattr(sys, "frozen", False)`** for the work-layer frozen-binary detection (no env-var convention; stdlib attribute set by all freezers)
  - Tree-shaking is NOT available; Nuitka/AOT/Cython do NOT reduce size (scientific stack is compiled C, ships unchanged)
```

---

## 1. Executive summary

**Recommend: PyInstaller `--onedir` as the primary build tool, with cx_Freeze as a documented fallback.**

The single biggest blocker for "ship a CLI installer" is **not** the build tool — it is two pre-existing packaging bugs that already break `pip install osimflow` today:

1. `pyproject.toml:79` — `[tool.setuptools].packages = ["osimflow", "osimflow.executors"]` is missing `osimflow.algorithms`, `osimflow.exporters`, `osimflow.importers` (subpackages) and `osimflow.observability`, `osimflow.pareto` (top-level modules). Verified at `/home/alex/Projects/OSimFlow/pyproject.toml:79`.
2. `osimflow/work.py:39-40` and `osimflow/campaign.py:176-177` — `BIN = PROJECT_ROOT / "bin"` is a hard-coded repo-root path. Verified by grep. A wheel install has no way to find the 5 work scripts.

Both bugs are fixed in the same issue, then PyInstaller (with `--exclude-module` on the lazy-imported optional extras) ships a single-artifact installer matrix.

---

## 2. Build tool comparison (TL;DR)

| Tool | Verdict | Why |
|---|---|---|
| **PyInstaller (--onedir)** | **PRIMARY** | First-class hooks for numpy/scipy/pandas/matplotlib/pyarrow. GPL bootloader + runtime exception (commercially usable). `--codesign-identity` built-in for macOS. Largest community (12.8k★, 4.7M PyPI dl/mo). 2026-current (v6.20.0). |
| Nuitka | Alternative primary | ~3.8 MB hello-world, 2-4× runtime speedup (irrelevant for I/O-bound 5min-4h campaigns), 5-10× slower build. Keep as rebuild path if cold-start becomes a complaint. |
| cx_Freeze | Fallback | Only mainstream tool that natively produces `.msi/.dmg/.deb/.rpm/.AppImage` from one `setup.py`. Smaller community. |
| PyOxidizer | NO | "Development is effectively stagnant" per Pantsbuild docs / indygreg. Issue #751 acknowledges maintenance gap. |
| shiv / pex | NO | Both produce zipapps that require Python on target — defeats the user's "no Python on my machine" goal. |
| briefcase | NO | Toga-centric GUI framework. CLI support exists but high friction. |
| Containers (Docker) | Out of scope | Cloud path already exists (`ghcr.io/anchapin/scientific_python_image`). Different distribution problem. |

**Source verification (2025-2026):** PyInstaller 6.20.0, cx_Freeze 8.6.4 (active), Nuitka 2.x, PyOxidizer stagnant — all cited in the design agent's full report (`.agents/results/plan-cross-platform-installers.md` §3 if needed; sources: pyinstaller.org, cx-freeze.readthedocs.io, nuitka.net, github.com/indygreg/PyOxidizer/issues/751, pantsbuild.org/dev/docs/python/integrations/pyoxidizer).

---

## 3. Artifact matrix (the shipping shape)

**HYBRID (user-confirmed):** `--onedir` for Linux/macOS (where `.deb`/`.dmg`/`.AppImage` give single-artifact UX for free) + `--onefile` for Windows (single `.exe`, double-click, no folder-ceremony). Both modes share the same `Analysis()` block and `excludes` list.

| OS | Artifact | Build tool |
|---|---|---|
| Windows | `osimflow-<version>-windows-x86_64.exe` (single, `--onefile`) wrapped in a thin `.zip` for Release attachment | PyInstaller `--onefile` + `Compress-Archive` |
| macOS | `osimflow-<version>-macos-x86_64.dmg` (UDZO) + `.tar.gz` (onedir folder) | PyInstaller `--onedir` wrapped in `.app` skeleton + `hdiutil` |
| Linux | `osimflow_<version>_amd64.deb` + `osimflow-<version>-1.x86_64.rpm` + `osimflow-<version>-x86_64.AppImage` + `.tar.gz` (onedir folder) | PyInstaller `--onedir` + `fpm` (gem) + `appimage-builder` |
| Universal fallback | PyPI sdist+wheel (already wired) | `pypa/gh-action-pypi-publish` (existing) + `softprops/action-gh-release` (new) |

**Code signing: deferred to 0.3.0** via OSSign free Authenticode + Apple notarisation. 0.1.x ships unsigned. Documented `xattr -d com.apple.quarantine` and Windows SmartScreen "Run anyway" workarounds in `docs/installation.md`.

### 3.1 Size budget (180-220 MB unpacked target)

**Baseline measurement (sandbox, Python 3.12, fresh venv):**

| Package | Installed size |
|---|---|
| pyarrow | 153 MB |
| scipy | 111 MB |
| pandas | 73 MB |
| numpy | 42 MB |
| matplotlib | 31 MB |
| seaborn | 2.4 MB |
| **Scientific stack total** | **~412 MB** |
| + Python 3.12 stdlib | ~25 MB |
| + Python interpreter (cpython-312 .so) | ~15 MB |
| **Total venv** | **~542 MB** |

PyInstaller's baseline onedir is **~280 MB** (it already excludes tests, docs, unused stdlib, wheel metadata — ~50% smaller than raw pip install). The 50 MB "extra" of the binary path is the bundled `libpython3.12.so` + bootloader + archive overhead.

**5 techniques to hit 180-220 MB target** (all cheap, all in `packaging/osimflow.spec`):

| # | Technique | Savings |
|---|---|---|
| 1 | `--strip` on `COLLECT()` + post-build `find ... -exec strip --strip-unneeded` | 5-15 MB |
| 2 | Exclude `*.tests` (pandas.tests, scipy.*.tests, numpy._core.tests, matplotlib.tests, etc.) | **30-50 MB** |
| 3 | Exclude `PySide6`/`PyQt5`/`PyQt6` (matplotlib's `qt_compat` hook pulls in PySide6 ~70 MB by default) | **15-40 MB** |
| 4 | Exclude `matplotlib.sphinxext`, `matplotlib.style`, `matplotlib.tests` | 5-10 MB |
| 5 | Exclude unused scipy submodules (`scipy.io`, `scipy.odr`, `scipy.cluster`, `scipy.fftpack`, `scipy.datasets`, `scipy.constants`) | 5-15 MB |
| **Total** | | **60-130 MB** |

**Tree-shaking is NOT possible** — PyInstaller's maintainer confirmed "not really" in [Discussion #8948](https://github.com/orgs/pyinstaller/discussions/8948). Python's dynamic imports force modulegraph to over-collect.

**Nuitka / AOT / Cython / mypyc do NOT help with size** — confirmed in benchmark [Nuitka #926](https://github.com/Nuitka/Nuitka/issues/926) (Nuitka is routinely 4-7× larger for hello-world; the scientific stack is compiled C extensions that ship unchanged either way). Cython/mypyc are source languages for writing new extensions, not tools for distributing existing apps. The 5% of code that's pure Python in OSimFlow is < 5% of the bundle.

**Comparable CLI tools (installed size, measured 2026-06-11):**

| Class | Tool | Size |
|---|---|---|
| Go CLIs (no scientific deps) | `gh` 14 MB, `kubectl` 55 MB, `helm` 17 MB, `terraform` 27 MB, `rye` 8 MB, `bazelisk` 7 MB | 7-55 MB |
| Python CLIs (PyInstaller, stdlib only) | `yt-dlp` 38 MB, `pdm` 22 MB | 22-38 MB |
| **OSimFlow's class: Python + scientific stack** | **No close existing example** (the scientific stack dominates) | **~200 MB** |
| Pip-install scientific Python apps | `spyder` 200 MB, `rdkit` 300 MB, `pymol` 200 MB | 200-300 MB |
| C++ + Python scientific installers | `qgis` 1.3 GB, `krita` 200 MB | 200 MB - 1.3 GB |
| Pure C++ desktop tools | `godot` 58 MB, `blender` 357 MB, `ffmpeg` 80 MB | 40-360 MB |

**Bottom line:** 180-220 MB after the 5 techniques is in the same size class as `pymol` (200 MB) and `spyder` (200 MB), and dramatically smaller than `qgis` (1.3 GB). It will never compete with Go CLIs (which have no scientific deps). Not too bad for this class of application.

---

## 4. Critical files to modify (must read first)

| File | Why |
|---|---|
| `pyproject.toml` (line 79) | `[tool.setuptools].packages` is incomplete. Switch to `find = {where = ["."]}`. Also line 125 (`tool.ruff.src`), line 142 (`per-file-ignores`), line 165 (`tool.mypy.exclude`) follow the `bin/` → `osimflow/_work_scripts/` move. |
| `osimflow/__init__.py` (lines 9-33) | Re-exports the executor classes and algorithm registry. The eager import surface is stdlib + numpy/scipy/pandas/pyarrow/matplotlib/seaborn/tqdm/pyyaml. Confirm nothing more is eager. |
| `osimflow/work.py` (lines 39-40, 68, 333, 359, 407, 464) | Hard-coded `BIN = PROJECT_ROOT / "bin"`. Replace with `importlib.resources.files("osimflow") / "_work_scripts"` lookup that works in dev, wheel, and frozen-PyInstaller modes. |
| `osimflow/campaign.py` (lines 176-177) | **Also** hard-codes `bin_dir = Path(__file__).resolve().parent.parent / "bin"`. Same fix. |
| `osimflow/executors/__init__.py` (lines ~305, ~573, ~956) | Confirms lazy imports for `submitit`, `boto3`, `urllib` (Nomad). These are the modules to `--exclude-module` in the PyInstaller spec. |
| `bin/*.py` (5 files) | `aggregate_results.py`, `apply_params_to_model.py`, `extract_kpis.py`, `generate_lhs.py`, `generate_plots.py`. Move into `osimflow/_work_scripts/`. |
| `.github/workflows/release.yml` | Existing PyPI publish pipeline. The new `release-installers.yml` chains off it via `workflow_run`. Do not modify. |
| `Makefile` | Optionally add `make build-installer` thin wrapper. Recommend yes. |

---

## 5. File-by-file change set

### Must do in this issue

| Path | Action | Description |
|---|---|---|
| `pyproject.toml` | **modify** | Line 79: `packages = ["osimflow", "osimflow.executors"]` → `find = {where = ["."]}`. Line 125: `src = [..., "bin", ...]` → drop `"bin"`. Line 142: `"bin/*" = ["PL", "SIM"]` → `"osimflow/_work_scripts/*" = ["PL", "SIM"]`. Line 165: `exclude` `"bin/"` → `"osimflow/_work_scripts/"`. Add `[tool.osimflow-installer]` with `app_name = "osimflow"`, `python_version = "3.12"`, `app_id = "io.github.anchapin.osimflow"`. |
| `osimflow/_work_scripts/` | **create** | New package. `__init__.py` (empty). Move `bin/{aggregate_results,apply_params_to_model,extract_kpis,generate_lhs,generate_plots}.py` here verbatim. |
| `osimflow/work.py` | **modify** | Replace `PROJECT_ROOT = Path(__file__).resolve().parent.parent` and `BIN = PROJECT_ROOT / "bin"` with a `_resolve_work_script(name: str) -> Path` helper using `importlib.resources.files("osimflow") / "_work_scripts" / name` (with `as_file()` for non-frozen wheels). Update 5 `subprocess.run` sites. |
| `osimflow/campaign.py` | **modify** | Line 176-177: same `_resolve_work_script` helper, drop the `bin_dir` glob for the new lookup. |
| `bin/` | **delete** | After move. |
| `packaging/osimflow.spec` | **create** | PyInstaller spec. **Onedir by default; the Windows build script flips to `--onefile` via the `EXE()` block with `exclude_binaries=False`.** Both modes share the same `Analysis()` + `excludes`. Excludes (per user-confirmed size target of 180-220 MB):<br>• **Optional extras (lazy-imported):** `boto3`, `botocore`, `s3transfer`, `submitit`, `mlflow`, `mlflow.*`, `SALib`, `pymoo`, `openstudio`, `openstudioenergyplus`<br>• **Test modules (~30-50 MB savings):** `pandas.tests`, `numpy.random.tests`, `numpy._core.tests`, `numpy.lib.tests`, `numpy.testing`, `numpy.typing.tests`, `scipy.tests`, `scipy.*.tests`, `matplotlib.tests`, `matplotlib.testing`, `seaborn.tests`, `pyarrow.tests`, `pytest`, `IPython`, `notebook`<br>• **Qt binding matplotlib pulls in via `qt_compat` (~15-40 MB):** `PySide2`, `PySide6`, `PyQt5`, `PyQt6` (only the one matplotlib selected — `qt_compat` picks the first installed; we exclude all and rely on the `agg` backend)<br>• **matplotlib parts unused by OSimFlow:** `matplotlib.sphinxext`, `matplotlib.style`, `matplotlib.projections` (only the default 2D projection is used)<br>• **numpy parts unused:** `numpy.f2py`, `numpy.polynomial`, `numpy.typing`, `numpy.ma`<br>• **scipy parts unused:** `scipy.io`, `scipy.odr`, `scipy.cluster`, `scipy.fftpack`, `scipy.datasets`, `scipy.constants`<br>Hidden imports: `pandas._libs.tslibs.base`, `pandas._libs.tslibs.np_datetime`, `pandas._libs.reduction`, `scipy._lib._util`, `scipy.spatial._ckdtree`, `matplotlib.backends.backend_agg`, `scipy.stats.qmc`, `scipy.optimize`, `scipy.spatial`. `datas=[("osimflow/_work_scripts/*.py", "osimflow/_work_scripts")]`. `runtime_hooks=["packaging/hooks/runtime-hook-osimflow.py"]`. **`strip=True` on the `COLLECT()` block** (PyInstaller's built-in `--strip`, ~5-15 MB). `optimize=2` to strip docstrings from .pyc. `upx=False` (no-op on Linux/macOS anyway). Reads version from `[tool.osimflow-installer]` or `pyproject.toml` via inline `tomllib`. |
| `packaging/hooks/runtime-hook-osimflow.py` | **create** | Prepends `sys._MEIPASS` to `sys.path` (PyInstaller default). **No env-var convention** — the work layer's `_resolve_work_script` helper uses `getattr(sys, "frozen", False)` to detect the frozen-binary mode (stdlib attribute set by all freezers: PyInstaller, cx_Freeze, Nuitka, py2exe, zipapps). |
| `packaging/scripts/build_linux.sh` | **create** | `set -euo pipefail`; runs `pyinstaller packaging/osimflow.spec --noconfirm --clean`; then `make_deb.sh`, `make_rpm.sh`, `make_appimage.sh`. |
| `packaging/scripts/build_macos.sh` | **create** | Same PyInstaller invocation; wraps binary in `.app` skeleton (`osimflow.app/Contents/{MacOS,Resources,Info.plist}`); then `make_dmg.sh`. |
| `packaging/scripts/build_windows.ps1` | **create** | PowerShell equivalent. **Uses `--onefile`** (user-confirmed hybrid: single `.exe`, double-click, no folder-ceremony). `pyinstaller packaging/osimflow.spec --noconfirm --clean` with the spec's Windows branch flipping `exclude_binaries=False`. Then `Compress-Archive` wraps the single `.exe` in `osimflow-<version>-windows.zip` (the wrapper is purely for the GitHub Release attachment). No signing (0.1.x). |
| `packaging/scripts/make_deb.sh` | **create** | `fpm -s dir -t deb -n osimflow -v <ver> -m "OSimFlow maintainers" --description "Parametric OpenStudio simulation campaigns" -p dist/osimflow_<ver>_amd64.deb dist/osimflow/=/opt/osimflow/`. |
| `packaging/scripts/make_rpm.sh` | **create** | `fpm -s dir -t rpm ...` analogue. |
| `packaging/scripts/make_dmg.sh` | **create** | `hdiutil create -ov -format UDZO -srcfolder dist/osimflow.app -volname "osimflow" dist/osimflow-<ver>-macos.dmg`. Guard with `[ "$(uname)" = "Darwin" ]`. |
| `packaging/scripts/make_appimage.sh` | **create** | `appimage-builder --recipe packaging/AppImageBuilder.yml --build dist/`. |
| `packaging/scripts/smoke_test.sh` | **create** | `$1` = path to built binary. Asserts `--help` exits 0. Asserts `osimflow import-osa --help` exits 0. Asserts `osimflow run --dry-run --n_samples 1 --input_variables example_package/variables.yml --template_sim_package example_package --outdir /tmp/osimflow-smoke-$$` exits 0, asserts `run.json` exists, asserts 4 output artifacts (`aggregated_results.csv`, `failed_simulations.csv`, KPI JSON, plot file). |
| `packaging/AppImageBuilder.yml` | **create** | 20-line recipe. `version: 1`, `AppDir: dist/AppDir`, `exec: usr/bin/osimflow`, `icon: packaging/icon.png`. |
| `packaging/icon.png` | **create** | 256×256 placeholder. Real icon is a follow-up. |
| `.github/workflows/release-installers.yml` | **create** | `on: workflow_run(workflows: ["release"], types: [completed], branches: [main])` filter `conclusion == 'success'`. Permissions: `contents: write`, `id-token: write`. Matrix `[ubuntu-latest, macos-latest, windows-latest]`. Steps: checkout, `actions/setup-python@v5` (3.12), install `pyinstaller==6.18.0` (or whatever is current at PR time), platform-specific build script, smoke test, `actions/upload-artifact@v4`, then a final `attach-to-release` job via `softprops/action-gh-release@v3` to attach artifacts. |
| `docs/installation.md` | **create** | (1) "I have Python" — `pip install osimflow` / `pipx install osimflow` + extras. (2) "I don't have Python" — table of GitHub Releases asset → install instructions per platform, with macOS Gatekeeper + Windows SmartScreen callouts. (3) Verifying — `osimflow --version` + `--dry-run` 1-sample. (4) Code-signing caveat for 0.1.x. |
| `README.md` | **modify** | Replace the "Quick start" `pip install -e ".[dev,aws,slurm]"` block (lines 26-37) with: `pip install osimflow` one-liner, then a link to `docs/installation.md`. Add a downloads badge pointing to `releases/latest`. |
| `AGENTS.md` | **modify** | §3 directory map: add `packaging/ \| PyInstaller spec, build scripts, smoke test for cross-platform installers (issue #N)`. |
| `Makefile` | **modify (optional)** | Add `build-installer: ## build local PyInstaller onedir (dev only)` → `$(PY) -m pip install pyinstaller && pyinstaller packaging/osimflow.spec --noconfirm --clean`. |

### Follow-up issue (NOT this one)

- WinGet manifest PR to `microsoft/winget-pkgs` (needs first tagged release)
- Homebrew formula (gated on ≥30★ + ≥75 watchers)
- conda-forge feedstock (gated on first stable release)
- OSSign code signing (gated on 0.3.0)
- cibuildwheel for cpubin/musllinux wheel matrix
- macOS `.app` polish (real `Info.plist`, signed-with-Developer-ID, Retina icon)
- Windows WiX v3 `.msi` (zip-only for 0.1.x)
- Linux `.deb` post-install hooks (man page, xdg-desktop)
- Nuitka rebuild path

---

## 6. Verification recipe (end-to-end, 11 steps)

1. Create branch `feat/issue-N-cross-platform-installers` off `main`.
2. **Fix `pyproject.toml` packages list** (line 79: `find = {where = ["."]}`). Verify in a fresh venv: `python -c "import osimflow, osimflow.algorithms, osimflow.exporters, osimflow.importers, osimflow.observability, osimflow.pareto"` succeeds. **`osimflow --help` works.** This is the load-bearing prerequisite.
3. **Relocate `bin/*.py` → `osimflow/_work_scripts/`**. Update `osimflow/work.py` and `osimflow/campaign.py` to use `_resolve_work_script()`. Update `pyproject.toml` `tool.ruff.src` / `per-file-ignores` / `tool.mypy.exclude`. Delete `bin/`. Run `make test` to confirm no regressions.
4. **Write `packaging/osimflow.spec` + `packaging/hooks/runtime-hook-osimflow.py`** + the six `packaging/scripts/build_*.sh` + `smoke_test.sh` + `packaging/AppImageBuilder.yml` + `packaging/icon.png`.
5. **Build locally**: `pip install pyinstaller==6.18.0 && pyinstaller packaging/osimflow.spec --noconfirm --clean`. Verify `dist/osimflow/osimflow --help` and `dist/osimflow/osimflow --version` (should print `0.1.0-dev`).
6. **Run the smoke test**: `bash packaging/scripts/smoke_test.sh dist/osimflow/osimflow`. Assert exit 0, all 4 output artifacts present under `/tmp/osimflow-smoke-*/`.
7. **Build installers on each platform**: `bash packaging/scripts/build_linux.sh` (→ `.deb`/`.rpm`/`.AppImage`/`.tar.gz`), `bash packaging/scripts/build_macos.sh` (→ `.dmg`/`.tar.gz`), `powershell -File packaging/scripts/build_windows.ps1` (→ `.zip`). Run the smoke test against each resulting binary.
8. **Push branch → open PR**. `.github/workflows/ci.yml` runs `lint + typecheck + test + contract + security` — all must pass. The new `release-installers.yml` does **not** run on PRs (it triggers on `workflow_run` of `release.yml`, which fires only on `v*` tag push).
9. **Tag `v0.1.0-rc.1`** (after merge) → `release.yml` runs (publishes sdist+wheel to TestPyPI + creates GitHub Release draft) → `release-installers.yml` runs (attaches `.zip`/`.dmg`/`.deb`/`.rpm`/`.AppImage`/`.tar.gz` to the GitHub Release).
10. **Manually download each artifact** on a fresh VM (or use `act` for Linux-only local verification). For Windows, use `windows-latest` GitHub Actions runner in an ad-hoc workflow. Document any quirks in the PR description.
11. **Update `README.md` and `AGENTS.md`**. Open the follow-up issues for OSSign signing, WinGet, Homebrew, conda-forge.

---

## 7. GitHub issue body (ready to file)

The issue can be filed with this `gh issue create` command (single-line, ready to copy):

```bash
gh issue create \
  --repo anchapin/OSimFlow \
  --title "Add cross-platform CLI installer builds (PyInstaller onedir, .msi/.dmg/.deb/.rpm/AppImage)" \
  --label "enhancement,infra" \
  --body "$(cat <<'EOF'
## Summary
Ship native installers for OSimFlow on Windows, macOS, and Linux so energy modelers without a Python toolchain can install the CLI from a double-clickable artifact.

## Motivation
The user base — building-energy modelers running OpenStudio — overlaps heavily with users who do not have Python on their workstation. The current install path (`pip install -e ".[dev,aws,slurm]"`) requires Python 3.12+ on PATH, `git clone`, and confidence with editable installs. Native installers remove that friction for the 80% case where a user only needs `osimflow run` against a local template package.

## Proposed solution
**Build tool: PyInstaller `--onedir`** as the primary, with **cx_Freeze** documented as a fallback. The decision rests on three facts:
1. PyInstaller's hooks for `numpy` / `scipy` / `pandas` / `pyarrow` / `matplotlib` / `seaborn` are first-class — every dependency in `[project.dependencies]` is covered without custom hook files.
2. PyInstaller natively supports `--codesign-identity` for macOS, which slots in cleanly when we adopt OSSign at 0.3.0.
3. The eager import surface is just stdlib + numpy + scipy + pandas + pyarrow + matplotlib + seaborn + tqdm + pyyaml — every optional extra (`boto3`, `submitit`, `mlflow`, `SALib`, `pymoo`, `openstudio`) is **lazy-imported** and can be `--exclude-module`-d from the installer.

### Artifact matrix

| OS | Installer | Tool |
|---|---|---|
| Windows | `.exe` in `.zip` | PyInstaller onedir + `Compress-Archive` |
| macOS | `.dmg` (UDZO) + `.tar.gz` | PyInstaller onedir in `.app` skeleton + `hdiutil` |
| Linux | `.deb` + `.rpm` + `.AppImage` + `.tar.gz` | PyInstaller + `fpm` + `appimage-builder` |
| Universal | PyPI sdist+wheel (already wired) | `pypa/gh-action-pypi-publish` (existing) + `softprops/action-gh-release` (new) |

Signing is **deferred to 0.3.0** (OSSign free Authenticode + Apple notarisation). 0.1.x ships unsigned with the documented `xattr -d com.apple.quarantine` and SmartScreen "Run anyway" workarounds.

Alternatives considered and rejected (full comparison in the PR description): Nuitka (smaller output but 5–10× slower build), PyOxidizer (stagnant), shiv/pex (need Python on target), briefcase (Toga-centric).

## Acceptance criteria
- [ ] `pyproject.toml` produces a working wheel — `pip install osimflow` from a fresh venv succeeds and `osimflow --help` returns the expected output
- [ ] `bin/*.py` work scripts ship with the install — `osimflow run --executor local --n_samples 1 --template_sim_package <bundled>` succeeds in stub mode
- [ ] PyInstaller `.spec` builds a `--onedir` folder for Linux + macOS + Windows with a single `pyinstaller` invocation
- [ ] `pyinstaller packaging/osimflow.spec` produces a binary under `dist/osimflow/` that prints `--help` correctly
- [ ] `.github/workflows/release-installers.yml` exists, runs on `workflow_run` after `release.yml` succeeds, builds installers for all 3 OSes
- [ ] Smoke test (`packaging/scripts/smoke_test.sh`) passes against every built binary: `--help` exits 0, `--dry-run` 1-sample campaign exits 0
- [ ] A test release tag (e.g. `v0.1.0-rc.1`) produces downloadable `.zip`/`.dmg`/`.deb`/`.rpm`/`.AppImage`/`.tar.gz` artifacts attached to the GitHub Release
- [ ] `docs/installation.md` is published, replacing the single `pip install -e` line in README with the split-paths flow
- [ ] A user on a fresh Ubuntu 22.04 / macOS 14 / Windows 11 machine without Python can download the artifact and run `osimflow --version` after 3 documented commands

## Out of scope
- Code signing (OSSign application is the 0.3.0 milestone)
- Homebrew formula, WinGet manifest, conda-forge feedstock (each needs a stable release; follow-up issues)
- Real `.msi` (WiX v3) — zip-only for Windows in this pass
- macOS `.app` polish (real `Info.plist`, signed-with-Developer-ID, Retina icon) — minimal skeleton for 0.1.x
- Nuitka rebuild path — only if cold-start becomes a complaint
- cibuildwheel / cpubin / musllinux wheel matrix — orthogonal, can land later
- `homebrew-core` PR — gated on ≥30★ + ≥75 watchers

## Risks
- **Binary size:** the scientific stack puts `--onedir` at ~200–280 MB unpacked. Acceptable for a scientific CLI (compare: `pymol-open-source` ~200 MB). Aggressive `--exclude-module` on the optional extras keeps it bounded.
- **Code signing UX (0.1.x):** Windows SmartScreen will show "Unknown publisher" — users must click "More info → Run anyway". macOS Gatekeeper will quarantine the `.dmg` — users must `xattr -d com.apple.quarantine`. Both documented in `docs/installation.md`.
- **macOS `--exclude-module openstudio`:** the real OpenStudio CLI is a separate `homebrew` install / Docker image, not a Python package. The work layer invokes `openstudio.cli run -w workflow.osw` as a subprocess. Documented.
- **EOL Python:** `requires-python = ">=3.12"` is current. The PyInstaller spec pins 3.12 and the install scripts install exactly that. When 3.13 becomes the minimum, the spec and the GitHub Actions matrix need to bump in lockstep.
- **Python interpreter cost in the binary:** PyInstaller ships a Python interpreter (~15 MB compressed, ~50 MB on disk) regardless of code size. Unavoidable with the PyInstaller model.

## Rollback plan
The new workflow (`release-installers.yml`) and the new `packaging/` directory are **strictly additive**. If the build breaks on a tagged release:
1. Delete `.github/workflows/release-installers.yml` from `main` — the workflow stops running on subsequent tags.
2. `release.yml` (existing PyPI publish) is untouched; wheel + sdist + TestPyPI + PyPI + Sigstore signing continue to work.
3. The `pyproject.toml` `find:` change and the `bin/*.py` → `osimflow/_work_scripts/` move are non-revertible without a coordinated change, but they improve the wheel (the current state is broken on import), so the rollback cost is "we can't go back to the broken state," not "we lose functionality."

## Dependencies / prerequisites
None external, but two pre-existing bugs must be fixed **in this same issue** (not a separate issue — they're load-bearing for the wheel to work, which the smoke test exercises):
- `pyproject.toml:79` `[tool.setuptools].packages` is missing 5 subpackages/modules. A wheel built from the current `pyproject.toml` is broken at import time.
- `bin/*.py` work scripts are referenced by hard-coded `REPO_ROOT/bin/` paths in `osimflow/work.py:39-40` and `osimflow/campaign.py:176-177`, so a wheel install has no way to find them.

Both are fixed by the `find:` switch and the `_work_scripts/` relocation in the change set above.

## Estimated effort
**M — 2-3 days** of focused work, end-to-end. ~700 LoC of new code across the spec, build scripts, workflow, and docs. ~50 LoC of modifications to `pyproject.toml` / `work.py` / `campaign.py` / `README.md` / `AGENTS.md`. ~1,000 LoC deleted from `bin/` (5 files moved verbatim).

## References
- PRD: [docs/OSimFlow.md §1.4 *Key Differentiators*](docs/OSimFlow.md)
- Architecture decision: [`.agents/results/architecture/0001-workflow-framework.md`](.agents/results/architecture/0001-workflow-framework.md)
- OpenStudio image distribution: [`docs/openstudio-image-distribution.md`](docs/openstudio-image-distribution.md) (why `openstudio.cli` is a *container/brew install*, not a Python package)
- Existing release pipeline: [`.github/workflows/release.yml`](.github/workflows/release.yml) (chained via `workflow_run`)
- PyInstaller docs: https://pyinstaller.org/en/stable/spec-files.html
- GitHub Actions `workflow_run` event: https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_run
- OSSign (free code signing, 0.3.0): https://ossign.org/
- fpm: https://github.com/jordansissel/fpm
EOF
)"
```

After the issue is filed, the issue number is the canonical handle for the PR (`Closes #N`) and the follow-up issues (Homebrew, WinGet, conda-forge, OSSign).

---

## 8. Open questions for the user

1. **Confirm: bundle the scientific stack in the installer (vs. ship a ~30 MB core + post-install `pip install`)?** The research says "bundle" matches the "I don't have Python" UX. Alternative: slim core → user must `pip install osimflow` post-install (defeats part of the point). **Defaulting to "bundle"** unless you say otherwise.
2. **Confirm: `--onedir` (folder) over `--onefile` (single ~80 MB binary with 3-5s cold start)?** OSimFlow campaigns are 5 min-4 h; the cold-start is irrelevant. **Defaulting to `--onedir`.**
3. **Confirm: `getattr(sys, "frozen", False)` for the work-layer runtime check (no env-var convention)?** Cleaner than `OSIMFLOW_FROZEN=1`. **Defaulting to the `sys.frozen` check.**

If any of these defaults are wrong, push back before I file the issue. Otherwise the plan is ready to execute as written.

---

## 9. Effort summary

| Aspect | Estimate |
|---|---|
| T-shirt size | **M (2-3 days, one developer)** |
| New LoC | ~700 (spec ~80, build scripts ~300, workflow ~120, runtime hook ~30, docs ~150, smoke test ~50, AppImage recipe ~20) |
| Modified LoC | ~50 (`pyproject.toml` ~15, `work.py` ~20, `campaign.py` ~5, `README.md` ~10) |
| Deleted LoC | ~1,000 (`bin/`, 5 files moved verbatim) |
| Files created | 14 (spec, hook, 9 scripts, AppImage recipe, icon, docs/installation.md) |
| Files modified | 5 (`pyproject.toml`, `osimflow/work.py`, `osimflow/campaign.py`, `README.md`, `AGENTS.md`) |
| Files deleted | 1 dir (`bin/`) |
| Risk-adjusted estimate | 3-4 days including iteration, code review, and the test-tag dance |

**Critical path (must run in this order):** (1) `pyproject.toml` fix + `bin/` relocation (2h) → (2) PyInstaller spec + runtime hook (3h) → (3) build scripts × 3 platforms (2h) → (4) CI workflow + smoke test (2h) → (5) `docs/installation.md` (1h) → (6) test-tag E2E + iteration (2-3h).
