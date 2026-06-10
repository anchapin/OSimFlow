# Docker / Container Onboarding for BEM Practitioners

> **Audience:** building energy modelers who are new to containers. If
> you have never used Docker or Podman, this guide is for you. If you
> already have Docker or Podman installed and working, skip ahead to
> [Pulling the OpenStudio image](#4-pulling-the-openstudio-image).

---

## 1. What is a container and why does OSimFlow need one?

A **container** is a lightweight, self-contained package that includes
everything a program needs to run — the executable, its libraries, and
its configuration — isolated from the rest of your system. Think of it
as a mini virtual machine that starts in seconds and shares your
computer's kernel.

**Why OSimFlow uses containers:** OpenStudio is a large, platform-specific
application with its own runtime (Ruby, C++ extensions, EnergyPlus).
Installing it directly on every laptop and cluster is fragile and
time-consuming. Instead, NREL publishes a ready-to-use OpenStudio
container image on Docker Hub. OSimFlow runs the OpenStudio CLI
*inside* that container, so you never have to install OpenStudio
yourself.

**What this means for you:**

- You install Docker (or Podman) once.
- OSimFlow downloads the OpenStudio image automatically when needed.
- Every simulation runs in a clean, reproducible environment.

**Docker vs. Podman:** Docker Desktop is the most popular container tool
but is **not free for enterprises with 250+ employees** (see the
[Docker Subscription Agreement](https://www.docker.com/legal/docker-subscription-service-agreement/)).
If your firm hits that threshold, use Podman instead — it is free,
OCI-compatible, and works identically for OSimFlow. A full Podman guide
lives at [`podman-guide.md`](podman-guide.md).

---

## 2. Installing Docker

### 2.1. macOS

1. Download **Docker Desktop for Mac** from
   <https://www.docker.com/products/docker-desktop/>.
2. Open the `.dmg` file and drag Docker to your Applications folder.
3. Launch Docker from Applications. You will see a whale icon in your
   menu bar — wait until it says "Docker Desktop is running."
4. Open Terminal and verify:

   ```bash
   docker --version
   # Docker version 27.x.x (or later)
   ```

**Resource settings:** Docker Desktop runs a Linux VM behind the scenes.
The default allocation (2 CPUs, 2 GB RAM) may be too small for large
campaigns. To adjust:

1. Click the Docker whale icon in the menu bar → **Settings** →
   **Resources**.
2. Set CPUs to 4+ and Memory to 8 GB+ if you plan to run more than a
   handful of samples.
3. Click **Apply & restart**.

**Apple Silicon (M1/M2/M3/M4) note:** Docker Desktop runs x86
containers via Rosetta 2 emulation. The NREL OpenStudio image is
x86-only, so it runs under emulation on Apple Silicon. Performance is
acceptable for small-to-medium campaigns; large campaigns will benefit
from an x86 Linux machine or HPC cluster.

### 2.2. Windows

1. Download **Docker Desktop for Windows** from
   <https://www.docker.com/products/docker-desktop/>.
2. Run the installer. When prompted, ensure **Use WSL 2 instead of
   Hyper-V** is selected (this is the default and recommended option).
3. After installation, Docker Desktop starts automatically. You may
   need to log out and log back in.
4. Open PowerShell or Command Prompt and verify:

   ```powershell
   docker --version
   # Docker version 27.x.x (or later)
   ```

**WSL 2 prerequisite:** Docker Desktop on Windows requires Windows
Subsystem for Linux 2. If the installer did not enable it:

```powershell
# Run in an elevated PowerShell
wsl --install
# Restart your computer, then install Docker Desktop
```

**Resource settings:** As on macOS, increase the default resources via
Docker Desktop → Settings → Resources. Set CPUs to 4+ and Memory to
8 GB+ for simulation workloads.

### 2.3. Linux (Docker Engine)

On Linux, install Docker Engine directly (no Docker Desktop needed).

**Ubuntu / Debian:**

```bash
# Remove any old Docker packages
sudo apt-get remove docker docker-engine docker.io containerd runc

# Set up the Docker apt repository
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin

# Add your user to the docker group (avoids sudo for every docker command)
sudo usermod -aG docker $USER

# Log out and log back in (or run: newgrp docker)
```

**RHEL / CentOS / Fedora:**

```bash
# RHEL / CentOS Stream
sudo dnf install -y dnf-utils
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo systemctl enable --now docker

# Fedora
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo systemctl enable --now docker

# Add your user to the docker group
sudo usermod -aG docker $USER
# Log out and log back in
```

Verify:

```bash
docker --version
# Docker version 27.x.x (or later)

# Test that your user can run docker without sudo
docker run --rm hello-world
```

---

## 3. Podman as an alternative

If you cannot use Docker Desktop due to licensing (firms with 250+
employees), use Podman instead. Full installation and configuration
instructions are in the dedicated
[Podman guide](podman-guide.md).

**Quick start (Linux):**

```bash
sudo dnf install -y podman    # RHEL/CentOS/Fedora
# or
sudo apt-get install -y podman  # Ubuntu

podman --version
```

**Quick start (macOS):**

```bash
brew install podman
podman machine init
podman machine start
podman --version
```

After installing Podman, use `podman` wherever this guide says `docker`
— the commands are identical for OSimFlow's purposes.

---

## 4. Pulling the OpenStudio image

OSimFlow uses the `nrel/openstudio` container image maintained by NREL
on Docker Hub. This image contains the OpenStudio CLI and EnergyPlus —
everything needed to run a building energy simulation.

**How large is the image?** Approximately 3 GB compressed (5–6 GB on
disk after extraction). The first pull takes 5–15 minutes depending on
your internet connection. Subsequent pulls for new versions download
only the changed layers.

**Do you need to pull manually?** Not necessarily. OSimFlow's executor
layer passes the image tag to the compute substrate, and the substrate
pulls it on first use. However, pre-pulling the image avoids a long
wait when your first campaign starts.

To pre-pull the image for the version you plan to use:

```bash
# Replace 3.10.0 with the version from your --openstudio_version flag
docker pull nrel/openstudio:3.10.0
```

Supported versions (see [`openstudio-image-distribution.md`](openstudio-image-distribution.md)
for the full list):

| Version | Tag | Notes |
|---|---|---|
| 3.7.0 | `nrel/openstudio:3.7.0` | LTS-compatible (Ubuntu 20.04 base) |
| 3.8.0 | `nrel/openstudio:3.8.0` | |
| 3.9.0 | `nrel/openstudio:3.9.0` | |
| 3.10.0 | `nrel/openstudio:3.10.0` | |
| 3.11.0 | `nrel/openstudio:3.11.0` | latest stable |

---

## 5. Verifying your installation

Run the OpenStudio CLI inside the container to confirm everything works:

```bash
docker run --rm nrel/openstudio:3.10.0 openstudio.cli --version
# Expected output: 3.10.0 (or the version you pulled)
```

If you see the version number, your container runtime is working
correctly and OSimFlow will be able to run simulations.

**What `docker run --rm` does:**

- `docker run` — starts a new container from the specified image.
- `--rm` — removes the container after it finishes (no leftover
  containers filling your disk).
- The rest (`nrel/openstudio:3.10.0 openstudio.cli --version`) is the
  image name and the command to run inside it.

---

## 6. How OSimFlow uses the container

You do not need to run `docker` commands yourself when using OSimFlow.
Here is what happens internally:

1. You run `osimflow run --openstudio_version 3.10.0 ...`.
2. OSimFlow constructs the image tag
   `docker.io/nrel/openstudio:3.10.0` (see
   [`openstudio-image-distribution.md`](openstudio-image-distribution.md)).
3. The executor passes this tag to the compute substrate:
   - **LocalExecutor** — the work function calls `openstudio.cli`
     directly (the CLI is expected to be on PATH inside the container
     if the local host has Docker).
   - **SlurmExecutor** — `submitit` submits a job with the container
     image as a directive. The Slurm cluster pulls and runs the image.
   - **AWSBatchExecutor** — the image tag becomes the container image
     in the Batch job definition. AWS pulls and runs it.
   - **NomadExecutor** — the image is used in the Docker driver task
     config.
4. Inside the container, OSimFlow invokes
   `openstudio.cli run -w workflow.osw` to run the actual simulation
   (see `osimflow/work.py:run_openstudio_sim`).

The container ensures that every simulation runs with the exact same
OpenStudio version, regardless of what is (or isn't) installed on your
laptop or cluster.

---

## 7. Troubleshooting common issues

### "Cannot connect to the Docker daemon"

**Symptom:** `Cannot connect to the Docker daemon at
unix:///var/run/docker.sock. Is the docker daemon running?`

**Fix (Docker Desktop — Mac/Windows):** Open Docker Desktop and wait
for it to report "Docker Desktop is running."

**Fix (Linux):** Start the Docker service:

```bash
sudo systemctl start docker
# To start automatically on boot:
sudo systemctl enable docker
```

**Fix (Podman):** This error means a tool is looking for the Docker
daemon. Enable the Podman socket instead:

```bash
systemctl --user enable --now podman.socket
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
```

### "permission denied while trying to connect to the Docker daemon"

**Symptom:** `Got permission denied while trying to connect to the
Docker daemon socket at unix:///var/run/docker.sock`

**Fix (Linux):** Your user is not in the `docker` group:

```bash
sudo usermod -aG docker $USER
# Log out and log back in (or: newgrp docker)
```

**Fix (Podman — rootless):** Podman does not require group membership
or sudo. Use `podman` instead of `docker`, or set up the `docker`
alias (see [`podman-guide.md`](podman-guide.md)).

### Image pull fails or is very slow

**Symptom:** `docker pull` hangs, times out, or shows network errors.

**Fixes:**

1. **Check disk space.** The OpenStudio image needs ~6 GB on disk. Run
   `df -h` (Linux/Mac) or check Docker Desktop → Settings → Resources
   → Disk image size.
2. **Corporate VPN / proxy.** If you are behind a corporate proxy, you
   may need to configure Docker to use it:

   **Docker Desktop (Mac/Windows):** Settings → Resources → Proxies.

   **Linux:** Create or edit `~/.docker/config.json`:

   ```json
   {
     "proxies": {
       "default": {
         "httpProxy": "http://proxy.example.com:8080",
         "httpsProxy": "http://proxy.example.com:8080",
         "noProxy": "localhost,127.0.0.1"
       }
     }
   }
   ```

3. **Docker Hub rate limits.** Anonymous pulls are limited to 100 per
   6 hours. If you hit the limit, create a free Docker Hub account and
   log in:

   ```bash
   docker login
   # Authenticated pulls get 200 per 6 hours (free tier)
   ```

4. **Pre-pull before starting a campaign** (see [§4](#4-pulling-the-openstudio-image))
   to avoid waiting during a campaign run.

### "no space left on device"

**Symptom:** `no space left on device` during image pull or simulation.

**Fix:** Docker stores images and container data in a virtual disk
(Docker Desktop) or `/var/lib/docker/` (Linux). Clean up unused data:

```bash
# Remove unused images, containers, and build cache
docker system prune -a

# Check how much space Docker is using
docker system df
```

On Docker Desktop (Mac/Windows), you can also increase the disk image
size in Settings → Resources → Disk image size.

### OpenStudio CLI not found inside the container

**Symptom:** `openstudio.cli: command not found` or simulations fail
with exit code 127.

This should not happen with the official `nrel/openstudio` image. If it
does:

1. Verify you pulled the correct image:
   `docker images | grep nrel/openstudio`.
2. Re-pull the image to ensure it is not corrupted:
   `docker pull nrel/openstudio:3.10.0`.
3. Check the OpenStudio version tag — make sure the tag you specified
   via `--openstudio_version` actually exists on
   [Docker Hub](https://hub.docker.com/r/nrel/openstudio/tags).

---

## 8. Singularity / Apptainer on HPC (for Slurm users)

Many HPC clusters do not allow Docker (it requires root privileges).
Instead, they provide **Singularity** (now called **Apptainer**), which
can run the same OCI container images without root.

**How it works:** Singularity converts a Docker image into a single
`.sif` file that can be executed like any other program on the cluster.
OSimFlow's Slurm executor can be configured to use Singularity as the
container runtime.

### Building the Singularity image

On a cluster node (or a machine with Singularity/Apptainer installed):

```bash
# Pull the Docker image and convert to SIF
singularity pull openstudio-3.10.0.sif docker://nrel/openstudio:3.10.0

# Verify
singularity exec openstudio-3.10.0.sif openstudio.cli --version
```

The resulting `.sif` file is ~5 GB. Place it on shared storage
accessible to all cluster nodes.

### Running OSimFlow with Singularity on Slurm

The `submitit`-based Slurm executor uses Singularity when the cluster's
container runtime is configured for it. Check your cluster documentation
for the specific invocation pattern — typically one of:

```bash
# Pattern 1: submitit handles container binding via slurm_container_image
# (depends on cluster's Slurm plugin configuration)

# Pattern 2: wrap the CLI invocation with singularity exec
singularity exec openstudio-3.10.0.sif openstudio.cli run -w workflow.osw
```

**Volume mounting with Singularity:** Unlike Docker, Singularity
automatically binds your home directory and `/tmp`. To bind additional
paths:

```bash
singularity exec -B /scratch:/scratch openstudio-3.10.0.sif \
  openstudio.cli run -w /scratch/workflow.osw
```

**Apptainer (the new name):** Apptainer is the successor to
Singularity. The CLI is identical (`apptainer` replaces `singularity`
in all commands). Both are actively maintained. Use whichever your
cluster provides.

---

## 9. Quick start checklist

1. **Install Docker** (or Podman) — see [§2](#2-installing-docker) or
   [§3](#3-podman-as-an-alternative).
2. **Verify the installation:** `docker run --rm hello-world`.
3. **Pull the OpenStudio image:**
   `docker pull nrel/openstudio:3.10.0`.
4. **Verify the OpenStudio image:**
   `docker run --rm nrel/openstudio:3.10.0 openstudio.cli --version`.
5. **Run your campaign:**
   `osimflow run --executor local --openstudio_version 3.10.0 ...`.
6. **On HPC:** use Singularity/Apptainer (see [§8](#8-singularity--apptainer-on-hpc-for-slurm-users)).

---

## 10. References

- [OpenStudio image distribution](openstudio-image-distribution.md) —
  where the `nrel/openstudio` image comes from and supported versions.
- [Podman guide](podman-guide.md) — full Podman installation and
  configuration instructions for enterprise users.
- [ADR-0002: Adopt `nrel/openstudio` upstream](../.agents/results/architecture/0002-adopt-nrel-upstream-image.md) —
  the decision record for consuming the NREL image directly.
- [NREL OpenStudio on Docker Hub](https://hub.docker.com/r/nrel/openstudio/tags) —
  all available image tags.
- [Docker documentation](https://docs.docker.com/)
- [Podman documentation](https://docs.podman.io/)
- [Singularity / Apptainer documentation](https://apptainer.org/documentation/)
