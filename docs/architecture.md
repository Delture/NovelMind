# 初始架构说明

## 总体结构

NovelMind 采用前后端分离的轻量结构：

- `backend/`：FastAPI 后端服务。
- `frontend/`：Streamlit 前端展示页面。
- `data/`：本地开发数据目录。
- `docs/`：项目文档。

## 后端目录

- `backend/app/main.py`：FastAPI 应用入口。
- `backend/app/api/`：FastAPI 接口路由。
- `backend/app/core/`：数据库连接等基础能力。
- `backend/app/models/`：SQLAlchemy 数据模型。
- `backend/app/parser/`：小说章节解析逻辑。
- `backend/app/rag/`：后续存放向量检索和 RAG 相关代码。
- `backend/app/analyzer/`：后续存放摘要、角色抽取、结构分析等逻辑。
- `backend/tests/`：后续存放 pytest 测试。

## 数据流设想

1. 用户上传 TXT 小说。
2. 后端解析小说章节。
3. 小说和章节信息保存到 SQLite。
4. 前端展示章节列表。
5. 后续将章节文本写入向量库，用于 RAG 问答和分析。

## SQLite 数据库

默认数据库文件为 `data/novelmind.db`，后端启动时自动创建缺少的表。
可以通过环境变量 `DATABASE_URL` 修改数据库地址。

当前包含两张表：

- `novels`：保存文件名、文本编码、章节数和创建时间。
- `chapters`：保存所属小说、章节顺序、标题、正文和字符数。

一个 `novel` 可以关联多个 `chapter`。小说与章节在同一个事务中保存，
避免出现只保存小说但章节不完整的情况。
