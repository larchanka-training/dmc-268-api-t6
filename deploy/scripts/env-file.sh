#!/usr/bin/env bash

# Helpers for Docker Compose .env files. Values are written so Compose reads them
# literally ($ escaped as $$). Never source these files as shell code.

# Application secrets go to per-container env files instead of compose interpolation: Compose turns
# an unset ${VAR} into an empty string, and app/bootstrap/reviews_api.py checks GITHUB_WEBHOOK_SECRET
# with `is not None`. A key that is not set in GitHub is simply absent from the file and from the
# container environment.
# One file per set of recipients (docs/SECRETS.md); a container reads only the files of its role.
APP_ENV_FILE_NAME="app.env"  # GitHub App credentials: worker and webhook-worker
GITLAB_ENV_FILE_NAME="gitlab.env"  # both workers and trusted operator only
API_ENV_FILE_NAME="api.env"  # api only: webhook signature and user authorization
WORKER_ENV_FILE_NAME="worker.env"  # worker only: LLM gateway and portal links
WEBHOOK_WORKER_ENV_FILE_NAME="webhook-worker.env"  # webhook-worker only: bot login
APP_ENV_KEYS=(GITHUB_APP_ID GITHUB_APP_PRIVATE_KEY)
GITLAB_ENV_KEYS=(GITLAB_CREDENTIAL_KEYS GITLAB_ACTIVE_KEY_ID GITLAB_ALLOWED_ORIGINS GITLAB_PRIVATE_ORIGINS)
API_ENV_KEYS=(GITLAB_WEBHOOK_INTAKE_ENABLED GITHUB_WEBHOOK_SECRET GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET AUTH_JWT_PRIVATE_KEY AUTH_JWT_PUBLIC_KEY)
WORKER_ENV_KEYS=(LLM_API_KEYS LLM_BASE_URL LLM_MODEL LLM_FALLBACK_MODEL LLM_VERIFIER_MODEL LLM_VERIFIER_FALLBACK_MODEL PORTAL_URL)
WEBHOOK_WORKER_ENV_KEYS=(GITHUB_APP_BOT_LOGIN)
ENV_FILE_NAMES=("${GITLAB_ENV_FILE_NAME}" "${APP_ENV_FILE_NAME}" "${API_ENV_FILE_NAME}" "${WORKER_ENV_FILE_NAME}" "${WEBHOOK_WORKER_ENV_FILE_NAME}")

# The env file of an allowlisted key; fails for any other name.
env_file_for_key() {
  local name="$1"
  if [[ " ${APP_ENV_KEYS[*]} " == *" ${name} "* ]]; then
    printf '%s' "${APP_ENV_FILE_NAME}"
  elif [[ " ${GITLAB_ENV_KEYS[*]} " == *" ${name} "* ]]; then
    printf '%s' "${GITLAB_ENV_FILE_NAME}"
  elif [[ " ${API_ENV_KEYS[*]} " == *" ${name} "* ]]; then
    printf '%s' "${API_ENV_FILE_NAME}"
  elif [[ " ${WORKER_ENV_KEYS[*]} " == *" ${name} "* ]]; then
    printf '%s' "${WORKER_ENV_FILE_NAME}"
  elif [[ " ${WEBHOOK_WORKER_ENV_KEYS[*]} " == *" ${name} "* ]]; then
    printf '%s' "${WEBHOOK_WORKER_ENV_FILE_NAME}"
  else
    return 1
  fi
}

read_compose_env_var() {
  local key="$1" file="$2"
  [[ -f "${file}" ]] || return 1
  awk -F= -v k="${key}" '
    $0 !~ /^[[:space:]]*#/ && $1 == k {
      sub(/^[^=]*=/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "")
      gsub(/\$\$/, "$")
      print
      exit
    }
  ' "${file}"
}

escape_compose_value() {
  local value="$1"
  value="${value//\$/\$\$}"
  printf '%s' "${value}"
}

write_compose_env_file() {
  local file="$1" image="$2" pg_user="$3" pg_password="$4" pg_db="$5" deploy_mode="$6" edge_alias="$7"
  local rabbitmq_user="$8" rabbitmq_password="$9" redis_password="${10}"
  umask 077
  {
    printf 'IMAGE=%s\n' "$(escape_compose_value "${image}")"
    printf 'POSTGRES_USER=%s\n' "$(escape_compose_value "${pg_user}")"
    printf 'POSTGRES_PASSWORD=%s\n' "$(escape_compose_value "${pg_password}")"
    printf 'POSTGRES_DB=%s\n' "$(escape_compose_value "${pg_db}")"
    printf 'DEPLOY_MODE=%s\n' "$(escape_compose_value "${deploy_mode}")"
    printf 'EDGE_ALIAS=%s\n' "$(escape_compose_value "${edge_alias}")"
    printf 'RABBITMQ_USER=%s\n' "$(escape_compose_value "${rabbitmq_user}")"
    printf 'RABBITMQ_PASSWORD=%s\n' "$(escape_compose_value "${rabbitmq_password}")"
    printf 'REDIS_PASSWORD=%s\n' "$(escape_compose_value "${redis_password}")"
  } > "${file}"
  chmod 600 "${file}"
}

