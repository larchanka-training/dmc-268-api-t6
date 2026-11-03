"""Compose, edge proxy and workflow contracts of the monitoring stack (deploy/monitoring)."""

from __future__ import annotations

import re

from tests.monitoring_deploy_host import MONITORING, REPO_ROOT, _job, _read

CADDYFILE = REPO_ROOT / "deploy" / "edge" / "Caddyfile"


def _service_blocks(compose: str) -> dict[str, str]:
    services = compose[compose.index("services:\n") :]
    services = services[: services.index("\nvolumes:\n")]
    names = list(re.finditer(r"^  ([a-z-]+):$", services, re.M))
    return {
        match.group(1): services[
            match.start() : (names[i + 1].start() if i + 1 < len(names) else None)
        ]
        for i, match in enumerate(names)
    }


# --- compose -------------------------------------------------------------------------------


def test_monitoring_images_are_pinned_by_digest() -> None:
    images = re.findall(r"^    image: (\S+)$", _read(MONITORING / "compose.yml"), re.M)

    assert len(images) == 11
    for image in images:
        assert re.fullmatch(r"[a-z0-9./_-]+:v?[\d.]+(-alpine)?@sha256:[0-9a-f]{64}", image), image


def test_backup_uses_the_postgres_image_of_staging() -> None:
    # pg_dump must match the server version: the same image, by the same digest.
    staging = re.search(
        r"^    image: (postgres:\S+)$", _read(REPO_ROOT / "deploy/compose/staging.yml"), re.M
    )
    backup = _service_blocks(_read(MONITORING / "compose.yml"))["pg-backup"]

    assert staging is not None
    assert f"    image: {staging.group(1)}\n" in backup


def test_every_monitoring_service_has_a_memory_limit() -> None:
    for name, block in _service_blocks(_read(MONITORING / "compose.yml")).items():
        assert re.search(r"^    mem_limit: \d+m$", block, re.M), name


def test_monitoring_publishes_no_port_except_loopback_grafana() -> None:
    assert "ports:" not in _read(MONITORING / "compose.yml")
    assert "ports:" not in _read(MONITORING / "compose.edge.yml")
    published = re.findall(r'^      - "(.+)"$', _read(MONITORING / "compose.ports.yml"), re.M)

    assert published == ["127.0.0.1:3000:3000"]


def test_monitoring_reads_the_application_network_without_joining_its_project() -> None:
    compose = _read(MONITORING / "compose.yml")
    blocks = _service_blocks(compose)

    assert "    name: ${API_NETWORK:?API_NETWORK is required}\n    external: true" in compose
    # Only what talks to postgres, rabbitmq or redis joins the application network, and Tempo,
    # which the application sends its traces to.
    on_api = {name for name, block in blocks.items() if "      api: {}" in block}
    assert on_api == {
        "prometheus",
        "grafana",
        "postgres-exporter",
        "redis-exporter",
        "pg-backup",
        "tempo",
    }
    # The backup job needs PostgreSQL only.
    assert "default: {}" not in blocks["pg-backup"]
    # Host collectors never reach the stores or the edge.
    for name in ("node-exporter", "cadvisor"):
        assert "networks:" not in blocks[name]


def test_only_grafana_prometheus_and_the_status_page_join_the_edge_network() -> None:
    edge = _read(MONITORING / "compose.edge.yml")

    assert set(re.findall(r"^  ([a-z-]+):$", edge, re.M)) == {
        "grafana",
        "prometheus",
        "status-page",
        "edge",
    }
    assert "          - ${GRAFANA_ALIAS:?GRAFANA_ALIAS is required}" in edge


def test_probes_run_from_the_project_network_only() -> None:
    # The probes reach the public names from outside: no store and no edge network, as nobody.
    block = _service_blocks(_read(MONITORING / "compose.yml"))["blackbox-exporter"]

    assert "networks:" not in block
    for overlay in MONITORING.glob("compose.*.yml"):
        assert "blackbox-exporter" not in _read(overlay), overlay.name
    assert '    user: "65534:65534"\n' in block
    assert re.search(r"^    mem_limit: \d+m$", block, re.M)
    assert "      - ./blackbox:/etc/blackbox_exporter:ro\n" in block
    assert "      - --config.file=/etc/blackbox_exporter/blackbox.yml\n" in block
    assert '"http://127.0.0.1:9115/-/healthy"]' in block


