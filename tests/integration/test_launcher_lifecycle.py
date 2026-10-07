"""Real Windows supervisor lifecycle using only disposable ports and directories."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "novelmind_launcher.py"


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout=20):
    deadline = time.monotonic()+timeout
    last = None
    while time.monotonic()<deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError) as error:
            last = error
        time.sleep(.15)
    raise AssertionError(f"Condition timed out: {last}")


def process_running(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000,False,pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle,0)==258
    finally:
        kernel.CloseHandle(handle)


def descendants(pid):
    """Read process ancestry; never enumerate candidates for global termination."""
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"],
                            capture_output=True,text=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0, result.stderr
    rows = json.loads(result.stdout)
    found = {pid}
    while True:
        added = {row["ProcessId"] for row in rows if row["ParentProcessId"] in found}-found
        if not added:
            return found
        found.update(added)


@pytest.fixture
def launcher_env(isolated_config):
    case = isolated_config
    env = os.environ.copy()
    port = unused_port()
    env.update(NOVELMIND_PORT=str(port),NOVELMIND_RUNTIME_DIR=str(case.root/"runtime"),
               NOVELMIND_LOG_DIR=str(case.root/"logs"),NOVELMIND_APP="tests.integration.launcher_probe:app",
               NOVELMIND_QA="launcher-isolated",PYTHONIOENCODING="utf-8",PYTHONDONTWRITEBYTECODE="1")
    return case.root,env,port


def request(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",timeout=1) as response:
        return response.read()


def command(env, *args):
    return subprocess.run([sys.executable,str(LAUNCHER),*args],cwd=ROOT,env=env,
                          capture_output=True,text=True,encoding="utf-8",timeout=35,
                          creationflags=subprocess.CREATE_NO_WINDOW)


@contextmanager
def running_launcher(root, env, port, *, development=False):
    output = (root/"launcher-process.log").open("wb")
    # The documented .cmd entry supplies a console. Keep that Windows signal
    # environment while hiding its window; CREATE_NO_WINDOW breaks Uvicorn's
    # CTRL_C_EVENT reload protocol and would test a different launch contract.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    child = subprocess.Popen([sys.executable,str(LAUNCHER),"--no-browser",*(["--dev"] if development else [])],
                             cwd=ROOT,env=env,stdout=output,stderr=output,
                             creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
    owned = {child.pid}
    try:
        statefile=root/"runtime"/"launcher.json"
        def ready():
            assert child.poll() is None, (root/"launcher-process.log").read_text(encoding="utf-8",errors="replace")
            state=json.loads(statefile.read_text(encoding="utf-8"))
            return state if state["status"]=="ready" else None
        state=wait_for(ready,50)
        owned.update(descendants(child.pid))
        yield child,state,owned
    finally:
        if child.poll() is None:
            result=command(env,"--stop")
            if result.returncode==0:
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
        if child.poll() is None:
            # Only the directly created process tree can be terminated as fallback.
            subprocess.run(["taskkill","/PID",str(child.pid),"/T","/F"],capture_output=True,timeout=10)
            child.wait(timeout=10)
        output.close()


def test_real_start_repeat_safe_stop_and_single_service(launcher_env):
    root,env,port=launcher_env
    with running_launcher(root,env,port) as (child,state,owned):
        assert state["backend_pid"]==state["frontend_pid"]
        assert json.loads(request(port,"/health"))["project"]=="NovelMind"
        assert b"app.js" in request(port,"/")
        serving=json.loads(request(port,"/api/review/process"))["pid"]
        assert serving in owned
        assert serving in descendants(state["backend_pid"])
        duplicate=command(env,"--no-browser")
        assert duplicate.returncode==0,duplicate.stdout+duplicate.stderr
        assert json.loads((root/"runtime"/"launcher.json").read_text())["launcher_pid"]==state["launcher_pid"]
        assert json.loads(request(port,"/api/review/process"))["pid"]==serving
        stop=command(env,"--stop")
        assert stop.returncode==0,stop.stdout+stop.stderr
        assert child.wait(timeout=15)==0
        wait_for(lambda: not any(process_running(pid) for pid in owned))
        assert not (root/"runtime"/"launcher.json").exists()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",port))


def test_occupied_port_is_rejected_without_touching_owner(launcher_env):
    root,env,port=launcher_env
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",port))
        listener.listen()
        result=command(env,"--no-browser")
        assert result.returncode==1,result.stdout+result.stderr
        assert "占用" in result.stdout+result.stderr
        assert listener.getsockname()==("127.0.0.1",port)
        assert not (root/"runtime"/"launcher.json").exists()


def test_real_reload_replaces_only_worker_and_stop_reaps_children(launcher_env):
    root,env,port=launcher_env
    probe=Path(__file__).resolve().with_name("launcher_probe.py")
    original=probe.stat()
    try:
        with running_launcher(root,env,port,development=True) as (child,state,owned):
            first=json.loads(request(port,"/api/review/process"))["pid"]
            def reloaded():
                # StatReload's first recursive scan may not have reached this file yet.
                # Continue changing only the test probe until a reload is acknowledged.
                log=(root/"logs"/"backend.log").read_text(encoding="utf-8",errors="replace")
                if "Reloading" not in log:
                    os.utime(probe,(original.st_atime, max(original.st_mtime,time.time())+5))
                current=json.loads(request(port,"/api/review/process"))["pid"]
                return current if current!=first else None
            second=wait_for(reloaded,25)
            owned.update(descendants(child.pid))
            assert second in owned and first!=second
            wait_for(lambda:not process_running(first))
            assert b"app.js" in request(port,"/")
            result=command(env,"--stop")
            assert result.returncode==0,result.stdout+result.stderr
            assert child.wait(timeout=15)==0
            wait_for(lambda:not any(process_running(pid) for pid in owned))
    finally:
        os.utime(probe,ns=(original.st_atime_ns,original.st_mtime_ns))


@pytest.mark.parametrize("development", [False, True])
def test_launcher_abrupt_exit_does_not_leave_owned_service(launcher_env, development):
    root,env,port=launcher_env
    with running_launcher(root,env,port,development=development) as (child,state,owned):
        # Kill only the actual supervisor named in our private state, to test Job Object ownership.
        result=subprocess.run(["taskkill","/PID",str(state["launcher_pid"]),"/F"],capture_output=True,timeout=10)
        assert result.returncode==0
        child.wait(timeout=15)
        wait_for(lambda:not any(process_running(pid) for pid in owned))
