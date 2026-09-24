#!/usr/bin/env bash
# Prepare a bare Debian host for Compose deploys: Docker Engine, Docker CLI and the Compose
# plugin from Debian packages, then APP_DIR. Run as root before deploy.sh.
#
# - Hosts that already have docker and docker compose (Terraform hosts via cloud-init) skip
#   the installation.
# - Safe to run twice in parallel, also from another app repo on the same host: installs are
#   serialized with flock on a shared lock file, and apt waits for the dpkg lock.
# - With EDGE_NETWORK set (course VPS), also creates that shared docker network.
# - Touches only Docker packages, the docker network and APP_DIR: no sshd, firewall or user changes.
set -euo pipefail
set +o xtrace

APP_DIR="${APP_DIR:-/opt/dmc-268-api}"
EDGE_NETWORK="${EDGE_NETWORK:-}"
LOCK_TIMEOUT="${PROVISION_LOCK_TIMEOUT:-600}"
LOCK_FILE="/var/lock/docker-provision.lock"

have_docker() {
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

install_docker() {
  local apt_opts=(-o "DPkg::Lock::Timeout=${LOCK_TIMEOUT}")
  export DEBIAN_FRONTEND=noninteractive

  # DPkg::Lock::Timeout does not cover the apt lists lock (apt-daily may hold it): retry update.
  local attempt
  for attempt in $(seq 1 30); do
    if apt-get "${apt_opts[@]}" update -qq; then
      break
    fi
    if [[ "${attempt}" -eq 30 ]]; then
      echo "apt-get update kept failing" >&2
      return 1
    fi
    sleep 10
  done

  # Debian 13 splits the engine (docker.io), the CLI (docker-cli) and Compose v2 (docker-compose,
  # plugin in /usr/libexec/docker/cli-plugins). No recommends: they pull needrestart, which may
  # restart running services, and git. apparmor is needed for containers on AppArmor kernels.
  apt-get "${apt_opts[@]}" install -y -qq --no-install-recommends \
    docker.io docker-cli docker-compose apparmor ca-certificates
  systemctl enable --now docker.service
}

if have_docker; then
  echo "docker and docker compose already installed"
else
  exec 9>"${LOCK_FILE}"
  if ! flock -w "${LOCK_TIMEOUT}" 9; then
    echo "timed out waiting for ${LOCK_FILE}" >&2
    exit 1
  fi
  # Another run may have installed Docker while this one waited for the lock.
  if have_docker; then
    echo "docker and docker compose installed by a concurrent run"
  else
    install_docker
  fi
  flock -u 9
  exec 9>&-
fi

if ! have_docker; then
  echo "docker compose is not available after provisioning" >&2
  exit 1
fi

for _ in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! docker info >/dev/null 2>&1; then
  echo "docker daemon is not running" >&2
  exit 1
fi

# A concurrent run may create the network between inspect and create: re-check on failure.
if [[ -n "${EDGE_NETWORK}" ]] && ! docker network inspect "${EDGE_NETWORK}" >/dev/null 2>&1; then
  docker network create "${EDGE_NETWORK}" >/dev/null 2>&1 || docker network inspect "${EDGE_NETWORK}" >/dev/null
  echo "created docker network ${EDGE_NETWORK}"
fi

mkdir -p "${APP_DIR}"
echo "$(docker --version); $(docker compose version)"