def test_the_status_page_is_a_hardened_stdlib_service_on_the_project_and_edge_networks() -> None:
    # Public status page (api#309): the one monitoring service the internet reaches, through Caddy.
    compose = _read(MONITORING / "compose.yml")
    block = _service_blocks(compose)["status-page"]
    edge = _service_blocks(_read(MONITORING / "compose.edge.yml") + "\nvolumes:\n")["status-page"]

    # Pinned like the other images, a patch tag of the alpine variant: no build, no pip.
    assert re.search(r"^    image: python:3\.13\.\d+-alpine@sha256:[0-9a-f]{64}$", block, re.M)
    assert '    command: ["python", "/app/status_page.py"]\n' in block
    assert "      - ./status:/app:ro\n" in block
    assert "      PROMETHEUS_URL: http://prometheus:9090\n" in block
    # Nobody, read-only root, no capability, no privilege gain: as the edge proxy.
    assert '    user: "65534:65534"\n' in block
    assert "    read_only: true\n" in block
    assert "    cap_drop: [ALL]\n" in block
    assert "    security_opt: [no-new-privileges:true]\n" in block
    assert '      PYTHONDONTWRITEBYTECODE: "1"\n' in block
    # It reads Prometheus on the project network and never the stores of the application.
    assert "      default: {}\n" in block
    assert "api: {}" not in block
    assert "ports:" not in block
    assert '"http://127.0.0.1:8080/"]' in block
    # The edge overlay adds the network and the alias of the contract <service>-<env>.
    assert "      default: {}\n      edge:\n        aliases:\n          - status-staging\n" in edge
    assert "status-page" not in _read(MONITORING / "compose.ports.yml")


def test_prometheus_probes_the_public_names_through_the_exporter_once_a_minute() -> None:
    config = _read(MONITORING / "prometheus" / "prometheus.yml")
    job = config[config.index("  - job_name: blackbox\n") :]
    job = job[: job.index("\n\n")] if "\n\n" in job else job
    module = re.search(
        r"^  https_2xx:\n    prober: http\n    timeout: (\d+)s$",
        _read(MONITORING / "blackbox" / "blackbox.yml"),
        re.M,
    )
    scrape_timeout = re.search(r"^    scrape_timeout: (\d+)s$", job, re.M)

    assert "    scrape_interval: 1m\n" in job
    assert "    metrics_path: /probe\n" in job
    assert "      module: [https_2xx]\n" in job
    assert '      - files: ["/etc/prometheus/targets/probes.json"]' in job
    # The probed URL becomes the target parameter and the instance; the exporter is scraped.
    assert "      - source_labels: [__address__]\n        target_label: __param_target\n" in job
    assert "      - source_labels: [__param_target]\n        target_label: instance\n" in job
    assert "      - target_label: __address__\n        replacement: blackbox-exporter:9115" in job
    # A slow name ends the probe as probe_success 0 before the scrape itself times out.
    assert module is not None
    assert scrape_timeout is not None
    assert int(module.group(1)) < int(scrape_timeout.group(1))


def test_prometheus_drops_a_stopped_container_at_the_next_scrape() -> None:
    config = _read(MONITORING / "prometheus" / "prometheus.yml")
    job = config[config.index("  - job_name: cadvisor\n") :]
    job = job[: job.index("\n\n")]

    # cAdvisor timestamps its samples: without stale markers a stopped container lingers 5 minutes.
    assert "    track_timestamps_staleness: true\n" in job
    assert "honor_timestamps: false" not in config


def test_config_changes_recreate_the_service_that_reads_them() -> None:
    blocks = _service_blocks(_read(MONITORING / "compose.yml"))
    digests = {
        "prometheus": "PROMETHEUS_CONFIG_DIGEST",
        "alertmanager": "ALERTMANAGER_CONFIG_DIGEST",
        "grafana": "GRAFANA_CONFIG_DIGEST",
        "postgres-exporter": "POSTGRES_EXPORTER_CONFIG_DIGEST",
        "pg-backup": "PG_BACKUP_CONFIG_DIGEST",
        "blackbox-exporter": "BLACKBOX_CONFIG_DIGEST",
        "tempo": "TEMPO_CONFIG_DIGEST",
        "status-page": "STATUS_CONFIG_DIGEST",
    }
    deploy = _read(MONITORING / "deploy.sh")

    for service, variable in digests.items():
        assert f"dmc.config-digest: ${{{variable}:-none}}" in blocks[service]
        assert f"printf '{variable}=%s\\n'" in deploy


def test_the_application_sends_traces_to_tempo_inside_the_docker_network() -> None:
    blocks = _service_blocks(_read(REPO_ROOT / "deploy/compose/staging.yml"))
    tempo = _read(MONITORING / "tempo" / "tempo.yml")
    datasources = _read(MONITORING / "grafana/provisioning/datasources/datasources.yml")

    for service in ("api", "worker", "webhook-worker"):
        # Staging's default; a stack sets its own in .env, and an empty one turns the export off.
        endpoint = (
            "      OTEL_EXPORTER_OTLP_ENDPOINT: ${OTEL_EXPORTER_OTLP_ENDPOINT-http://tempo:4318}\n"
        )
        assert endpoint in blocks[service], service
    # OTLP/HTTP only; no gRPC receiver, no host port (test above), 72 h of traces.
    assert "        http:\n          endpoint: 0.0.0.0:4318\n" in tempo
    assert "grpc" not in tempo
    assert "block_retention: 72h" in tempo
    assert "  - name: Tempo\n    uid: tempo\n    type: tempo\n" in datasources
    assert "    url: http://tempo:3200\n" in datasources


# --- edge proxy ----------------------------------------------------------------------------


