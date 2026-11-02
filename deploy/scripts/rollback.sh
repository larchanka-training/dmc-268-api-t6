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
SWITCH_FILE="${APP_DIR}/.deploy-switch"
ENV_FILE="${APP_DIR}/.env"
REQUESTED_IMAGE="${1:-}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(basename "${APP_DIR}")}"
BOOTSTRAP_NAME="${BOOTSTRAP_NAME:-${COMPOSE_PROJECT}-bootstrap}"
BOOTSTRAP_IMAGE="${BOOTSTRAP_IMAGE:-nginx:1.27-alpine}"
EDGE_NETWORK="${EDGE_NETWORK:-dmc268-edge}"
# auto: a failed deploy is being undone; the failed image must not become the rollback target.
# manual: an operator rolls back a release; it becomes the previous release (mirrors :staging-previous).
ROLLBACK_MODE="${ROLLBACK_MODE:-manual}"
# What replaces a release that has no previous one to go back to:
# bootstrap: a stock nginx under the API alias or port (as cloud-init's placeholder does);
# stop: nothing, the stack is only taken down (prod: the edge proxy's own 503 for the API host is
#   better than the nginx welcome page).
# Set by the workflow and kept in the stack's .env (see below); empty means bootstrap.

if [[ "${ROLLBACK_MODE}" != "auto" && "${ROLLBACK_MODE}" != "manual" ]]; then
  echo "ROLLBACK_MODE must be auto or manual, got: ${ROLLBACK_MODE}" >&2
  exit 1
fi

# CI auto rollback (DEPLOY_RUN set): a deploy of this run that stopped before the release switch
# left the running release, .env and .deploy-state as they were, so there is nothing to undo.
if [[ "${ROLLBACK_MODE}" == "auto" && -n "${DEPLOY_RUN:-}" ]]; then
  if [[ ! -f "${SWITCH_FILE}" || "$(awk -F= '/^run=/{print $2}' "${SWITCH_FILE}")" != "${DEPLOY_RUN}" ]]; then
    echo "release unchanged: deploy run ${DEPLOY_RUN} stopped before the release switch; skipping auto rollback"
    exit 0
  fi
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

check_target_revision() {
  local probe_output probe_status
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
  # Keep the checker protocol on stdout; Compose progress and driver stderr are never forwarded.
  # The sentinel preserves trailing newlines so extra output cannot resemble a valid verdict.
  probe_status=0
  probe_output="$(
    IMAGE="${TARGET_IMAGE_ID}" COMPOSE_PROJECT="${COMPOSE_PROJECT}" COMPOSE_IGNORE_ORPHANS=true \
      POSTGRES_USER="${POSTGRES_USER:-app}" POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
      POSTGRES_DB="${POSTGRES_DB:-app}" \
      docker compose -p "${COMPOSE_PROJECT}" -f "${PROBE_DIR}/probe.yml" \
        run --rm --no-deps -T bootstrap python - \
        < "${SCRIPT_DIR}/check-rollback-revision.py" 2>/dev/null
    status=$?
    printf '.'
    exit "${status}"
  )" || probe_status=$?
  if [[ "${probe_status}" != 0 ]]; then
    case "${probe_output}" in
      "rollback revision check: refused: target must have exactly one head"$'\n.'|\
      "rollback revision check: refused: database must have exactly one tracked revision"$'\n.'|\
      "rollback revision check: refused: database revision is unknown"$'\n.'|\
      "rollback revision check: refused: database revision is not an ancestor of target head"$'\n.'|\
      "rollback revision check: refused: cannot inspect target graph or database"$'\n.')
        refuse_rollback "${probe_output%$'\n.'}" ;;
      *) refuse_rollback "target revision probe failed (exit ${probe_status})" ;;
    esac
  fi
  [[ "${probe_output}" == "rollback revision check: compatible"$'\n.' ]] || \
    refuse_rollback "target revision probe returned an unexpected result"
}

diagnostics_failed() {
  echo "rollback completion failed: $1 environment key diagnostics failed" >&2
  echo "The healthy target release is recorded. Inspect service health and retry only the standalone diagnostics in SECRETS.md; do not rerun rollback.sh or rollback.yml." >&2
  exit 1
}

