"""Public status page of the monitoring stack (docs/MONITORING.md, api#309).

A small stdlib-only service: every 30 seconds it reads Prometheus over the docker network, folds
the firing alerts into four components per environment by a fixed table and serves the result as
cached HTML. Nothing Prometheus says is shown except times: the page prints the names and the
public texts of this table, never a label or an annotation (they carry commands, URLs and spend).
"""

from __future__ import annotations

import html
import json
import os
import re
import signal
import sys
import threading
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

ENVIRONMENTS = ("prod", "staging")
POLL_SECONDS = 30.0  # = evaluation_interval of prometheus.yml: nothing new appears faster
STALE_AFTER = timedelta(minutes=2)
MAX_ANSWER_BYTES = 4 * 1024 * 1024
DEFAULT_PROMETHEUS_URL = "http://prometheus:9090"
DEFAULT_PORT = 8080

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
    environment of an alert is decided apart (``environments``).
    """

    alert: str
    description: str
    role: str = ""
    jobs: tuple[str, ...] = ()
    project_prefix: str = ""

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

# The last firing sample of the shown alerts within a week (Prometheus keeps 15 days). The series
# are rare, so the subquery is cheap; tests/monitoring/status.test.yml runs this very text.
LAST_FIRING_QUERY = (
    'max_over_time(timestamp(ALERTS{alertstate="firing",alertname=~"'
    + "|".join(sorted({rule.alert for component in COMPONENTS for rule in component.rules}))
    + '"})[7d:1m])'
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
    "LlmMonthlyBudgetExceeded",
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


def environments(signal: Signal) -> tuple[str, ...]:
    """The environments an alert belongs to: the first rule that applies decides."""
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
    return rule.matches(signal) and env in environments(signal)


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


# --- polling Prometheus --------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _has_series(payload: object) -> bool:
    data = _data(payload)
    result = data.get("result")
    if data.get("resultType") != "vector" or not isinstance(result, list):
        raise StatusError("the answer is not a vector")
    return bool(result)


class Poller:
    """Reads Prometheus and keeps the last snapshot; a public request never reaches Prometheus."""

    def __init__(
        self,
        prometheus_url: str,
        clock: Callable[[], datetime] = _utc_now,
        timeout: float = 10.0,
    ) -> None:
        self._url = prometheus_url.rstrip("/")
        self.clock = clock
        self._timeout = timeout
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    def _get(self, path: str, params: Mapping[str, str] | None = None) -> object:
        url = self._url + path + (f"?{urlencode(params)}" if params else "")
        with urllib.request.urlopen(url, timeout=self._timeout) as answer:
            body: bytes = answer.read(MAX_ANSWER_BYTES + 1)
        if len(body) > MAX_ANSWER_BYTES:
            raise StatusError("the answer is too large")
        return json.loads(body)

    def _query(self, expression: str) -> object:
        return self._get("/api/v1/query", {"query": expression})

    def refresh(self) -> None:
        """One poll. Any failure leaves no snapshot: the page says no data, never working."""
        try:
            alerts = parse_alerts(self._get("/api/v1/alerts"))
            last_firing = parse_last_firing(self._query(LAST_FIRING_QUERY))
            covered = {
                (component.key, env): _has_series(self._query(expression))
                for component in COMPONENTS
                for env, expression in component.coverage.items()
            }
            snapshot: Snapshot | None = Snapshot(self.clock(), alerts, last_firing, covered)
        except (OSError, HTTPException, ValueError, StatusError) as error:
            snapshot = None
            # One line per failed poll, in the container log: the class and the message of the
            # error (fixed texts of this module or of the HTTP stack), never the answer itself.
            message = " ".join(str(error).split())
            print(f"status-page: poll failed: {type(error).__name__}: {message}", file=sys.stderr)
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    def run(self, stop: threading.Event, interval: float = POLL_SECONDS) -> None:
        while not stop.is_set():
            self.refresh()
            stop.wait(interval)


# --- the page ------------------------------------------------------------------------------

_STATE_TEXT = {STATE_UP: "работает", STATE_DOWN: "не работает", STATE_UNKNOWN: "нет данных"}
_ENVIRONMENT_TITLE = {"prod": "prod", "staging": "staging"}

_STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1b1f23;--muted:#59636e;--line:#d0d7de;
--up:#1a7f37;--down:#cf222e;--unknown:#9a6700}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;
--line:#30363d;--up:#3fb950;--down:#ff7b72;--unknown:#d29922}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:42rem;margin:0 auto;padding:1.5rem 1rem}
h1{font-size:1.5rem;margin:0 0 .25rem}h2{font-size:1.1rem;margin:2rem 0 .5rem}
p.updated{color:var(--muted);margin:0}
ul{list-style:none;margin:0;padding:0}
li.row{border-top:1px solid var(--line);padding:.6rem 0}
.name{font-weight:600}.state{margin-left:.5rem;font-weight:600}
.up .state{color:var(--up)}.down .state{color:var(--down)}.unknown .state{color:var(--unknown)}
.since,.why{color:var(--muted);font-size:.9rem}
.why{margin:.25rem 0 0 1rem;list-style:disc}
"""


