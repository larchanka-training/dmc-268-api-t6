"""The parallel-Runs load scenario: its guard, metrics, verdict and stub (#163); no docker."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from app.modules.integrations.webhooks.api.pull_request_dtos import parse_pull_request_label_event
from app.modules.integrations.webhooks.infrastructure.github_pull_request_dtos import (
    parse_current_pull_request,
)
from app.modules.reviews.infrastructure.llm.answers import (
    conventions_answer_validator,
    validate_review_answer,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str) -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture(scope="module")
def driver() -> Iterator[ModuleType]:
    yield from _load("parallel_runs_load")


@pytest.fixture(scope="module")
def stub() -> Iterator[ModuleType]:
    yield from _load("review_load_stub")


# --- project guard ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "project,message",
    [
        (None, "--project is required: the load stack runs under its own compose project"),
        ("", "--project is required: the load stack runs under its own compose project"),
        ("dmc-268-api-t6", "--project must differ from the shared local stand 'dmc-268-api-t6'"),
        ("Dmc163load", "--project must be lowercase letters, digits, '-' or '_'"),
        ("dmc163 load", "--project must be lowercase letters, digits, '-' or '_'"),
    ],
    ids=["missing", "empty", "shared-stand", "uppercase", "space"],
)
def test_project_guard_refuses_the_shared_stand(
    driver: ModuleType, project: str | None, message: str
) -> None:
    assert driver.project_error(project) == message


def test_project_guard_refuses_this_checkouts_default_project(
    driver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver, "REPO_ROOT", Path("/work/My.Clone-163d"))

    assert driver.compose_project_name("My.Clone-163d") == "myclone-163d"
    assert driver.project_error("myclone-163d") == (
        "--project must differ from 'myclone-163d', this checkout's default compose project"
    )
    assert driver.project_error("dmc163load") is None


@pytest.mark.parametrize(
    "argv",
    [[], ["--project", "dmc-268-api-t6"], ["--project", "dmc-268-api-t6", "--keep"]],
    ids=["no-project", "shared-stand", "shared-stand-keep"],
)
def test_main_exits_2_before_any_docker_command(
    driver: ModuleType, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    def no_docker(*args: object, **kwargs: object) -> None:
        raise AssertionError("docker must not run")

    monkeypatch.setattr(driver.subprocess, "run", no_docker)

    assert driver.main(argv) == 2


@pytest.mark.parametrize(
    "argv,message",
    [
        (["--runs", "5", "--workers", "6"], "--workers must be from 1 to --runs"),
        (["--runs", "0"], "--runs must be from 1 to 20"),
        (["--workers", "0"], "--workers must be from 1 to --runs"),
    ],
    ids=["more-workers-than-runs", "no-runs", "no-workers"],
)
def test_invalid_run_shapes_exit_2(
    driver: ModuleType, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        driver.parse_args(["--project", "dmc163test", *argv])

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_workers_default_to_runs(driver: ModuleType) -> None:
    assert driver.parse_args(["--runs", "7"]).workers == 7
    assert driver.parse_args(["--runs", "10", "--workers", "5"]).workers == 5


def test_every_compose_command_names_the_project_and_both_files(driver: ModuleType) -> None:
    assert driver.compose_command("dmc163load", "down", "-v") == [
        "docker",
        "compose",
        "-p",
        "dmc163load",
        "--env-file",
        "/dev/null",
        "-f",
        "docker-compose.yml",
        "-f",
        "deploy/load/compose.parallel-runs.yml",
        "--profile",
        "webhooks",
        "down",
        "-v",
    ]


def test_compose_sees_only_path_home_docker_and_the_generated_values(driver: ModuleType) -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "DOCKER_HOST": "unix:///run/docker.sock",
        "DOCKER_CONFIG": "/home/u/.docker",
        "AUTH_JWT_PRIVATE_KEY": "real-key",
        "LLM_API_KEYS": "real-llm-key",
        "GITHUB_APP_PRIVATE_KEY": "real-app-key",
        "COMPOSE_PROFILES": "everything",
        "POSTGRES_USER": "someone-else",
    }

    env = driver.stack_env(environ, {"BACKEND_PORT": "18001", "LOAD_WEBHOOK_SECRET": "s"})

    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "DOCKER_HOST": "unix:///run/docker.sock",
        "DOCKER_CONFIG": "/home/u/.docker",
        "POSTGRES_USER": "app",
        "POSTGRES_PASSWORD": "app",
        "POSTGRES_DB": "app",
        "RABBITMQ_USER": "app",
        "RABBITMQ_PASSWORD": "app",
        "BACKEND_PORT": "18001",
        "LOAD_WEBHOOK_SECRET": "s",
    }


# --- metrics and verdict ---------------------------------------------------------------------


def _row(pr: int, **values: object) -> dict[str, object]:
    row: dict[str, object] = {
        "pr": pr,
        "state": "succeeded",
        "attempt": 1,
        "worker_id": f"worker-{pr}:1",
        "error_code": None,
        "received": 1000.0,
        "created": 1000.25,
        "published": 1000.3,
        "started": 1000.5,
        "finished": 1007.125,
    }
    row.update(values)
    return row


def test_run_metrics_on_literal_rows(driver: ModuleType) -> None:
    rows = [
        _row(1),
        _row(2, state="queued", attempt=0, worker_id=None, started=None, finished=None),
        _row(3, received=None, published=None, error_code="llm_unavailable", state="failed"),
    ]

    metrics = driver.run_metrics(rows)

    assert [(m.pr, m.state, m.attempt, m.worker_id, m.error_code) for m in metrics] == [
        (1, "succeeded", 1, "worker-1:1", None),
        (2, "queued", 0, None, None),
        (3, "failed", 1, "worker-3:1", "llm_unavailable"),
    ]
    assert [(m.projection_ms, m.queue_wait_ms, m.broker_ms, m.total_ms) for m in metrics] == [
        (250.0, 250.0, 200.0, 6875.0),
        (250.0, None, None, None),
        (None, 250.0, None, 6875.0),
    ]


def test_max_parallel_counts_overlapping_runs(driver: ModuleType) -> None:
    overlapping = [_row(n, started=1000.0 + n / 10, finished=1010.0) for n in range(1, 6)]
    back_to_back = [
        _row(1, started=0.0, finished=10.0),
        _row(2, started=1.0, finished=5.0),
        _row(3, started=5.0, finished=6.0),
        _row(4, started=11.0, finished=12.0),
        _row(5, started=None, finished=None),
    ]

    assert driver.max_parallel(overlapping) == 5
    assert driver.max_parallel(back_to_back) == 2
    assert driver.max_parallel([]) == 0


_CHECKS = (
    "finished_in_time",
    "all_succeeded",
    "all_first_attempt",
    "one_run_per_pr",
    "one_review_per_pr",
    "one_review_call_per_run",
    "every_delivery_projected",
    "all_workers_used",
    "parallel",
    "queue_wait_within_limit",
    "dlq_empty",
    "no_unhandled_stub_requests",
)
_ROWS = [_row(1), _row(2), _row(3)]
_GOOD: dict[str, Any] = {
    "review_posts": {"1": 1, "2": 1, "3": 1},
    "llm_calls": {"RepoConventionsDraft": 3, "ReviewOutput": 3},
    "receipts": {"projected": 6},
    "dlq_depth": 0,
    "peak": 3,
    "timed_out": False,
    "unhandled": [],
}


@pytest.mark.parametrize(
    "rows,changes,workers,failed",
    [
        (_ROWS, {}, 3, []),
        (_ROWS, {"timed_out": True}, 3, ["finished_in_time"]),
        ([_row(1), _row(2, state="failed"), _row(3)], {}, 3, ["all_succeeded"]),
        ([_row(1), _row(2, attempt=2), _row(3)], {}, 3, ["all_first_attempt"]),
        ([_row(1), _row(3)], {}, 2, ["one_run_per_pr"]),
        ([*_ROWS, _row(3, worker_id="worker-3:1")], {}, 3, ["one_run_per_pr"]),
        (_ROWS, {"review_posts": {"1": 1, "2": 2, "3": 1}}, 3, ["one_review_per_pr"]),
        (_ROWS, {"review_posts": {"1": 1, "3": 1}}, 3, ["one_review_per_pr"]),
        (_ROWS, {"llm_calls": {"ReviewOutput": 4}}, 3, ["one_review_call_per_run"]),
        (_ROWS, {"llm_calls": {}}, 3, ["one_review_call_per_run"]),
        (_ROWS, {"receipts": {"projected": 3}}, 3, ["every_delivery_projected"]),
        (_ROWS, {"receipts": {"projected": 5, "failed": 1}}, 3, ["every_delivery_projected"]),
        ([_row(1), _row(2), _row(3, worker_id="worker-1:1")], {}, 3, ["all_workers_used"]),
        (_ROWS, {}, 2, ["all_workers_used"]),
        (_ROWS, {"peak": 2}, 3, ["parallel"]),
        (_ROWS, {"peak": 2}, 2, ["all_workers_used"]),
        ([_row(1), _row(2, started=1030.26), _row(3)], {}, 3, ["queue_wait_within_limit"]),
        ([_row(1), _row(2, started=None), _row(3)], {}, 3, ["queue_wait_within_limit"]),
        (_ROWS, {"dlq_depth": 1}, 3, ["dlq_empty"]),
        (_ROWS, {"dlq_depth": None}, 3, ["dlq_empty"]),
        (_ROWS, {"unhandled": ["GET /nowhere"]}, 3, ["no_unhandled_stub_requests"]),
    ],
    ids=[
        "pass",
        "timed-out",
        "failed-run",
        "second-attempt",
        "missing-run",
        "extra-run",
        "two-reviews",
        "missing-review",
        "extra-review-call",
        "no-review-call",
        "duplicate-not-projected",
        "failed-receipt",
        "idle-worker",
        "fewer-workers-than-expected",
        "serial-runs",
        "peak-meets-fewer-workers",
        "queue-wait-over-limit",
        "never-started",
        "dlq-message",
        "no-dlq",
        "unhandled-stub-request",
    ],
)
def test_verdict_rules(
    driver: ModuleType,
    rows: list[dict[str, object]],
    changes: dict[str, Any],
    workers: int,
    failed: list[str],
) -> None:
    observed = driver.Observed(**{**_GOOD, **changes})

    checks = driver.verdict(
        driver.run_metrics(rows), observed, runs=3, workers=workers, max_queue_wait_ms=30000.0
    )

    assert checks == {name: name not in failed for name in _CHECKS}


def test_queue_wait_limit_is_inclusive(driver: ModuleType) -> None:
    rows = [_row(1, started=1030.25), _row(2), _row(3)]

    checks = driver.verdict(
        driver.run_metrics(rows),
        driver.Observed(**_GOOD),
        runs=3,
        workers=3,
        max_queue_wait_ms=30000.0,
    )

    assert checks["queue_wait_within_limit"]


# --- deliveries and the stub agree -----------------------------------------------------------


def test_driver_and_stub_serve_the_same_pull_requests(driver: ModuleType, stub: ModuleType) -> None:
    assert (driver.INSTALLATION_ID, driver.REPOSITORY_ID, driver.REPOSITORY) == (
        stub.INSTALLATION_ID,
        stub.REPOSITORY_ID,
        stub.REPOSITORY,
    )
    assert (driver.PR_ID_OFFSET, driver.BASE_SHA, driver.MAX_RUNS) == (
        stub.PR_ID_OFFSET,
        stub.BASE_SHA,
        stub.MAX_PRS,
    )
    heads = [stub.head_sha(n) for n in range(1, stub.MAX_PRS + 1)]
    assert heads == [driver.head_sha(n) for n in range(1, driver.MAX_RUNS + 1)]
    assert len(set(heads)) == 20
    assert {stub.BASE_SHA, stub.FILE_SHA, stub.README_SHA}.isdisjoint(heads)
    assert stub.head_sha(1) == "01" * 20
    for number in (1, 5, 20):
        body, _ = driver.build_delivery("secret", number)
        labeled = parse_pull_request_label_event(json.loads(body))

        current = parse_current_pull_request(labeled.pull_request, stub.pull_request(number))

        assert labeled.label_name == "ai-review"
        assert labeled.pull_request.installation_external_id == 17
        assert current.number == number
        assert current.head_sha == stub.head_sha(number)
        assert current.base_sha == "b" * 40
        assert current.current_label_names == frozenset({"ai-review"})


def test_delivery_is_signed_with_a_fresh_load_id(driver: ModuleType) -> None:
    body, headers = driver.build_delivery("s3cret", 4)
    _, again = driver.build_delivery("s3cret", 4)

    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers["X-Hub-Signature-256"] == f"sha256={expected}"
    assert headers["X-GitHub-Event"] == "pull_request"
    assert re.fullmatch(r"loadtest-runs-[0-9a-f-]{36}", headers["X-GitHub-Delivery"])
    assert headers["X-GitHub-Delivery"] != again["X-GitHub-Delivery"]
    payload = json.loads(body)
    assert payload["action"] == "labeled"
    assert payload["pull_request"]["id"] == 9004
    assert payload["pull_request"]["head"]["sha"] == "04" * 20


# --- stub ------------------------------------------------------------------------------------


def _chat(schema: str) -> bytes:
    request = {
        "model": "load-stub-model",
        "messages": [{"role": "user", "content": "review"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": schema}},
    }
    return json.dumps(request).encode()


def _content(reply: tuple[int, Any]) -> str:
    status, payload = reply
    assert status == 200
    content = payload["choices"][0]["message"]["content"]
    assert isinstance(content, str)
    return content


def test_stub_llm_answers_validate_against_both_schemas(stub: ModuleType) -> None:
    delays: list[float] = []
    state = stub.StubState(prs=5, llm_delay_ms=3000, sleep=delays.append)

    conventions = _content(
        state.handle("POST", "/v1/chat/completions", _chat("RepoConventionsDraft"))
    )
    review = _content(state.handle("POST", "/v1/chat/completions", _chat("ReviewOutput")))

    assert conventions_answer_validator(("app/example.py",))(conventions)["key_patterns"] == [
        "Pattern one.",
        "Pattern two.",
        "Pattern three.",
    ]
    accepted = validate_review_answer(review)
    assert accepted["summary"] == {
        "problem": "The branch is inverted.",
        "done_well": "The change is small.",
        "effort": "small",
    }
    assert delays == [3.0, 3.0]
    assert state.stats()["llm_calls"] == {"RepoConventionsDraft": 1, "ReviewOutput": 1}


def test_stub_rejects_an_unknown_schema_without_delay(stub: ModuleType) -> None:
    delays: list[float] = []
    state = stub.StubState(prs=5, llm_delay_ms=3000, sleep=delays.append)

    status, payload = state.handle("POST", "/v1/chat/completions", _chat("VerifierOutput"))

    assert status == 400
    assert payload == {
        "error": {"message": "the load stub has no answer for schema 'VerifierOutput'"}
    }
    assert delays == []
    assert state.stats()["llm_calls"] == {"VerifierOutput": 1}


def _review(commit_id: str, comments: int) -> bytes:
    body = {
        "commit_id": commit_id,
        "event": "COMMENT",
        "body": "Review\n\n<!-- marker -->",
        "comments": [{"path": "app/example.py", "line": 11, "body": "x"}] * comments,
    }
    return json.dumps(body).encode()


def test_stub_counts_review_posts_per_pr_and_lists_them(stub: ModuleType) -> None:
    state = stub.StubState(prs=5, llm_delay_ms=0)
    repo = "/repos/load/parallel-runs/pulls"

    first = state.handle("POST", f"{repo}/1/reviews", _review("01" * 20, 2))
    state.handle("POST", f"{repo}/1/reviews", _review("01" * 20, 0))
    state.handle("POST", f"{repo}/3/reviews", _review("03" * 20, 1))

    assert state.stats()["review_posts"] == {"1": 2, "3": 1}
    status, listed = state.handle("GET", f"{repo}/1/reviews?per_page=100", b"")
    assert status == 200
    assert [item["body"] for item in listed] == ["Review\n\n<!-- marker -->"] * 2
    assert state.handle("GET", f"{repo}/2/reviews", b"") == (200, [])
    review_id = first[1]["id"]
    status, comments = state.handle("GET", f"{repo}/1/reviews/{review_id}/comments", b"")
    assert status == 200 and len(comments) == 2


def test_stub_keeps_check_runs_per_head(stub: ModuleType) -> None:
    state = stub.StubState(prs=5, llm_delay_ms=0)
    repo = "/repos/load/parallel-runs"
    payload = {"name": "AI review", "head_sha": "02" * 20, "external_id": "run-2"}

    status, created = state.handle("POST", f"{repo}/check-runs", json.dumps(payload).encode())

    assert status == 201
    status, listed = state.handle("GET", f"{repo}/commits/{'02' * 20}/check-runs?filter=all", b"")
    assert status == 200
    assert listed["check_runs"] == [
        {"id": created["id"], "name": "AI review", "head_sha": "02" * 20, "external_id": "run-2"}
    ]
    assert state.handle("GET", f"{repo}/commits/{'01' * 20}/check-runs", b"") == (
        200,
        {"total_count": 0, "check_runs": []},
    )
    assert state.handle("PATCH", f"{repo}/check-runs/{created['id']}", b"{}") == (
        200,
        {"id": created["id"]},
    )


def test_stub_serves_the_worker_reads_of_each_pr(stub: ModuleType) -> None:
    state = stub.StubState(prs=5, llm_delay_ms=0)
    repo = "/repos/load/parallel-runs"
    head = "05" * 20

    assert state.handle("POST", "/app/installations/17/access_tokens", b"")[0] == 201
    assert state.handle("GET", f"{repo}/pulls/5", b"")[1]["head"]["sha"] == head
    assert state.handle("GET", f"{repo}/pulls/5/commits?per_page=100", b"") == (
        200,
        [{"commit": {"message": "feat: load change 5"}}],
    )
    files = state.handle("GET", f"{repo}/pulls/5/files?per_page=100&page=1", b"")[1]
    assert [(item["filename"], item["sha"]) for item in files] == [("app/example.py", "f" * 40)]
    assert state.handle("GET", f"{repo}/compare/{'b' * 40}...{head}", b"") == (
        200,
        {"merge_base_commit": {"sha": "b" * 40}},
    )
    head_tree = state.handle("GET", f"{repo}/git/trees/{head}?recursive=1", b"")[1]["tree"]
    assert [item["path"] for item in head_tree] == ["app/example.py"]
    base_tree = state.handle("GET", f"{repo}/git/trees/{'b' * 40}", b"")[1]["tree"]
    assert [item["path"] for item in base_tree] == ["README.md"]
    blob = state.handle("GET", f"{repo}/git/blobs/{'f' * 40}", b"")[1]
    assert (blob["encoding"], blob["content"]) == ("base64", "bGluZTEwCmxpbmUxMQpsaW5lMTIK")
    assert state.stats()["unhandled"] == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/repos/load/parallel-runs/pulls/6"),
        ("GET", "/repos/load/parallel-runs/git/trees/" + "06" * 20),
        ("GET", "/repos/other/repo/pulls/1"),
        ("POST", "/app/installations/18/access_tokens"),
        ("GET", "/repos/load/parallel-runs/tarball/main"),
    ],
    ids=["pr-out-of-range", "unknown-head", "other-repo", "other-installation", "tarball"],
)
def test_stub_answers_404_and_lists_unhandled_requests(
    stub: ModuleType, method: str, path: str
) -> None:
    state = stub.StubState(prs=5, llm_delay_ms=0)

    assert state.handle(method, path + "?page=1", b"") == (404, {"message": "Not Found"})
    assert state.stats()["unhandled"] == [f"{method} {path}"]


def test_stub_http_server_routes_through_the_state(stub: ModuleType) -> None:
    state = stub.StubState(prs=2, llm_delay_ms=0)
    server = stub.make_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        posted = httpx.post(
            f"{base}/repos/load/parallel-runs/pulls/2/reviews", content=_review("02" * 20, 1)
        )
        missing = httpx.get(f"{base}/nowhere")
        stats = httpx.get(f"{base}/_stats").json()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert posted.status_code == 200 and posted.json() == {"id": 1001}
    assert missing.status_code == 404
    assert stats == {"review_posts": {"2": 1}, "llm_calls": {}, "unhandled": ["GET /nowhere"]}


# --- compose override ------------------------------------------------------------------------


def _service_env(path: Path, service: str) -> str:
    text = path.read_text()
    block = text.split(f"\n  {service}:\n", 1)[1]
    block = re.split(r"\n  [a-z-]+:\n", block, maxsplit=1)[0]
    return block.split("    environment:\n", 1)[1]


def test_override_pins_every_llm_and_github_setting_of_the_worker() -> None:
    base = _service_env(REPO_ROOT / "docker-compose.yml", "worker")
    override = _service_env(REPO_ROOT / "deploy/load/compose.parallel-runs.yml", "worker")
    llm_keys = re.findall(r"(?m)^      (LLM_[A-Z_]+):", base)

    assert len(llm_keys) == 15
    for key in [*llm_keys, "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_API_URL"]:
        assert re.search(rf"(?m)^      {key}: ", override), key
    # Only the throwaway App key comes from the environment the driver builds.
    assert re.findall(r"\$\{(\w+)", override) == ["LOAD_GITHUB_APP_PRIVATE_KEY"]
    assert '      LLM_API_KEYS: ""\n' in override
    assert "      LLM_BASE_URL: http://stub:9999/v1\n" in override


def test_override_keeps_the_stub_on_loopback_and_points_every_github_client_at_it() -> None:
    text = (REPO_ROOT / "deploy/load/compose.parallel-runs.yml").read_text()

    assert (
        '      - "127.0.0.1:${LOAD_STUB_PORT:?set by scripts/parallel_runs_load.py}:9999"\n' in text
    )
    assert "      - ./scripts/review_load_stub.py:/srv/load/review_load_stub.py:ro\n" in text
    for service in ("backend", "worker", "webhook-worker"):
        assert "      GITHUB_API_URL: http://stub:9999\n" in _service_env(
            REPO_ROOT / "deploy/load/compose.parallel-runs.yml", service
        ), service


def test_override_publishes_on_loopback_without_yaml_tags() -> None:
    text = (REPO_ROOT / "deploy/load/compose.parallel-runs.yml").read_text()
    base = (REPO_ROOT / "docker-compose.yml").read_text()

    # No `!override` / `!reset`: the repo's check-yaml hook (PyYAML safe_load) rejects tags.
    assert not re.search(r"(?m):\s*!\w", text)
    assert re.findall(r"(?m)^    ports:$", text) == ["    ports:"]  # only the stub's
    published = re.findall(r'(?m)^      - "([^"]+:\d+)"$', text)
    assert published == ["127.0.0.1:${LOAD_STUB_PORT:?set by scripts/parallel_runs_load.py}:9999"]
    # The driver's "127.0.0.1:<port>" values rely on the first two mappings; the base file keeps
    # the broker and Redis on loopback itself.
    for entry in (
        '      - "${BACKEND_PORT:-8000}:8000"\n',
        '      - "${POSTGRES_PORT:-5432}:5432"\n',
        '      - "127.0.0.1:${RABBITMQ_PORT:-5672}:5672"\n',
        '      - "127.0.0.1:${RABBITMQ_MANAGEMENT_PORT:-15672}:15672"\n',
        '      - "127.0.0.1:${REDIS_PORT:-6379}:6379"\n',
    ):
        assert entry in base


def test_port_values_carry_the_loopback_address_where_the_base_file_needs_it(
    driver: ModuleType,
) -> None:
    ports = {
        "BACKEND_PORT": 18001,
        "POSTGRES_PORT": 18002,
        "RABBITMQ_PORT": 18003,
        "RABBITMQ_MANAGEMENT_PORT": 18004,
        "REDIS_PORT": 18005,
        "LOAD_STUB_PORT": 18006,
    }

    assert sorted(driver.PORT_FLAGS.values()) == sorted(ports)
    assert driver.port_env(ports) == {
        "BACKEND_PORT": "127.0.0.1:18001",
        "POSTGRES_PORT": "127.0.0.1:18002",
        "RABBITMQ_PORT": "18003",
        "RABBITMQ_MANAGEMENT_PORT": "18004",
        "REDIS_PORT": "18005",
        "LOAD_STUB_PORT": "18006",
    }


def _config(*ports: dict[str, object]) -> dict[str, Any]:
    return {
        "services": {
            "backend": {"ports": list(ports)},
            "redis": {"ports": [{"host_ip": "127.0.0.1", "published": "18005", "target": 6379}]},
            "worker": {},
        }
    }


def test_exposed_ports_lists_everything_not_on_loopback(driver: ModuleType) -> None:
    loopback = {"host_ip": "127.0.0.1", "published": "18001", "target": 8000}

    assert driver.exposed_ports(_config(loopback)) == []
    assert driver.exposed_ports(_config({"published": "8000", "target": 8000})) == [
        "backend 0.0.0.0:8000->8000"
    ]
    assert driver.exposed_ports(
        _config(loopback, {"host_ip": "0.0.0.0", "published": "18009", "target": 8001})
    ) == ["backend 0.0.0.0:18009->8001"]
    assert driver.exposed_ports(_config({"host_ip": "::", "published": "1", "target": 2})) == [
        "backend :::1->2"
    ]


def test_the_removal_hint_works_as_printed_without_the_secrets(
    driver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver, "REPO_ROOT", Path("/work/my clone"))

    hint = driver.removal_hint("dmc163test")

    assert hint == (
        "cd '/work/my clone' && LOAD_RUNS=1 LOAD_LLM_DELAY_MS=0 LOAD_STUB_PORT=1 "
        "LOAD_WEBHOOK_SECRET=unused LOAD_GITHUB_APP_PRIVATE_KEY=unused "
        "docker compose -p dmc163test --env-file /dev/null -f docker-compose.yml "
        "-f deploy/load/compose.parallel-runs.yml --profile webhooks down -v"
    )
    # Every variable the override requires has a placeholder, so `down` can interpolate it.
    override = (REPO_ROOT / "deploy/load/compose.parallel-runs.yml").read_text()
    required = set(re.findall(r"\$\{(\w+):\?", override))
    assert required == set(driver._DOWN_PLACEHOLDERS)


def test_down_runs_compose_in_its_own_session(
    driver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 3, "", "")

    monkeypatch.setattr(driver.subprocess, "run", run)
    stack = driver.Stack("dmc163test", {"PATH": "/usr/bin"}, {"BACKEND_PORT": 18001})

    assert stack.down() == 3
    [(command, kwargs)] = seen
    assert command[-2:] == ["down", "-v"] and command[2:4] == ["-p", "dmc163test"]
    assert kwargs["start_new_session"] is True and kwargs["check"] is False


# --- docker endpoint -------------------------------------------------------------------------

_INSPECT = ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
_LOCAL_SOCKET = "unix:///var/run/docker.sock"
_STOPPED = "stopped at the first command past the endpoint"


def _endpoint_stack(
    driver: ModuleType, monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], context: str
) -> tuple[Any, list[list[str]]]:
    """A real Stack: `docker context inspect` answers ``context``, any other command stops."""
    env = driver.stack_env({"PATH": "/usr/bin", "HOME": "/home/u", **environ}, {})
    stack = driver.Stack("dmc163test", env, {"BACKEND_PORT": 18001})
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        # Resolved with the very env compose gets, DOCKER_CONTEXT and DOCKER_CONFIG included.
        assert kwargs["env"] == env
        if command == _INSPECT:
            return subprocess.CompletedProcess(command, 0, f"{context}\n", "")
        raise OSError(_STOPPED)

    monkeypatch.setattr(driver.subprocess, "run", run)
    return stack, commands


@pytest.mark.parametrize(
    "environ,context,inspected,endpoint",
    [
        (
            {"DOCKER_HOST": "tcp://203.0.113.5:2375", "DOCKER_CONTEXT": "default"},
            _LOCAL_SOCKET,
            False,
            "tcp://203.0.113.5:2375",
        ),
        (
            {"DOCKER_HOST": "ssh://deploy@203.0.113.5"},
            _LOCAL_SOCKET,
            False,
            "ssh://deploy@203.0.113.5",
        ),
        (
            {"DOCKER_CONTEXT": "course-vps", "DOCKER_CONFIG": "/home/u/.docker"},
            "ssh://deploy@203.0.113.5",
            True,
            "ssh://deploy@203.0.113.5",
        ),
    ],
    ids=["tcp-docker-host", "ssh-docker-host", "ssh-context"],
)
def test_a_remote_docker_endpoint_is_refused_before_any_compose_command(
    driver: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    context: str,
    inspected: bool,
    endpoint: str,
) -> None:
    stack, commands = _endpoint_stack(driver, monkeypatch, environ, context)

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 2
    # DOCKER_HOST wins over any context, as in the docker CLI: then nothing runs at all.
    assert commands == ([_INSPECT] if inspected else [])
    assert capsys.readouterr().err == (
        f"error: docker would run the load stack on '{endpoint}', not on a local unix:// socket; "
        "unset DOCKER_HOST and DOCKER_CONTEXT or switch to a local docker context\n"
    )
    assert not (tmp_path / "report.json").exists()


@pytest.mark.parametrize(
    "environ,inspected,endpoint",
    [
        (
            {"DOCKER_HOST": "unix:///run/user/1000/docker.sock"},
            False,
            "unix:///run/user/1000/docker.sock",
        ),
        ({}, True, _LOCAL_SOCKET),
        # The docker CLI treats an empty DOCKER_HOST as unset.
        ({"DOCKER_HOST": ""}, True, _LOCAL_SOCKET),
    ],
    ids=["unix-docker-host", "local-context", "empty-docker-host"],
)
def test_a_local_unix_socket_lets_the_run_reach_compose(
    driver: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    inspected: bool,
    endpoint: str,
) -> None:
    stack, commands = _endpoint_stack(driver, monkeypatch, environ, _LOCAL_SOCKET)

    # The fake runner stops the first compose command, so the run ends there with 2.
    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 2
    assert commands == [
        *([_INSPECT] if inspected else []),
        driver.compose_command("dmc163test", "ps", "-aq"),
    ]
    # The stack line comes only after the endpoint passed (a refusal prints the error alone).
    assert capsys.readouterr().err == (
        f"load stack 'dmc163test' on {endpoint}, loopback ports {{'BACKEND_PORT': 18001}}\n"
        f"error: cannot inspect compose project 'dmc163test': {_STOPPED}\n"
    )


def test_an_unknown_docker_context_is_refused(
    driver: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        raise subprocess.CalledProcessError(1, command, "", 'context "gone": context not found\n')

    monkeypatch.setattr(driver.subprocess, "run", run)
    stack = driver.Stack("dmc163test", {"PATH": "/usr/bin", "DOCKER_CONTEXT": "gone"}, {})

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 2
    assert commands == [_INSPECT]
    assert capsys.readouterr().err == (
        "error: cannot resolve the docker endpoint: docker context inspect exited 1: "
        'context "gone": context not found\n'
    )


def test_main_passes_loopback_port_values_and_numeric_ports(
    driver: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: list[Any] = []
    monkeypatch.setattr(driver.shutil, "which", lambda name: "/usr/bin/docker")

    def execute(stack: Any, args: Any, secret: str) -> int:
        captured.append(stack)
        return 0

    monkeypatch.setattr(driver, "execute", execute)
    argv = ["--project", "dmc163test", "--backend-port", "18001", "--postgres-port", "18002"]

    assert driver.main([*argv, "--redis-port", "18005"]) == 0
    [stack] = captured
    assert (stack.env["BACKEND_PORT"], stack.env["POSTGRES_PORT"]) == (
        "127.0.0.1:18001",
        "127.0.0.1:18002",
    )
    assert stack.env["REDIS_PORT"] == "18005"
    assert (stack.ports["BACKEND_PORT"], stack.ports["REDIS_PORT"]) == (18001, 18005)
    # Nothing about the stack is printed before `execute` has checked the docker endpoint.
    assert capsys.readouterr().err == ""


def test_override_probes_rabbitmq_only_after_the_server_wrote_its_cookie() -> None:
    text = (REPO_ROOT / "deploy/load/compose.parallel-runs.yml").read_text()
    base = (REPO_ROOT / "docker-compose.yml").read_text()

    # Without the guard, a root probe can create a cookie the server cannot read (eacces).
    assert (
        '      test: ["CMD-SHELL", "test -f /var/lib/rabbitmq/.erlang.cookie'
        ' && rabbitmq-diagnostics -q ping"]\n'
    ) in text.split("\n  rabbitmq:\n", 1)[1]
    # Drift guard: a change of the base probe needs a new look at this override.
    assert '      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]\n' in base


# --- the scenario on a fake stack ------------------------------------------------------------


class FakeStack:
    """The compose project as `execute` drives it, without docker."""

    def __init__(
        self,
        *,
        leftovers: tuple[str, ...] = (),
        up_error: Exception | None = None,
        existing_error: Exception | None = None,
        rows: list[dict[str, object]] | None = None,
        on_up: Callable[[], None] | None = None,
        reject: bool = False,
        receipts: dict[str, int] | None = None,
        exposed: tuple[dict[str, object], ...] = (),
        config_error: Exception | None = None,
        down_code: int = 0,
        unhandled: tuple[str, ...] = (),
    ) -> None:
        self.project = "dmc163test"
        self.ports = {"BACKEND_PORT": 18001}
        self.leftovers = leftovers
        self.up_error = up_error
        self.existing_error = existing_error
        self.exposed = exposed
        self.config_error = config_error
        self.down_code = down_code
        self.unhandled = unhandled
        self._rows = _ROWS if rows is None else rows
        self.on_up = on_up
        self.reject = reject
        self._receipts = {"projected": 6} if receipts is None else receipts
        self.calls: list[str] = []
        self.deliveries: list[tuple[int, str]] = []

    def endpoint(self) -> str:
        self.calls.append("endpoint")
        return "unix:///var/run/docker.sock"

    def existing(self) -> list[str]:
        self.calls.append("existing")
        if self.existing_error is not None:
            raise self.existing_error
        return list(self.leftovers)

    def config(self) -> dict[str, Any]:
        self.calls.append("config")
        if self.config_error is not None:
            raise self.config_error
        loopback = {"host_ip": "127.0.0.1", "published": "18001", "target": 8000}
        return _config(loopback, *self.exposed)

    def up(self, workers: int) -> None:
        self.calls.append(f"up {workers}")
        if self.on_up is not None:
            self.on_up()
        if self.up_error is not None:
            raise self.up_error

    def seed(self) -> None:
        self.calls.append("seed")

    def post_delivery(self, body: bytes, headers: Mapping[str, str]) -> tuple[int, object]:
        number = json.loads(body)["pull_request"]["number"]
        self.deliveries.append((number, headers["X-GitHub-Delivery"]))
        if self.reject:
            return 401, {"detail": "invalid GitHub webhook signature"}
        return 202, {"status": "pending"}

    def rows(self) -> list[dict[str, object]]:
        return self._rows

    def receipts(self) -> dict[str, int]:
        return self._receipts

    def stub_stats(self) -> dict[str, Any]:
        return {
            "review_posts": {"1": 1, "2": 1, "3": 1},
            "llm_calls": {"RepoConventionsDraft": 3, "ReviewOutput": 3},
            "unhandled": list(self.unhandled),
        }

    def dlq_depth(self) -> int | None:
        return 0

    def log_tail(self) -> None:
        self.calls.append("logs")

    def down(self) -> int:
        self.calls.append("down")
        return self.down_code


def _args(driver: ModuleType, tmp_path: Path, *extra: str) -> Any:
    return driver.parse_args(
        [
            "--project",
            "dmc163test",
            "--runs",
            "3",
            "--timeout",
            "0.2",
            "--settle",
            "0",
            "--poll-interval",
            "0",
            "--report-json",
            str(tmp_path / "report.json"),
            *extra,
        ]
    )


def test_a_passing_run_sends_one_duplicate_per_pr_and_removes_its_project(
    driver: ModuleType, tmp_path: Path
) -> None:
    stack = FakeStack()

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 0
    assert stack.calls == ["endpoint", "existing", "config", "up 3", "seed", "down"]
    assert [number for number, _ in stack.deliveries] == [1, 2, 3, 1, 2, 3]
    assert len({delivery for _, delivery in stack.deliveries}) == 6
    report = json.loads((tmp_path / "report.json").read_text())
    assert (report["passed"], report["timed_out"], report["duplicate_deliveries"]) == (
        True,
        False,
        3,
    )
    assert (report["workers"], report["max_parallel"], report["distinct_worker_ids"]) == (3, 3, 3)
    assert report["receipts"] == {"projected": 6}
    assert report["max_queue_wait_ms"] == 250.0


def test_a_failed_up_still_removes_the_project(driver: ModuleType, tmp_path: Path) -> None:
    stack = FakeStack(up_error=driver.StackError("docker compose up exited 1: pull denied"))

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 1
    assert stack.calls == ["endpoint", "existing", "config", "up 3", "logs", "down"]
    assert not (tmp_path / "report.json").exists()


def test_a_failed_verdict_writes_the_report_and_removes_the_project(
    driver: ModuleType, tmp_path: Path
) -> None:
    stack = FakeStack(rows=[_row(1), _row(2, state="failed"), _row(3)])

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 1
    assert stack.calls == ["endpoint", "existing", "config", "up 3", "seed", "logs", "down"]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["passed"] is False
    assert [name for name, passed in report["checks"].items() if not passed] == ["all_succeeded"]


def test_an_unhandled_stub_request_fails_the_verdict(driver: ModuleType, tmp_path: Path) -> None:
    stack = FakeStack(unhandled=("GET /repos/load/parallel-runs/tarball/main",))

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 1
    assert stack.calls == ["endpoint", "existing", "config", "up 3", "seed", "logs", "down"]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["unhandled_stub_requests"] == ["GET /repos/load/parallel-runs/tarball/main"]
    failed = [name for name, passed in report["checks"].items() if not passed]
    assert failed == ["no_unhandled_stub_requests"]


def test_keep_leaves_the_project_up(driver: ModuleType, tmp_path: Path) -> None:
    stack = FakeStack()

    assert driver.execute(stack, _args(driver, tmp_path, "--keep"), "secret") == 0
    assert "down" not in stack.calls


@pytest.mark.parametrize(
    "leftovers",
    [("2 container(s)",), ("3 volume(s)",), ("1 container(s)", "3 volume(s)")],
    ids=["containers", "volumes", "both"],
)
def test_an_existing_project_is_refused_and_left_alone(
    driver: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    leftovers: tuple[str, ...],
) -> None:
    stack = FakeStack(leftovers=leftovers)

    assert driver.execute(stack, _args(driver, tmp_path, "--keep"), "secret") == 2
    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 2
    assert stack.calls == ["endpoint", "existing", "endpoint", "existing"]
    assert f"already has {' and '.join(leftovers)}" in capsys.readouterr().err


def test_a_project_that_cannot_be_inspected_is_not_touched(
    driver: ModuleType, tmp_path: Path
) -> None:
    stack = FakeStack(existing_error=driver.StackError("docker compose ps exited 1: no daemon"))

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 2
    assert stack.calls == ["endpoint", "existing"]


def test_runs_that_never_finish_time_out_with_a_report(driver: ModuleType, tmp_path: Path) -> None:
    running = [_row(n, state="running", finished=None) for n in (1, 2, 3)]
    stack = FakeStack(rows=running)

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 1
    assert stack.calls[-1] == "down"
    report = json.loads((tmp_path / "report.json").read_text())
    assert (report["timed_out"], report["deliveries_to_terminal_s"]) == (True, None)
    assert report["checks"]["finished_in_time"] is False
    assert [m["state"] for m in report["per_run"]] == ["running"] * 3


def test_a_rejected_delivery_fails_and_removes_the_project(
    driver: ModuleType, tmp_path: Path
) -> None:
    stack = FakeStack(reject=True)

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 1
    assert stack.deliveries[0][0] == 1 and len(stack.deliveries) == 1
    assert stack.calls == ["endpoint", "existing", "config", "up 3", "seed", "logs", "down"]


def _marker(signum: int, frame: object) -> None:
    raise AssertionError("the test's own handler must not run")


def test_sigterm_unwinds_into_the_cleanup_and_restores_the_handlers(
    driver: ModuleType, tmp_path: Path
) -> None:
    originals = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    # Known handlers, so "restored" cannot pass by accident when an earlier test left SIG_IGN.
    signal.signal(signal.SIGTERM, _marker)
    signal.signal(signal.SIGINT, _marker)
    stack = FakeStack(on_up=lambda: os.kill(os.getpid(), signal.SIGTERM))
    try:
        code = driver.execute(stack, _args(driver, tmp_path), "secret")
        restored = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    finally:
        for sig, handler in zip((signal.SIGTERM, signal.SIGINT), originals, strict=True):
            signal.signal(sig, handler)

    assert code == 128 + signal.SIGTERM
    assert stack.calls == ["endpoint", "existing", "config", "up 3", "down"]
    assert restored == (_marker, _marker)


@pytest.mark.parametrize(
    "receipts",
    [{"projected": 5, "pending": 1}, {"projected": 3}],
    ids=["one-pending", "duplicates-not-stored"],
)
def test_an_unfinished_delivery_keeps_the_poll_waiting_until_the_timeout(
    driver: ModuleType, tmp_path: Path, receipts: dict[str, int]
) -> None:
    # Every Run finished, but not every delivery has finished its projection.
    stack = FakeStack(receipts=receipts)

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["timed_out"] is True
    failed = [name for name, passed in report["checks"].items() if not passed]
    assert failed == ["finished_in_time", "every_delivery_projected"]


def test_the_stack_is_scaled_to_the_workers_not_the_runs(
    driver: ModuleType, tmp_path: Path
) -> None:
    stack = FakeStack()

    driver.execute(stack, _args(driver, tmp_path, "--workers", "2"), "secret")

    assert stack.calls[:4] == ["endpoint", "existing", "config", "up 2"]


def test_a_port_beyond_loopback_is_refused_before_up(
    driver: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = FakeStack(exposed=({"host_ip": "0.0.0.0", "published": "5432", "target": 5432},))

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 2
    assert stack.calls == ["endpoint", "existing", "config"]
    assert "publishes ports beyond 127.0.0.1: backend 0.0.0.0:5432->5432" in capsys.readouterr().err


def test_a_config_that_cannot_be_rendered_is_refused_before_up(
    driver: ModuleType, tmp_path: Path
) -> None:
    stack = FakeStack(config_error=driver.StackError("docker compose config exited 15: bad"))

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 2
    assert stack.calls == ["endpoint", "existing", "config"]


def test_a_failed_down_prints_the_removal_command(
    driver: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = FakeStack(down_code=1)

    assert driver.execute(stack, _args(driver, tmp_path), "secret") == 0
    err = capsys.readouterr().err
    assert "warning: docker compose down -v exited 1; remove the project with:\n" in err
    assert driver.removal_hint("dmc163test") in err


def test_keep_prints_the_removal_command(
    driver: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = FakeStack()

    assert driver.execute(stack, _args(driver, tmp_path, "--keep"), "secret") == 0
    err = capsys.readouterr().err
    assert "--keep: the stack 'dmc163test' is still up; remove it with:\n" in err
    assert driver.removal_hint("dmc163test") in err
    assert "secret" not in driver.removal_hint("dmc163test")
