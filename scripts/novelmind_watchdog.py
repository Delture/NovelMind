"""POSIX 启动器持有管道写端；启动器退出后只回收其创建的进程组。"""

import os
import signal
import sys
import time


def reap_group(pipe: int, group: int) -> None:
    with os.fdopen(pipe, "rb") as stream:
        stream.read()
    try:
        os.killpg(group, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            os.killpg(group, 0)
            time.sleep(0.1)
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


if __name__ == "__main__":
    reap_group(int(sys.argv[1]), int(sys.argv[2]))
