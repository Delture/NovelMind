"""macOS/POSIX 的真实启动、重载与所属进程回收，全部使用临时数据。"""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX process groups and pipe watchdog"
)
ROOT = Path(__file__).resolve().parents[2]


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError("POSIX launcher condition timed out")


def alive(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
    )
    return (
        result.returncode == 0
        and result.stdout.strip()
        and not result.stdout.strip().startswith("Z")
    )


@pytest.mark.parametrize(
    "development,abrupt", [(False, False), (True, False), (True, True)]
)
def test_posix_launcher_lifecycle(tmp_path, development, abrupt):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(
        PYTHON_DOTENV_DISABLED="1",
        DATABASE_URL="sqlite:///" + str(tmp_path / "app.db"),
        VECTOR_STORE_PATH=str(tmp_path / "vectors"),
        MODEL_SETTINGS_PATH=str(tmp_path / "models.json"),
        DEEPSEEK_API_KEY="",
        EMBEDDING_API_KEY="",
        NOVELMIND_PORT=str(port),
        NOVELMIND_RUNTIME_DIR=str(tmp_path / "runtime"),
        NOVELMIND_LOG_DIR=str(tmp_path / "logs"),
        NOVELMIND_APP="tests.integration.launcher_probe:app",
        NOVELMIND_QA="launcher-isolated",
    )
    command = [sys.executable, str(ROOT / "scripts/novelmind_launcher.py")]
    state_file = tmp_path / "runtime/launcher.json"
    log = (tmp_path / "launcher-console.log").open("wb")
    watched = ROOT / f"novelmind_reload_probe_{os.getpid()}_{port}.py"
    if development and not abrupt:
        watched.write_text("# initial reload probe\n")
    child = subprocess.Popen(
        [*command, "--no-browser", *(["--dev"] if development else [])],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    state = None
    worker = None

    def probe(path):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}" + path, timeout=1
        ) as response:
            return response.read()

    try:
        state = wait_for(
            lambda: (
                json.loads(state_file.read_text())
                if state_file.exists()
                and json.loads(state_file.read_text()).get("status") == "ready"
                else None
            )
        )
        assert json.loads(probe("/health"))["status"] == "ok"
        assert b"app.js" in probe("/")
        worker = json.loads(probe("/api/review/process"))["pid"]
        duplicate = subprocess.run(
            [*command, "--no-browser"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert duplicate.returncode == 0
        assert json.loads(state_file.read_text())["launcher_pid"] == child.pid
        if development and not abrupt:
            # Touch an isolated source file inside the watched project; remove it afterwards.
            try:
                time.sleep(0.5)
                watched.write_text("# changed reload probe\n")
                replacement = wait_for(
                    lambda: (
                        value
                        if (value := json.loads(probe("/api/review/process"))["pid"])
                        != worker
                        else None
                    )
                )
                wait_for(lambda: not alive(worker))
                worker = replacement
            finally:
                watched.unlink(missing_ok=True)
        if abrupt:
            child.kill()
            child.wait(timeout=10)
        else:
            stop = subprocess.run(
                [*command, "--stop"], cwd=ROOT, env=env, capture_output=True, timeout=10
            )
            assert stop.returncode == 0
            assert child.wait(timeout=15) == 0
            assert not state_file.exists()
        wait_for(lambda: not alive(worker) and not alive(state["backend_pid"]))
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        if state and alive(state["backend_pid"]):
            os.killpg(state["backend_pid"], signal.SIGKILL)
        log.close()
        watched.unlink(missing_ok=True)
