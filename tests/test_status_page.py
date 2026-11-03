"""The public status page of the monitoring stack (deploy/monitoring/status, api#309).

The service folds the firing alerts of Prometheus into four components per environment and
serves the result as HTML. These tests drive it through its seams: the Prometheus JSON in, the
rows and the page out. The module is loaded from its path, as it runs in the container.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_PAGE = REPO_ROOT / "deploy" / "monitoring" / "status" / "status_page.py"
ALERT_RULES = REPO_ROOT / "deploy" / "monitoring" / "prometheus" / "rules" / "alerts.yml"

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
    # 4. no label at all: on main the rules without labels watch the stores of staging.
    assert down(_alert("PostgresDown")) == {
        ("staging", "api"),
        ("staging", "review"),
        ("staging", "webhooks"),
    }


def test_the_llm_budget_is_one_key_for_both_environments(page: ModuleType) -> None:
    rows = _rows(page, _snapshot(page, _alert("LlmMonthlyBudgetExceeded")))

    assert _states(rows) == {
        **_all("up"),
        ("prod", "review"): "down",
        ("staging", "review"): "down",
    }
    assert _alert_names(rows[("prod", "review")]) == ("LlmMonthlyBudgetExceeded",)


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
        _snapshot(page, _alert("LlmMonthlyBudgetExceeded"), uncovered=uncovered),
    )
    assert rows[("prod", "review")].state == "down"
    assert rows[("prod", "api")].state == "unknown"


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
