# NovelMind

NovelMind 是一个基于 RAG 的网络小说结构分析与写作辅助系统。

当前阶段已经实现 TXT 小说上传、章节解析，以及使用 SQLite 保存小说和章节。后续将逐步加入 Streamlit 上传页面、章节摘要、角色抽取、RAG 问答和分析报告。

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

```bash
streamlit run frontend/streamlit_app.py
```

## 当前 MVP 功能

- 已创建项目目录结构。
- 已提供 FastAPI 健康检查接口。
- 已提供 Streamlit 最小展示页面。
- 已提供 TXT 章节解析器。
- 已提供 TXT 小说上传接口。
- 已使用 SQLite 和 SQLAlchemy 保存小说及章节。

默认数据库文件位于 `data/novelmind.db`，启动后端时自动创建。该文件已被 Git 忽略。
