#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Static checks of the monitoring stack (deploy/monitoring, docs/MONITORING.md): the CI job
# monitoring-validate runs this script, and the same command works locally with Docker.
# Every tool comes from the image the host runs, read from deploy/monitoring/compose.yml.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
MONITORING=deploy/monitoring

image_of() {
  local images
  images="$(sed -n "s|^    image: \($1:.*\)$|\1|p" "${MONITORING}/compose.yml")"
  if [[ -z "${images}" || "${images}" == *$'\n'* ]]; then
    echo "${MONITORING}/compose.yml must name exactly one $1 image" >&2
    return 1
  fi
  printf '%s' "${images}"
}
prometheus_image="$(image_of prom/prometheus)"
alertmanager_image="$(image_of prom/alertmanager)"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
# The containers run as nobody: the rendered files must be readable by others.
chmod 755 "${tmp}"

echo "== promtool check config (prometheus.yml and rules)"
docker run --rm --entrypoint promtool -v "${PWD}/${MONITORING}/prometheus:/etc/prometheus:ro" \
  "${prometheus_image}" check config /etc/prometheus/prometheus.yml

echo "== promtool test rules"
docker run --rm --entrypoint promtool -v "${PWD}:/w:ro" -w /w \
  "${prometheus_image}" test rules tests/monitoring/alerts.test.yml

echo "== amtool check-config (Telegram and no receiver)"
# shellcheck source=../deploy/monitoring/alertmanager-config.sh
source "${MONITORING}/alertmanager-config.sh"
for chat_id in -1001234567890 ""; do
  dir="${tmp}/alertmanager-${chat_id:-none}"
  mkdir -p "${dir}/secrets"
  cp -R "${MONITORING}/alertmanager/templates" "${dir}/"
  printf '123456:ci-placeholder' > "${dir}/secrets/telegram_bot_token"
  render_alertmanager_config "${dir}/alertmanager.yml" "${chat_id}"
  chmod -R a+rX "${dir}"
  docker run --rm --entrypoint amtool -v "${dir}:/etc/alertmanager:ro" \
    "${alertmanager_image}" check-config /etc/alertmanager/alertmanager.yml
done
docker run --rm --entrypoint amtool -v "${PWD}/${MONITORING}/alertmanager/templates:/t:ro" \
  "${alertmanager_image}" template render --template.glob='/t/*.tmpl' \
  --template.text='{{ template "dmc.telegram.message" . }}' >/dev/null

echo "== docker compose config (edge and ports)"
for mode in edge ports; do
  API_NETWORK=ci_default GRAFANA_ADMIN_PASSWORD=ci GRAFANA_DB_PASSWORD=ci PG_EXPORTER_PASSWORD=ci \
    GRAFANA_ALIAS=grafana-staging \
    docker compose -p dmc-268-monitoring -f "${MONITORING}/compose.yml" \
    -f "${MONITORING}/compose.${mode}.yml" config -q
done

echo "== dashboards are JSON"
for dashboard in "${MONITORING}"/grafana/dashboards/*.json; do
  python3 -m json.tool "${dashboard}" >/dev/null
done

echo "monitoring configuration is valid"
