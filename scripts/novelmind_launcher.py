"""启动、监控并安全停止本机 NovelMind 后端和前端。

启动器先检查重复实例和端口，启动唯一 FastAPI 并
等待健康检查和网页就绪，最后才打开浏览器。状态文件和
随机本机控制端口让另一个“停止”命令能联系正确启动器，token 防止无关本机请求
误触发停止。Windows Job Object 确保启动器异常退出时子进程树不会变成孤儿。

本模块刻意只用 Python 标准库：只要虚拟环境存在，依赖损坏时仍能输出可读日志，
而不会因导入 FastAPI 失败导致启动管理器本身也无法运行。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any, Sequence

from ctypes import wintypes


# 所有路径都从脚本真实位置推导，而不是依赖用户当前工作目录；双击 cmd 和终端
# 启动因此具有相同行为。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
RUNTIME_DIR = Path(os.environ.get("NOVELMIND_RUNTIME_DIR", str(PROJECT_ROOT / "data" / "runtime")))
STATE_FILE = RUNTIME_DIR / "launcher.json"
LOG_DIR = Path(os.environ.get("NOVELMIND_LOG_DIR", str(PROJECT_ROOT / "logs")))
BACKEND_LOG = LOG_DIR / "backend.log"
FRONTEND_LOG = LOG_DIR / "frontend.log"
LAUNCHER_LOG = LOG_DIR / "launcher.log"

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = int(os.environ.get("NOVELMIND_PORT", "8501"))
FRONTEND_HOST = "127.0.0.1"
FRONTEND_PORT = BACKEND_PORT
BACKEND_HEALTH_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}/health"
FRONTEND_URL = f"http://{FRONTEND_HOST}:{FRONTEND_PORT}"
# 本机探测不经过系统代理（macOS urllib 会读取系统代理设置）。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
STARTUP_TIMEOUT_SECONDS = 45.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0


class LauncherError(RuntimeError):
    """A user-facing launcher failure."""


@dataclass(frozen=True)
class ManagedProcess:
    """把子进程与其日志句柄绑定，确保终止时两项资源一起清理。"""
    name: str
    process: subprocess.Popen[bytes]
    log_handle: IO[bytes]
    guard: subprocess.Popen | None = None
    guard_pipe: int | None = None


class _JobBasicLimitInformation(ctypes.Structure):
    """映射 Windows ``JOBOBJECT_BASIC_LIMIT_INFORMATION`` 的内存布局。

    ``ctypes.Structure`` 让 Python 按 ``_fields_`` 顺序构造 C 结构体；字段名和类型
    必须与 Windows API 完全一致，不能像普通 Python 类那样随意调整。
    """
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobIoCounters(ctypes.Structure):
    """映射 Job Object API 要求的 I/O 计数结构；本项目只负责提供正确布局。"""
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    """组合基础限制和 I/O 结构，用于开启“句柄关闭即杀死进程树”标志。"""
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsKillOnCloseJob:
    """让 Windows 内核拥有子进程树，句柄关闭时自动结束全部子进程。"""

    _KILL_ON_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        """在 Windows 创建内核 Job Object；其他系统直接保持禁用状态。

        无参数。初始化失败只记录日志，因为启动器仍可使用普通进程终止作为降级
        路径；``self._handle`` 为 None 就代表后续 assign/close 无需操作。
        """
        self._handle: int | None = None
        self._kernel32: Any = None
        if os.name != "nt":
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [
                wintypes.LPVOID,
                wintypes.LPCWSTR,
            ]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
            information = _JobExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = self._KILL_ON_CLOSE
            if not kernel32.SetInformationJobObject(
                handle,
                self._EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                kernel32.CloseHandle(handle)
                raise OSError(
                    ctypes.get_last_error(),
                    "SetInformationJobObject",
                )
            self._kernel32 = kernel32
            self._handle = int(handle)
        except Exception:
            logging.exception(
                "Could not enable Windows kill-on-close process ownership"
            )

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        """把一个已启动子进程加入 Job Object。

        参数 ``process`` 是 ``subprocess.Popen`` 返回对象。函数无返回值；若内核
        归属不可用只记错误，不能因此遗忘已经成功启动的子进程。
        """
        if self._handle is None:
            return
        if not self._kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle),
            wintypes.HANDLE(process._handle),  # type: ignore[attr-defined]
        ):
            logging.error(
                "Could not attach child PID %s to launcher job object",
                process.pid,
            )

    def close(self) -> None:
        """关闭 Job Object 句柄；Windows 随即结束仍归属于它的子进程树。"""
        if self._handle is None:
            return
        self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
        self._handle = None


def _configure_logging() -> None:
    """创建日志目录并把启动器日志固定写入 UTF-8 ``launcher.log``。

    ``force=True`` 会替换当前进程已有的 logging 配置，保证从 cmd 双击和测试入口
    启动时格式一致。本函数没有参数和返回值。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LAUNCHER_LOG, encoding="utf-8"),
        ],
        force=True,
    )


