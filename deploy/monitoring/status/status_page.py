"""Public status page of the monitoring stack (docs/MONITORING.md, api#309).

A small stdlib-only service: every 30 seconds it reads Prometheus over the docker network, folds
the firing alerts into four components per environment by a fixed table and serves the result as
cached HTML. Nothing Prometheus says is shown except times: the page prints the names and the
public texts of this table, never a label or an annotation (they carry commands, URLs and spend).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ENVIRONMENTS = ("prod", "staging")
STALE_AFTER = timedelta(minutes=2)

STATE_UP = "up"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"


class StatusError(Exception):
    """Prometheus answered something the page cannot read."""


@dataclass(frozen=True)
class Signal:
    """The labels of an alert the page reads: where it belongs and which row it is."""

    alertname: str
    host: str = ""
    project: str = ""
    environment: str = ""
    job: str = ""


@dataclass(frozen=True)
class Alert:
    signal: Signal
    firing: bool
    active_at: datetime | None


@dataclass(frozen=True)
class LastFiring:
    """The last firing sample of an alert series within the lookback window."""

    signal: Signal
    at: datetime


def _host_role(host: str) -> str:
    """``staging-api.example.test`` and ``api.example.test`` are both the role ``api``."""
    return host.lower().removeprefix("staging-").split(".", 1)[0]


@dataclass(frozen=True)
class Rule:
    """One alert of a component with the public text that explains it.

    The matchers are environment-agnostic: ``role`` is the host without a ``staging-`` prefix, the
    environment of an alert is decided apart (``environments``). A ``shared`` row belongs to both
    environments.
    """

    alert: str
    description: str
    role: str = ""
    jobs: tuple[str, ...] = ()
    project_prefix: str = ""
    shared: bool = False

    def matches(self, signal: Signal) -> bool:
        if signal.alertname != self.alert:
            return False
        if self.role and _host_role(signal.host) != self.role:
            return False
        if self.jobs and signal.job not in self.jobs:
            return False
        return signal.project.startswith(self.project_prefix)


@dataclass(frozen=True)
class Component:
    key: str
    name: str
    rules: tuple[Rule, ...]
    # Per environment: a PromQL expression with a series while the component is observed at all.
    coverage: Mapping[str, str]


# Presence expressions; in a PromQL string two backslashes are one regex backslash. Probes exist per
# public name. The consumers of the review queues carry no environment label on main, so
# `environment!="prod"` matches them as staging. The webhook intake is seen through the postgres of
# its compose project, not through the webhook-worker: the worker vanishing is the alert itself.
_API_STAGING = r'probe_success{job="blackbox",host=~"staging-api\\..+"}'
_API_PROD = r'probe_success{job="blackbox",host=~"api\\..+"}'
_UI_STAGING = r'probe_success{job="blackbox",host=~"staging-ui\\..+"}'
_UI_PROD = r'probe_success{job="blackbox",host=~"ui\\..+"}'
_QUEUES_STAGING = (
    r'rabbitmq_detailed_queue_consumers{queue=~"review\\.run\\.fast|review\\.publish",'
    r'environment!="prod"}'
)
_QUEUES_PROD = (
    r'rabbitmq_detailed_queue_consumers{queue=~"review\\.run\\.fast|review\\.publish",'
    r'environment="prod"}'
)
_INTAKE_STAGING = (
    'container_last_seen{container_label_com_docker_compose_service="postgres",'
    'container_label_com_docker_compose_project=~"dmc-268-api.*",'
    'container_label_com_docker_compose_project!~".*-prod"}'
)
_INTAKE_PROD = (
    'container_last_seen{container_label_com_docker_compose_service="postgres",'
    'container_label_com_docker_compose_project="dmc-268-api-prod"}'
)

_DATABASE_DOWN = "база данных недоступна"
_PROBE_DOWN = "внешняя проверка не получает ответ 2xx"
_HIGH_5XX = "много ответов 5xx"

COMPONENTS = (
    Component(
        "api",
        "API",
        (
            Rule("PublicEndpointDown", _PROBE_DOWN, role="api"),
            Rule("ApiHigh5xxRate", _HIGH_5XX, role="api"),
            Rule("PostgresDown", _DATABASE_DOWN),
        ),
        {"staging": _API_STAGING, "prod": _API_PROD},
    ),
    Component(
        "ui",
        "UI",
        (
            Rule("PublicEndpointDown", _PROBE_DOWN, role="ui"),
            Rule("ApiHigh5xxRate", _HIGH_5XX, role="ui"),
        ),
        {"staging": _UI_STAGING, "prod": _UI_PROD},
    ),
    Component(
        "review",
        "Ревью PR",
        (
            Rule("ReviewQueueWithoutConsumer", "очередь ревью без обработчика"),
            Rule("ReviewRunsStuckInQueue", "запуски ревью не берутся в работу"),
            Rule("TargetDown", "брокер очередей недоступен", jobs=("rabbitmq", "rabbitmq-queues")),
            Rule(
                "LlmMonthlyBudgetExceeded",
                "лимит расходов на LLM исчерпан, ревью приостановлены",
                shared=True,
            ),
            Rule("PostgresDown", _DATABASE_DOWN),
        ),
        {"staging": _QUEUES_STAGING, "prod": _QUEUES_PROD},
    ),
    Component(
        "webhooks",
        "Приём вебхуков",
        (
            Rule(
                "WebhookWorkerDown", "обработчик вебхуков не работает", project_prefix="dmc-268-api"
            ),
            Rule(
                "PublicEndpointDown", "приём вебхуков идёт через API, а он не отвечает", role="api"
            ),
            Rule("PostgresDown", _DATABASE_DOWN),
        ),
        {"staging": _INTAKE_STAGING, "prod": _INTAKE_PROD},
    ),
)

# Alerts of alerts.yml the page deliberately does not show; a test pins this list and the table
# against the rules, so a new alert needs a decision in one of them.
NOT_SHOWN = (
    "RedisDown",
    "TlsCertificateExpiresSoon",
    "TlsCertificateExpiring",
    "ReviewRunsFailed",
    "ReviewDeadLetters",
    "ReviewRunsBudgetPaused",
    "ReviewQueueBacklog",
    "LlmLatencyHigh",
    "LlmMonthlyBudgetWarning",
    "HostMemoryLow",
    "HostDiskFull",
    "HostCpuHigh",
    "ContainerOomKilled",
    "PostgresBackupStale",
    "PostgresBackupMissing",
    "PostgresBackupFailed",
    "PostgresBackupOffsiteStale",
    "PostgresBackupOffsiteFailing",
)


# --- reading Prometheus --------------------------------------------------------------------


def _mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise StatusError(f"{what} is not an object")
    return value


def _data(payload: object) -> Mapping[str, object]:
    body = _mapping(payload, "answer")
    if body.get("status") != "success":
        raise StatusError("status is not success")
    return _mapping(body.get("data"), "data")


def _signal(labels: Mapping[str, object]) -> Signal:
    def text(name: str) -> str:
        value = labels.get(name, "")
        return value if isinstance(value, str) else ""

    alertname = text("alertname")
    if not alertname:
        raise StatusError("a series has no alertname")
    return Signal(
        alertname=alertname,
        host=text("host"),
        project=text("container_label_com_docker_compose_project"),
        environment=text("environment"),
        job=text("job"),
    )


_FRACTION = re.compile(r"(\.\d{1,9})(?=Z|[+-]\d\d:\d\d$)")


def _parse_time(value: object) -> datetime | None:
    """RFC 3339 as Prometheus writes it: up to nine fraction digits, ``Z``; None when unreadable."""
    if not isinstance(value, str):
        return None
    # Python reads microseconds only: cut the nanoseconds.
    text = _FRACTION.sub(lambda match: match.group(1)[:7], value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def parse_alerts(payload: object) -> list[Alert]:
    """``GET /api/v1/alerts``: the alerts with the labels the page needs and nothing else."""
    raw = _data(payload).get("alerts")
    if not isinstance(raw, list):
        raise StatusError("data.alerts is not a list")
    alerts: list[Alert] = []
    for item in raw:
        alert = _mapping(item, "alert")
        alerts.append(
            Alert(
                signal=_signal(_mapping(alert.get("labels"), "labels")),
                firing=alert.get("state") == "firing",
                active_at=_parse_time(alert.get("activeAt")),
            )
        )
    return alerts


def parse_last_firing(payload: object) -> list[LastFiring]:
    """``GET /api/v1/query`` of the last-firing expression: one timestamp per alert series."""
    data = _data(payload)
    result = data.get("result")
    if data.get("resultType") != "vector" or not isinstance(result, list):
        raise StatusError("the answer is not a vector")
    series: list[LastFiring] = []
    for item in result:
        sample = _mapping(item, "sample")
        value = sample.get("value")
        if not isinstance(value, list) or len(value) != 2:
            raise StatusError("a sample has no value")
        try:
            at = datetime.fromtimestamp(float(str(value[1])), UTC)
        except (ValueError, OverflowError, OSError) as error:
            raise StatusError("a sample value is not a timestamp") from error
        series.append(LastFiring(signal=_signal(_mapping(sample.get("metric"), "metric")), at=at))
    return series


# --- folding alerts into components --------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """What one poll of Prometheus saw: alerts, last-firing times and where data exists."""

    taken_at: datetime
    alerts: Sequence[Alert]
    last_firing: Sequence[LastFiring]
    # (component key, environment) -> the presence expression has a series.
    covered: Mapping[tuple[str, str], bool]


@dataclass(frozen=True)
class Finding:
    """A firing alert of a row: its name and the public text of the table."""

    alert: str
    description: str


@dataclass(frozen=True)
class Row:
    env: str
    key: str
    name: str
    state: str
    # Down: when the problem began. Up: when the last problem ended (None: none within a week).
    since: datetime | None
    alerts: tuple[Finding, ...]


def environments(signal: Signal, shared: bool = False) -> tuple[str, ...]:
    """The environments an alert belongs to: the first rule that applies decides."""
    if shared:
        return ENVIRONMENTS
    if signal.host:
        return ("staging",) if signal.host.lower().startswith("staging-") else ("prod",)
    if signal.project:
        return ("prod",) if signal.project.endswith("-prod") else ("staging",)
    if signal.environment in ENVIRONMENTS:
        return (signal.environment,)
    if signal.environment == "shared":
        return ENVIRONMENTS
    # On main the rules without labels watch the stores of staging only.
    return ("staging",)


def _belongs(rule: Rule, signal: Signal, env: str) -> bool:
    return rule.matches(signal) and env in environments(signal, rule.shared)


def _row(component: Component, env: str, snapshot: Snapshot) -> Row:
    # Per rule of the table: the start times of its firing alerts (several queues, one rule).
    firing: dict[int, list[datetime]] = {}
    for alert in snapshot.alerts:
        for index, rule in enumerate(component.rules):
            if alert.firing and _belongs(rule, alert.signal, env):
                firing.setdefault(index, [])
                if alert.active_at is not None:
                    firing[index].append(alert.active_at)
    if firing:
        findings = tuple(
            Finding(component.rules[index].alert, component.rules[index].description)
            for index in sorted(firing)
        )
        started = [at for times in firing.values() for at in times]
        return Row(
            env, component.key, component.name, STATE_DOWN, min(started, default=None), findings
        )
    if not snapshot.covered.get((component.key, env), False):
        return Row(env, component.key, component.name, STATE_UNKNOWN, None, ())
    ended = [
        sample.at
        for sample in snapshot.last_firing
        if any(_belongs(rule, sample.signal, env) for rule in component.rules)
    ]
    return Row(env, component.key, component.name, STATE_UP, max(ended, default=None), ())


def build_status(snapshot: Snapshot | None, now: datetime) -> list[Row]:
    """Rows of the page, prod before staging. No snapshot or an old one: no data, never up."""
    fresh = snapshot is not None and now - snapshot.taken_at <= STALE_AFTER
    rows: list[Row] = []
    for env in ENVIRONMENTS:
        for component in COMPONENTS:
            if snapshot is not None and fresh:
                rows.append(_row(component, env, snapshot))
            else:
                rows.append(Row(env, component.key, component.name, STATE_UNKNOWN, None, ()))
    return rows
