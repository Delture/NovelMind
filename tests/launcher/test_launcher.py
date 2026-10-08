from __future__ import annotations
"""Tests for the local one-click process supervisor."""


import os
import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import novelmind_launcher as launcher


def test_console_output_failure_does_not_interrupt_launcher_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "builtins.print",
        Mock(side_effect=OSError(22, "console closed")),
    )

    launcher._print("正在停止 NovelMind……")
    launcher._write_error("NovelMind 启动失败")


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(200, True), (204, False), (404, False)],
)
def test_frontend_readiness_requires_exact_success_page(
    status_code: int,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status = status_code

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    assert launcher._frontend_is_ready() is expected


def test_commands_use_virtual_environment_python_and_loopback() -> None:
    python = Path("C:/project/.venv/Scripts/python.exe")

    backend = launcher.build_backend_command(python, development=False)
    frontend = launcher.build_frontend_command(python)

    assert backend[:4] == [
        str(python),
        "-m",
        "uvicorn",
        "backend.app.main:app",
    ]
    assert "--reload" not in backend
    assert backend[backend.index("--host") + 1] == "127.0.0.1"
    assert backend[backend.index("--port") + 1] == "8501"
    assert frontend[:4] == [str(python), "-m", "uvicorn", "backend.app.main:app"]
    assert frontend[frontend.index("--host") + 1] == "127.0.0.1"
    assert frontend[frontend.index("--port") + 1] == "8501"


def test_development_command_enables_backend_reload() -> None:
    command = launcher.build_backend_command(Path("python.exe"), development=True)

    assert "--reload" in command
    assert command[command.index("--reload-dir") + 1] == str(
        launcher.PROJECT_ROOT
    )


def test_runtime_state_is_written_atomically_and_read_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "runtime" / "launcher.json"
    monkeypatch.setattr(launcher, "STATE_FILE", state_file)

    launcher._save_state(
        token="local-token",
        control_port=12345,
        development=False,
        status="ready",
        backend_pid=101,
        frontend_pid=102,
    )

    state = launcher._read_state()
    assert state is not None
    assert state["project"] == "NovelMind"
    assert state["token"] == "local-token"
    assert state["backend_pid"] == 101
    assert state["frontend_pid"] == 102
    assert not list(state_file.parent.glob("*.tmp"))


def test_control_server_requires_token_and_accepts_stop() -> None:
    stop_event = threading.Event()
    server, thread = launcher._start_control_server("secret-token", stop_event)
    port = int(server.server_address[1])
    try:
        assert launcher._json_request(
            f"http://127.0.0.1:{port}/status",
            token="wrong-token",
        ) is None
        status = launcher._json_request(
            f"http://127.0.0.1:{port}/status",
            token="secret-token",
        )
        assert status is not None
        assert status["project"] == "NovelMind"
        response = launcher._json_request(
            f"http://127.0.0.1:{port}/stop",
            method="POST",
            token="secret-token",
        )
        assert response == {"project": "NovelMind", "stopping": True}
        assert stop_event.wait(1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_wait_until_ready_reports_early_process_exit() -> None:
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = 7

    with pytest.raises(launcher.LauncherError, match="退出码 7"):
        launcher._wait_until_ready(
            "后端",
            process,
            lambda: False,
            timeout=0.1,
        )


def test_terminate_process_closes_log_for_finished_process() -> None:
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = 0
    log_handle = Mock()
    managed = launcher.ManagedProcess("backend", process, log_handle)

    launcher._terminate_process(managed)

    log_handle.close.assert_called_once_with()
    process.send_signal.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows CTRL_BREAK_EVENT")
def test_terminate_process_requests_graceful_windows_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    log_handle = Mock()
    managed = launcher.ManagedProcess("frontend", process, log_handle)
    monkeypatch.setattr(launcher.os, "name", "nt")

    launcher._terminate_process(managed)

    process.send_signal.assert_called_once_with(signal.CTRL_BREAK_EVENT)
    process.wait.assert_called_once_with(timeout=launcher.SHUTDOWN_TIMEOUT_SECONDS)
    log_handle.close.assert_called_once_with()


def test_parse_args_keeps_daily_mode_as_default() -> None:
    daily = launcher._parse_args([])
    development = launcher._parse_args(["--dev", "--no-browser"])
    stop = launcher._parse_args(["--stop"])

    assert not daily.dev and not daily.stop and not daily.no_browser
    assert development.dev and development.no_browser
    assert stop.stop and not stop.dev
