"""Record commit context with live eval cases."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from review.scripts.eval_live import RecorderError, main
from tests.eval_live_fakes import (
    DATASET_ROOT,
    REPO_ROOT,
    VALID,
    FakeTransport,
    answer,
    commit_context_repo,
    fixture_root,
    record,
    record_with_repo,
    settings,
)


def test_frozen_revision_message_reaches_the_real_case_provider_request(tmp_path: Path) -> None:
    root = fixture_root(tmp_path, ("CLEAN-03",))
    transport = FakeTransport([answer("primary-model", VALID)])

    record(root, transport)

    assert len(transport.requests) == 1
    assert (
        "<commit_messages>\n"
        "<commit>refactor(core): extract react.ts from index.ts</commit>\n"
        "</commit_messages>"
    ) in transport.requests[0].messages[1].content


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "duplicate",
        "source_url",
        "revision",
        "page_url",
        "message",
    ],
)
def test_invalid_late_real_snapshot_fails_before_any_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    root = fixture_root(tmp_path, ("CLEAN-03", "SYN-04"))
    payload = json.loads((REPO_ROOT / "review/eval/pr-commit-messages.json").read_text())
    late = payload["records"][-1]
    assert late["case_id"] == "SYN-04"
    if defect == "missing":
        payload["records"].pop()
    elif defect == "duplicate":
        payload["records"].append(dict(late))
    elif defect == "message":
        late["pages"][0]["entries"][0]["message"] = None
    elif defect == "page_url":
        late["pages"][0]["verified_via"] = "https://github.com/evil/repo/commit/" + "f" * 40
    else:
        late[defect] = {
            "source_url": "https://github.com/evil/repo/pull/999",
            "revision": "f" * 40,
        }[defect]
    repo = commit_context_repo(tmp_path, monkeypatch, payload)
    transport = FakeTransport([answer("primary-model", VALID)] * 2)

    with pytest.raises(RecorderError, match="commit context"):
        record_with_repo(root, repo, transport)

    assert transport.requests == []
    assert not (root / "responses").exists()
    assert list(root.glob(".responses-*")) == []


@pytest.mark.parametrize(
    "defect",
    [
        "invalid_json",
        "nested_json",
        "missing_file",
        "root_array",
        "missing_records",
        "records_object",
        "unknown_schema",
        "bool_schema",
        "unknown_kind",
        "nonobject_record",
        "missing_field",
        "invalid_message",
        "empty_case_id",
        "bad_revision",
        "bad_source_url",
        "bad_commit_url",
        "bad_unicode",
        "synthetic_record",
    ],
)
def test_malformed_commit_sidecar_is_rejected_before_calls_or_capture_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    root = fixture_root(tmp_path, ("CLEAN-03", "SEC-01", "SYN-04"))
    payload = json.loads((REPO_ROOT / "review/eval/pr-commit-messages.json").read_text())
    unused = payload["records"][2]
    if defect == "root_array":
        payload = []
    elif defect == "missing_records":
        del payload["records"]
    elif defect == "records_object":
        payload["records"] = {}
    elif defect in {"unknown_schema", "bool_schema"}:
        payload["schema_version"] = True if defect == "bool_schema" else 3
    elif defect == "unknown_kind":
        payload["context_kind"] = "complete_historical_pr"
    elif defect == "nonobject_record":
        payload["records"].append(None)
    elif defect == "missing_field":
        del unused["pages"][0]["entries"][0]["message"]
    elif defect == "invalid_message":
        unused["pages"][0]["entries"][0]["message"] = ["invented history"]
    elif defect == "empty_case_id":
        unused["case_id"] = ""
    elif defect == "bad_revision":
        unused["revision"] = "not-an-immutable-sha"
    elif defect == "bad_source_url":
        unused["source_url"] = "http://github.com/aio-libs/aiohttp/pull/7944"
    elif defect == "bad_commit_url":
        unused["pages"][0]["verified_via"] = (
            "https://github.com/aio-libs/aiohttp/commit/" + "f" * 40
        )
    elif defect == "bad_unicode":
        unused["pages"][0]["entries"][0]["message"] = "\ud800"
    elif defect == "synthetic_record":
        invented = dict(payload["records"][0])
        invented["case_id"] = "SEC-01"
        payload["records"].append(invented)
    repo = commit_context_repo(tmp_path, monkeypatch, payload)
    sidecar = repo / "review/eval/pr-commit-messages.json"
    if defect == "invalid_json":
        sidecar.write_text("private invalid sidecar text", encoding="utf-8")
    elif defect == "nested_json":
        sidecar.write_text("[" * 10_000 + "]" * 10_000, encoding="utf-8")
    elif defect == "missing_file":
        sidecar.unlink()
    transport = FakeTransport([answer("primary-model", VALID)] * 3)

    with pytest.raises(RecorderError, match="commit context") as raised:
        record_with_repo(root, repo, transport)

    assert "private invalid sidecar text" not in str(raised.value)
    assert transport.requests == []
    assert not (root / "responses").exists()
    assert list(root.glob(".responses-*")) == []


@pytest.mark.parametrize("capture_mode", ["recorder", "cli"])
def test_all_active_cases_capture_offline_with_actual_bounded_pr_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_mode: str,
) -> None:
    def forbidden_http(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline corpus capture must not query external APIs")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_http)
    case_ids = tuple(sorted(path.name for path in (DATASET_ROOT / "cases").iterdir()))
    assert len(case_ids) == 24
    root = fixture_root(tmp_path, case_ids)
    transport = FakeTransport([answer("primary-model", VALID)] * 24)

    if capture_mode == "cli":
        assert main(["--root", str(root)], transport=transport, model_settings=settings()) == 0
        manifest = json.loads((root / "responses/manifest.json").read_text())
    else:
        manifest = record(root, transport)

    expected = {
        "CLEAN-03": "refactor(core): extract react.ts from index.ts",
        "LOG-03": (
            "fix(server): reassemble COTP fragments and fix INT/DINT byte count (#804)\n\n"
            "Two bugs in the pure-Python server:\n\n"
            "1. COTP fragmentation not reassembled: the server's Connection Confirm\n"
            "   omitted the TPDU size parameter, causing spec-compliant clients to\n"
            "   fall back to the ISO 8073 default of 128 bytes and fragment requests.\n"
            "   The server then tried to process each fragment as a standalone PDU.\n"
            "   Fix: parse TPDU size from the client's Connection Request, echo it\n"
            "   in the Connection Confirm, an…"
        ),
        "RES-03": (
            "#7583",
            "[pre-commit.ci] auto fixes from pre-commit.com hooks\n\nfor more information, see https://pre-commit.ci",
            "Update aiohttp/cookiejar.py\n\nCo-authored-by: Sam Bull &lt;aa6bs0@sambull.org&gt;",
            "Update aiohttp/cookiejar.py\n\nCo-authored-by: Sam Bull &lt;aa6bs0@sambull.org&gt;",
            "Update aiohttp/cookiejar.py\n\nCo-authored-by: Sam Bull &lt;aa6bs0@sambull.org&gt;",
            "remove print",
            "modify to suggetions",
            "[pre-commit.ci] auto fixes from pre-commit.com hooks\n\nfor more information, see https://pre-commit.ci",
            "modify tests to suggestions",
            "[pre-commit.ci] auto fixes from pre-commit.com hooks\n\nfor more information, see https://pre-commit.ci",
            "Update aiohttp/cookiejar.py\n\nCo-authored-by: Sam Bull &lt;aa6bs0@sambull.org&gt;",
            "modify tests to suggestions",
            "[pre-commit.ci] auto fixes from pre-commit.com hooks\n\nfor more information, see https://pre-commit.ci",
            "Update tests/test_cookiejar.py\n\nCo-authored-by: Sam Bull &lt;aa6bs0@sambull.org&gt;",
            "modify cookiejar to handle no-path-cookie",
            "Nitpicks",
            "Update aiohttp/cookiejar.py\n\nCo-authored-by: Sam Bull &lt;git@sambull.org&gt;",
            "[pre-commit.ci] auto fixes from pre-commit.com hooks\n\nfor more information, see https://pre-commit.ci",
            "Update test_cookiejar.py",
            "Update 7583.feature",
        ),
        "SEC-04": "enable secret key rotation",
        "SYN-04": (
            "imaginary code that uses uSES",
            "revert backward compatibility code as this is not going to be v4",
            "use use-sync-external-store",
            "revert to react 17",
            "handle error by our own",
            "v4.0.0-alpha.2",
            "fix&amp;refactor a bit",
            "Merge branch 'main' into use-sync-external-store",
            "update uSES experimental package",
            "remove error propagation hack",
            "update size snapshot",
            "update uSES and add dts",
            "split react.ts and no export wild",
            "split useStore impl",
            "context to follow the new api, export wild again",
            "v4.0.0-alpha.3",
            "add missing await",
            "merge main",
            "merge main",
            "update uSES",
        ),
    }
    assert len(expected["LOG-03"]) == 500
    assert len(transport.requests) == 24
    assert len(manifest["responses"]) == 24
    for case_id, request in zip(case_ids, transport.requests, strict=True):
        if case_id in expected:
            messages = expected[case_id]
            if isinstance(messages, str):
                messages = (messages,)
            block = "<commit_messages>\n" + "\n".join(
                "<commit>" + message + "</commit>" for message in messages
            )
            if case_id in {"RES-03", "SYN-04"}:
                block += "\n<commit>[Additional commits may be omitted]</commit>"
            block += "\n</commit_messages>"
            assert block in request.messages[1].content
        else:
            assert "<commit_messages>" not in request.messages[1].content
    assert sum("<commit_messages>" in r.messages[1].content for r in transport.requests) == 5
    payload = json.loads((REPO_ROOT / "review/eval/pr-commit-messages.json").read_text())
    assert len(payload["records"]) == 5
    long_snapshot = next(item for item in payload["records"] if item["case_id"] == "LOG-03")
    assert len(long_snapshot["pages"][0]["entries"][0]["message"]) == 878
    assert long_snapshot["pages"][0]["entries"][0]["message"].endswith("\n\nCloses #804")


def test_empty_string_snapshot_is_a_valid_real_commit_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = fixture_root(tmp_path, ("CLEAN-03",))
    payload = json.loads((REPO_ROOT / "review/eval/pr-commit-messages.json").read_text())
    payload["records"][0]["pages"][0]["entries"][0]["message"] = ""
    repo = commit_context_repo(tmp_path, monkeypatch, payload)
    transport = FakeTransport([answer("primary-model", VALID)])

    record_with_repo(root, repo, transport)

    assert "<commit_messages>\n<commit></commit>\n</commit_messages>" in (
        transport.requests[0].messages[1].content
    )


@pytest.mark.parametrize(
    "defect",
    [
        "unexpected_record",
        "inactive_source_mismatch",
        "inactive_revision_mismatch",
        "missing_inactive_record",
        "changed_head",
        "duplicate_sha",
        "out_of_order_page",
        "bool_page",
        "float_retrieval",
        "float_schema",
        "extra_page",
        "too_many_entries",
        "missing_tail",
        "inconsistent_link",
        "next_link_without_tail",
        "full_last_page_without_tail",
        "tail_after_short_last_page",
        "wrong_retrieval_budget",
        "wrong_response_encoding",
        "out_of_interval_date",
        "missing_canonical_case",
        "symlink_canonical_case",
        "symlink_canonical_parent",
        "wrong_canonical_id",
    ],
)
def test_frozen_prefix_evidence_and_inactive_bindings_fail_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    root = fixture_root(tmp_path, ("CLEAN-03",))
    payload = json.loads((REPO_ROOT / "review/eval/pr-commit-messages.json").read_text())
    inactive = payload["records"][2]
    assert inactive["case_id"] == "RES-03"
    if defect == "unexpected_record":
        inactive["case_id"] = "RES-99"
    elif defect == "inactive_source_mismatch":
        inactive["source_url"] = "https://github.com/other/repo/pull/1"
        api = "https://api.github.com/repos/other/repo/pulls/1"
        inactive["head_evidence"]["verified_via"] = api
        for page in inactive["pages"]:
            page["verified_via"] = api + f"/commits?per_page=10&page={page['page']}"
    elif defect == "inactive_revision_mismatch":
        inactive["revision"] = "f" * 40
        inactive["head_evidence"]["before_sha"] = "f" * 40
        inactive["head_evidence"]["after_sha"] = "f" * 40
    elif defect == "missing_inactive_record":
        payload["records"].pop(2)
    elif defect == "changed_head":
        inactive["head_evidence"]["after_sha"] = "f" * 40
    elif defect == "duplicate_sha":
        inactive["pages"][1]["entries"][0]["sha"] = inactive["pages"][0]["entries"][0]["sha"]
    elif defect == "bool_page":
        inactive["pages"][0]["page"] = True
    elif defect == "float_retrieval":
        payload["retrieval"]["per_page"] = 10.0
    elif defect == "float_schema":
        payload["schema_version"] = 2.0
    elif defect == "out_of_order_page":
        inactive["pages"].reverse()
    elif defect == "extra_page":
        inactive["pages"].append(dict(inactive["pages"][-1]))
    elif defect == "too_many_entries":
        inactive["pages"][0]["entries"].append(dict(inactive["pages"][0]["entries"][0]))
    elif defect == "missing_tail":
        inactive["incomplete"] = False
    elif defect == "inconsistent_link":
        inactive["pages"][-1]["next_relation"] = False
    elif defect == "next_link_without_tail":
        single = next(item for item in payload["records"] if item["case_id"] == "SEC-04")
        single["pages"][0]["link_header"] = '<https://api.github.com/page2>; rel="next"'
        single["pages"][0]["next_relation"] = True
    elif defect == "full_last_page_without_tail":
        inactive["pages"][-1]["link_header"] = None
        inactive["pages"][-1]["next_relation"] = False
        inactive["pages"][-1]["entries"][-1]["sha"] = inactive["revision"]
        inactive["head_evidence"]["total_commits"] = 20
        inactive["incomplete"] = False
    elif defect == "tail_after_short_last_page":
        inactive["pages"][-1]["entries"].pop()
        inactive["pages"][-1]["link_header"] = None
        inactive["pages"][-1]["next_relation"] = False
    elif defect == "wrong_retrieval_budget":
        payload["retrieval"]["max_message_chars"] = 501
    elif defect == "wrong_response_encoding":
        inactive["pages"][-1]["content_encoding"] = "gzip"
    elif defect == "out_of_interval_date":
        inactive["pages"][-1]["response_date"] = "2026-10-13T11:19:44Z"
    repo = commit_context_repo(tmp_path, monkeypatch, payload)
    canonical = repo / "test-prs-dataset/cases/RES-03/case.json"
    if defect == "missing_canonical_case":
        canonical.unlink()
    elif defect == "symlink_canonical_case":
        canonical.unlink()
        canonical.symlink_to(REPO_ROOT / "test-prs-dataset/cases/RES-03/case.json")
    elif defect == "symlink_canonical_parent":
        shutil.rmtree(repo / "test-prs-dataset")
        (repo / "test-prs-dataset").symlink_to(
            REPO_ROOT / "test-prs-dataset", target_is_directory=True
        )
    elif defect == "wrong_canonical_id":
        identity = json.loads(canonical.read_text())
        identity["id"] = "CLEAN-03"
        canonical.write_text(json.dumps(identity))
    transport = FakeTransport([answer("primary-model", VALID)])

    with pytest.raises(RecorderError, match="commit context"):
        record_with_repo(root, repo, transport)

    assert transport.requests == []
    assert not (root / "responses").exists()
    assert list(root.glob(".responses-*")) == []


def test_custom_synthetic_subset_keeps_empty_history_and_explicit_identity_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_root(tmp_path, ("SEC-01",))
    case_dir = root / "cases/SEC-01"
    case = json.loads((case_dir / "case.json").read_text())
    case["id"] = "SEC-99"
    (case_dir / "case.json").write_text(json.dumps(case))
    case_dir.rename(root / "cases/SEC-99")
    payload = json.loads((REPO_ROOT / "review/eval/pr-commit-messages.json").read_text())
    repo = commit_context_repo(tmp_path, monkeypatch, payload)
    transport = FakeTransport([answer("primary-model", VALID)])

    manifest = record_with_repo(root, repo, transport)

    assert len(transport.requests) == 1
    assert "<commit_messages>" not in transport.requests[0].messages[1].content
    assert set(manifest["responses"]) == {"SEC-99"}
    assert "test-prs-dataset/cases/RES-03/case.json" in manifest["static_inputs"]
    assert "test-prs-dataset/cases/SYN-04/case.json" in manifest["static_inputs"]
