"""启动隔离的浏览器验收环境，执行检查后自动关闭子进程；不触碰开发数据。"""

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def free_port():
    """向系统申请一个空闲回环端口用于临时验收。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    """运行真实前后端和 Playwright 检查，使用临时数据库与空密钥。

    进程输出、截图和检查结果默认保存在系统临时目录，可用 QA_OUTPUT 指定。无论检查
    成功、失败还是超时，都只清理本次创建的进程；临时目录由标准库安全释放。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader-alignment", action="store_true",
                        help="仅检查阅读目录对齐、选中态及已有点击/键盘操作")
    parser.add_argument("--suite", choices=("web", "reader-features", "provider-settings", "materials"),
                        default="web", help="选择隔离浏览器验收组")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = Path(os.environ["QA_OUTPUT"]) if os.environ.get("QA_OUTPUT") else Path(tempfile.mkdtemp(prefix="novelmind-web-qa-"))
    output.mkdir(parents=True, exist_ok=True)
    processes, logs = [], []
    with tempfile.TemporaryDirectory(prefix="novelmind-web-") as temporary:
        env = os.environ.copy()
        for key in tuple(env):
            if key.startswith(("DEEPSEEK_", "EMBEDDING_", "GENERATION_", "SUMMARY_")):
                env.pop(key)
        backend_port = frontend_port = free_port()
        env.update(DATABASE_URL="sqlite:///" + (Path(temporary) / "test.db").as_posix(),
                   VECTOR_STORE_PATH=str(Path(temporary) / "vectors"),
                   MODEL_SETTINGS_PATH=str(Path(temporary) / "models.json"),
                   DEEPSEEK_API_KEY="", EMBEDDING_API_KEY="",
                   PYTHON_DOTENV_DISABLED="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   NOVELMIND_RUNTIME_DIR=str(Path(temporary) / "runtime"),
                   NOVELMIND_LOG_DIR=str(Path(temporary) / "logs"),
                   BACKEND_URL=f"http://127.0.0.1:{backend_port}",
                   QA_URL=f"http://127.0.0.1:{frontend_port}", QA_OUTPUT=str(output), NOVELMIND_QA="isolated")
        try:
            for name, module, port in [("backend", "tests.web.web_browser_server:app", backend_port)]:
                log = (output / f"{name}.log").open("wb")
                logs.append(log)
                process = subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
                                           cwd=project, env=env, stdout=log, stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                processes.append(process)
                url = f"http://127.0.0.1:{port}" + ("/health" if name == "backend" else "/")
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} exited; see {output}")
                    try:
                        with urllib.request.urlopen(url, timeout=1) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError(f"{name} startup timed out")
            script = "check_reader_alignment.cjs" if args.reader_alignment else {
                "web": "check_web.cjs", "reader-features": "check_reader_features.cjs",
                "provider-settings": "check_provider_settings.cjs", "materials": "check_materials.cjs",
            }[args.suite]
            result = subprocess.run(["node", str(project / "scripts" / script)], cwd=project, env=env, timeout=180)
            print(f"Browser QA output: {output}")
            return result.returncode
        finally:
            for process in reversed(processes):
                if os.name == "nt" and process.poll() is None:
                    # Windows venv 的 python.exe 可能启动实际解释器子进程，须一起回收。
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for log in logs:
                log.close()


if __name__ == "__main__":
    raise SystemExit(main())
