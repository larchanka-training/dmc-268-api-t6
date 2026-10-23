"""The parallel-Runs load scenario: its guard, metrics, verdict and stub (#163); no docker."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import re
import sys
import threading
from collections.abc import Iterator
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


def test_project_guard_accepts_a_separate_project(driver: ModuleType) -> None:
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


_PASSING = {
    "all_succeeded": True,
    "all_first_attempt": True,
    "one_run_per_pr": True,
    "one_review_per_pr": True,
    "dlq_empty": True,
}
_ONE_REVIEW_EACH = {"1": 1, "2": 1, "3": 1}


@pytest.mark.parametrize(
    "rows,review_posts,dlq_depth,failed",
    [
        ([_row(1), _row(2), _row(3)], _ONE_REVIEW_EACH, 0, []),
        ([_row(1), _row(2, state="failed"), _row(3)], _ONE_REVIEW_EACH, 0, ["all_succeeded"]),
        ([_row(1), _row(2, attempt=2), _row(3)], _ONE_REVIEW_EACH, 0, ["all_first_attempt"]),
        ([_row(1), _row(3)], _ONE_REVIEW_EACH, 0, ["one_run_per_pr"]),
        ([_row(1), _row(2), _row(3), _row(3)], _ONE_REVIEW_EACH, 0, ["one_run_per_pr"]),
        ([_row(1), _row(2), _row(3)], {"1": 1, "2": 2, "3": 1}, 0, ["one_review_per_pr"]),
        ([_row(1), _row(2), _row(3)], {"1": 1, "3": 1}, 0, ["one_review_per_pr"]),
        ([_row(1), _row(2), _row(3)], _ONE_REVIEW_EACH, 1, ["dlq_empty"]),
        ([_row(1), _row(2), _row(3)], _ONE_REVIEW_EACH, None, ["dlq_empty"]),
        (
            [],
            {},
            0,
            ["all_succeeded", "all_first_attempt", "one_run_per_pr", "one_review_per_pr"],
        ),
    ],
    ids=[
        "pass",
        "failed-run",
        "second-attempt",
        "missing-run",
        "extra-run",
        "two-reviews",
        "missing-review",
        "dlq-message",
        "no-dlq",
        "no-runs",
    ],
)
def test_verdict_rules(
    driver: ModuleType,
    rows: list[dict[str, object]],
    review_posts: dict[str, int],
    dlq_depth: int | None,
    failed: list[str],
) -> None:
    checks = driver.verdict(
        driver.run_metrics(rows), runs=3, review_posts=review_posts, dlq_depth=dlq_depth
    )

    assert checks == {name: name not in failed for name in _PASSING}


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
