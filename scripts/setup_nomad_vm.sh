#!/usr/bin/env bash
set -euo pipefail

PYTHON_IMAGE_SOURCE="${PYTHON_IMAGE_SOURCE:-ghcr.io/anchapin/scientific_python_image:latest}"
PYTHON_IMAGE_LOCAL_TAG="${PYTHON_IMAGE_LOCAL_TAG:-scientific_python_image:local}"
PYTHON_IMAGE_TAR="${PYTHON_IMAGE_TAR:-}"

echo "=== Updating package indexes ==="
sudo apt-get update

echo "=== Installing prerequisites ==="
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release wget jq

echo "=== Installing Docker ==="
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io

echo "=== Adding ubuntu user to docker group ==="
sudo usermod -aG docker ubuntu

echo "=== Preparing local Python worker image tag for OSimFlow ==="
if [[ -n "${PYTHON_IMAGE_TAR}" ]]; then
  echo "Loading Python image tarball: ${PYTHON_IMAGE_TAR}"
  sudo docker load -i "${PYTHON_IMAGE_TAR}"
  if sudo docker image inspect "${PYTHON_IMAGE_SOURCE}" >/dev/null 2>&1; then
    sudo docker tag "${PYTHON_IMAGE_SOURCE}" "${PYTHON_IMAGE_LOCAL_TAG}"
  else
    LOADED_IMAGE="$(sudo docker image ls --format '{{.Repository}}:{{.Tag}}' | head -n 1)"
    sudo docker tag "${LOADED_IMAGE}" "${PYTHON_IMAGE_LOCAL_TAG}"
  fi
else
  if [[ -n "${GHCR_USERNAME:-}" && -n "${GHCR_TOKEN:-}" ]]; then
    echo "Logging into ghcr.io with provided credentials"
    echo "${GHCR_TOKEN}" | sudo docker login ghcr.io -u "${GHCR_USERNAME}" --password-stdin
  fi
  echo "Pulling Python image: ${PYTHON_IMAGE_SOURCE}"
  sudo docker pull "${PYTHON_IMAGE_SOURCE}"
  sudo docker tag "${PYTHON_IMAGE_SOURCE}" "${PYTHON_IMAGE_LOCAL_TAG}"
fi

if ! sudo docker image inspect "${PYTHON_IMAGE_LOCAL_TAG}" >/dev/null 2>&1; then
  echo "ERROR: local Python image tag not available: ${PYTHON_IMAGE_LOCAL_TAG}" >&2
  exit 1
fi

echo "=== Verifying local Python image is runnable ==="
sudo docker run --rm "${PYTHON_IMAGE_LOCAL_TAG}" python --version

echo "=== Writing OSimFlow runtime env override ==="
sudo tee /etc/profile.d/osimflow_nomad_env.sh > /dev/null <<EOF
export OSIMFLOW_PYTHON_CONTAINER_IMAGE="${PYTHON_IMAGE_LOCAL_TAG}"
EOF

echo "=== Installing HashiCorp Nomad ==="
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor --yes -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt-get update
sudo apt-get install -y nomad

echo "=== Configuring Nomad as single-node Server + Client ==="
sudo mkdir -p /etc/nomad.d
sudo tee /etc/nomad.d/nomad.hcl > /dev/null <<EOF
data_dir = "/opt/nomad/data"
bind_addr = "0.0.0.0"

server {
  enabled          = true
  bootstrap_expect = 1
}

client {
  enabled = true
  servers = ["127.0.0.1"]
  
  options {
    "driver.allow_privileged" = "true"
  }
}
EOF

echo "=== Starting Nomad Service ==="
sudo systemctl daemon-reload
sudo systemctl enable nomad
sudo systemctl restart nomad

echo "=== Checking Nomad status ==="
sleep 5
nomad node status
nomad agent-info

echo "=== Completed ==="
echo "Local tag ready: ${PYTHON_IMAGE_LOCAL_TAG}"
echo "Runtime override written to /etc/profile.d/osimflow_nomad_env.sh"
