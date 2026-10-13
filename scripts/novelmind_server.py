"""uvicorn 0.39 的 Windows reload 依赖控制台 CTRL_C_EVENT 且无界 join，启动器的新进程组或无控制台环境可能不传递该事件。
本适配只替换 worker 的停止协议，继续复用 uvicorn 的监听 socket、spawn、文件监视和日志，不修改第三方包。
"""

import argparse
import logging
import multiprocessing
import signal
import threading

from uvicorn import Config, Server
from uvicorn._subprocess import get_subprocess
from uvicorn.supervisors.statreload import StatReload

logger = logging.getLogger("uvicorn.error")


class WorkerTarget:
    """可被 spawn 序列化；配置和停止事件只传给自己的子进程。"""

    def __init__(self, config, stop_event):
        """不传线程或已创建的 Server。"""
        self.config = config
        self.stop_event = stop_event

    def run(self, sockets=None):
        server = Server(self.config)

        def request_exit():
            """事件独立于控制台，uvicorn 自己完成连接、后台任务和 lifespan 收尾。"""
            self.stop_event.wait()
            server.should_exit = True

        threading.Thread(target=request_exit, daemon=True).start()
        server.run(sockets=sockets)


class OwnedWorkerReload(StatReload):
    """只重启自己持有的 Process，先优雅退出，再按有界超时终止。"""

    def __init__(self, config, sockets):
        self.worker_stopped = False
        self.worker_stop = multiprocessing.get_context("spawn").Event()
        target = WorkerTarget(config, self.worker_stop)
        super().__init__(config, target=target.run, sockets=sockets)
        self.reloader_name = "OwnedWorkerReload"

    def startup(self):
        """保留标准 spawn 与日志，并接受启动器发给所属进程组的 CTRL_BREAK。"""
        super().startup()
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self.signal_handler)

    def _stop_worker(self):
        """最多等六秒正常退出；超时仅终止该 Process 句柄，不按端口枚举进程。"""
        self.worker_stop.set()
        self.process.join(timeout=6)
        if self.process.is_alive():
            logger.warning("Owned worker %s did not stop in time; terminating it", self.process.pid)
            self.process.terminate()
            self.process.join(timeout=2)
        if self.process.is_alive():
            raise RuntimeError("Owned worker did not terminate; refusing to start another worker")
        self.process.close()
        self.worker_stopped = True

    def restart(self):
        """旧 worker 确认退出后复用监听 socket；停止期间不再派生新 worker。"""
        self._stop_worker()
        self.mtimes = {}
        if self.should_exit.is_set():
            return
        self.worker_stop.clear()
        self.process = get_subprocess(config=self.config, target=self.target, sockets=self.sockets)
        self.process.start()
        self.worker_stopped = False

    def shutdown(self):
        try:
            # restart 可能已响应停止请求并关闭旧 Process 句柄。
            if not self.worker_stopped:
                self._stop_worker()
        finally:
            for sock in self.sockets:
                sock.close()
            logger.info("Stopping reloader process [%s]", self.pid)


def main():
    import os

    logger.info("novelmind_server starting pid=%s", os.getpid())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--reload", action="store_true", required=True)
    parser.add_argument("--reload-dir", action="append", required=True)
    args = parser.parse_args()
    config = Config(args.app, host=args.host, port=args.port, reload=True,
                    reload_dirs=args.reload_dir, timeout_graceful_shutdown=5)
    sock = config.bind_socket()
    supervisor = OwnedWorkerReload(config, [sock])
    try:
        supervisor.run()
    finally:
        sock.close()
        logger.info("novelmind_server stopped pid=%s", os.getpid())


if __name__ == "__main__":
    main()