print_environment_keys() {
  local service keys output python_source key_list_pattern
  key_list_pattern='^\[("[A-Za-z_][A-Za-z0-9_]*"(, "[A-Za-z_][A-Za-z0-9_]*")*)?\]$'
  python_source="$(cat <<'PYTHON'
import json
import os
import re
print(json.dumps(sorted(name for name in os.environ.keys() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))))
PYTHON
  )"
  for service in api worker webhook-worker; do
    # Capture stdout only and retain its final newline; stderr is never part of the protocol.
    if ! output="$(
      "${COMPOSE[@]}" exec -T "${service}" python -c "${python_source}" 2>/dev/null
      status=$?
      printf '.'
      exit "${status}"
    )"; then
      diagnostics_failed "${service}"
    fi
    [[ "${output}" == *$'\n.' ]] || diagnostics_failed "${service}"
    keys="${output%$'\n.'}"
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
  if [[ "${NO_PREVIOUS_RELEASE}" == "stop" ]]; then
    # Stopping is the whole rollback here: a failed down must not report success while the failed
    # release keeps running, and an unreadable project counts as not stopped.
    local remaining
    if ! docker compose -p "${COMPOSE_PROJECT}" down --remove-orphans \
      || ! remaining="$(docker compose -p "${COMPOSE_PROJECT}" ps -aq)" || [[ -n "${remaining}" ]]; then
      echo "failed to stop compose project ${COMPOSE_PROJECT}; the failed release may still run" >&2
      return 1
    fi
    docker rm -f "${BOOTSTRAP_NAME}" >/dev/null 2>&1 || true
    rm -f "${STATE_FILE}" "${PREVIOUS_FILE}"
    echo "stopped compose project ${COMPOSE_PROJECT}; no bootstrap container (NO_PREVIOUS_RELEASE=stop)"
    return 0
  fi
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
  NO_PREVIOUS_RELEASE="${NO_PREVIOUS_RELEASE:-$(read_compose_env_var NO_PREVIOUS_RELEASE "${ENV_FILE}")}"
fi

NO_PREVIOUS_RELEASE="${NO_PREVIOUS_RELEASE:-bootstrap}"
if [[ "${NO_PREVIOUS_RELEASE}" != "bootstrap" && "${NO_PREVIOUS_RELEASE}" != "stop" ]]; then
  echo "NO_PREVIOUS_RELEASE must be bootstrap or stop, got: ${NO_PREVIOUS_RELEASE}" >&2
  exit 1
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
  echo "no previous release; NO_PREVIOUS_RELEASE=${NO_PREVIOUS_RELEASE}" >&2
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
  if ! registry_reason="$(printf '%s\n' "${GHCR_TOKEN}" | run_registry_command docker login ghcr.io -u "${GHCR_USER:-github}" --password-stdin)"; then
    refuse_rollback "registry login failed${registry_reason:+ (${registry_reason})}"
  fi
  unset GHCR_TOKEN
fi

if ! registry_reason="$(run_registry_command docker pull "${IMAGE}")"; then
  refuse_rollback "target image pull failed${registry_reason:+ (${registry_reason})}"
fi
resolve_target_identity refuse_rollback
check_target_revision
gitlab_bundle_status=0
gitlab_bundle="$(gitlab_bundle_from_env_files "${APP_DIR}" 2>/dev/null)" || gitlab_bundle_status=$?
if [[ "${gitlab_bundle_status}" != 0 ]]; then
  refuse_rollback "$(gitlab_deployment_failure_reason "${gitlab_bundle_status}")"
fi
if ! validate_gitlab_deployment "${TARGET_IMAGE_ID}" "${gitlab_bundle}" literal-gitlab >/dev/null 2>&1; then
  refuse_rollback "GitLab configuration_invalid"
fi

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
  "${REDIS_PASSWORD}" \
  "${NO_PREVIOUS_RELEASE}"

# App secrets are not tied to an image: the files of the last deploy stay as they are.
ensure_app_env_files "${APP_DIR}"

IMAGE="${TARGET_IMAGE_ID}" "${COMPOSE[@]}" up --pull missing -d --remove-orphans --wait --wait-timeout 180

# Shared root account: drop credentials after pinned-image startup with --pull missing.
logout_registry

# Healthy up has changed the running release; persist its identity before fallible diagnostics.
if [[ "${ROLLBACK_MODE}" == "manual" && -f "${STATE_FILE}" ]]; then
  cp "${STATE_FILE}" "${PREVIOUS_FILE}"
fi

{
  echo "current_image=${RELEASE_REF}"
  echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "rolled_back=true"
} > "${STATE_FILE}"

print_environment_keys

echo "rolled back to ${RELEASE_REF}"
