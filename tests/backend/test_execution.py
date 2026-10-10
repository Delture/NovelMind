
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
