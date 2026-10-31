#!/usr/bin/env bash
set -euo pipefail
set +o xtrace

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env-file.sh
source "${SCRIPT_DIR}/env-file.sh"

APP_DIR="${APP_DIR:-/opt/dmc-268-api}"
IMAGE="${1:-${IMAGE:-}}"
COMPOSE_FILE="${APP_DIR}/compose.yml"
STATE_FILE="${APP_DIR}/.deploy-state"
SWITCH_FILE="${APP_DIR}/.deploy-switch"
ENV_FILE="${APP_DIR}/.env"
ROLLBACK_SCRIPT="${APP_DIR}/rollback.sh"
# One compose project per app dir: /opt/dmc-268-api (Terraform host), /opt/dmc-268-api-staging (course VPS).
COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(basename "${APP_DIR}")}"
BOOTSTRAP_NAME="${BOOTSTRAP_NAME:-${COMPOSE_PROJECT}-bootstrap}"
# Named volume "postgres-data" of the compose project.
POSTGRES_VOLUME="${POSTGRES_VOLUME:-${COMPOSE_PROJECT}_postgres-data}"
# RabbitMQ, like Postgres, fixes its default user only when the volume "rabbitmq-data" is initialised.
RABBITMQ_VOLUME="${RABBITMQ_VOLUME:-${COMPOSE_PROJECT}_rabbitmq-data}"

# Per-run registry credentials: API and UI deploys share the root account on the course VPS, so
# docker login must not touch /root/.docker/config.json. A rollback started from here reuses it.
APP_STAGE_DIR=""
DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG

logout_registry() {
  docker logout ghcr.io >/dev/null 2>&1 || true
}
cleanup_registry() {
  logout_registry
  rm -rf "${DOCKER_CONFIG}"
  [[ -z "${APP_STAGE_DIR}" ]] || rm -rf "${APP_STAGE_DIR}"
}
trap cleanup_registry EXIT

generate_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

if [[ -z "${IMAGE}" ]]; then
  echo "usage: deploy.sh <image-ref>" >&2
  exit 1
fi

mkdir -p "${APP_DIR}"
cd "${APP_DIR}"

REQUESTED_IMAGE="${IMAGE}"

if [[ -f "${ENV_FILE}" ]]; then
  POSTGRES_USER="${POSTGRES_USER:-$(read_compose_env_var POSTGRES_USER "${ENV_FILE}")}"
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(read_compose_env_var POSTGRES_PASSWORD "${ENV_FILE}")}"
  POSTGRES_DB="${POSTGRES_DB:-$(read_compose_env_var POSTGRES_DB "${ENV_FILE}")}"
  DEPLOY_MODE="${DEPLOY_MODE:-$(read_compose_env_var DEPLOY_MODE "${ENV_FILE}")}"
  EDGE_ALIAS="${EDGE_ALIAS:-$(read_compose_env_var EDGE_ALIAS "${ENV_FILE}")}"
  RABBITMQ_USER="${RABBITMQ_USER:-$(read_compose_env_var RABBITMQ_USER "${ENV_FILE}")}"
  RABBITMQ_PASSWORD="${RABBITMQ_PASSWORD:-$(read_compose_env_var RABBITMQ_PASSWORD "${ENV_FILE}")}"
  REDIS_PASSWORD="${REDIS_PASSWORD:-$(read_compose_env_var REDIS_PASSWORD "${ENV_FILE}")}"
  NO_PREVIOUS_RELEASE="${NO_PREVIOUS_RELEASE:-$(read_compose_env_var NO_PREVIOUS_RELEASE "${ENV_FILE}")}"
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
# What rollback.sh puts in place of a release without a previous one (see there); kept in .env.
if [[ -n "${NO_PREVIOUS_RELEASE:-}" && "${NO_PREVIOUS_RELEASE}" != "bootstrap" && "${NO_PREVIOUS_RELEASE}" != "stop" ]]; then
  echo "NO_PREVIOUS_RELEASE must be bootstrap or stop, got: ${NO_PREVIOUS_RELEASE}" >&2
  exit 1