def _print(message: str) -> None:
    """同时写日志和控制台状态；控制台已关闭时仍继续清理流程。

    参数 ``message`` 是面向用户的单行文字。``flush=True`` 让启动进度立即可见。
    """
    logging.info(message)
    try:
        print(message, flush=True)
    except (OSError, ValueError):
        # A detached or forcibly closed console must never interrupt child
        # process cleanup. The same status remains available in launcher.log.
        logging.warning("Console output is unavailable")


def _write_error(message: str) -> None:
    """Write a final diagnostic without failing on a detached console."""
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        logging.warning("Console error output is unavailable")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再原子替换状态文件，避免其他进程读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            logging.warning("Could not restrict launcher state permissions")
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            logging.warning("Could not remove temporary launcher state")
        raise


def _read_state() -> dict[str, Any] | None:
    """读取并验证启动器状态文件的顶层 JSON 类型。

    返回字典表示文件可读，返回 None 表示文件不存在、损坏或不是 JSON 对象。
    此处把损坏当“无可信状态”，调用者还会通过控制端点验证真实进程。
    """
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _remove_owned_state(token: str) -> None:
    """仅当状态文件仍属于当前 token 时删除，避免旧进程删掉新进程状态。

    参数 ``token`` 是本次启动生成的随机所有权标识。不存在或所有者变化时不做事。
    """
    state = _read_state()
    if state is None or state.get("token") != token:
        return
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def _port_is_available(host: str, port: int) -> bool:
    """尝试绑定 ``host:port``，返回该 TCP 地址当前是否可供新服务使用。

    这里只短暂绑定后立刻由 ``with`` 关闭 socket；检查与真正启动之间仍可能存在
    极短竞态，所以子进程启动错误仍必须正常处理。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _json_request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    timeout: float = 1.5,
) -> dict[str, Any] | None:
    """向本机控制/健康端点发送小型 JSON 请求。

    参数 ``url`` 是完整地址；``method`` 默认 GET；``token`` 若提供会写入 Bearer
    头；``timeout`` 单位为秒。成功且顶层为对象时返回字典，任何连接、编码或
    JSON 错误返回 None，适合用于轮询探测而非抛出长异常。
    """
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with _LOCAL_OPENER.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None
    return payload if isinstance(payload, dict) else None


def _running_launcher_state() -> dict[str, Any] | None:
    """同时验证状态文件和带 token 的控制端点，不能只相信可能过期的 PID。"""
    state = _read_state()
    if state is None:
        return None
    token = state.get("token")
    control_port = state.get("control_port")
    if not isinstance(token, str) or not isinstance(control_port, int):
        return None
    response = _json_request(
        f"http://127.0.0.1:{control_port}/status",
        token=token,
    )
    if response is None or response.get("project") != "NovelMind":
        return None
    return {**state, **response}


class _ControlServer(ThreadingHTTPServer):
    """只监听随机回环端口的控制服务器，保存认证 token 和停止事件。"""
    daemon_threads = True

    def __init__(self, token: str, stop_event: threading.Event) -> None:
        """保存控制凭据/事件并让操作系统分配空闲端口（端口参数 0）。"""
        self.control_token = token
        self.stop_event = stop_event
        self.launcher_status = "starting"
        super().__init__(("127.0.0.1", 0), _ControlHandler)


class _ControlHandler(BaseHTTPRequestHandler):
    """实现 ``GET /status`` 与 ``POST /stop`` 的最小认证 HTTP 协议。"""
    server: _ControlServer

    def _authorized(self) -> bool:
        """比较 Authorization 头和本次随机 token，返回请求是否获准。"""
        return self.headers.get("Authorization") == (
            f"Bearer {self.server.control_token}"
        )

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        """写出状态码、UTF-8 JSON 和准确 Content-Length；无返回值。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """处理状态读取；方法名由标准库协议规定，故保留非 snake_case 命名。"""
        if self.path != "/status":
            self._write_json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._write_json(403, {"error": "forbidden"})
            return
        self._write_json(
            200,
            {
                "project": "NovelMind",
                "status": self.server.launcher_status,
                "launcher_pid": os.getpid(),
            },
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """处理停止请求：先回成功 JSON，再设置线程安全 stop_event。"""
        if self.path != "/stop":
            self._write_json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._write_json(403, {"error": "forbidden"})
            return
        self._write_json(200, {"project": "NovelMind", "stopping": True})
        self.server.stop_event.set()

    def log_message(self, _format: str, *args: object) -> None:
        """关闭标准库逐请求 stderr 日志；启动器已有自己的结构化日志。"""
        return


def _start_control_server(
    token: str,
    stop_event: threading.Event,
) -> tuple[_ControlServer, threading.Thread]:
    """启动守护控制线程，返回服务器对象和线程，供最终 shutdown/join。

    ``token`` 用于认证，``stop_event`` 用于跨线程通知主监督循环退出。
    """
    server = _ControlServer(token, stop_event)
    thread = threading.Thread(
        target=server.serve_forever,
        name="novelmind-control",
        daemon=True,
    )
    thread.start()
    return server, thread


def _python_executable() -> Path:
    """返回项目虚拟环境解释器绝对路径；缺失时抛用户可读 LauncherError。"""
    if VENV_PYTHON.is_file():
        return VENV_PYTHON
    raise LauncherError(
        f"未找到 {VENV_PYTHON}。请先按照 README 完成一次环境安装。"
    )


def build_backend_command(python: Path, *, development: bool) -> list[str]:
    """构造 uvicorn 参数列表，不启动进程。

    ``python`` 是解释器路径；``development=True`` 追加热重载；Windows 使用进程事件停止 worker，避免依赖
    控制台 Ctrl+C 传递。返回列表而不是拼接
    shell 字符串，可避免带空格路径的转义问题和命令注入。
    """
    command = [
        str(python),
        "-m",
        "scripts.novelmind_server" if development and os.name == "nt" else "uvicorn",
        os.environ.get("NOVELMIND_APP", "backend.app.main:app"),
        "--host",
        BACKEND_HOST,
        "--port",
        str(BACKEND_PORT),
    ]
    if development:
        command.extend(["--reload", "--reload-dir", str(PROJECT_ROOT)])
    return command


def build_frontend_command(python: Path) -> list[str]:
    """兼容旧调用方的命令构造；默认启动流程仅调用一次应用命令。"""
    return build_backend_command(python, development=False)


def _process_creation_flags() -> int:
    """Windows 返回新进程组标志以支持 CTRL_BREAK；其他系统返回 0。"""
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]