def _stamp(moment: datetime) -> str:
    return f"{moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def _row_html(row: Row) -> str:
    alerts = " ".join(finding.alert for finding in row.alerts)
    if row.state == STATE_DOWN:
        since = f"с {_stamp(row.since)}" if row.since else ""
    elif row.state == STATE_UP:
        since = f"с {_stamp(row.since)}" if row.since else "сбоев за 7 дн. не было"
    else:
        since = ""
    reasons = "".join(
        f"<li>{html.escape(finding.alert)}: {html.escape(finding.description)}</li>"
        for finding in row.alerts
    )
    return (
        f'<li class="row {row.state}" data-env="{html.escape(row.env)}" '
        f'data-component="{html.escape(row.key)}" data-state="{row.state}" '
        f'data-alerts="{html.escape(alerts)}">'
        f'<span class="name">{html.escape(row.name)}</span>'
        f'<span class="state">{_STATE_TEXT[row.state]}</span>'
        + (f' <span class="since">{html.escape(since)}</span>' if since else "")
        + (f'<ul class="why">{reasons}</ul>' if reasons else "")
        + "</li>"
    )


def render_page(
    rows: Sequence[Row],
    snapshot: Snapshot | None,
    now: datetime,
    shown: Sequence[str] = ENVIRONMENTS,
) -> str:
    """The HTML of the page: only the texts of this module and times, nothing from Prometheus."""
    if snapshot is None:
        updated = "Данные не получены"
    elif now - snapshot.taken_at > STALE_AFTER:
        updated = f"Данные устарели: последнее обновление {_stamp(snapshot.taken_at)}"
    else:
        updated = f"Обновлено: {_stamp(snapshot.taken_at)}"
    sections = "".join(
        f'<section data-env="{env}"><h2>{_ENVIRONMENT_TITLE[env]}</h2><ul>'
        + "".join(_row_html(row) for row in rows if row.env == env)
        + "</ul></section>"
        for env in ENVIRONMENTS
        if env in shown
    )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="30">'
        f"<title>Статус сервиса</title><style>{_STYLE}</style></head>"
        f'<body><main><h1>Статус сервиса</h1><p class="updated">{html.escape(updated)}</p>'
        f"{sections}</main></body></html>"
    )


def visible_environments(host_header: str) -> tuple[str, ...]:
    """``staging-status.<domain>`` shows staging only; any other name (a healthcheck) shows all."""
    host = host_header.lower().split(":", 1)[0]
    return ("staging",) if host.startswith("staging-status.") else ENVIRONMENTS


# --- the server ----------------------------------------------------------------------------

_HEADERS = (
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Cache-Control", "no-store"),
)


def make_server(poller: Poller, bind: str, port: int) -> ThreadingHTTPServer:
    """GET and HEAD on ``/`` answer the page, any other path 404, any other method 405."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "status-page"
        sys_version = ""
        timeout = 10  # a stalled client does not hold a thread

        def _reply(self, status: int, body: bytes, kind: str, head: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for name, value in _HEADERS:
                self.send_header(name, value)
            if status == 405:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _page(self, head: bool) -> None:
            if self.path.split("?", 1)[0] != "/":
                self._reply(404, b"Not Found\n", "text/plain; charset=utf-8", head)
                return
            now = poller.clock()
            snapshot = poller.snapshot()
            page = render_page(
                build_status(snapshot, now),
                snapshot,
                now,
                visible_environments(self.headers.get("Host", "")),
            )
            self._reply(200, page.encode(), "text/html; charset=utf-8", head)

        def do_GET(self) -> None:
            self._page(head=False)

        def do_HEAD(self) -> None:
            self._page(head=True)

        def _not_allowed(self) -> None:
            self._reply(405, b"Method Not Allowed\n", "text/plain; charset=utf-8")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    return ThreadingHTTPServer((bind, port), Handler)


def main() -> None:
    poller = Poller(os.environ.get("PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL))
    server = make_server(poller, "0.0.0.0", int(os.environ.get("PORT", DEFAULT_PORT)))
    stop = threading.Event()
    threading.Thread(target=poller.run, args=(stop,), daemon=True).start()
    # PID 1 of the container ignores SIGTERM by default: stop at once instead of after the kill.
    signal.signal(
        signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start()
    )
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
