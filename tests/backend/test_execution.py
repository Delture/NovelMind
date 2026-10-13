
import asyncio
import threading
from unittest.mock import Mock
import pytest
from backend.app.services.errors import ServiceError
from backend.app.services.execution import JobExecutor


def test_bounded_executor_uses_dedicated_thread_and_cancels_pending_resource():
    runner = JobExecutor(workers=1, pending=1)
    entered, release = threading.Event(), threading.Event()
    names = []

    def block(key):
        names.append(threading.current_thread().name)
        entered.set()
        assert release.wait(5)

    provider = Mock()
    first = runner.reserve(block, ("first",), {}, None)
    second = runner.reserve(
        lambda *args: pytest.fail("cancelled worker executed"),
        ("second", provider),
        {},
        None,
    )
    with pytest.raises(ServiceError):
        runner.reserve(block, ("overflow",), {}, None)

    async def exercise():
        task1 = asyncio.create_task(runner.run(first))
        task2 = asyncio.create_task(runner.run(second))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            assert runner.cancel("second") == ["second"]
            await task2
        finally:
            release.set()
            await task1

    try:
        asyncio.run(exercise())
        assert names[0].startswith("novelmind-job")
        provider.close.assert_called_once_with()
        assert not runner.jobs
    finally:
        release.set()
        runner.pool.shutdown(wait=True)


def test_shutdown_returns_while_worker_still_running():
    runner = JobExecutor(workers=1, pending=1)
    entered, release = threading.Event(), threading.Event()

    def block(key):
        entered.set()
        assert release.wait(5)

    handle = runner.reserve(block, ("blocking",), {}, None)

    async def exercise():
        task = asyncio.create_task(runner.run(handle))
        while not entered.wait(0):
            await asyncio.sleep(0.01)
        await asyncio.wait_for(runner.shutdown(), timeout=2)
        release.set()
        await task

    try:
        asyncio.run(exercise())
    finally:
        release.set()



def test_reserve_rejected_after_shutdown():
    runner = JobExecutor(workers=1, pending=1)

    async def exercise():
        await runner.shutdown()
        with pytest.raises(ServiceError) as rejected:
            runner.reserve(lambda key: None, ("after",), {}, None)
        assert rejected.value.status_code == 503
        assert "关闭" in rejected.value.detail

    asyncio.run(exercise())


def test_shutdown_sets_stop_event_and_keeps_running_slot():
    runner = JobExecutor(workers=1, pending=1)
    entered, release = threading.Event(), threading.Event()

    def block(key):
        entered.set()
        assert release.wait(5)

    handle = runner.reserve(block, ("blocking",), {}, None)

    async def exercise():
        task = asyncio.create_task(runner.run(handle))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        await asyncio.wait_for(runner.shutdown(), timeout=2)
        assert runner.is_stopping()
        assert handle.job_id in runner.jobs
        release.set()
        await task
        assert handle.job_id not in runner.jobs

    try:
        asyncio.run(exercise())
    finally:
        release.set()