fi
COMPOSE=(docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" -f "${APP_DIR}/compose.${DEPLOY_MODE}.yml" --env-file "${ENV_FILE}")

IMAGE="${REQUESTED_IMAGE}"

# Postgres fixes the password when the volume is initialised: generate one only on a fresh host,
# keep it in .env (0600) and reuse it on every later deploy and rollback.
if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  if [[ -f "${ENV_FILE}" ]] || docker volume inspect "${POSTGRES_VOLUME}" >/dev/null 2>&1; then
    echo "POSTGRES_PASSWORD is not set and ${ENV_FILE} has none, but ${ENV_FILE} or volume ${POSTGRES_VOLUME} already exists; refusing to generate a new password" >&2
    exit 1
  fi
  POSTGRES_PASSWORD="$(generate_password)"
  if [[ ! "${POSTGRES_PASSWORD}" =~ ^[0-9a-f]{48}$ ]]; then
    echo "failed to generate POSTGRES_PASSWORD" >&2
    exit 1
  fi
  echo "generated POSTGRES_PASSWORD on the host (stored in ${ENV_FILE})"
fi

# Same rule for RabbitMQ; the .env of a host deployed before RabbitMQ existed has no password yet,
# so only an existing volume blocks generation.
if [[ -z "${RABBITMQ_PASSWORD:-}" ]]; then
  if docker volume inspect "${RABBITMQ_VOLUME}" >/dev/null 2>&1; then
    echo "RABBITMQ_PASSWORD is not set and ${ENV_FILE} has none, but volume ${RABBITMQ_VOLUME} already exists; refusing to generate a new password" >&2
    exit 1
  fi
  RABBITMQ_PASSWORD="$(generate_password)"
  if [[ ! "${RABBITMQ_PASSWORD}" =~ ^[0-9a-f]{48}$ ]]; then
    echo "failed to generate RABBITMQ_PASSWORD" >&2
    exit 1
  fi
  echo "generated RABBITMQ_PASSWORD on the host (stored in ${ENV_FILE})"
fi

# Redis is a cache without a volume: a new password only drops the cache.
if [[ -z "${REDIS_PASSWORD:-}" ]]; then
  REDIS_PASSWORD="$(generate_password)"
  if [[ ! "${REDIS_PASSWORD}" =~ ^[0-9a-f]{48}$ ]]; then
    echo "failed to generate REDIS_PASSWORD" >&2
    exit 1
  fi
  echo "generated REDIS_PASSWORD on the host (stored in ${ENV_FILE})"
fi

# Stage candidates without replacing or chmodding the current role files. An absent manual
# bundle retains them; CI explicitly supplies even an empty bundle to remove configuration.
umask 077
APP_STAGE_DIR="$(mktemp -d "${APP_DIR}/.stage.XXXXXX")"
if [[ -n "${APP_SECRETS_B64+set}" ]]; then
  write_app_env_files "${APP_STAGE_DIR}" "${APP_SECRETS_B64}"
  unset APP_SECRETS_B64
else
  for file_name in "${ENV_FILE_NAMES[@]}"; do
    if [[ -f "${APP_DIR}/${file_name}" ]]; then
      cp "${APP_DIR}/${file_name}" "${APP_STAGE_DIR}/${file_name}"
    fi
  done
  ensure_app_env_files "${APP_STAGE_DIR}"
fi

refuse_deployment() {
  echo "deployment refused: $1" >&2
  exit 1
}

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  if ! registry_reason="$(printf '%s\n' "${GHCR_TOKEN}" | run_registry_command docker login ghcr.io -u "${GHCR_USER:-github}" --password-stdin)"; then
    refuse_deployment "registry login failed${registry_reason:+ (${registry_reason})}"
  fi
  unset GHCR_TOKEN
fi
if ! registry_reason="$(run_registry_command docker pull "${IMAGE}")"; then
  refuse_deployment "target image pull failed${registry_reason:+ (${registry_reason})}"
fi
resolve_target_identity refuse_deployment
gitlab_bundle_status=0
gitlab_bundle="$(gitlab_bundle_from_env_files "${APP_STAGE_DIR}" 2>/dev/null)" || gitlab_bundle_status=$?
if [[ "${gitlab_bundle_status}" != 0 ]]; then
  refuse_deployment "$(gitlab_deployment_failure_reason "${gitlab_bundle_status}")"
fi
if ! validate_gitlab_deployment "${TARGET_IMAGE_ID}" "${gitlab_bundle}" literal-gitlab >/dev/null 2>&1; then
  refuse_deployment "GitLab configuration_invalid"
fi
IMAGE="${RELEASE_REF}"

# Admission succeeded. Only now replace live files and record the previous release.
for file_name in "${ENV_FILE_NAMES[@]}"; do
  mv -f "${APP_STAGE_DIR}/${file_name}" "${APP_DIR}/${file_name}"
done
if [[ -f "${STATE_FILE}" ]]; then
  cp "${STATE_FILE}" "${STATE_FILE}.previous"
fi

# Release switch: from here on the host moves to IMAGE. The CI run id (DEPLOY_RUN) lets the CI
# auto rollback of this run tell a deploy that got this far from one that stopped earlier.
if [[ -n "${DEPLOY_RUN:-}" ]]; then
  {
    echo "run=${DEPLOY_RUN}"
    echo "image=${IMAGE}"
  } > "${SWITCH_FILE}"
fi

write_compose_env_file \
  "${ENV_FILE}" \
  "${IMAGE}" \
  "${POSTGRES_USER:-app}" \
  "${POSTGRES_PASSWORD}" \
  "${POSTGRES_DB:-app}" \
  "${DEPLOY_MODE}" \
  "${EDGE_ALIAS:-}" \
  "${RABBITMQ_USER:-app}" \
  "${RABBITMQ_PASSWORD}" \
  "${REDIS_PASSWORD}" \
  "${NO_PREVIOUS_RELEASE:-}"

if docker inspect "${BOOTSTRAP_NAME}" >/dev/null 2>&1; then
  docker rm -f "${BOOTSTRAP_NAME}" >/dev/null
fi

# A completed bootstrap container from the previous image must not suppress the
# migration/seed pipeline for this deploy.  API startup is gated on its success
# in compose.yml.
"${COMPOSE[@]}" rm -sf bootstrap >/dev/null 2>&1 || true

if ! IMAGE="${TARGET_IMAGE_ID}" "${COMPOSE[@]}" up --pull missing -d --remove-orphans --wait --wait-timeout 180; then
  echo "compose up failed; rolling back" >&2
  ROLLBACK_MODE=auto "${ROLLBACK_SCRIPT}"
  exit 1
fi

# Shared root account: drop credentials after startup pinned to the admitted local image
# with --pull missing. The EXIT trap stays as a fallback.
logout_registry

{
  echo "current_image=${IMAGE}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${STATE_FILE}"

echo "deployed ${IMAGE}"
