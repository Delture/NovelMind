# Streamlit 兼容入口

保留旧页面与功能，默认网页不依赖 Streamlit、Altair 或 requests。
安装兼容依赖：`python -m pip install -r legacy/streamlit/requirements.txt`。
从项目根目录启动 API：`python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000`。
然后启动旧界面：`python -m streamlit run legacy/streamlit/streamlit_app.py --server.address 127.0.0.1 --server.port 8502`。
兼容界面的 BACKEND_URL 默认 8000，可指向已启动的主应用 8501。不要再启动第二个 API。
兼容测试：`python -m pytest tests/legacy -q`。使用本目录依赖时可运行 `python -m pytest tests -q` 验收全部分组。

根目录 `.streamlit/config.toml` 仅供从项目根目录启动的旧 Streamlit 读取，保留其回环地址与界面配置。FastAPI 和默认原生网页不读取该文件。
