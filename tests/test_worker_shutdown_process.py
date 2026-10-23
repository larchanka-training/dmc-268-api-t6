"""Actual process signals exercise main and asyncio.Runner teardown (#185)."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("stage", ["loop_bound", "before_loop_binding"])
def test_main_handles_sigterm_and_restores_handlers(stage: str) -> None:
    program = textwrap.dedent(
        f"""
        import asyncio
        import os
        import signal
        import app.worker as worker

        previous = signal.getsignal(signal.SIGTERM)
        worker.WorkerSettings.from_environment = lambda mapping: object()

        async def run(settings, *, stop=None):
            if {stage!r} == "loop_bound":
                os.kill(os.getpid(), signal.SIGTERM)
            if stop is None:
                await asyncio.Event().wait()
            else:
                await stop.wait()
            print("stopped", flush=True)

        worker.run_worker = run
        if {stage!r} == "before_loop_binding":
            real_run = asyncio.run
            def signal_before_run(coroutine):
                os.kill(os.getpid(), signal.SIGTERM)
                return real_run(coroutine)
            asyncio.run = signal_before_run
        worker.main()
        assert signal.getsignal(signal.SIGTERM) == previous
        print("restored", flush=True)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=8, check=False
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "stopped\nrestored" in result.stdout


@pytest.mark.parametrize(
    "parked", ["resistant_task", "executor", "blocked_loop", "repeated_signals"]
)
def test_first_signal_deadline_bounds_running_work_and_runner_teardown(parked: str) -> None:
    program = textwrap.dedent(
        f"""
        import asyncio
        import os
        import signal
        import threading
        import time
        import app.worker as worker
        import app.bootstrap.worker_shutdown as shutdown

        shutdown._PROCESS_TIMEOUT = 0.35
        worker.WorkerSettings.from_environment = lambda mapping: object()
        first_signal = None
        real_exit = os._exit
        def measured_exit(code):
            print("deadline_elapsed=" + str(time.monotonic() - first_signal), flush=True)
            real_exit(code)
        shutdown.os._exit = measured_exit

        async def resistant():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                print("runner_cancelled_child", flush=True)
                await asyncio.Event().wait()

        async def run(settings, *, stop):
            global first_signal
            if {parked!r} == "resistant_task":
                asyncio.create_task(resistant())
                await asyncio.sleep(0)
            elif {parked!r} == "executor":
                entered = threading.Event()
                def parked_thread():
                    entered.set()
                    threading.Event().wait()
                asyncio.get_running_loop().run_in_executor(None, parked_thread)
                while not entered.is_set():
                    await asyncio.sleep(0)
            first_signal = time.monotonic()
            os.kill(os.getpid(), signal.SIGTERM)
            if {parked!r} == "repeated_signals":
                def repeat():
                    for _ in range(3):
                        time.sleep(0.15)
                        os.kill(os.getpid(), signal.SIGTERM)
                threading.Thread(target=repeat, daemon=True).start()
                await asyncio.Event().wait()
            elif {parked!r} == "blocked_loop":
                time.sleep(60)
            else:
                await stop.wait()
                print("worker_returned", flush=True)

        worker.run_worker = run
        worker.main()
        raise AssertionError("deadline must terminate the parked process")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=8, check=False
    )
    assert result.returncode == 143, (result.returncode, result.stdout, result.stderr)
    elapsed = float(result.stdout.split("deadline_elapsed=")[1].splitlines()[0])
    assert 0.25 <= elapsed < 0.6, result.stdout
    if parked in {"resistant_task", "executor"}:
        assert "worker_returned" in result.stdout
    if parked == "resistant_task":
        assert "runner_cancelled_child" in result.stdout


def test_clean_signal_shutdown_disarms_deadline_after_runner_returns() -> None:
    program = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        import time
        import app.worker as worker
        import app.bootstrap.worker_shutdown as shutdown

        shutdown._PROCESS_TIMEOUT = 0.08
        worker.WorkerSettings.from_environment = lambda mapping: object()
        async def run(settings, *, stop):
            os.kill(os.getpid(), signal.SIGTERM)
            await stop.wait()
        worker.run_worker = run
        worker.main()
        time.sleep(0.16)
        print("deadline_disarmed", flush=True)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=8, check=False
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "deadline_disarmed" in result.stdout


def test_signal_after_loop_closed_before_wrapper_returns_is_safe() -> None:
    program = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        import app.worker as worker

        worker.WorkerSettings.from_environment = lambda mapping: object()
        async def run(settings, *, stop):
            return
        worker.run_worker = run
        real_run = asyncio.run
        def signal_after_run(coroutine):
            result = real_run(coroutine)
            os.kill(os.getpid(), signal.SIGTERM)
            return result
        asyncio.run = signal_after_run
        worker.main()
        print("late_signal_handled", flush=True)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=8, check=False
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "late_signal_handled" in result.stdout


@pytest.mark.parametrize("during_startup", [False, True])
def test_actual_worker_composition_handles_real_sigterm(during_startup: bool) -> None:
    program = textwrap.dedent(
        f"""
        import asyncio
        import os
        import signal
        import pytest
        import app.worker as worker
        import app.bootstrap.worker_shutdown as shutdown
        from tests.test_worker_shutdown import install_worker_harness

        shutdown._PROCESS_TIMEOUT = 1.0
        patch = pytest.MonkeyPatch()
        started = []
        cancelled = []
        async def consumer(name, stop):
            started.append(name)
            if len(started) == 3:
                os.kill(os.getpid(), signal.SIGTERM)
            try:
                await stop.wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        harness = install_worker_harness(patch, consumer, pause_startup={during_startup!r})
        os.environ["DATABASE_URL"] = "postgresql+psycopg://test"
        os.environ["RABBITMQ_URL"] = "amqp://test"
        if {during_startup!r}:
            real_run_worker = worker.run_worker
            async def run(settings, *, stop):
                async def signal_when_connecting():
                    await harness.startup_entered.wait()
                    os.kill(os.getpid(), signal.SIGTERM)
                sender = asyncio.create_task(signal_when_connecting())
                await real_run_worker(settings, stop=stop)
                await sender
            worker.run_worker = run
        worker.main()
        assert cancelled == []
        assert harness.resources_closed[-1] == "database"
        assert len(started) == (0 if {during_startup!r} else 3)
        print("composition_stopped", flush=True)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=8, check=False
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "composition_stopped" in result.stdout


def test_actual_composition_preserves_resources_under_resistant_cleanup_until_backstop() -> None:
    program = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        import pytest
        import app.worker as worker
        import app.bootstrap.worker_shutdown as shutdown
        from tests.test_worker_shutdown import install_worker_harness

        shutdown._PROCESS_TIMEOUT = 0.35
        shutdown._DRAIN_TIMEOUT = 0.03
        shutdown._CLEANUP_TIMEOUT = 0.03
        patch = pytest.MonkeyPatch()
        started = []
        cancellations = []
        async def consumer(name, stop):
            started.append(name)
            if len(started) == 3:
                os.kill(os.getpid(), signal.SIGTERM)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations.append(name)
                print("cleanup_started:" + name, flush=True)
                await asyncio.Event().wait()

        harness = install_worker_harness(patch, consumer)
        real_run = worker.run_worker
        async def run(settings, *, stop):
            async def inspect_after_cleanup_budget():
                await asyncio.sleep(0.12)
                assert len(cancellations) == 3
                assert harness.resources_closed == []
                print("resources_open_after_cleanup_budget", flush=True)
            inspector = asyncio.create_task(inspect_after_cleanup_budget())
            await real_run(settings, stop=stop)
            await inspector
        worker.run_worker = run
        os.environ["DATABASE_URL"] = "postgresql+psycopg://test"
        os.environ["RABBITMQ_URL"] = "amqp://test"
        worker.main()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=8, check=False
    )
    assert result.returncode == 143, (result.returncode, result.stdout, result.stderr)
    assert result.stdout.count("cleanup_started:") == 3
    assert "resources_open_after_cleanup_budget" in result.stdout
    assert "Worker cleanup deadline exceeded" in result.stderr