def _start_process(
    name: str,
    command: Sequence[str],
    log_path: Path,
    *,
    ownership_job: _WindowsKillOnCloseJob | None = None,
) -> ManagedProcess:
    """启动一个受管理子进程，并把 stdout/stderr 追加到指定日志。

    ``name`` 用于诊断，``command`` 是已分词参数，``log_path`` 是输出文件，
    ``ownership_job`` 可选负责 Windows 进程树归属。失败会先关闭已打开日志句柄；
    成功返回同时持有 Popen 和句柄的 ``ManagedProcess``。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    log_handle.write(
        f"\n\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {name} start ===\n".encode(
            "utf-8"
        )
    )
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    try:
        process = subprocess.Popen(
            list(command),
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=_process_creation_flags(),
            start_new_session=os.name != "nt",
        )
    except Exception:
        log_handle.close()
        raise
    if ownership_job is not None:
        ownership_job.assign(process)
    guard = None
    guard_pipe = None
    if os.name != "nt":
        read_pipe, guard_pipe = os.pipe()
        try:
            guard = subprocess.Popen([str(_python_executable()), "-m", "scripts.novelmind_watchdog",
                                      str(read_pipe), str(process.pid)], cwd=PROJECT_ROOT,
                                     pass_fds=(read_pipe,), start_new_session=True,
                                     stdout=log_handle, stderr=subprocess.STDOUT)
        except Exception:
            os.close(guard_pipe)
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            log_handle.close()
            raise
        finally:
            os.close(read_pipe)
    return ManagedProcess(name=name, process=process, log_handle=log_handle, guard=guard, guard_pipe=guard_pipe)


def _backend_is_ready() -> bool:
    """请求健康端点并同时核对 status 与 project，避免误认占用端口的其他服务。"""
    payload = _json_request(BACKEND_HEALTH_URL, timeout=1.0)
    return bool(
        payload
        and payload.get("status") == "ok"
        and payload.get("project") == "NovelMind"
    )


def _frontend_is_ready() -> bool:
    """请求新前端根页面，返回是否已能得到 HTTP 200。"""
    try:
        with _LOCAL_OPENER.open(FRONTEND_URL, timeout=1.0) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _wait_until_ready(
# “进程还活着”不等于服务可用；必须轮询实际 HTTP 就绪信号并设置总超时。
    name: str,
    process: subprocess.Popen[bytes],
    check: Any,
    *,
    timeout: float = STARTUP_TIMEOUT_SECONDS,
) -> None:
    """轮询 ``check`` 直到服务就绪、子进程退出或超过总超时。

    ``name`` 用于错误文字；``process`` 用来检测提前退出；``check`` 是无参数布尔
    回调；``timeout`` 为总秒数。失败抛 LauncherError，成功无返回值。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise LauncherError(
                f"{name}启动失败（退出码 {return_code}），请查看对应日志。"
            )
        if check():
            return
        time.sleep(0.25)
    raise LauncherError(f"等待{name}就绪超时，请查看对应日志。")


