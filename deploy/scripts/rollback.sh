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
PROBE_DIR=""
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
  if [[ -n "${PROBE_DIR}" ]]; then
    rm -rf "${PROBE_DIR}"
  fi
  if [[ -n "${OWN_DOCKER_CONFIG}" ]]; then
    rm -rf "${OWN_DOCKER_CONFIG}"
  fi
}
trap cleanup_registry EXIT

refuse_rollback() {
  echo "rollback refused: $1" >&2
  echo "Select a compatible image or follow the PostgreSQL recovery runbook." >&2
  exit 1
}

inspect_image_id() {
  local output id_pattern
  # Sentinel retains the CLI newline: reject noise, multiple IDs and extra output lines.
  if ! output="$(docker image inspect --format '{{.Id}}' "$1" 2>/dev/null && printf '.')"; then
    return 1
  fi
  id_pattern='^sha256:[0-9a-f]{64}'$'\n''\.$'
  [[ "${output}" =~ ${id_pattern} ]] || return 1
  printf '%s' "${output%$'\n.'}"
}

resolve_target_identity() {
  local repository_pattern digest_pattern candidate_pattern tag_pattern repository candidates candidate candidate_id matches
  repository_pattern='[a-z0-9][a-z0-9.-]*(:[0-9]+)?/[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*'
  digest_pattern="^(${repository_pattern})(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}$"
  candidate_pattern="^(${repository_pattern})@sha256:[0-9a-f]{64}$"
  tag_pattern="^(${repository_pattern})(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$"
  TARGET_IMAGE_ID="$(inspect_image_id "${IMAGE}")" || refuse_rollback "target image identity is unavailable"
  if [[ "${IMAGE}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    [[ "${TARGET_IMAGE_ID}" == "${IMAGE}" ]] || refuse_rollback "target image identity is inconsistent"
    RELEASE_REF="${IMAGE}"
  elif [[ "${IMAGE}" =~ ${digest_pattern} ]]; then
    # Preserve the supplied immutable ref for forward promotion's state equality guard.
    RELEASE_REF="${IMAGE}"
  elif [[ "${IMAGE}" =~ ${tag_pattern} ]]; then
    repository="${BASH_REMATCH[1]}"
    if ! candidates="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${TARGET_IMAGE_ID}" 2>/dev/null)"; then
      refuse_rollback "target release digest is unavailable"
    fi
    matches=0
    while IFS= read -r candidate; do
      [[ "${candidate}" =~ ${candidate_pattern} ]] || refuse_rollback "target release digest is unavailable"
      if [[ "${BASH_REMATCH[1]}" == "${repository}" ]]; then
        RELEASE_REF="${candidate}"
        matches=$((matches + 1))
      fi
    done <<< "${candidates}"
    [[ "${matches}" == 1 ]] || refuse_rollback "target release digest is unavailable"
    candidate_id="$(inspect_image_id "${RELEASE_REF}")" || refuse_rollback "target release digest is unavailable"
    [[ "${candidate_id}" == "${TARGET_IMAGE_ID}" ]] || refuse_rollback "target release digest is inconsistent"
  else
    refuse_rollback "target release reference is unsupported"
  fi
}

check_target_revision() {
  local probe_output
  [[ -r "${SCRIPT_DIR}/check-rollback-revision.py" ]] || refuse_rollback "target checker is unavailable"
  umask 077
  PROBE_DIR="$(mktemp -d)"
  # Compose loads every role's env_file before service filtering. A private bootstrap-only
  # config avoids requiring/recreating missing app files before admission. It joins the existing
  # project's DB network only; external=true prevents creating a replacement network.
  cat > "${PROBE_DIR}/probe.yml" <<'PROBE_COMPOSE'
services:
  bootstrap:
    image: ${IMAGE:?IMAGE is required}
    environment:
      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-app}:${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}@postgres:5432/${POSTGRES_DB:-app}
    restart: "no"
networks:
  default:
    external: true
    name: ${COMPOSE_PROJECT:?COMPOSE_PROJECT is required}_default
PROBE_COMPOSE
  if ! probe_output="$(
    IMAGE="${TARGET_IMAGE_ID}" COMPOSE_PROJECT="${COMPOSE_PROJECT}" COMPOSE_IGNORE_ORPHANS=true \
      POSTGRES_USER="${POSTGRES_USER:-app}" POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
      POSTGRES_DB="${POSTGRES_DB:-app}" \
      docker compose -p "${COMPOSE_PROJECT}" -f "${PROBE_DIR}/probe.yml" \
        run --rm --no-deps -T bootstrap python - \
        < "${SCRIPT_DIR}/check-rollback-revision.py" 2>&1
  )"; then
    # Docker/driver output can contain secrets. Only exact checker messages may cross this boundary.
    case "${probe_output}" in
      "rollback revision check: refused: target must have exactly one head"|\
      "rollback revision check: refused: database must have exactly one tracked revision"|\
      "rollback revision check: refused: database revision is unknown"|\
      "rollback revision check: refused: database revision is not an ancestor of target head"|\
      "rollback revision check: refused: cannot inspect target graph or database")
        refuse_rollback "${probe_output}" ;;
      *) refuse_rollback "target revision probe failed" ;;
    esac
  fi
  [[ "${probe_output}" == "rollback revision check: compatible" ]] || \
    refuse_rollback "target revision probe returned an unexpected result"
}

