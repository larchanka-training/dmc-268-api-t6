"""The public status page of the monitoring stack (deploy/monitoring/status, api#309).

The service folds the firing alerts of Prometheus into four components per environment and
serves the result as HTML. These tests drive it through its seams: the Prometheus JSON in, the
rows and the page out. The module is loaded from its path, as it runs in the container.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_PAGE = REPO_ROOT / "deploy" / "monitoring" / "status" / "status_page.py"
ALERT_RULES = REPO_ROOT / "deploy" / "monitoring" / "prometheus" / "rules" / "alerts.yml"
PROMTOOL_TEST = REPO_ROOT / "tests" / "monitoring" / "status.test.yml"
VALIDATE = REPO_ROOT / "scripts" / "validate_monitoring.sh"

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
# Prometheus writes activeAt as RFC 3339 with nanoseconds and a trailing Z.
ACTIVE_AT = "2026-11-03T10:15:30.123456789Z"
ACTIVE_AT_PARSED = datetime(2026, 11, 3, 10, 15, 30, 123456, tzinfo=UTC)
LATER_ACTIVE_AT = "2026-11-03T11:00:00Z"
LAST_FIRING_AT = 1793700000.0  # 2026-11-03T10:00:00Z


@pytest.fixture(scope="module")
def page() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("status_page", STATUS_PAGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _alert(
    name: str, state: str = "firing", active_at: str | None = ACTIVE_AT, **labels: str
) -> dict[str, Any]:
    # The labels and annotations of an alert are the poisoned part: the page must never show them.
    alert: dict[str, Any] = {
        "labels": {"alertname": name, "severity": "critical", **labels},
        "annotations": {
            "summary": "SECRET-SUMMARY acme/secret-repo",
            "description": (
                "docker compose -p dmc-268-api-prod ps; token ghp_SECRET <script>x</script>"
            ),
        },
        "state": state,
        "value": "1e+00",
    }
    if active_at is not None:
        alert["activeAt"] = active_at
    return alert


def _alerts_payload(*alerts: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", "data": {"alerts": list(alerts)}}


def _series(name: str, at: float = LAST_FIRING_AT, **labels: str) -> dict[str, Any]:
    return {
        "metric": {"alertname": name, "alertstate": "firing", "severity": "critical", **labels},
        "value": [1793703600, str(at)],
    }


def _last_firing_payload(*series: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "vector", "result": list(series)}}


def _snapshot(
    page: ModuleType,
    *alerts: dict[str, Any],
    last_firing: tuple[dict[str, Any], ...] = (),
    uncovered: frozenset[tuple[str, str]] = frozenset(),
    taken_at: datetime = NOW,
) -> Any:
    covered = {
        (component.key, env): (component.key, env) not in uncovered
        for component in page.COMPONENTS
        for env in page.ENVIRONMENTS
    }
    return page.Snapshot(
        taken_at=taken_at,
        alerts=page.parse_alerts(_alerts_payload(*alerts)),
        last_firing=page.parse_last_firing(_last_firing_payload(*last_firing)),
        covered=covered,
    )


def _rows(page: ModuleType, snapshot: Any, now: datetime = NOW) -> dict[tuple[str, str], Any]:
    return {(row.env, row.key): row for row in page.build_status(snapshot, now)}


def _states(rows: dict[tuple[str, str], Any]) -> dict[tuple[str, str], str]:
    return {key: row.state for key, row in rows.items()}


def _all(state: str) -> dict[tuple[str, str], str]:
    return {
        (env, key): state
        for env in ("prod", "staging")
        for key in ("api", "ui", "review", "webhooks")
    }


def _alert_names(row: Any) -> tuple[str, ...]:
    return tuple(finding.alert for finding in row.alerts)


# --- S2: alerts, last firing and coverage folded into states -------------------------------


def test_everything_covered_and_quiet_is_up_in_both_environments(page: ModuleType) -> None:
    assert _states(_rows(page, _snapshot(page))) == _all("up")


def test_a_staging_api_probe_down_takes_the_api_and_webhook_intake_of_staging_only(
    page: ModuleType,
) -> None:
    rows = _rows(
        page,
        _snapshot(page, _alert("PublicEndpointDown", host="staging-api.example.test")),
    )

    assert _states(rows) == {
        **_all("up"),
        ("staging", "api"): "down",
        ("staging", "webhooks"): "down",
    }
    # Webhooks arrive through the API: the row says so with its own public text.
    assert _alert_names(rows[("staging", "api")]) == ("PublicEndpointDown",)
    assert rows[("staging", "api")].since == ACTIVE_AT_PARSED
    assert rows[("staging", "webhooks")].since == ACTIVE_AT_PARSED


def test_a_prod_api_probe_down_takes_the_prod_section_only(page: ModuleType) -> None:
    # The table reads host=staging-api. as "the role api": api.<domain> is the same row in prod.
    rows = _rows(page, _snapshot(page, _alert("PublicEndpointDown", host="api.example.test")))

    assert _states(rows) == {
        **_all("up"),
        ("prod", "api"): "down",
        ("prod", "webhooks"): "down",
    }


def test_a_ui_probe_takes_the_ui_row_only(page: ModuleType) -> None:
    rows = _rows(
        page, _snapshot(page, _alert("PublicEndpointDown", host="staging-ui.example.test"))
    )

    assert _states(rows) == {**_all("up"), ("staging", "ui"): "down"}


def test_high_5xx_of_the_api_host_is_an_api_problem_and_of_the_ui_host_a_ui_problem(
    page: ModuleType,
) -> None:
    rows = _rows(
        page,
        _snapshot(
            page,
            _alert("ApiHigh5xxRate", host="staging-api.example.test"),
            _alert("ApiHigh5xxRate", host="staging-ui.example.test"),
        ),
    )

    assert _states(rows) == {
        **_all("up"),
        ("staging", "api"): "down",
        ("staging", "ui"): "down",
    }
    assert _alert_names(rows[("staging", "api")]) == ("ApiHigh5xxRate",)


def test_a_pending_alert_is_not_an_outage(page: ModuleType) -> None:
    rows = _rows(
        page,
        _snapshot(page, _alert("PublicEndpointDown", state="pending", host="staging-api.x.test")),
    )

    assert _states(rows) == _all("up")


def test_the_environment_of_an_alert_follows_host_then_compose_project_then_label_then_staging(
    page: ModuleType,
) -> None:
    def down(*alerts: dict[str, Any]) -> set[tuple[str, str]]:
        rows = _rows(page, _snapshot(page, *alerts))
        return {key for key, row in rows.items() if row.state == "down"}

    # 1. host wins over everything below it.
    assert down(_alert("PublicEndpointDown", host="api.x.test", environment="staging")) == {
        ("prod", "api"),
        ("prod", "webhooks"),
    }
    # 2. the compose project: -prod is prod, any other project staging.
    assert down(
        _alert(
            "WebhookWorkerDown",
            container_label_com_docker_compose_project="dmc-268-api-prod",
            environment="staging",
        )
    ) == {("prod", "webhooks")}
    assert down(
        _alert(
            "WebhookWorkerDown",
            container_label_com_docker_compose_project="dmc-268-api-staging",
        )
    ) == {("staging", "webhooks")}
    # 3. the environment label.
    assert down(_alert("PostgresDown", environment="prod")) == {
        ("prod", "api"),
        ("prod", "review"),
        ("prod", "webhooks"),
    }
    assert down(_alert("PostgresDown", environment="shared")) == {
        (env, key) for env in ("prod", "staging") for key in ("api", "review", "webhooks")
    }
    # 4. no label at all: the default is staging.
    assert down(_alert("PostgresDown")) == {
        ("staging", "api"),
        ("staging", "review"),
        ("staging", "webhooks"),
    }


def test_an_exceeded_llm_budget_is_not_an_outage(page: ModuleType) -> None:
    # The alert pauses no reviews (alerts.yml): the service works, so the page stays quiet.
    rows = _rows(page, _snapshot(page, _alert("LlmMonthlyBudgetExceeded")))

    assert _states(rows) == _all("up")
    assert "LlmMonthlyBudgetExceeded" in page.NOT_SHOWN


def test_the_review_component_listens_to_the_queues_the_worker_and_the_broker(
    page: ModuleType,
) -> None:
    for alert in (
        _alert("ReviewQueueWithoutConsumer", queue="review.run.fast"),
        _alert("ReviewRunsStuckInQueue"),
        _alert("TargetDown", job="rabbitmq", instance="rabbitmq:15692"),
        _alert("TargetDown", job="rabbitmq-queues", instance="rabbitmq:15692"),
    ):
        rows = _rows(page, _snapshot(page, alert))
        assert _states(rows) == {**_all("up"), ("staging", "review"): "down"}, alert["labels"]


def test_alerts_outside_the_table_and_other_scrape_targets_change_nothing(
    page: ModuleType,
) -> None:
    rows = _rows(
        page,
        _snapshot(
            page,
            _alert("TargetDown", job="node", instance="node-exporter:9100"),
            _alert("RedisDown"),
            _alert("HostDiskFull"),
            _alert("ReviewRunsFailed"),
            _alert("SomethingNobodyAddedYet"),
            _alert("PublicEndpointDown", host="staging-webhook.example.test"),
        ),
    )

    assert _states(rows) == _all("up")


def test_a_component_without_coverage_has_no_data_and_a_firing_alert_beats_that(
    page: ModuleType,
) -> None:
    uncovered = frozenset({("review", "prod"), ("api", "prod")})

    rows = _rows(page, _snapshot(page, uncovered=uncovered))
    assert _states(rows) == {
        **_all("up"),
        ("prod", "review"): "unknown",
        ("prod", "api"): "unknown",
    }

    rows = _rows(
        page,
        _snapshot(page, _alert("PostgresDown", environment="prod"), uncovered=uncovered),
    )
    assert rows[("prod", "review")].state == "down"
    assert rows[("prod", "api")].state == "down"
    assert rows[("prod", "ui")].state == "up"


def test_down_since_is_the_earliest_start_of_the_firing_alerts_of_the_component(
    page: ModuleType,
) -> None:
    rows = _rows(
        page,
        _snapshot(
            page,
            _alert("ReviewRunsStuckInQueue", active_at=LATER_ACTIVE_AT),
            _alert("ReviewQueueWithoutConsumer", queue="review.run.fast", active_at=ACTIVE_AT),
            _alert("ReviewQueueWithoutConsumer", queue="review.publish", active_at=LATER_ACTIVE_AT),
        ),
    )
    row = rows[("staging", "review")]

    assert row.since == ACTIVE_AT_PARSED
    # One entry per alert name, in the order of the table, whatever the queue.
    assert _alert_names(row) == ("ReviewQueueWithoutConsumer", "ReviewRunsStuckInQueue")


def test_up_since_is_the_last_firing_sample_of_the_component_in_that_environment(
    page: ModuleType,
) -> None:
    rows = _rows(
        page,
        _snapshot(
            page,
            last_firing=(
                _series("PublicEndpointDown", at=LAST_FIRING_AT, host="staging-api.x.test"),
                _series("PostgresDown", at=LAST_FIRING_AT + 600),
                _series("PublicEndpointDown", at=LAST_FIRING_AT + 1200, host="api.x.test"),
                _series("TargetDown", at=LAST_FIRING_AT + 9999, job="node"),
            ),
        ),
    )

    assert rows[("staging", "api")].since == datetime.fromtimestamp(LAST_FIRING_AT + 600, UTC)
    assert rows[("prod", "api")].since == datetime.fromtimestamp(LAST_FIRING_AT + 1200, UTC)
    # Nothing for the UI in a week: no time, the page says there were no outages.
    assert rows[("staging", "ui")].since is None
    assert rows[("staging", "ui")].state == "up"
    # The webhook intake of prod depends on the same api probe.
    assert rows[("prod", "webhooks")].since == datetime.fromtimestamp(LAST_FIRING_AT + 1200, UTC)


def test_the_public_text_of_a_finding_comes_from_the_table(page: ModuleType) -> None:
    rows = _rows(page, _snapshot(page, _alert("ReviewRunsStuckInQueue")))

    finding = rows[("staging", "review")].alerts[0]
    assert finding.alert == "ReviewRunsStuckInQueue"
    assert finding.description == "запуски ревью не берутся в работу"


# --- S3: what Prometheus says never reaches the output --------------------------------------


def test_labels_and_annotations_of_alerts_are_not_kept_in_the_rows(page: ModuleType) -> None:
    snapshot = _snapshot(
        page,
        _alert("WebhookWorkerDown", container_label_com_docker_compose_project="dmc-268-api-prod"),
        _alert("TargetDown", job="rabbitmq", instance="secret-host:15692"),
        _alert(
            "PublicEndpointDown", host="staging-api.example.test", instance="https://leak.test/"
        ),
    )

    dump = repr(page.build_status(snapshot, NOW))

    for secret in ("SECRET", "acme/secret-repo", "ghp_", "<script>", "secret-host", "leak.test"):
        assert secret not in dump, secret
    assert "docker compose" not in dump


# --- S4: staleness --------------------------------------------------------------------------


def test_a_snapshot_older_than_two_minutes_says_no_data_instead_of_working(
    page: ModuleType,
) -> None:
    snapshot = _snapshot(page, taken_at=NOW - timedelta(minutes=2, seconds=1))

    assert _states(_rows(page, snapshot)) == _all("unknown")
    # Even a firing alert of a stale snapshot is not shown: the whole picture is old.
    stale = _snapshot(
        page,
        _alert("PostgresDown"),
        taken_at=NOW - timedelta(minutes=5),
    )
    assert _states(_rows(page, stale)) == _all("unknown")


def test_a_snapshot_of_two_minutes_is_still_fresh(page: ModuleType) -> None:
    snapshot = _snapshot(page, taken_at=NOW - timedelta(minutes=2))

    assert _states(_rows(page, snapshot)) == _all("up")


def test_no_snapshot_yet_means_no_data(page: ModuleType) -> None:
    assert _states(_rows(page, None)) == _all("unknown")


# --- parsing the answers of Prometheus ------------------------------------------------------


def test_active_at_with_nanoseconds_or_without_a_fraction_is_understood(page: ModuleType) -> None:
    alerts = page.parse_alerts(
        _alerts_payload(
            _alert("PostgresDown", active_at="2026-11-03T10:15:30.123456789Z"),
            _alert("PostgresDown", active_at="2026-11-03T10:15:30Z"),
            _alert("PostgresDown", active_at="2026-11-03T10:15:30.5Z"),
            _alert("PostgresDown", active_at="2026-11-03T13:15:30+03:00"),
            _alert("PostgresDown", active_at="garbage"),
            _alert("PostgresDown", active_at=None),
        )
    )

    assert [alert.active_at for alert in alerts] == [
        datetime(2026, 11, 3, 10, 15, 30, 123456, tzinfo=UTC),
        datetime(2026, 11, 3, 10, 15, 30, tzinfo=UTC),
        datetime(2026, 11, 3, 10, 15, 30, 500000, tzinfo=UTC),
        datetime(2026, 11, 3, 10, 15, 30, tzinfo=UTC),
        None,
        None,
    ]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"status": "error", "error": "boom"},
        {"status": "success"},
        {"status": "success", "data": {"alerts": "x"}},
        {"status": "success", "data": {"alerts": [1]}},
        {"status": "success", "data": {"alerts": [{"labels": {}, "state": "firing"}]}},
    ],
)
def test_an_unexpected_alerts_answer_is_an_error_not_an_empty_list(
    page: ModuleType, payload: object
) -> None:
    with pytest.raises(page.StatusError):
        page.parse_alerts(payload)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"status": "error"},
        {"status": "success", "data": {"resultType": "matrix", "result": []}},
        {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}}]}},
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"alertname": "A"}, "value": [1, "NaN-ish"]}],
            },
        },
    ],
)
def test_an_unexpected_last_firing_answer_is_an_error(page: ModuleType, payload: object) -> None:
    with pytest.raises(page.StatusError):
        page.parse_last_firing(payload)


# --- S5: the table and alerts.yml -----------------------------------------------------------


def _rule_alerts() -> set[str]:
    return set(re.findall(r"^\s+- alert: (\w+)$", ALERT_RULES.read_text("utf-8"), re.M))


def test_every_alert_of_the_rules_is_either_shown_or_explicitly_not_shown(
    page: ModuleType,
) -> None:
    shown = {rule.alert for component in page.COMPONENTS for rule in component.rules}

    assert _rule_alerts()
    assert shown.isdisjoint(page.NOT_SHOWN)
    # A new alert without a decision turns this red: add it to the table or to NOT_SHOWN.
    assert _rule_alerts() == shown | set(page.NOT_SHOWN)


def test_the_table_has_the_four_components_in_order(page: ModuleType) -> None:
    assert [(component.key, component.name) for component in page.COMPONENTS] == [
        ("api", "API"),
        ("ui", "UI"),
        ("review", "Ревью PR"),
        ("webhooks", "Приём вебхуков"),
    ]
    assert page.ENVIRONMENTS == ("prod", "staging")


def test_every_component_has_a_presence_expression_for_each_environment(page: ModuleType) -> None:
    for component in page.COMPONENTS:
        assert set(component.coverage) == {"prod", "staging"}, component.key
        for expression in component.coverage.values():
            assert expression.strip(), component.key


# --- S1: the HTTP surface, with a stub Prometheus -------------------------------------------


class PrometheusStub:
    """Answers the three kinds of query of the poller from a state the test sets."""

    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []
        self.last_firing: list[dict[str, Any]] = []
        self.uncovered: set[str] = set()
        self.down = False
        self.raw: tuple[int, object] | None = None  # answers every request with this
        self.requests: list[tuple[str, str]] = []
        self.last_firing_query = ""
        self.coverage: dict[str, str] = {}  # expression -> "component/env", set by the fixture

    def answer(self, path: str, query: str) -> tuple[int, object]:
        self.requests.append((path, query))
        if self.raw is not None:
            return self.raw
        if self.down:
            return 503, {"status": "error"}
        if path == "/api/v1/alerts":
            return 200, _alerts_payload(*self.alerts)
        if path == "/api/v1/query":
            expression = parse_qs(query)["query"][0]
            if expression == self.last_firing_query:
                return 200, _last_firing_payload(*self.last_firing)
            target = self.coverage.get(expression)
            if target is None:
                return 400, {"status": "error", "error": "unexpected expression"}
            present = [] if target in self.uncovered else [{"metric": {}, "value": [1, "1"]}]
            return 200, {"status": "success", "data": {"resultType": "vector", "result": present}}
        return 404, {}


@pytest.fixture
def prometheus(page: ModuleType) -> Iterator[tuple[PrometheusStub, str]]:
    stub = PrometheusStub()
    stub.last_firing_query = page.LAST_FIRING_QUERY
    stub.coverage = {
        expression: f"{component.key}/{env}"
        for component in page.COMPONENTS
        for env, expression in component.coverage.items()
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            status, payload = stub.answer(parts.path, parts.query)
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield stub, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def site(
    page: ModuleType, prometheus: tuple[PrometheusStub, str]
) -> Iterator[tuple[str, Any, PrometheusStub, Clock]]:
    stub, url = prometheus
    clock = Clock()
    poller = page.Poller(url, clock=clock)
    server = page.make_server(poller, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", poller, stub, clock
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


PROD_HOST = {"Host": "status.example.test"}
STAGING_HOST = {"Host": "staging-status.example.test"}


def _row_tags(html: str) -> list[tuple[str, str, str, str]]:
    """(env, component, state, alerts) of every row, from the data attributes only."""
    return re.findall(
        r'<li class="row [a-z]+" data-env="([a-z]+)" data-component="([a-z]+)" '
        r'data-state="([a-z]+)" data-alerts="([^"]*)">',
        html,
    )


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_the_page_answers_get_and_head_with_the_security_headers(
    site: tuple[str, Any, PrometheusStub, Clock],
) -> None:
    base, poller, _, _ = site
    poller.refresh()

    got = httpx.get(f"{base}/", headers=PROD_HOST)
    head = httpx.head(f"{base}/", headers=PROD_HOST)

    assert got.status_code == head.status_code == 200
    assert got.headers["content-type"] == "text/html; charset=utf-8"
    for response in (got, head):
        assert response.headers["content-security-policy"] == (
            "default-src 'none'; style-src 'unsafe-inline'"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
    assert head.content == b""
    assert head.headers["content-length"] == got.headers["content-length"]
    assert '<meta http-equiv="refresh" content="30">' in got.text
    assert "<script" not in got.text


def test_a_query_string_does_not_change_the_page(
    site: tuple[str, Any, PrometheusStub, Clock],
) -> None:
    base, poller, _, _ = site
    poller.refresh()

    assert httpx.get(f"{base}/?utm=1", headers=PROD_HOST).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/query?query=up",
        "/api/v1/alerts",
        "/api/v2/alerts",
        "/api/v2/silences",
        "/-/ready",
        "/metrics",
        "/api/health",
        "/login",
        "/graph",
        "/x",
        "/index.html",
    ],
)
def test_every_other_path_is_not_found(
    site: tuple[str, Any, PrometheusStub, Clock], path: str
) -> None:
    base, poller, stub, _ = site
    poller.refresh()
    before = len(stub.requests)

    response = httpx.get(f"{base}{path}", headers=PROD_HOST)

    assert response.status_code == 404
    # A public request never reaches Prometheus.
    assert len(stub.requests) == before


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_a_method_other_than_get_and_head_is_405(
    site: tuple[str, Any, PrometheusStub, Clock], method: str
) -> None:
    base, _, _, _ = site

    response = httpx.request(method, f"{base}/", headers=PROD_HOST, content=b"x")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


def test_the_host_chooses_the_sections(site: tuple[str, Any, PrometheusStub, Clock]) -> None:
    base, poller, _, _ = site
    poller.refresh()

    everything = [
        (env, key) for env in ("prod", "staging") for key in ("api", "ui", "review", "webhooks")
    ]
    staging = [("staging", key) for key in ("api", "ui", "review", "webhooks")]
    for headers, expected in (
        (PROD_HOST, everything),
        (STAGING_HOST, staging),
        ({"Host": "STAGING-STATUS.example.test:443"}, staging),
        ({"Host": "127.0.0.1:8080"}, everything),
    ):
        tags = _row_tags(httpx.get(f"{base}/", headers=headers).text)
        assert [(env, key) for env, key, _, _ in tags] == expected, headers


def test_the_page_shows_a_down_component_with_the_name_of_the_alert_and_the_public_text(
    site: tuple[str, Any, PrometheusStub, Clock],
) -> None:
    base, poller, stub, _ = site
    stub.alerts = [
        _alert("ReviewQueueWithoutConsumer", queue="review.run.fast"),
        _alert("ReviewQueueWithoutConsumer", queue="review.publish", active_at=LATER_ACTIVE_AT),
    ]
    stub.last_firing = [_series("PublicEndpointDown", host="staging-api.example.test")]
    poller.refresh()

    html = httpx.get(f"{base}/", headers=STAGING_HOST).text

    tags = {(env, key): (state, alerts) for env, key, state, alerts in _row_tags(html)}
    assert tags[("staging", "review")] == ("down", "ReviewQueueWithoutConsumer")
    assert tags[("staging", "api")] == ("up", "")
    assert tags[("staging", "ui")] == ("up", "")
    assert "Ревью PR" in html
    text = _text(html)
    assert "Ревью PR не работает с 2026-11-03 10:15 UTC" in text
    assert "ReviewQueueWithoutConsumer: очередь ревью без обработчика" in text
    # The last problem of the API ended at 10:00 UTC; the UI had none within a week.
    assert "API работает с 2026-11-03 10:00 UTC" in text
    assert "UI работает сбоев за 7 дн. не было" in text
    assert "Обновлено: 2026-11-03 12:00 UTC" in text


def test_nothing_prometheus_says_reaches_the_page(
    site: tuple[str, Any, PrometheusStub, Clock],
) -> None:
    base, poller, stub, _ = site
    stub.alerts = [
        _alert(
            "WebhookWorkerDown",
            container_label_com_docker_compose_project="dmc-268-api-prod",
            instance="https://leak.test/<script>",
            host="staging-api.<b>evil</b>.test",
        ),
        _alert("TargetDown", job="rabbitmq", instance="secret-host:15692"),
    ]
    poller.refresh()

    html = httpx.get(f"{base}/", headers=PROD_HOST).text

    for secret in (
        "SECRET",
        "acme/secret-repo",
        "ghp_",
        "<script>",
        "<b>",
        "secret-host",
        "leak.test",
        "docker compose",
        "dmc-268-api-prod",
    ):
        assert secret not in html, secret


def test_an_unreachable_prometheus_means_no_data_not_working(
    site: tuple[str, Any, PrometheusStub, Clock],
) -> None:
    base, poller, stub, _ = site
    poller.refresh()
    assert {state for _, _, state, _ in _row_tags(httpx.get(f"{base}/").text)} == {"up"}

    stub.down = True
    poller.refresh()
    html = httpx.get(f"{base}/").text

    assert {state for _, _, state, _ in _row_tags(html)} == {"unknown"}
    assert "нет данных" in html
    assert "Данные не получены" in html


def test_a_failed_poll_writes_one_line_to_stderr_without_the_answer(
    site: tuple[str, Any, PrometheusStub, Clock], capsys: pytest.CaptureFixture[str]
) -> None:
    _, poller, stub, _ = site
    poller.refresh()
    assert capsys.readouterr().err == ""

    stub.down = True
    poller.refresh()
    assert capsys.readouterr().err == (
        "status-page: poll failed: HTTPError: HTTP Error 503: Service Unavailable\n"
    )

    # An answer Prometheus got wrong: its text is not copied into the line.
    stub.down = False
    stub.raw = (200, {"status": "error", "error": "SECRET-ERROR acme/secret-repo\nforged line"})
    poller.refresh()
    err = capsys.readouterr().err
    assert err == "status-page: poll failed: StatusError: status is not success\n"
    stub.raw = (200, {"status": "success", "data": {"alerts": [{"labels": {"x": "SECRET"}}]}})
    poller.refresh()
    err = capsys.readouterr().err
    assert err == "status-page: poll failed: StatusError: a series has no alertname\n"
    assert "SECRET" not in err

    # A recovered Prometheus: quiet again, the page is back.
    stub.raw = None
    poller.refresh()
    assert capsys.readouterr().err == ""
    assert poller.snapshot() is not None


def test_a_line_of_a_failed_poll_never_carries_a_newline_from_the_error(
    page: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    # Nothing listens there: the OS error is logged as one line.
    poller = page.Poller("http://127.0.0.1:9", timeout=1.0)

    poller.refresh()

    err = capsys.readouterr().err
    assert err.startswith("status-page: poll failed: URLError: ")
    assert err.count("\n") == 1
    assert poller.snapshot() is None


def test_before_the_first_poll_the_page_says_no_data(
    site: tuple[str, Any, PrometheusStub, Clock],
) -> None:
    base, _, _, _ = site

    html = httpx.get(f"{base}/").text

    assert {state for _, _, state, _ in _row_tags(html)} == {"unknown"}
    assert "Данные не получены" in html


def test_a_snapshot_that_is_no_longer_refreshed_ages_into_no_data(
    site: tuple[str, Any, PrometheusStub, Clock],
) -> None:
    base, poller, _, clock = site
    poller.refresh()
    clock.now = NOW + timedelta(minutes=2)
    assert {state for _, _, state, _ in _row_tags(httpx.get(f"{base}/").text)} == {"up"}

    clock.now = NOW + timedelta(minutes=2, seconds=1)
    html = httpx.get(f"{base}/").text

    assert {state for _, _, state, _ in _row_tags(html)} == {"unknown"}
    assert "Данные устарели: последнее обновление 2026-11-03 12:00 UTC" in html


def test_a_poll_sends_the_presence_and_last_firing_expressions_unchanged(
    page: ModuleType, site: tuple[str, Any, PrometheusStub, Clock]
) -> None:
    _, poller, stub, _ = site

    poller.refresh()

    sent = [parse_qs(query)["query"][0] for path, query in stub.requests if path.endswith("query")]
    expected = [page.LAST_FIRING_QUERY] + [
        expression for component in page.COMPONENTS for expression in component.coverage.values()
    ]
    assert sorted(sent) == sorted(expected)
    assert [path for path, _ in stub.requests].count("/api/v1/alerts") == 1


def test_the_last_firing_query_asks_for_the_last_firing_sample_of_the_shown_alerts_in_a_week(
    page: ModuleType,
) -> None:
    shown = sorted({rule.alert for component in page.COMPONENTS for rule in component.rules})

    expected = (
        'max_over_time(timestamp(ALERTS{alertstate="firing",alertname=~"'
        + "|".join(shown)
        + '"})[7d:1m])'
    )
    assert expected == page.LAST_FIRING_QUERY


def test_the_poller_keeps_polling_until_it_is_stopped(
    page: ModuleType, prometheus: tuple[PrometheusStub, str]
) -> None:
    stub, url = prometheus
    poller = page.Poller(url)
    stop = threading.Event()
    thread = threading.Thread(target=poller.run, args=(stop, 0.01), daemon=True)

    thread.start()
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=5)
        while [p for p, _ in stub.requests].count("/api/v1/alerts") < 3:
            assert datetime.now(UTC) < deadline, "the poller does not poll"
            stop.wait(0.01)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert poller.snapshot() is not None


# --- S6: the last-firing query runs under promtool ------------------------------------------


def test_promtool_runs_the_very_query_the_page_sends(page: ModuleType) -> None:
    tested = re.findall(r"^\s+- expr: '(.+)'$", PROMTOOL_TEST.read_text("utf-8"), re.M)

    # status.test.yml holds the text of the constant, so what promtool proves is what is sent.
    assert tested == [page.LAST_FIRING_QUERY]
    assert "test rules tests/monitoring/status.test.yml" in " ".join(
        VALIDATE.read_text("utf-8").split()
    )
