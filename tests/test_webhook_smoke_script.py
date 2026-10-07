"""The signed webhook smoke script (`scripts/webhook_smoke.py`) against the real route (#56).

The script's deliveries go through the real `POST /webhooks/github` route in-process, with a fake
receipt store and the secret overridden, so no PostgreSQL or container is needed here. The CI job
"Webhook container smoke" runs the same script against the built image and a migrated database,
then checks the outcome line of the delivery in the log of the image's webhook worker (#80).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import app, get_github_webhook_secret
from tests.test_github_webhook_delivery import FakeReceiptUnitOfWork

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "webhook_smoke.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"
_SECRET = "smoke-test-secret"
_URL = "http://testserver/webhooks/github"


@pytest.fixture(scope="module")
def smoke() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("webhook_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture
def receipts() -> Iterator[FakeReceiptUnitOfWork]:
    store = FakeReceiptUnitOfWork()
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: lambda: store
    try:
        yield store
    finally:
        app.dependency_overrides.clear()


def _in_process(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> tuple[int, object, float]:
    """The script's sender, served by the real route in-process instead of over a socket."""
    started = time.perf_counter()
    response = TestClient(app).post(urlsplit(url).path, content=body, headers=dict(headers))
    return response.status_code, response.json(), time.perf_counter() - started


