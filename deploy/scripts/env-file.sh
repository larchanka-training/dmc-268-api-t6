#!/usr/bin/env bash

# Helpers for Docker Compose .env files. Values are written so Compose reads them
# literally ($ escaped as $$). Never source these files as shell code.

read_compose_env_var() {
  local key="$1" file="$2"
  [[ -f "${file}" ]] || return 1
  awk -F= -v k="${key}" '
    $0 !~ /^[[:space:]]*#/ && $1 == k {
      sub(/^[^=]*=/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "")
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
  local file="$1" image="$2" pg_user="$3" pg_password="$4" pg_db="$5"
  umask 077
  {
    printf 'IMAGE=%s\n' "$(escape_compose_value "${image}")"
    printf 'POSTGRES_USER=%s\n' "$(escape_compose_value "${pg_user}")"
    printf 'POSTGRES_PASSWORD=%s\n' "$(escape_compose_value "${pg_password}")"
    printf 'POSTGRES_DB=%s\n' "$(escape_compose_value "${pg_db}")"
  } > "${file}"
  chmod 600 "${file}"
}