# The bundle is base64 of "NAME=<base64 of value>" lines, one per secret that is set; CI builds it
# so multi-line PEM values cross the SSH action and the shell as one opaque line. Values are written
# single-quoted: Compose reads them across lines without interpolation, and a backslash stays a
# backslash except right before a quote. So a value may contain neither ' nor a trailing \.
# Command substitution drops trailing newlines of a value, which PEM parsing ignores.
# Everything is validated before any file is replaced; the body runs in a subshell so that the
# EXIT trap removes the temporary files on any failure. Temporary file i belongs to ENV_FILE_NAMES[i].
write_app_env_files() (
  set -o pipefail
  dir="$1"
  bundle="$2"
  tmp_files=()
  trap 'rm -f ${tmp_files[@]+"${tmp_files[@]}"}' EXIT
  if ! decoded="$(printf '%s' "${bundle}" | base64 -d 2>/dev/null)"; then
    echo "app secrets bundle is not valid base64" >&2
    exit 1
  fi
  umask 077
  for file_name in "${ENV_FILE_NAMES[@]}"; do
    temporary_file="$(mktemp "${dir}/.${file_name}.XXXXXX")" || exit 1
    [[ -n "${temporary_file}" && -f "${temporary_file}" ]] || exit 1
    tmp_files+=("${temporary_file}")
  done
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    name="${line%%=*}"
    encoded="${line#*=}"
    # Checked before the name is printed anywhere: a malformed line could carry part of a value.
    if [[ ! "${name}" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
      echo "app secrets bundle has a malformed line" >&2
      exit 1
    fi
    if ! value="$(printf '%s' "${encoded}" | base64 -d 2>/dev/null)"; then
      echo "app secret ${name}: value is not valid base64" >&2
      exit 1
    fi
    [[ -n "${value}" ]] || continue
    if [[ "${value}" == *"'"* ]]; then
      echo "app secret ${name}: single quotes are not supported in values" >&2
      exit 1
    fi
    if [[ "${value}" == *\\ ]]; then
      echo "app secret ${name}: a trailing backslash is not supported in values" >&2
      exit 1
    fi
    if ! file_name="$(env_file_for_key "${name}")"; then
      echo "app secret ${name} is not in the allowlist of env-file.sh" >&2
      exit 1
    fi
    for i in "${!ENV_FILE_NAMES[@]}"; do
      if [[ "${ENV_FILE_NAMES[i]}" == "${file_name}" ]]; then
        printf "%s='%s'\n" "${name}" "${value}" >> "${tmp_files[i]}" || exit 1
      fi
    done
  done <<< "${decoded}"
  chmod 600 "${tmp_files[@]}" || exit 1
  for i in "${!ENV_FILE_NAMES[@]}"; do
    mv -f "${tmp_files[i]}" "${dir}/${ENV_FILE_NAMES[i]}" || exit 1
  done
)

# Compose requires every env_file to exist; a host deployed without a bundle gets empty files.
ensure_app_env_files() {
  local dir="$1" name
  umask 077
  for name in "${ENV_FILE_NAMES[@]}"; do
    if [[ ! -f "${dir}/${name}" ]]; then
      : > "${dir}/${name}" || return 1
    fi
    chmod 600 "${dir}/${name}" || return 1
  done
}

# Validate in the target image, not in the runner/host interpreter. The opaque input preserves
# multi-line JSON without Docker's raw env-file parser treating it as several variable entries.
# CI normalizes the full application bundle; host input is already generated GitLab records.
# Only GitLab names enter the preflight container; all temporary recipient files stay owner-only.
validate_gitlab_deployment() (
  set -euo pipefail
  image="$1"
  input_bundle="$2"
  input_kind="${3:-application}"
  umask 077
  preflight_dir="$(mktemp -d "${TMPDIR:-/tmp}/gitlab-validation.XXXXXX")" || exit 1
  [[ -n "${preflight_dir}" && -d "${preflight_dir}" ]] || exit 1
  trap 'rm -rf "${preflight_dir}"' EXIT
  write_app_env_files "${preflight_dir}" "${input_bundle}" || exit 1
  case "${input_kind}" in
    application)
      gitlab_bundle="$(gitlab_bundle_from_env_files "${preflight_dir}")" || exit 1
      ;;
    literal-gitlab)
      # Host input was read from the files being admitted: preserve its exact values.
      gitlab_bundle="${input_bundle}"
      ;;
    *) exit 1 ;;
  esac
  gitlab_names=("${GITLAB_ENV_KEYS[@]}")
  for name in "${API_ENV_KEYS[@]}"; do
    if [[ "${name}" == GITLAB_* ]]; then
      gitlab_names+=("${name}")
    fi
  done
  printf 'GITLAB_DEPLOYMENT_BUNDLE=%s\n' "${gitlab_bundle}" > "${preflight_dir}/preflight.env" || exit 1
  printf 'GITLAB_DEPLOYMENT_KEYS=%s\n' "${gitlab_names[*]}" >> "${preflight_dir}/preflight.env" || exit 1
  chmod 600 "${preflight_dir}/preflight.env" || exit 1
  validator="$(cat <<'PYTHON'
