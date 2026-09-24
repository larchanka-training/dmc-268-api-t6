#!/usr/bin/env bash
set -euo pipefail
set +o xtrace

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env-file.sh
source "${SCRIPT_DIR}/env-file.sh"

APP_DIR="${APP_DIR:-/opt/dmc-268-api}"
COMPOSE_FILE="${APP_DIR}/compose.yml"
STATE_FILE="${APP_DIR}/.deploy-state"
PREVIOUS_FILE="${STATE_FILE}.previous"
ENV_FILE="${APP_DIR}/.env"
REQUESTED_IMAGE="${1:-}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(basename "${APP_DIR}")}"
BOOTSTRAP_NAME="${BOOTSTRAP_NAME:-${COMPOSE_PROJECT}-bootstrap}"
BOOTSTRAP_IMAGE="${BOOTSTRAP_IMAGE:-nginx:1.27-alpine}"
EDGE_NETWORK="${EDGE_NETWORK:-dmc268-edge}"
# auto: a failed deploy is being undone; the failed image must not become the rollback target.
# manual: an operator rolls back a release; it becomes the previous release (mirrors :staging-previous).
ROLLBACK_MODE="${ROLLBACK_MODE:-manual}"

if [[ "${ROLLBACK_MODE}" != "auto" && "${ROLLBACK_MODE}" != "manual" ]]; then
  echo "ROLLBACK_MODE must be auto or manual, got: ${ROLLBACK_MODE}" >&2
  exit 1
fi

# Per-run registry credentials (see deploy.sh). Started by deploy.sh, the rollback inherits its
# DOCKER_CONFIG, which still holds the login needed to pull the previous image.
OWN_DOCKER_CONFIG=""
if [[ -z "${DOCKER_CONFIG:-}" ]]; then
  DOCKER_CONFIG="$(mktemp -d)"
  OWN_DOCKER_CONFIG="${DOCKER_CONFIG}"
  export DOCKER_CONFIG
fi

logout_registry() {
  docker logout ghcr.io >/dev/null 2>&1 || true
}
cleanup_registry() {
  logout_registry
  if [[ -n "${OWN_DOCKER_CONFIG}" ]]; then
    rm -rf "${OWN_DOCKER_CONFIG}"
  fi
}
trap cleanup_registry EXIT

restore_bootstrap() {
  if [[ -f "${COMPOSE_FILE}" && -f "${ENV_FILE}" ]]; then
    "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  fi

  docker rm -f "${BOOTSTRAP_NAME}" >/dev/null 2>&1 || true
  docker pull "${BOOTSTRAP_IMAGE}"
  if [[ "${DEPLOY_MODE}" == "edge" ]]; then
    # Behind the proxy under the API alias; the proxy dials port 8000, nginx listens on 80 by default.
    docker run -d --name "${BOOTSTRAP_NAME}" \
      --restart unless-stopped \
      --label dmc-268.role=bootstrap \
      --network "${EDGE_NETWORK}" --network-alias "${EDGE_ALIAS}" \
      "${BOOTSTRAP_IMAGE}" \
      sh -c "sed -i 's/listen  *80;/listen 8000;/' /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'"
  else
    docker run -d --name "${BOOTSTRAP_NAME}" \
      --restart unless-stopped \
      --label dmc-268.role=bootstrap \
      -p 80:80 "${BOOTSTRAP_IMAGE}"
  fi

  rm -f "${STATE_FILE}" "${PREVIOUS_FILE}"
  echo "restored bootstrap container (${BOOTSTRAP_IMAGE})"
}

if [[ -f "${ENV_FILE}" ]]; then
  POSTGRES_USER="${POSTGRES_USER:-$(read_compose_env_var POSTGRES_USER "${ENV_FILE}")}"
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(read_compose_env_var POSTGRES_PASSWORD "${ENV_FILE}")}"
  POSTGRES_DB="${POSTGRES_DB:-$(read_compose_env_var POSTGRES_DB "${ENV_FILE}")}"
  DEPLOY_MODE="${DEPLOY_MODE:-$(read_compose_env_var DEPLOY_MODE "${ENV_FILE}")}"
  EDGE_ALIAS="${EDGE_ALIAS:-$(read_compose_env_var EDGE_ALIAS "${ENV_FILE}")}"
fi

# ports: publish the API on host port 80 (dedicated Terraform host).
# edge: no host port; join the edge proxy network as EDGE_ALIAS (shared course VPS).
DEPLOY_MODE="${DEPLOY_MODE:-ports}"
if [[ "${DEPLOY_MODE}" != "ports" && "${DEPLOY_MODE}" != "edge" ]]; then
  echo "DEPLOY_MODE must be ports or edge, got: ${DEPLOY_MODE}" >&2
  exit 1
fi
if [[ "${DEPLOY_MODE}" == "edge" && ! "${EDGE_ALIAS:-}" =~ ^[a-z0-9]+(-[a-z0-9]+)+$ ]]; then
  echo "EDGE_ALIAS (<service>-<env>) is required in edge mode, got: ${EDGE_ALIAS:-}" >&2
  exit 1
fi
COMPOSE=(docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" -f "${APP_DIR}/compose.${DEPLOY_MODE}.yml" --env-file "${ENV_FILE}")

if [[ -n "${REQUESTED_IMAGE}" ]]; then
  IMAGE="${REQUESTED_IMAGE}"
elif [[ -f "${PREVIOUS_FILE}" ]]; then
  IMAGE="$(awk -F= '/^current_image=/{print $2}' "${PREVIOUS_FILE}")"
else
  echo "no previous release; restoring bootstrap" >&2
  restore_bootstrap
  exit 0
fi

if [[ -z "${IMAGE}" ]]; then
  echo "previous image reference is empty" >&2
  exit 1
fi

if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  echo "POSTGRES_PASSWORD is required (expected in ${ENV_FILE})" >&2
  exit 1
fi

cd "${APP_DIR}"

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER:-github}" --password-stdin >/dev/null
  unset GHCR_TOKEN
fi

write_compose_env_file \
  "${ENV_FILE}" \
  "${IMAGE}" \
  "${POSTGRES_USER:-app}" \
  "${POSTGRES_PASSWORD}" \
  "${POSTGRES_DB:-app}" \
  "${DEPLOY_MODE}" \
  "${EDGE_ALIAS:-}"

docker pull "${IMAGE}"
"${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout 180

# Shared root account: drop the GHCR credential after the last pull (compose up, pull_policy: always).
logout_registry

if [[ "${ROLLBACK_MODE}" == "manual" && -f "${STATE_FILE}" ]]; then
  cp "${STATE_FILE}" "${PREVIOUS_FILE}"
fi

{
  echo "current_image=${IMAGE}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "rolled_back=true"
} > "${STATE_FILE}"

echo "rolled back to ${IMAGE}"
