"""专用有界长任务线程池；HTTP 请求线程池只承担短事务。"""

import asyncio
import logging
import inspect
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from backend.app.services.errors import ServiceError

logger = logging.getLogger(__name__)


class TaskDispatcher(Protocol):
    def add_task(self, function: Callable, *args: Any, **kwargs: Any) -> None: ...


@dataclass
class JobHandle:
    job_id: str
    function: Callable
    args: tuple
    kwargs: dict
    parent_id: str | None = None
    future: Future | None = None
    cancelled: bool = False


class JobExecutor:
    """四个执行线程、最多十六个等待任务，记录 Future 支持取消。"""

    def __init__(self, workers: int = 4, pending: int = 16):
        self.workers = workers
        self.pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="novelmind-job"
        )
        self.stopped = False
        self.capacity = threading.BoundedSemaphore(workers + pending)
        self.lock = threading.Lock()
        self.jobs: dict[str, JobHandle] = {}

    def start(self):
        with self.lock:
            if self.stopped:
                self.pool = ThreadPoolExecutor(
                    max_workers=self.workers, thread_name_prefix="novelmind-job"
                )
                self.stopped = False

    async def shutdown(self):
        await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True)
        self.stopped = True

    def reserve(self, function, args, kwargs, parent_id):
        self.start()
        if not self.capacity.acquire(blocking=False):
            raise ServiceError(503, "后台任务队列已满，请等待现有任务完成。")
        handle = JobHandle(str(args[0]), function, args, kwargs, parent_id)
        with self.lock:
            self.jobs[handle.job_id] = handle
        return handle

    def release(self, handle):
        with self.lock:
            if self.jobs.get(handle.job_id) is not handle:
                return
            del self.jobs[handle.job_id]
        self.capacity.release()

    def cancel(self, job_id):
        with self.lock:
            handles = [
                h
                for h in self.jobs.values()
                if h.job_id == job_id or h.parent_id == job_id
            ]
            for handle in handles:
                handle.cancelled = True
                if handle.future is not None:
                    handle.future.cancel()
            return [h.job_id for h in handles]

    def child_ids(self, job_id):
        with self.lock:
            return [h.job_id for h in self.jobs.values() if h.parent_id == job_id]

    async def run(self, handle):
        try:
            with self.lock:
                if not handle.cancelled:
                    handle.future = self.pool.submit(
                        handle.function, *handle.args, **handle.kwargs
                    )
            if handle.future is None:
                await self.close_unstarted(handle)
                return
            try:
                await asyncio.wrap_future(handle.future)
            except asyncio.CancelledError:
                if handle.future.cancelled():
                    await self.close_unstarted(handle)
                elif not handle.cancelled:
                    raise
        finally:
            # A running worker retains its slot until it actually exits.
            if handle.future is not None and not handle.future.done():
                handle.future.add_done_callback(lambda _: self.release(handle))
            else:
                self.release(handle)

    @staticmethod
    async def close_unstarted(handle):
        for value in (*handle.args[1:], *handle.kwargs.values()):
            async_close = getattr(value, "aclose", None)
            close = getattr(value, "close", None)
            if inspect.iscoroutinefunction(async_close) or callable(close):
                try:
                    if inspect.iscoroutinefunction(async_close):
                        await async_close()
                    else:
                        close()
                except Exception:
                    logger.error("Could not close an unstarted job resource")


executor = JobExecutor()


class JobTasks:
    def __init__(self, background=None, *, parent_id=None, runner=executor):
        self.background = background
        self.parent_id = parent_id
        self.runner = runner
        self.handles = []

    def add_task(self, function, *args, **kwargs):
        handle = self.runner.reserve(function, args, kwargs, self.parent_id)
        try:
            if self.background is not None:
                self.background.add_task(self.runner.run, handle)
            else:
                self.handles.append(handle)
        except Exception:
            self.runner.release(handle)
            raise

    async def __call__(self):
        for handle in self.handles:
            await self.runner.run(handle)
