# 架构说明

## 总体结构

NovelMind 采用前后端分离的轻量结构：

- `backend/`：FastAPI 后端服务。
- `frontend/`：Streamlit 前端展示页面。
- `data/`：本地开发数据目录。
- `docs/`：项目文档。

运行时基线为 Python 3.12。FastAPI 和 Streamlit 都只监听
`127.0.0.1`；Streamlit 配置显式开启 CORS 与 XSRF 防护，不对局域网或公网
提供服务。

## 后端目录

- `backend/app/main.py`：FastAPI 应用入口。
- `backend/app/api/`：FastAPI 接口路由。
- `backend/app/core/`：配置、数据库连接和版本迁移。
- `backend/app/models/`：SQLAlchemy 数据模型。
- `backend/app/parser/`：小说章节解析逻辑。
- `backend/app/rag/`：后续存放向量检索和 RAG 相关代码。
- `backend/app/analyzer/`：模型提供商契约、DeepSeek 客户端和摘要任务。
- `backend/tests/`：pytest 单元、接口、迁移和并发测试。

## 当前数据流

1. 用户上传 TXT 小说。
2. Streamlit 通过 HTTP 把文件字节发送到 FastAPI。
3. 后端检查文件类型、大小和编码，并解析小说章节。
4. 小说和章节信息保存到 SQLite。
5. 前端通过分页接口展示资料库和章节元数据，只在阅读时获取单章正文。
6. 用户在摘要工作区查看外部处理范围并明确同意发送正文。
7. 后端创建持久化任务，逐章调用 DeepSeek V4 Pro，校验结构化 JSON 后保存
   摘要；前端通过任务接口恢复进度并分页读取结果。
8. 后续 RAG 会调用外部 embedding API，再把派生向量写入本地 Chroma。

## 前端目录

- `frontend/streamlit_app.py`：页面入口和界面渲染。
- `frontend/api_client.py`：集中调用 FastAPI，并保留 HTTP 状态码供界面决策。
- `frontend/ui_styles.py`：集中保存页面样式，避免业务代码中混入大量 CSS。

前端通过环境变量 `BACKEND_URL` 获取后端地址，未配置时默认使用
`http://127.0.0.1:8000`。

## SQLite 数据库

默认数据库文件为 `data/novelmind.db`，后端启动时自动建表并执行有版本记录的
幂等迁移。可以通过环境变量 `DATABASE_URL` 修改数据库地址。

当前核心表包括：

- `novels`：保存文件名、文本编码、章节数和创建时间。
- `chapters`：保存所属小说、章节顺序、标题、正文和字符数。
- `chapter_summaries`：保存结构化摘要、来源哈希、模型和提示词版本。
- `analysis_jobs`：保存任务范围、状态、进度和安全错误信息。

一个 `novel` 可以关联多个 `chapter`。小说与章节在同一个事务中保存，
避免出现只保存小说但章节不完整的情况。SQLite 外键默认启用，删除小说会
级联删除章节、摘要和任务。partial unique 索引确保同一小说、同一任务类型
最多只有一个 `queued` 或 `running` 任务。

当前迁移版本为 4：第 2 版加入摘要与任务表，第 3 版加入任务目标章节和
活动任务唯一索引，第 4 版持久化 `force_refresh`。后台执行始终以数据库中
保存的任务范围和 force 值为准，不依赖进程内回调参数。

## 模型边界

- 本地不运行大语言模型或 embedding 模型。
- `backend/app/analyzer/llm.py` 定义提供商无关的结构化生成契约。
- DeepSeek 密钥仅从本地环境读取；状态接口不会返回密钥。
- DeepSeek 基础地址只接受 `api.deepseek.com` 的 HTTPS 根路径或 `/v1`；
  不允许自定义主机、HTTP、用户信息或端口。
- 外部 embedding 端点必须使用 HTTPS，避免未来的密钥与派生文本通过明文
  HTTP 传输。
- 原文只在用户明确同意后发送，日志和任务错误不记录正文、密钥或推理内容。
- 外部处理确认和内容处理权利确认是两个独立门禁；确认通过前不会构造
  DeepSeek 客户端。
- 超长章节按段落切分并分层归并；估算接口使用同一切分算法展示最大尝试数。