def test_resistant_review_pipeline_retains_resources_and_lease_until_process_backstop() -> None:
    program = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        import pytest
        import app.worker as worker
        import app.bootstrap.worker_shutdown as shutdown
        import app.modules.reviews.application.handle_review_run as review
        from tests.test_worker_shutdown import install_worker_harness
        from tests.test_review_shutdown import Database, make_handler
        from tests.test_review_run_worker import NOW, RUN

        shutdown._PROCESS_TIMEOUT = 0.35
        shutdown._DRAIN_TIMEOUT = 0.01
        shutdown._CLEANUP_TIMEOUT = 0.04
        review._PIPELINE_CANCEL_WAIT = 0.02
        patch = pytest.MonkeyPatch()
        database = Database()
        cancel_count = 0
        async def pipeline(claimed):
            global cancel_count
            os.kill(os.getpid(), signal.SIGTERM)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_count += 1
                await asyncio.Event().wait()
        handler = make_handler(database, pipeline)
        async def consumer(name, stop):
            if name == "review.run.fast":
                await handler.execute(RUN)
            else:
                await stop.wait()
        harness = install_worker_harness(patch, consumer)
        real_run = worker.run_worker
        async def run(settings, *, stop):
            async def inspect_after_cleanup_budget():
                await asyncio.sleep(0.12)
                assert cancel_count == 1
                assert harness.resources_closed == []
                assert database.row.lease_until > NOW
                assert database.commits == 1
                print("review_resources_and_lease_retained", flush=True)
            inspector = asyncio.create_task(inspect_after_cleanup_budget())
            await real_run(settings, stop=stop)
            await inspector
        worker.run_worker = run
        os.environ["DATABASE_URL"] = "postgresql+psycopg://test"
        os.environ["RABBITMQ_URL"] = "amqp://test"
        worker.main()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=8, check=False
    )
    assert result.returncode == 143, (result.returncode, result.stdout, result.stderr)
    assert "review_resources_and_lease_retained" in result.stdout
    assert "interrupted lease retained until quiescence" in result.stderr
    assert "Worker cleanup deadline exceeded" in result.stderr