def test_edge_exposes_request_metrics_on_an_internal_port_only() -> None:
    caddyfile = _read(CADDYFILE)
    global_options = caddyfile[: caddyfile.index("\n}\n")]
    edge_ports = re.findall(r'^      - "(.+)"$', _read(REPO_ROOT / "deploy/edge/compose.yml"), re.M)

    assert "\tmetrics {\n\t\tper_host\n\t}" in global_options
    assert "http://:9180 {\n\tmetrics /metrics\n}" in caddyfile
    assert edge_ports == ["80:80", "443:443", "443:443/udp"]
    # The admin API stays on localhost: nothing rebinds it.
    assert not re.search(r"^\s*admin\s", caddyfile, re.M)


def test_grafana_has_its_own_host_and_prometheus_none() -> None:
    caddyfile = _read(CADDYFILE)
    site = caddyfile[caddyfile.index("grafana.{$APP_DOMAIN} {") :]
    site = site[: site.index("\n}\n")]
    api_site = caddyfile[caddyfile.index("staging-api.{$APP_DOMAIN} {") :]
    api_site = api_site[: api_site.index("\n}\n")]

    assert re.findall(r"reverse_proxy (\S+)", site) == ["grafana-staging:3000"]
    assert "import hsts" in site
    # /metrics of Grafana needs no login: answered 404 at the edge before the proxy route.
    blocked = site.index(
        "@metrics path /metrics /metrics/*\n\thandle @metrics {\n\t\trespond 404\n\t}"
    )
    assert blocked < site.index("reverse_proxy grafana-staging:3000")
    # The API host is the API only; Prometheus has no authentication and gets no public site.
    assert re.findall(r"reverse_proxy (\S+)", api_site) == ["api-staging:8000"]
    assert not re.search(r"^(prometheus|victoria-metrics)\.", caddyfile, re.M)
    assert "GRAFANA_ROOT_URL: https://grafana.${{ vars.APP_DOMAIN }}/" in _job("deploy-monitoring")


# --- workflow ------------------------------------------------------------------------------


def test_push_image_waits_for_the_monitoring_validation() -> None:
    job = _job("monitoring-validate")

    assert "run: scripts/validate_monitoring.sh" in job
    assert "continue-on-error" not in job
    assert "- monitoring-validate" in _job("push-image")


def test_monitoring_deploys_after_the_application_and_never_gates_it() -> None:
    job = _job("deploy-monitoring")

    assert "    needs: deploy-staging\n" in job
    assert "needs.deploy-staging.result == 'success'" in job
    assert "github.ref == 'refs/heads/main'" in job
    assert "rollback" not in job.lower()
    # Promotion depends on the application deploy only.
    assert "deploy-monitoring" not in _job("promote-staging")
    # Its own group: in staging-deploy it would hold the VPS lock for the monitoring deploy and
    # delay promote-staging and every queued member behind it; it only reads the app stack.
    assert re.findall(r"^      group: (\S+)$", job, re.M) == ["staging-monitoring"]


def test_monitoring_upload_and_secrets_reach_only_the_monitoring_dir() -> None:
    job = _job("deploy-monitoring")

    assert (
        'source: "deploy/monitoring,deploy/scripts/env-file.sh,deploy/scripts/check-isolation.sh"'
        in job
    )
    assert "target: ${{ env.MONITORING_DIR }}" in job
    assert "ALERT_TELEGRAM_BOT_TOKEN: ${{ secrets.ALERT_TELEGRAM_BOT_TOKEN }}" in job
    assert "ALERT_TELEGRAM_CHAT_ID: ${{ vars.ALERT_TELEGRAM_CHAT_ID }}" in job
    assert "ALERT_TELEGRAM" not in _job("deploy-staging")


def test_monitoring_deploy_gets_the_domain_of_the_probed_names() -> None:
    job = _job("deploy-monitoring")
    envs = re.search(r"^          envs: (\S+)$", job, re.M)

    assert "          APP_DOMAIN: ${{ vars.APP_DOMAIN }}\n" in job
    assert envs is not None
    assert "APP_DOMAIN" in envs.group(1).split(",")


def test_monitoring_deploy_gets_the_production_names_to_probe() -> None:
    job = _job("deploy-monitoring")
    envs = re.search(r"^          envs: (\S+)$", job, re.M)

    assert "          PROBE_PROD_HOSTS: ${{ vars.PROBE_PROD_HOSTS }}\n" in job
    assert envs is not None
    assert "PROBE_PROD_HOSTS" in envs.group(1).split(",")
    assert "PROBE_PROD_HOSTS" not in _job("deploy-staging")


def test_monitoring_deploy_gets_the_monthly_llm_budget() -> None:
    job = _job("deploy-monitoring")
    envs = re.search(r"^          envs: (\S+)$", job, re.M)

    assert "          LLM_MONTHLY_BUDGET_USD: ${{ vars.LLM_MONTHLY_BUDGET_USD }}\n" in job
    assert envs is not None
    assert "LLM_MONTHLY_BUDGET_USD" in envs.group(1).split(",")
    assert "LLM_MONTHLY_BUDGET_USD" not in _job("deploy-staging")
