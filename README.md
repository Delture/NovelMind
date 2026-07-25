# NovelMind

NovelMind 是一个基于 RAG 的网络小说结构分析与写作辅助系统。

当前阶段已经打通 TXT 小说的前端上传、章节解析、SQLite 保存和章节展示。后续将逐步加入章节摘要、角色抽取、RAG 问答和分析报告。

## 技术栈

- 后端：Python + FastAPI
- 前端展示：Streamlit
- 数据库：SQLite
- ORM：SQLAlchemy
- 向量库：Chroma（当前只预留目录和接口设计，不完整接入）
- 测试：pytest

## 安装依赖

建议先创建虚拟环境：

```bash
python -m venv .venv
```

Windows PowerShell 启动虚拟环境：

```bash
.\.venv\Scripts\Activate.ps1
```

安装依赖：

```bash
python -m pip install -r requirements.txt
```

## 启动后端

```bash
uvicorn backend.app.main:app --reload
```

访问：

- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/docs

## 启动前端

保持后端终端运行，再打开一个 PowerShell 终端，进入项目目录并激活虚拟环境：

```bash
streamlit run frontend/streamlit_app.py
```

浏览器通常会自动打开 `http://localhost:8501`。页面右上角显示“后端已连接”后，即可选择 TXT 文件并点击“解析并保存”。

## 当前 MVP 功能

- 已创建项目目录结构。
- 已提供 FastAPI 健康检查接口。
- 已提供 TXT 章节解析器。
- 已提供 TXT 小说上传接口。
- 已使用 SQLite 和 SQLAlchemy 保存小说及章节。
- 已提供 Streamlit 上传页面、章节列表和正文预览。

默认数据库文件位于 `data/novelmind.db`，启动后端时自动创建。该文件已被 Git 忽略。

项目内置的原创测试文件位于 `data/samples/manual_test_novel.txt`，上传后应解析出 3 章。

## 运行测试

```bash
pytest
```
