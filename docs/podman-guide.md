# Using Podman as a Docker Desktop Alternative

> **Audience:** Enterprise BEM practitioners at firms that cannot use
> Docker Desktop due to licensing. If your organization has 250+
> employees or exceeds $10M in annual revenue, the Docker Desktop
> Subscription Agreement requires a paid license. This guide shows you
> how to use Podman instead — no license required.

---

## 1. Why Podman?

Docker Desktop is not free for enterprises. Since the 2021 licensing
change, organizations with **250+ employees** or **$10M+ annual
revenue** must purchase a Docker Desktop Pro/Business/Team subscription.
Many architectural engineering (AE) and building-energy consulting firms
exceed these thresholds.

**Podman** is a free, open-source, daemonless container engine developed
by Red Hat that is OCI-compatible and serves as a drop-in replacement
for Docker. For OSimFlow users, the practical differences are minimal:

- Podman pulls and runs the same `nrel/openstudio` images from Docker
  Hub that OSimFlow already consumes (see
  [`openstudio-image-distribution.md`](openstudio-image-distribution.md)).
- Podman understands the same CLI syntax (`build`, `run`, `pull`, `push`)
  that Docker does.
- Podman has no daemon — each container is a child process, which avoids
  the privilege-escalation surface of the Docker daemon.
- Podman is the default container tool on RHEL, CentOS Stream, Fedora,
  and many enterprise Linux distributions.

**When to use Docker Desktop instead:**

If your firm has a Docker Desktop license or you are under the
employee/revenue thresholds, Docker Desktop works fine with OSimFlow.
This guide is specifically for teams that need a free alternative.

---

## 2. Installation

### 2.1. Linux (RHEL / CentOS Stream / Fedora)

Podman is pre-installed or available from the default repositories on
most enterprise Linux distributions:

```bash
# RHEL / CentOS Stream
sudo dnf install -y podman

# Fedora
sudo dnf install -y podman

# Ubuntu 22.04 / 24.04
sudo apt-get update
sudo apt-get install -y podman
```

Verify the installation:

```bash
podman --version
# podman version 4.x or 5.x
```

### 2.2. macOS

Podman runs inside a Linux virtual machine on macOS. The recommended
installation path is via Homebrew:

```bash
# Install Podman and its CLI
brew install podman

# Initialize and start the VM (one-time)
podman machine init
podman machine start

# Verify
podman --version
```

The default VM allocates 2 CPUs, 2 GB RAM, and 20 GB disk. For
large campaigns, increase these resources:

```bash
# Remove the default machine and recreate with more resources
podman machine stop
podman machine rm

# 4 CPUs, 8 GB RAM, 100 GB disk
podman machine init --cpus 4 --memory 8192 --disk-size 100
podman machine start
```

### 2.3. Windows (WSL2)

Podman runs inside WSL2 on Windows. There are two installation options:

**Option A: Podman Desktop (GUI)**

