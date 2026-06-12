# Installation

OSimFlow can be installed via **pip** (requires Python 3.12+) or as a
**standalone binary** (no Python needed).

---

## pip install (from PyPI)

```bash
pip install osimflow
```

For optional features:

```bash
pip install "osimflow[aws]"       # AWS Batch executor
pip install "osimflow[slurm]"     # Slurm executor (submitit)
pip install "osimflow[mlflow]"    # MLflow tracking
pip install "osimflow[api]"       # REST API server
```

### Development install

```bash
git clone https://github.com/anchapin/OSimFlow.git
cd OSimFlow
pip install -e ".[dev,aws,slurm]"
```

---

## Binary install (no Python required)

Standalone binaries are attached to each
[GitHub Release](https://github.com/anchapin/OSimFlow/releases).

### Linux

```bash
# Download the latest release
curl -L -o osimflow.tar.gz \
  https://github.com/anchapin/OSimFlow/releases/latest/download/osimflow-<version>-linux-x86_64.tar.gz

# Extract
tar xzf osimflow.tar.gz

# Run directly
./osimflow/osimflow --help
```

Optional: add to PATH:

```bash
sudo mv osimflow /usr/local/bin/osimflow-bin
sudo ln -s /usr/local/bin/osimflow-bin/osimflow /usr/local/bin/osimflow
```

### macOS

```bash
# Download the latest release
curl -L -o osimflow.tar.gz \
  https://github.com/anchapin/OSimFlow/releases/latest/download/osimflow-<version>-macos-x86_64.tar.gz

# Extract
tar xzf osimflow.tar.gz

# Remove quarantine attribute (unsigned binary in 0.1.x)
xattr -d com.apple.quarantine osimflow/osimflow

# Run
./osimflow/osimflow --help
```

> **macOS Gatekeeper:** The binary is unsigned in the 0.1.x series. On
> first launch, macOS may block it. To bypass:
> 1. Right-click the binary → "Open" → confirm in the dialog, **or**
> 2. Run `xattr -d com.apple.quarantine osimflow/osimflow` before first use.
>
> Code signing (OSSign + Apple notarisation) is planned for 0.3.0.

### Windows

1. Download `osimflow-<version>-windows-x86_64.zip` from the
   [latest release](https://github.com/anchapin/OSimFlow/releases).
2. Extract the zip file.
3. Open a terminal in the extracted directory.
4. Run `.\osimflow.exe --help`.

> **Windows SmartScreen:** The binary is unsigned in 0.1.x. On first
> launch, SmartScreen may show "Unknown publisher". Click
> "More info" → "Run anyway".
>
> **Note:** Windows builds use `--onefile` mode. The first launch after
> a reboot may take 3–8 seconds for extraction + Windows Defender scan.
> Subsequent launches are fast.

---

## Binary size expectations

| Platform | Format | Approximate Size |
|----------|--------|------------------|
| Linux    | .tar.gz (onedir) | 180–220 MB unpacked |
| macOS    | .tar.gz (onedir) | 180–220 MB unpacked |
| Windows  | .exe (onefile)   | 80–120 MB on disk  |

The size is driven by the scientific Python stack (numpy, scipy, pandas,
matplotlib). PyInstaller excludes test suites, Qt bindings, and unused
submodules to keep the bundle under 220 MB.

---

## Signing status

| Version | Windows | macOS | Linux |
|---------|---------|-------|-------|
| 0.1.x   | Unsigned (SmartScreen "Run anyway") | Unsigned (`xattr -d`) | N/A |
| 0.3.0+  | Authenticode (planned) | Notarised (planned) | N/A |

---

## Verifying the install

After installation, verify with:

```bash
osimflow --version
osimflow --help
```

Run a quick smoke test:

```bash
osimflow run \
  --executor local \
  --dry-run \
  --input_variables variables.yml \
  --template_sim_package ./my_model \
  --n_samples 1 \
  --outdir ./smoke_test
```

See the [User Guide](user-guide.md) for full usage instructions.
