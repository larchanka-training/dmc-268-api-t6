#!/usr/bin/env bash

# Helpers for Docker Compose .env files. Values are written so Compose reads them
# literally ($ escaped as $$). Never source these files as shell code.

# Application secrets go to per-container env files instead of compose interpolation: Compose turns
# an unset ${VAR} into an empty string, and app/bootstrap/reviews_api.py checks GITHUB_WEBHOOK_SECRET
# with `is not None`. A key that is not set in GitHub is simply absent from the file and from the
# container environment.
APP_ENV_FILE_NAME="app.env"  # GitHub App credentials: worker and webhook-worker (next PR of #35)
API_ENV_FILE_NAME="api.env"  # api only: webhook signature and user authorization
APP_ENV_KEYS=(GITHUB_APP_ID GITHUB_APP_PRIVATE_KEY)
API_ENV_KEYS=(GITHUB_WEBHOOK_SECRET GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET AUTH_JWT_PRIVATE_KEY AUTH_JWT_PUBLIC_KEY)

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
# Everything is validated before either file is replaced; the body runs in a subshell so that the
# EXIT trap removes the temporary files on any failure.
write_app_env_files() (
  dir="$1"
  bundle="$2"
  app_tmp=""
  api_tmp=""
  trap 'rm -f "${app_tmp}" "${api_tmp}"' EXIT
  if ! decoded="$(printf '%s' "${bundle}" | base64 -d 2>/dev/null)"; then
    echo "app secrets bundle is not valid base64" >&2
    exit 1
  fi
  umask 077
  app_tmp="$(mktemp "${dir}/.${APP_ENV_FILE_NAME}.XXXXXX")"
  api_tmp="$(mktemp "${dir}/.${API_ENV_FILE_NAME}.XXXXXX")"
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
    if [[ " ${APP_ENV_KEYS[*]} " == *" ${name} "* ]]; then
      printf "%s='%s'\n" "${name}" "${value}" >> "${app_tmp}"
    elif [[ " ${API_ENV_KEYS[*]} " == *" ${name} "* ]]; then
      printf "%s='%s'\n" "${name}" "${value}" >> "${api_tmp}"
    else
      echo "app secret ${name} is not in the allowlist of env-file.sh" >&2
      exit 1
    fi
  done <<< "${decoded}"
  chmod 600 "${app_tmp}" "${api_tmp}"
  mv -f "${app_tmp}" "${dir}/${APP_ENV_FILE_NAME}"
  mv -f "${api_tmp}" "${dir}/${API_ENV_FILE_NAME}"
)

# Compose requires every env_file to exist; a host deployed without a bundle gets empty files.
ensure_app_env_files() {
  local dir="$1" name
  umask 077
  for name in "${APP_ENV_FILE_NAME}" "${API_ENV_FILE_NAME}"; do
    [[ -f "${dir}/${name}" ]] || : > "${dir}/${name}"
    chmod 600 "${dir}/${name}"
  done
}