import base64
import binascii
import os
import sys

try:
    encoded = os.environ.pop("GITLAB_DEPLOYMENT_BUNDLE")
    raw = base64.b64decode(encoded, validate=True).decode("utf-8")
    allowed = set(os.environ.pop("GITLAB_DEPLOYMENT_KEYS").split())
    values = {}
    for line in raw.splitlines():
        name, separator, value = line.partition("=")
        if not separator or name not in allowed or name in values:
            raise ValueError
        values[name] = base64.b64decode(value, validate=True).decode("utf-8")
    # Truly absent GitLab configuration preserves old GitHub-only images.
    if values:
        try:
            from app.bootstrap.gitlab_deployment import validate_environment
        except ModuleNotFoundError as error:
            if error.name != "app.bootstrap.gitlab_deployment":
                raise
            from app.modules.integrations.gitlab.infrastructure.settings import (
                GitLabApiSettings, GitLabRuntimeSettings,
            )
            runtime = GitLabRuntimeSettings.optional_from_environment({
                **values, "DATABASE_URL": "postgresql+psycopg://localhost/deployment_preflight",
            })
            admission = GitLabApiSettings.from_environment(values)
            if admission.intake_enabled and runtime is None:
                raise ValueError
        else:
            validate_environment(values)
except (ValueError, TypeError, UnicodeError, KeyError, AttributeError, ImportError, binascii.Error):
    print("configuration_invalid", file=sys.stderr)
    raise SystemExit(2)
PYTHON
  )" || exit 1
  docker run --rm --network none --env-file "${preflight_dir}/preflight.env" \
    "${image}" python -c "${validator}"
)

# Resolve one pulled target for both admission and startup; the caller owns refusal diagnostics.
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
  local identity_refusal="$1"
  local repository_pattern digest_pattern candidate_pattern tag_pattern repository candidates candidate candidate_id matches
  repository_pattern='[a-z0-9][a-z0-9.-]*(:[0-9]+)?/[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*'
  digest_pattern="^(${repository_pattern})(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}$"
  candidate_pattern="^(${repository_pattern})@sha256:[0-9a-f]{64}$"
  tag_pattern="^(${repository_pattern})(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$"
  TARGET_IMAGE_ID="$(inspect_image_id "${IMAGE}")" || "${identity_refusal}" "target image identity is unavailable"
  if [[ "${IMAGE}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    [[ "${TARGET_IMAGE_ID}" == "${IMAGE}" ]] || "${identity_refusal}" "target image identity is inconsistent"
    # Verifier-only: its scoped wrapper validates a prebuilt local ID at the pull boundary.
    # Operators use registry refs; ordinary docker pull does not accept a raw local image ID.
    RELEASE_REF="${IMAGE}"
  elif [[ "${IMAGE}" =~ ${digest_pattern} ]]; then
    # Preserve the supplied immutable ref for forward promotion's state equality guard.
    RELEASE_REF="${IMAGE}"
  elif [[ "${IMAGE}" =~ ${tag_pattern} ]]; then
    repository="${BASH_REMATCH[1]}"
    if ! candidates="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${TARGET_IMAGE_ID}" 2>/dev/null)"; then
      "${identity_refusal}" "target release digest is unavailable"
    fi
    matches=0
    while IFS= read -r candidate; do
      [[ "${candidate}" =~ ${candidate_pattern} ]] || "${identity_refusal}" "target release digest is unavailable"
      if [[ "${BASH_REMATCH[1]}" == "${repository}" ]]; then
        RELEASE_REF="${candidate}"
        matches=$((matches + 1))
      fi
    done <<< "${candidates}"
    [[ "${matches}" == 1 ]] || "${identity_refusal}" "target release digest is unavailable"
    candidate_id="$(inspect_image_id "${RELEASE_REF}")" || "${identity_refusal}" "target release digest is unavailable"
    [[ "${candidate_id}" == "${TARGET_IMAGE_ID}" ]] || "${identity_refusal}" "target release digest is inconsistent"
  else
    "${identity_refusal}" "target release reference is unsupported"
  fi
}