def _terminate_process(managed: ManagedProcess) -> None:
    """先请求优雅退出，超时后强制结束完整进程树，最终必定关闭日志句柄。

    参数是 start 阶段返回的 ``ManagedProcess``。函数可重复面对已退出进程。
    """
    process = managed.process
    try:
        if process.poll() is None:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        logging.warning("%s did not stop gracefully; forcing termination", managed.name)
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        process.wait(timeout=5)
    finally:
        if managed.guard_pipe is not None:
            os.close(managed.guard_pipe)
        if managed.guard is not None:
            managed.guard.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS + 2)
        managed.log_handle.close()


def _verify_ports_are_free() -> None:
    """检查应用端口；冲突时拒绝自动杀死未知进程并抛 LauncherError。"""
    conflicts = []
    if not _port_is_available(BACKEND_HOST, BACKEND_PORT):
        conflicts.append(str(BACKEND_PORT))
    if conflicts:
        joined = "、".join(conflicts)
        raise LauncherError(
            f"端口 {joined} 已被其他程序占用。启动器不会自动结束未知进程。"
        )


def _save_state(
    *,
    token: str,
    control_port: int,
    development: bool,
    status: str,
    backend_pid: int | None = None,
    frontend_pid: int | None = None,
) -> None:
    """原子保存本次启动所有权、控制端口、模式、状态及可选子进程 PID。

    所有参数仅限关键字，避免多个 int/bool/string 被按位置传错。PID 只用于诊断，
    是否存活仍以带 token 的控制端点为准。
    """
    _atomic_write_json(
        STATE_FILE,
        {
            "project": "NovelMind",
            "token": token,
            "control_port": control_port,
            "launcher_pid": os.getpid(),
            "backend_pid": backend_pid,
            "frontend_pid": frontend_pid,
            "development": development,
            "status": status,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )


def start(*, development: bool, open_browser: bool = True) -> int:
    """启动并持续监督完整 NovelMind 本机服务。

    ``development`` 决定后端是否开启 reload；``open_browser`` 决定全部就绪后是否
    打开页面，二者均为仅限关键字参数。函数处理重复启动、状态文件、控制服务器、
    API/网页顺序就绪、信号和意外退出；finally 反向清理全部资源。成功返回 0，
    可操作失败抛 LauncherError 交给 main 转换退出码。
    """
    _configure_logging()
    running = _running_launcher_state()
    if running is not None:
        _print(
            f"NovelMind 已在运行（管理进程 {running.get('launcher_pid')}）。"
        )
        if open_browser and running.get("status") == "ready":
            webbrowser.open(FRONTEND_URL)
        return 0

    if STATE_FILE.exists():
        logging.warning("Removing stale launcher state file")
        try:
            STATE_FILE.unlink()
        except OSError as exc:
            raise LauncherError(f"无法清理过期运行状态：{exc}") from exc

    python = _python_executable()
    _verify_ports_are_free()

    stop_event = threading.Event()
    token = secrets.token_urlsafe(32)
    control_server, control_thread = _start_control_server(token, stop_event)
    control_port = int(control_server.server_address[1])
    processes: list[ManagedProcess] = []
    ownership_job = _WindowsKillOnCloseJob()
    exit_code = 0

    def request_stop(_signum: int, _frame: object) -> None:
        """信号处理回调只设置 Event，把实际清理留给主循环的 finally。"""
        stop_event.set()

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    # 从状态文件创建开始，所有资源获取都位于 try/finally 管理范围内。
    try:
        _save_state(
            token=token,
            control_port=control_port,
            development=development,
            status="starting",
        )
        mode_label = "开发模式" if development else "日常模式"
        _print(f"正在以{mode_label}启动 NovelMind……")

        # 前端依赖后端 API，因此不能在后端健康检查通过之前启动/打开页面。
        backend = _start_process(
            "backend",
            build_backend_command(python, development=development),
            BACKEND_LOG,
            ownership_job=ownership_job,
        )
        processes.append(backend)
        _print("正在等待后端服务……")
        _wait_until_ready("后端", backend.process, _backend_is_ready)
        _print(f"后端已就绪：{BACKEND_HEALTH_URL}")

        _wait_until_ready("网页", backend.process, _frontend_is_ready)

        control_server.launcher_status = "ready"
        _save_state(
            token=token,
            control_port=control_port,
            development=development,
            status="ready",
            backend_pid=backend.process.pid,
            frontend_pid=backend.process.pid,
        )
        _print(f"NovelMind 已启动：{FRONTEND_URL}")
        _print("按 Ctrl+C，或双击“停止 NovelMind”即可安全停止。")
        if open_browser:
            webbrowser.open(FRONTEND_URL)

        while not stop_event.wait(0.5):
            for managed in processes:
                return_code = managed.process.poll()
                if return_code is not None:
                    raise LauncherError(
                        f"{managed.name} 意外退出（退出码 {return_code}），"
                        "请查看日志。"
                    )
    except LauncherError:
        exit_code = 1
        raise
    # finally 覆盖正常 Ctrl+C、子进程异常和启动中途失败，清理顺序与启动相反。
    finally:
        control_server.launcher_status = "stopping"
        _print("正在停止 NovelMind……")
        for managed in reversed(processes):
            _terminate_process(managed)
        ownership_job.close()
        control_server.shutdown()
        control_server.server_close()
        control_thread.join(timeout=2)
        _remove_owned_state(token)
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        _print("NovelMind 已停止。")
    return exit_code


def stop() -> int:
    """通过仅监听回环地址的认证控制端点，请求现有启动器执行统一清理。"""
    _configure_logging()
    state = _running_launcher_state()
    if state is None:
        if STATE_FILE.exists():
            logging.warning("Launcher state exists but control endpoint is unavailable")
        _print("NovelMind 当前没有由启动管理器运行。")
        return 0
    token = state["token"]
    control_port = state["control_port"]
    response = _json_request(
        f"http://127.0.0.1:{control_port}/stop",
        method="POST",
        token=token,
        timeout=3.0,
    )
    if response is None or response.get("stopping") is not True:
        raise LauncherError("未能向 NovelMind 发送停止请求，请在启动窗口按 Ctrl+C。")
    _print("已发送安全停止请求，正在等待服务退出……")
    deadline = time.monotonic() + (SHUTDOWN_TIMEOUT_SECONDS * 2 + 5)
    while time.monotonic() < deadline:
        if _running_launcher_state() is None:
            _print("NovelMind 已安全停止。")
            return 0
        time.sleep(0.25)
    raise LauncherError(
        "NovelMind 未在预期时间内停止，请查看启动窗口和 launcher.log。"
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析 ``--stop``、``--dev``、``--no-browser``，返回 argparse 命名空间。

    ``argv=None`` 表示读取真实命令行；测试传列表可避免修改全局 ``sys.argv``。
    """
    parser = argparse.ArgumentParser(description="NovelMind 本地启动管理器")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--stop", action="store_true", help="停止正在运行的 NovelMind")
    action.add_argument("--dev", action="store_true", help="使用后端热重载开发模式")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动就绪后不自动打开浏览器",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行总入口：选择 start/stop，把已知与未知异常转换为退出码 1。

    返回 0 表示成功，1 表示失败。文件末尾 ``SystemExit(main())`` 将返回值交给
    Windows cmd；作为模块导入时不会自动执行。
    """
    args = _parse_args(argv)
    try:
        if args.stop:
            return stop()
        return start(development=args.dev, open_browser=not args.no_browser)
    except LauncherError as exc:
        logging.exception("Launcher failed")
        _write_error(f"\n启动管理器错误：{exc}")
        _write_error(f"日志位置：{LAUNCHER_LOG}")
        return 1
    except Exception as exc:  # pragma: no cover - last-resort user diagnostics
        logging.exception("Unexpected launcher failure")
        _write_error(f"\n未预期的启动错误：{exc}")
        _write_error(f"日志位置：{LAUNCHER_LOG}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