1. Download [Podman Desktop](https://podman.io/getting-started/installation).
2. Run the installer — it will set up WSL2 and the Podman machine
   automatically.
3. Open a terminal and verify: `podman --version`.

**Option B: Command-line only via WSL2**

```powershell
# From an elevated PowerShell — ensure WSL2 is installed
wsl --install

# Inside your WSL2 distribution (e.g., Ubuntu)
sudo apt-get update
sudo apt-get install -y podman

# Verify
podman --version
```

---

## 3. OSimFlow Compatibility

OSimFlow consumes OCI container images — it does not call the Docker
daemon directly. The `LocalExecutor` runs work in Python threads (no
container runtime required for the stub mode). When the real OpenStudio
CLI is invoked via `osimflow/work.py:run_openstudio_sim`, it calls
`openstudio.cli run -w workflow.osw` inside the `nrel/openstudio`
container.

The executor layer (`osimflow/executors/`) passes the container image
string as the `container=` argument. On Slurm, this maps to a
`submitit` job with a container directive. On AWS Batch, it becomes the
container image in the job definition. On Nomad, it becomes the Docker
driver image.

**Podman is compatible because:**

1. OSimFlow pulls images from Docker Hub (`docker.io/nrel/openstudio`).
   Podman can pull from Docker Hub without any configuration changes.
2. The executor code does not invoke `docker` CLI commands directly — it
   delegates to the substrate (Slurm, AWS Batch, Nomad) which uses its
   own container runtime. On systems where Podman provides the `docker`
   alias, everything works transparently.

---

## 4. Configuration

### 4.1. Pulling the OpenStudio image

Pull the image you need before running a campaign:

```bash
# Pull the version you plan to use
podman pull docker.io/nrel/openstudio:3.10.0

# Verify it's available locally
podman images
```

### 4.2. Using `docker` as an alias for `podman`

On Linux, Podman provides a `docker` alias out of the box on most
distributions. If your system does not have it:

```bash
# Create a persistent alias (add to ~/.bashrc or ~/.zshrc)
alias docker=podman

# Or create a symlink (requires root)
sudo ln -sf $(which podman) /usr/local/bin/docker
```

Once aliased, any tool or script that calls `docker` will transparently
use Podman instead.

### 4.3. Setting `DOCKER_HOST` for macOS / Windows

On macOS and Windows, Podman runs inside a VM. Some tools expect a
Docker-compatible socket at `/var/run/docker.sock`. Podman provides a
compatibility socket:

```bash
# macOS — enable the Docker-compatible socket
podman machine init  # already done during installation
podman machine start

# The socket is available at:
# ~/.local/share/containers/podman/machine/podman.sock

# Set DOCKER_HOST to point to the Podman socket (add to ~/.zshrc)
export DOCKER_HOST="unix://${HOME}/.local/share/containers/podman/machine/podman.sock"
```

```powershell
# Windows (PowerShell) — after starting the Podman machine
$env:DOCKER_HOST = "npipe:////./pipe/podman-machine-default"
```

### 4.4. Rootless mode (Linux)

Podman defaults to rootless mode on Linux, which is a security advantage.
Your containers run with your user's UID — no root privileges required.

For OSimFlow's workload (read-heavy simulation I/O), rootless mode works
without any special configuration. If you encounter permission issues
with mounted volumes, see the troubleshooting section below.

---

## 5. Known Differences from Docker

### 5.1. Volume mounting on macOS

On macOS, Podman runs inside a Linux VM. Volume mounts are relative to
the VM filesystem, not the macOS host. The Podman machine automatically
mounts your home directory, so paths like `~/Projects/OSimFlow` work:

```bash
# This works — ~/Projects is auto-mounted into the VM
podman run -v ~/Projects/OSimFlow:/workspace:Z \
  docker.io/nrel/openstudio:3.10.0 \
  openstudio.cli run -w /workspace/workflow.osw
```

The `:Z` relabel flag (SELinux) is automatically handled by Podman on
systems that need it. You can omit it on macOS.

### 5.2. No daemon to manage

Unlike Docker Desktop, Podman has no background daemon consuming
resources. On macOS/Windows, the VM (`podman machine`) is the only
background process:

```bash
# Check if the VM is running (macOS/Windows)
podman machine info

# Stop the VM when you're done running campaigns
podman machine stop
```

### 5.3. Image storage location

Podman stores images in a different location than Docker:

| Platform | Docker | Podman |
|---|---|---|
| Linux | `/var/lib/docker/` | `~/.local/share/containers/` (rootless) |
| macOS | VM disk | VM disk (managed by `podman machine`) |
| Windows | VM disk | VM disk (managed by `podman machine`) |

Docker and Podman do not share image caches. After switching, pull the
images you need:

```bash
podman pull docker.io/nrel/openstudio:3.10.0
podman pull docker.io/nrel/openstudio:3.11.0
```

### 5.4. Docker Compose compatibility

Podman supports Docker Compose via `podman-compose` (a separate package)
or the built-in `podman compose` command (Podman 3.0+):

```bash
# Using podman-compose
pip install podman-compose
podman-compose up

# Using built-in compose (Podman 3.0+)
podman compose up
```

OSimFlow does not currently use Docker Compose, but if you integrate it
with other tools that do, Podman handles it.

---

## 6. Troubleshooting

### "Cannot connect to the Docker daemon"

This error means a tool is trying to reach the Docker daemon socket.
On Linux with Podman, there is no daemon. Fix it by enabling the
Podman socket:

```bash
# Enable the user socket (systemd-based Linux)
systemctl --user enable --now podman.socket

# Set DOCKER_HOST
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
```

On macOS, ensure the Podman machine is running:

```bash
podman machine start
```

### "permission denied" on volume mounts (Linux rootless)

Rootless Podman maps your UID into the container. If the container
process writes as root (UID 0), the files on the host will be owned by
your UID — usually the right behavior. If you see permission errors:

```bash
# Use the :U flag to chown the mounted directory to the container user
podman run -v ./results:/results:U docker.io/nrel/openstudio:3.10.0 ...

# Or use the :Z flag for SELinux relabeling (RHEL/CentOS/Fedora)
podman run -v ./results:/results:Z docker.io/nrel/openstudio:3.10.0 ...
```

### Image pull is slow

Podman pulls from Docker Hub by default. If pulls are slow due to rate
limits or network conditions:

```bash
# Check pull progress
podman pull docker.io/nrel/openstudio:3.10.0 --log-level=debug

# If you have a private registry mirror (enterprise proxy):
# Edit ~/.config/containers/registries.conf
# Add your mirror under [registries.mirrors]
```

### `podman machine` won't start (macOS/Windows)

The Podman VM requires virtualization support (Virtualization.framework
on macOS, Hyper-V/WSL2 on Windows):

```bash
# macOS — reset the machine if it's in a bad state
podman machine rm
podman machine init --cpus 4 --memory 8192 --disk-size 100
podman machine start
```

```powershell
# Windows — ensure WSL2 is up to date
wsl --update
```

### Nomad executor with Podman

The Nomad executor in OSimFlow uses the Docker task driver. On hosts
where Podman provides the Docker socket, the Nomad Docker driver
detects Podman transparently. No configuration changes are needed.

If Nomad cannot find the container runtime, set the Docker socket path
in the Nomad client configuration:

```hcl
# nomad.hcl (client stanza)
client {
  enabled = true
}
plugin "docker" {
  config {
    socket_path = "/run/user/$UID/podman/podman.sock"
  }
}
```

---

## 7. Quick Start Checklist

Follow these steps to replace Docker Desktop with Podman for OSimFlow:

1. **Install Podman** (see [§2](#2-installation)).
2. **Start the VM** (macOS/Windows only): `podman machine start`.
3. **Pull the OpenStudio image**:
   `podman pull docker.io/nrel/openstudio:3.10.0`.
4. **Verify**: `podman run --rm docker.io/nrel/openstudio:3.10.0
   openstudio.cli --version`.
5. **Run your OSimFlow campaign** — no code changes needed.

---

## 8. References

- [Podman documentation](https://docs.podman.io/)
- [Podman installation guide](https://podman.io/getting-started/installation)
- [OpenStudio image distribution](openstudio-image-distribution.md) —
  where the `nrel/openstudio` image comes from and supported versions.
- [Docker Desktop Subscription Agreement](https://www.docker.com/legal/docker-subscription-service-agreement/)
  — the license terms that motivate this guide.
- [NREL OpenStudio on Docker Hub](https://hub.docker.com/r/nrel/openstudio/tags)
