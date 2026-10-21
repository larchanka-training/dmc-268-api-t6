"""Deterministic proof ordering: SQL cancellation can precede CLI completion."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest


@pytest.fixture
def proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    module: ModuleType = importlib.import_module("gitlab_network_proof")
    (tmp_path / "manifest.json").write_text("{}")
    return module.Proof(
        {
            "source": str(root),
            "scratch": str(tmp_path),
            "database_url": "postgresql+psycopg://synthetic@127.0.0.1:1/synthetic",
            "rabbitmq_url": "amqp://127.0.0.1:1/synthetic",
            "credential_key": "synthetic",
            "model_key": "synthetic",
            "tls_port": 49443,
            "tenants": {"A": {"origin": "https://gitlab.example"}},
        }
    )


def test_sql_cancel_does_not_verify_retirement_before_cli_completion(
    proof: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        visible = asyncio.Event()
        completion = asyncio.get_running_loop().create_future()
        run = UUID("11111111-1111-1111-1111-111111111111")
        control_calls: list[str] = []

        async def wait_run(actual: UUID, state: str) -> dict[str, Any]:
            assert actual == run and state == "cancelled"
            visible.set()
            return {"state": "cancelled", "error_code": "installation_revoked"}

        def control(tenant: str) -> dict[str, Any]:
            control_calls.append(tenant)
            return {"attempts": {"14:summary": 1}}

        monkeypatch.setattr(proof, "wait_run", wait_run)
        monkeypatch.setattr(proof, "control", control)
        observing = asyncio.create_task(
            proof.observe_retirement(run, completion, revoked=lambda: completion.done())
        )
        try:
            await visible.wait()
            if observing.done():
                await observing  # RED reports the real premature assertion, not an import seam.
            assert not proof.state["checks"]
            assert control_calls == []
            completion.set_result(None)
            result = await observing
            assert result == {"state": "cancelled", "error_code": "installation_revoked"}
            assert control_calls == ["A"]
            assert proof.state["checks"] == [
                {"name": "supported retirement fences current publication retry", "passed": True}
            ]
        finally:
            if not observing.done():
                observing.cancel()
            await asyncio.gather(observing, return_exceptions=True)
            if not completion.done():
                completion.cancel()

    asyncio.run(scenario())


def test_operator_failure_propagates_without_waiting_for_cancelled_sql(
    proof: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        completion = asyncio.get_running_loop().create_future()
        primary = LookupError("synthetic operator failure")
        entered, finished = asyncio.Event(), asyncio.Event()

        async def wait_run(*args: object) -> dict[str, Any]:
            entered.set()
            try:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            finally:
                finished.set()

        monkeypatch.setattr(proof, "wait_run", wait_run)
        observing = asyncio.create_task(
            proof.observe_retirement(UUID(int=1), completion, revoked=lambda: False)
        )
        await entered.wait()
        completion.set_exception(primary)
        with pytest.raises(LookupError) as caught:
            await observing
        assert caught.value is primary
        assert finished.is_set()
        assert proof.state["checks"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("control", ["cancel", "timeout"])
def test_cancellation_and_deadline_reap_the_pending_sql_read(
    proof: Any,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    async def scenario() -> None:
        completion = asyncio.get_running_loop().create_future()
        entered, finished = asyncio.Event(), asyncio.Event()

        async def wait_run(*args: object) -> dict[str, Any]:
            entered.set()
            try:
                if control == "timeout":
                    return {"state": "cancelled", "error_code": "installation_revoked"}
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            finally:
                finished.set()

        monkeypatch.setattr(proof, "wait_run", wait_run)
        if control == "cancel":
            observing = asyncio.create_task(
                proof.observe_retirement(UUID(int=1), completion, revoked=lambda: False)
            )
            await entered.wait()
            observing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observing
        else:
            with pytest.raises(TimeoutError):
                await proof.observe_retirement(
                    UUID(int=1), completion, revoked=lambda: False, timeout_seconds=0.01
                )
        assert finished.is_set()
        assert completion.cancelled()
        assert proof.state["checks"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error_code", "attempts", "revoked"),
    [
        ("unexpected", 1, True),
        (None, 1, True),
        ("installation_revoked", 2, True),
        ("installation_revoked", 1, False),
    ],
)
def test_each_original_retirement_requirement_remains_mandatory_and_diagnostic(
    proof: Any,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str | None,
    attempts: int,
    revoked: bool,
) -> None:
    async def scenario() -> None:
        completion = asyncio.get_running_loop().create_future()
        completion.set_result(None)

        async def wait_run(*args: object) -> dict[str, Any]:
            return {"state": "cancelled", "error_code": error_code}

        monkeypatch.setattr(proof, "wait_run", wait_run)
        monkeypatch.setattr(proof, "control", lambda tenant: {"attempts": {"14:summary": attempts}})
        with pytest.raises(
            AssertionError, match="supported retirement fences current publication retry"
        ):
            await proof.observe_retirement(UUID(int=1), completion, revoked=lambda: revoked)
        assert proof.state["retirement_observation"] == {
            "cli_completed": revoked,
            "cancelled_error_code": error_code,
            "summary_attempt_count": attempts,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("scenario", [None, "all", "retirement"])
def test_fixed_retirement_selection_runs_only_its_existing_case(
    proof: Any,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str | None,
) -> None:
    if scenario is not None:
        proof.config["scenario"] = scenario
    proof.sessions = object()  # No database operation is admitted by this offline selection test.
    calls: list[object] = []

    class DefaultSelected(Exception):
        pass

    async def setup() -> None:
        calls.append("setup")

    async def component(*args: object) -> None:
        calls.append("default-component")
        raise DefaultSelected

    async def start_worker(github: bool) -> None:
        calls.append(("worker", github))

    async def retirement() -> None:
        calls.append("retirement")

    monkeypatch.setattr(proof, "setup_tenants", setup)
    monkeypatch.setattr(proof, "component_run", component)
    monkeypatch.setattr(proof, "start_worker", start_worker)
    monkeypatch.setattr(proof, "retirement", retirement)
    monkeypatch.setattr(proof, "verify_final_wire", lambda: calls.append("wire-guards"))
    if scenario == "retirement":
        asyncio.run(proof.run_groups())
        assert calls == ["setup", ("worker", True), "retirement", "wire-guards"]
        assert proof.state["groups"]["retirement-sibling"] == "passed"
        assert [v for k, v in proof.state["groups"].items() if k != "retirement-sibling"] == [
            "unreached"
        ] * 6
    else:
        with pytest.raises(DefaultSelected):
            asyncio.run(proof.run_groups())
        assert calls == ["setup", "default-component"]


def test_unknown_private_scenario_is_rejected_before_any_setup(proof: Any) -> None:
    proof.config["scenario"] = "other"
    with pytest.raises(ValueError, match="Unsupported proof scenario"):
        asyncio.run(proof.run_groups())
