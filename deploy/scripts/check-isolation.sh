#!/usr/bin/env bash
# Read-only proof that the stores of two compose projects on one host cannot reach each other
# (#308, AC1): the default networks of the projects share no container, no container of one project
# sits on the other's default network, every postgres, rabbitmq and redis sits on its own project's
# default network only, and none publishes a host port. Containers of other projects (monitoring)
# may join either network. Prints a markdown table (container, networks, host ports, verdict) and
# an "isolation:" line. Exit 0: isolated; 1: a violation; 2: usage, a project without a running
# store, or a failed docker query. Only docker compose ps, docker network inspect and docker inspect
# with a template that reads names, the compose project label, networks and ports: no exec, no
# environment, no secrets.
set -euo pipefail

STORES=(postgres rabbitmq redis)
# The IDs of a network's members; the name, compose project, networks and host port bindings of a
# container (one label, never the environment).
# shellcheck disable=SC2016  # Go template, not shell
NETWORK_FORMAT='{{range $id, $container := .Containers}}{{println $id}}{{end}}'
# shellcheck disable=SC2016  # Go template, not shell
INSPECT_FORMAT='{{.Name}}{{"\t"}}{{index .Config.Labels "com.docker.compose.project"}}{{"\t"}}{{range $name, $endpoint := .NetworkSettings.Networks}}{{$name}} {{end}}{{"\t"}}{{range $port, $bindings := .NetworkSettings.Ports}}{{range $bindings}}{{.HostIp}}:{{.HostPort}}->{{$port}} {{end}}{{end}}'

if [[ $# -ne 2 || -z "$1" || -z "$2" || "$1" == "$2" ]]; then
  echo "usage: check-isolation.sh <project> <other-project>" >&2
  exit 2
fi
PROJECTS=("$1" "$2")
FIRST_NETWORK="$1_default"
SECOND_NETWORK="$2_default"

# `docker compose -p NAME ps` reads only the project's containers; from / no compose file or .env
# of the caller's directory can be picked up instead.
cd /

# A failed query is no verdict: exit 2 (from the command substitution), never the violation code 1.
query() {
  if ! docker "$@"; then
    echo "check-isolation: a docker query failed; isolation not checked" >&2
    exit 2
  fi
}

# join SEPARATOR ITEM...: the items joined, or "none".
join() {
  local separator="$1" joined item
  shift
  if [[ $# -eq 0 ]]; then
    echo "none"
    return
  fi
  joined="$1"
  shift
  for item in "$@"; do
    joined+="${separator}${item}"
  done
  echo "${joined}"
}

declare -A OWN_NETWORK=()
STORE_IDS=()
for project in "${PROJECTS[@]}"; do
  for store in "${STORES[@]}"; do
    ids="$(query compose -p "${project}" ps -q "${store}")"
    if [[ -z "${ids}" ]]; then
      echo "check-isolation: no running ${store} container in compose project ${project}" >&2
      exit 2
    fi
    while IFS= read -r id; do
      if [[ -n "${id}" ]]; then
        STORE_IDS+=("${id}")
        OWN_NETWORK["${id}"]="${project}_default"
      fi
    done <<< "${ids}"
  done
done

# Every other member of either default network: it gets a row only if it breaks a rule.
declare -A SEEN=()
MEMBER_IDS=()
for network in "${FIRST_NETWORK}" "${SECOND_NETWORK}"; do
  members="$(query network inspect --format "${NETWORK_FORMAT}" "${network}")"
  while IFS= read -r id; do
    if [[ -n "${id}" && -z "${OWN_NETWORK[${id}]:-}" && -z "${SEEN[${id}]:-}" ]]; then
      SEEN["${id}"]=1
      MEMBER_IDS+=("${id}")
    fi
  done <<< "${members}"
done

ROWS=()
VIOLATIONS=0
add_row() {
  local id="$1" line name project rest own network verdict networks=() bindings=() reasons=()
  line="$(query inspect --format "${INSPECT_FORMAT}" "${id}")"
  name="${line%%$'\t'*}"
  rest="${line#*$'\t'}"
  project="${rest%%$'\t'*}"
  rest="${rest#*$'\t'}"
  read -r -a networks <<< "${rest%%$'\t'*}"
  read -r -a bindings <<< "${rest#*$'\t'}"
  own="${OWN_NETWORK[${id}]:-}"
  if [[ -z "${own}" ]]; then
    if [[ " ${networks[*]} " == *" ${FIRST_NETWORK} "* && " ${networks[*]} " == *" ${SECOND_NETWORK} "* ]]; then
      reasons+=("on both ${FIRST_NETWORK} and ${SECOND_NETWORK}")
    fi
    if [[ "${project}" == "${PROJECTS[1]}" && " ${networks[*]} " == *" ${FIRST_NETWORK} "* ]]; then
      reasons+=("compose project ${project} on ${FIRST_NETWORK}")
    fi
    if [[ "${project}" == "${PROJECTS[0]}" && " ${networks[*]} " == *" ${SECOND_NETWORK} "* ]]; then
      reasons+=("compose project ${project} on ${SECOND_NETWORK}")
    fi
    if [[ ${#reasons[@]} -eq 0 ]]; then
      return
    fi
  else
    if [[ " ${networks[*]} " != *" ${own} "* ]]; then
      reasons+=("not on ${own}")
    fi
    for network in "${networks[@]}"; do
      if [[ "${network}" != "${own}" ]]; then
        reasons+=("on ${network}")
      fi
    done
    if [[ ${#bindings[@]} -gt 0 ]]; then
      reasons+=("publishes host ports")
    fi
  fi
  verdict="ok"
  if [[ ${#reasons[@]} -gt 0 ]]; then
    verdict="FAIL: $(join "; " "${reasons[@]}")"
    VIOLATIONS=$((VIOLATIONS + 1))
  fi
  ROWS+=("| ${name#/} | $(join ", " "${networks[@]}") | $(join ", " "${bindings[@]}") | ${verdict} |")
}

for id in "${STORE_IDS[@]}" "${MEMBER_IDS[@]}"; do
  add_row "${id}"
done

echo "| container | networks | host ports | verdict |"
echo "|---|---|---|---|"
printf '%s\n' "${ROWS[@]}"
echo
if [[ ${VIOLATIONS} -gt 0 ]]; then
  echo "isolation: FAIL (${VIOLATIONS} container(s) break the isolation of ${FIRST_NETWORK} and ${SECOND_NETWORK})"
  exit 1
fi
echo "isolation: pass (${#STORE_IDS[@]} stores on their own network only, no host ports; ${FIRST_NETWORK} and ${SECOND_NETWORK} share no container and hold none of the other project)"