# Parse only the generated single-quoted format as data; never source credential files.
# The whole record regex permits multiline values and forbids unrecognized shell syntax.
gitlab_bundle_from_env_files() (
  set -euo pipefail
  directory="$1"
  records=""
  entry_pattern="^([A-Z][A-Z0-9_]*)='([^']*)'"$'\n'
  for file_name in "${GITLAB_ENV_FILE_NAME}" "${API_ENV_FILE_NAME}"; do
    [[ -f "${directory}/${file_name}" ]] || continue
    if [[ "${file_name}" == "${GITLAB_ENV_FILE_NAME}" ]]; then
      read_failure=21
      format_failure=22
    else
      read_failure=23
      format_failure=24
    fi
    content="$(cat "${directory}/${file_name}" 2>/dev/null && printf '.')" || exit "${read_failure}"
    content="${content%.}"
    while [[ -n "${content}" ]]; do
      if [[ ! "${content}" =~ ${entry_pattern} ]]; then
        exit "${format_failure}"
      fi
      entry="${BASH_REMATCH[0]}"
      name="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      content="${content:${#entry}}"
      if ! expected_file="$(env_file_for_key "${name}")" || [[ "${expected_file}" != "${file_name}" ]]; then
        exit "${format_failure}"
      fi
      if [[ "${name}" == GITLAB_* ]]; then
        encoded_value="$(printf '%s' "${value}" | base64 | tr -d '\n')" || exit 1
        records+="${name}=${encoded_value}"$'\n'
      fi
    done
  done
  printf '%s' "${records}" | base64 | tr -d '\n'
)


# Only trusted helper statuses select diagnostics; never include file contents or raw CLI errors.
gitlab_deployment_failure_reason() {
  case "$1" in
    21) printf '%s' 'GitLab configuration_invalid (gitlab.env: read_failed)' ;;
    22) printf '%s' 'GitLab configuration_invalid (gitlab.env: format_invalid)' ;;
    23) printf '%s' 'GitLab configuration_invalid (api.env: read_failed)' ;;
    24) printf '%s' 'GitLab configuration_invalid (api.env: format_invalid)' ;;
    *) printf '%s' 'GitLab configuration_invalid' ;;
  esac
}


# Capture untrusted registry stderr privately; only static categories leave this subshell.
# Explicit guards are required because callers use this helper in conditional contexts.
run_registry_command() (
  set -o pipefail
  umask 077
  [[ -n "${DOCKER_CONFIG:-}" && -d "${DOCKER_CONFIG}" ]] || exit 1
  capture_file="$(mktemp "${DOCKER_CONFIG}/registry-error.XXXXXX" 2>/dev/null)" || exit 1
  [[ -n "${capture_file}" && -f "${capture_file}" ]] || exit 1
  trap 'rm -f "${capture_file}" 2>/dev/null || exit 1' EXIT
  chmod 600 "${capture_file}" 2>/dev/null || exit 1
  printf '%s' 'capture-ready' 2>/dev/null > "${capture_file}" || exit 1
  capture_probe="$(cat "${capture_file}" 2>/dev/null)" || exit 1
  [[ "${capture_probe}" == capture-ready ]] || exit 1
  printf '' 2>/dev/null > "${capture_file}" || exit 1
  registry_status=0
  { "$@" >/dev/null 2>"${capture_file}"; } 2>/dev/null || registry_status=$?
  [[ "${registry_status}" != 0 ]] || exit 0
  registry_error="$(cat "${capture_file}" 2>/dev/null)" || exit 1
  reason=""
  case "${registry_error}" in
    *unauthorized*|*"authentication required"*|*"denied:"*|*"login denied"*|*"pull access denied"*)
      reason='authentication_denied: check registry credentials and package access'
      ;;
    *"manifest unknown"*|*manifest*"not found"*)
      reason='manifest_missing: verify the reviewed image reference'
      ;;
    *"no space left on device"*|*"No space left on device"*|*"disk quota exceeded"*)
      reason='insufficient_storage: check host disk space'
      ;;
    *"network is unreachable"*|*"no route to host"*|*"connection refused"*|*"i/o timeout"*|\
    *"context deadline exceeded"*|*"TLS handshake timeout"*|*"no such host"*|*"connection reset"*)
      reason='network_unavailable: check registry connectivity'
      ;;
  esac
  printf '%s' "${reason}" || exit 1
  exit 1
)