diagnostics_failed() {
  echo "rollback completion failed: $1 environment key diagnostics failed" >&2
  echo "The target stack may be running. Inspect service health and retry diagnostics before recording completion." >&2
  exit 1
}

print_environment_keys() {
  local service keys python_source key_list_pattern
  key_list_pattern='^\[("[A-Za-z_][A-Za-z0-9_]*"(, "[A-Za-z_][A-Za-z0-9_]*")*)?\]$'
  python_source="$(cat <<'PYTHON'
import json
import os
import re
print(json.dumps(sorted(name for name in os.environ.keys() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))))
PYTHON
  )"
  for service in api worker webhook-worker; do
    if ! keys="$("${COMPOSE[@]}" exec -T "${service}" python -c "${python_source}" 2>&1)"; then
      diagnostics_failed "${service}"
    fi
    # Never echo raw exec output: only a JSON list of validated ASCII names can reach the log.
    if [[ ! "${keys}" =~ ${key_list_pattern} ]]; then
      diagnostics_failed "${service}"
    fi
    echo "${service} environment keys: ${keys}"
  done
}

restore_bootstrap() {
  # By project name only, without compose.yml: an older .env may lack variables the current file
  # requires, and a stack left running would share the API alias with the bootstrap container.
  if ! docker compose -p "${COMPOSE_PROJECT}" down --remove-orphans; then
    echo "compose down failed for project ${COMPOSE_PROJECT}; not starting the bootstrap container" >&2
    return 1
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
  RABBITMQ_USER="${RABBITMQ_USER:-$(read_compose_env_var RABBITMQ_USER "${ENV_FILE}")}"
  RABBITMQ_PASSWORD="${RABBITMQ_PASSWORD:-$(read_compose_env_var RABBITMQ_PASSWORD "${ENV_FILE}")}"
  REDIS_PASSWORD="${REDIS_PASSWORD:-$(read_compose_env_var REDIS_PASSWORD "${ENV_FILE}")}"
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

for name in POSTGRES_PASSWORD RABBITMQ_PASSWORD REDIS_PASSWORD; do
  if [[ -z "${!name:-}" ]]; then
    echo "${name} is required (expected in ${ENV_FILE})" >&2
    exit 1
  fi
done

cd "${APP_DIR}"

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  if ! echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER:-github}" --password-stdin >/dev/null 2>&1; then
    refuse_rollback "registry login failed"
  fi
  unset GHCR_TOKEN
fi

if ! docker pull "${IMAGE}" >/dev/null 2>&1; then
  refuse_rollback "target image pull failed"
fi
resolve_target_identity
check_target_revision

write_compose_env_file \
  "${ENV_FILE}" \
  "${RELEASE_REF}" \
  "${POSTGRES_USER:-app}" \
  "${POSTGRES_PASSWORD}" \
  "${POSTGRES_DB:-app}" \
  "${DEPLOY_MODE}" \
  "${EDGE_ALIAS:-}" \
  "${RABBITMQ_USER:-app}" \
  "${RABBITMQ_PASSWORD}" \
  "${REDIS_PASSWORD}"

# App secrets are not tied to an image: the files of the last deploy stay as they are.
ensure_app_env_files "${APP_DIR}"

IMAGE="${TARGET_IMAGE_ID}" "${COMPOSE[@]}" up --pull never -d --remove-orphans --wait --wait-timeout 180

# Shared root account: drop the GHCR credential after the last pull (compose up, pull_policy: always).
logout_registry

print_environment_keys

if [[ "${ROLLBACK_MODE}" == "manual" && -f "${STATE_FILE}" ]]; then
  cp "${STATE_FILE}" "${PREVIOUS_FILE}"
fi

{
  echo "current_image=${RELEASE_REF}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "rolled_back=true"
} > "${STATE_FILE}"

echo "rolled back to ${RELEASE_REF}"