def test_watchdog_failed_pipeline_is_joined_before_process_resource_teardown() -> None:
    program = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        from datetime import timedelta
        import pytest
        import app.worker as worker
        import app.bootstrap.worker_shutdown as shutdown
        import app.modules.reviews.application.handle_review_run as review
        from tests.test_worker_shutdown import install_worker_harness
        from tests.test_review_shutdown import Database
        from tests.test_review_run_worker import NOW, RUN, RetryQueue

        shutdown._PROCESS_TIMEOUT = 0.35
        shutdown._DRAIN_TIMEOUT = 0.2
        shutdown._CLEANUP_TIMEOUT = 0.1
        review._PIPELINE_CANCEL_WAIT = 0.01
        patch = pytest.MonkeyPatch()
        database = Database()
        retry = RetryQueue()
        clock = NOW
        cancel_count = 0
        async def pipeline(claimed):
            global clock, cancel_count
            clock += timedelta(seconds=2)
            os.kill(os.getpid(), signal.SIGTERM)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_count += 1
                await asyncio.Event().wait()
        handler = review.HandleReviewRun(
            uow_factory=database.uow, pipeline=pipeline, retry_queue=retry,
            check_runs=None, worker_id="worker-185", now=lambda: clock,
            heartbeat_interval=timedelta(milliseconds=1), attempt_deadline=timedelta(seconds=1),
        )
        async def consumer(name, stop):
            if name == "review.run.fast":
                assert await handler.execute(RUN) == "ack"
            else:
                await stop.wait()
        harness = install_worker_harness(patch, consumer, review_handler=handler)
        real_run = worker.run_worker
        async def run(settings, *, stop):
            async def inspect_pending_join():
                await asyncio.sleep(0.12)
                assert cancel_count == 1 and harness.resources_closed == []
                assert database.row.state == "failed"
                assert database.row.error_code == "deadline_exceeded"
                assert database.row.attempt == 1 and database.row.lease_until is None
                assert retry.published == []
                print("watchdog_outcome_preserved_resources_open", flush=True)
            inspector = asyncio.create_task(inspect_pending_join())
            await real_run(settings, stop=stop)
            await inspector
        worker.run_worker = run
        os.environ["DATABASE_URL"] = "postgresql+psycopg://test"
        os.environ["RABBITMQ_URL"] = "amqp://test"
        worker.main()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=8, check=False
    )
    assert result.returncode == 143, (result.returncode, result.stdout, result.stderr)
    assert "watchdog_outcome_preserved_resources_open" in result.stdout
    assert "still-active review pipelines" in result.stderr