def _assert_no_secret(output: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in output
    assert "sha256=" not in output


def test_built_delivery_is_stored_once_by_the_real_route(
    smoke: ModuleType, receipts: FakeReceiptUnitOfWork
) -> None:
    delivery_id = "smoke-delivery-1"
    body, headers = smoke.build_delivery(_SECRET, delivery_id)
    client = TestClient(app)

    first = client.post("/webhooks/github", content=body, headers=headers)
    second = client.post("/webhooks/github", content=body, headers=headers)

    assert (first.status_code, first.json()) == (202, {"status": "pending"})
    assert (second.status_code, second.json()) == (202, {"status": "duplicate"})
    assert list(receipts.rows) == [delivery_id]
    stored = receipts.rows[delivery_id].delivery
    assert stored.event_name == "pull_request"
    assert json.loads(stored.payload_json)["action"] == "labeled"
    assert json.loads(body)["label"] == {"name": "ai-review"}


def test_corrupted_signature_passes_the_format_check_but_is_rejected(
    smoke: ModuleType, receipts: FakeReceiptUnitOfWork
) -> None:
    delivery_id = "smoke-delivery-2"
    body, headers = smoke.build_delivery(_SECRET, delivery_id)
    corrupted = smoke.corrupt_signature(headers)

    response = TestClient(app).post("/webhooks/github", content=body, headers=corrupted)

    assert corrupted["X-Hub-Signature-256"] != headers["X-Hub-Signature-256"]
    assert re.fullmatch(r"sha256=[0-9a-f]{64}", corrupted["X-Hub-Signature-256"])
    assert (response.status_code, response.json()) == (
        401,
        {"detail": "invalid GitHub webhook signature"},
    )
    assert receipts.rows == {}


@pytest.mark.parametrize("count", [0, 1, 3])
def test_main_passes_every_check_against_the_real_route(
    smoke: ModuleType,
    receipts: FakeReceiptUnitOfWork,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    count: int,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)

    assert smoke.main(["--url", _URL, "--count", str(count)], send=_in_process) == 0

    out, err = capsys.readouterr()
    # The first delivery and the burst are stored; the duplicate and the corrupted one are not.
    assert len(receipts.rows) == 1 + count
    assert ("p95" in out) is (count > 0)
    _assert_no_secret(out + err, _SECRET)


def test_main_reports_a_secret_mismatch_as_an_unexpected_response(
    smoke: ModuleType,
    receipts: FakeReceiptUnitOfWork,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "another-smoke-secret")

    assert smoke.main(["--url", _URL], send=_in_process) == 1

    out, err = capsys.readouterr()
    assert "expected 202" in out + err
    assert "got 401" in out + err
    assert receipts.rows == {}
    _assert_no_secret(out + err, _SECRET, "another-smoke-secret")


def test_main_truncates_an_unexpected_body(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)

    def server_error(
        url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, object, float]:
        return 500, "Internal Server Error " * 100, 0.002

    assert smoke.main(["--url", _URL], send=server_error) == 1

    out, err = capsys.readouterr()
    assert "got 500" in out + err
    assert "Internal Server Error " * 20 not in out + err
    # Compose migrates in bootstrap on the way up; only a stack without it migrates by hand.
    assert "uv run alembic upgrade head" in out + err
    _assert_no_secret(out + err, _SECRET)


@pytest.mark.parametrize("secret", [None, ""])
def test_main_without_a_secret_exits_2_before_sending(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    secret: str | None,
) -> None:
    if secret is None:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

    def refuse(
        url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, object, float]:
        raise AssertionError("nothing is sent without a secret")

    assert smoke.main(["--url", _URL], send=refuse) == 2
    assert "GITHUB_WEBHOOK_SECRET" in capsys.readouterr().err


def test_main_exits_2_with_a_hint_when_the_api_is_unreachable(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)

    def unreachable(
        url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, object, float]:
        raise URLError(ConnectionRefusedError(111, "Connection refused"))

    assert smoke.main(["--url", _URL], send=unreachable) == 2

    out, err = capsys.readouterr()
    # With Compose, bootstrap migrates before backend starts: no manual step for that stack.
    assert "uv run alembic upgrade head" in err
    assert "compose run" not in err
    assert "(docker compose logs bootstrap)" in err
    _assert_no_secret(out + err, _SECRET)


@pytest.mark.parametrize(
    "url", ["localhost:8000/webhooks/github", "/webhooks/github", "ftp://localhost/webhooks/github"]
)
def test_main_rejects_a_url_without_an_http_scheme_before_sending(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)

    def refuse(
        url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, object, float]:
        raise AssertionError("nothing is sent to an invalid --url")

    with pytest.raises(SystemExit) as exited:
        smoke.main(["--url", url], send=refuse)

    assert exited.value.code == 2
    out, err = capsys.readouterr()
    assert "--url" in err
    _assert_no_secret(out + err, _SECRET)


class _Recorder(BaseHTTPRequestHandler):
    """Answers like the route would: 202 JSON, 401 JSON, or a plain-text 500."""

    replies = {
        "/accepted": (202, "application/json", b'{"status": "pending"}'),
        "/rejected": (401, "application/json", b'{"detail": "invalid GitHub webhook signature"}'),
        "/broken": (500, "text/plain", b"Internal Server Error"),
    }
    received: list[tuple[dict[str, str], bytes]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.received.append((dict(self.headers.items()), self.rfile.read(length)))
        status, content_type, payload = self.replies[self.path]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def http_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        _Recorder.received.clear()


@pytest.mark.parametrize(
    ("path", "status", "payload"),
    [
        ("/accepted", 202, {"status": "pending"}),
        ("/rejected", 401, {"detail": "invalid GitHub webhook signature"}),
        ("/broken", 500, "Internal Server Error"),
    ],
)
def test_post_delivery_returns_status_and_body_for_any_http_answer(
    smoke: ModuleType, http_server: str, path: str, status: int, payload: object
) -> None:
    delivery_id = "smoke-delivery-3"
    body, headers = smoke.build_delivery(_SECRET, delivery_id)

    got_status, got_payload, elapsed = smoke.post_delivery(http_server + path, body, headers, 5.0)

    assert (got_status, got_payload) == (status, payload)
    assert elapsed > 0
    sent_headers, sent_body = _Recorder.received[0]
    assert sent_body == body
    assert sent_headers["Content-Type"] == "application/json"
    assert sent_headers["X-Hub-Signature-256"] == headers["X-Hub-Signature-256"]


def test_post_delivery_raises_os_error_when_nothing_listens(smoke: ModuleType) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    closed_port = server.server_address[1]
    server.server_close()
    delivery_id = "smoke-delivery-4"
    body, headers = smoke.build_delivery(_SECRET, delivery_id)

    with pytest.raises(OSError):
        smoke.post_delivery(f"http://127.0.0.1:{closed_port}/", body, headers, 5.0)


class _NotHttp(BaseHTTPRequestHandler):
    """Reads the whole request, then answers with a line that is not an HTTP status line."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.wfile.write(b"SSH-2.0-OpenSSH_9.6\r\n")

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_main_exits_2_with_a_hint_when_the_listener_is_not_http(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NotHttp)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/webhooks/github"
        code = smoke.main(["--url", url, "--timeout", "5"])
    finally:
        server.shutdown()
        server.server_close()

    assert code == 2
    out, err = capsys.readouterr()
    assert "no HTTP answer" in err
    assert "hint:" in err
    _assert_no_secret(out + err, _SECRET)


def test_percentile_is_nearest_rank(smoke: ModuleType) -> None:
    samples = [5.0, 1.0, 4.0, 2.0, 3.0]

    assert smoke.percentile(samples, 50) == 3.0
    assert smoke.percentile(samples, 95) == 5.0
    assert smoke.percentile([7.0], 95) == 7.0


def test_ci_smokes_the_built_image_before_the_push() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow[workflow.index("  webhook-smoke:") : workflow.index("  push-image:")]
    push_image = workflow[workflow.index("  push-image:") : workflow.index("  deploy-staging:")]

    assert "name: Webhook container smoke" in job
    assert "needs: docker-build" in job
    assert "alembic upgrade head" in job
    assert "astral-sh/setup-uv@" in job
    assert "uv run --locked python scripts/webhook_smoke.py" in job
    # A throwaway secret generated in the job, never a repository or environment secret.
    assert "openssl rand -hex 32" in job
    assert "::add-mask::" in job
    assert "secrets." not in job
    assert "- webhook-smoke" in push_image
    # After the smoke the webhook worker from the same image replays the stored deliveries
    # through the GitHub stub; its log must show the outcome of the signed `labeled` delivery
    # (repository 101 is not seeded). The App key is throwaway too (#80).
    assert "python -m app.webhook_worker" in job
    worker = job.index("python -m app.webhook_worker")
    assert job.index("scripts/webhook_smoke.py") < worker
    assert job.index("uv run --locked python scripts/github_stub.py") < worker
    assert "--name webhook-worker" in job
    assert "GITHUB_API_URL=http://127.0.0.1:9999" in job
    assert "openssl genrsa 2048" in job
    wait = job[
        job.index("- name: Wait for the labeled outcome") : job.index("- name: API container logs")
    ]
    assert (
        "expected='event=pull_request status=ignored_unknown_repository "
        "detail=action=labeled unknown_repository'"
    ) in wait
    assert 'logs="$(docker logs webhook-worker 2>&1)"' in wait
    assert 'grep -m 1 -F "${expected}" <<< "${logs}"' in wait
    # A worker that exited (e.g. on its configuration) fails the step early.
    assert "docker inspect -f '{{.State.Running}}' webhook-worker" in wait
    # No line before the timeout: the loop ends and the step fails.
    assert wait.rstrip().endswith("exit 1")
    assert "docker logs webhook-worker || true" in job
