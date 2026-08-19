# NovelMind

NovelMind 是一个面向本地单用户的网络小说结构分析与写作辅助系统。

本地负责 TXT 解析、SQLite 存储、任务编排、统计和界面展示；生成与理解类
能力通过外部 API 完成。生成模型与向量模型可在前端的“模型与密钥”页面
即时切换；默认接入 DeepSeek 与硅基流动，也支持 OpenAI 兼容端点。

第一次安装或需要配置 API 时，请直接阅读
[完整使用说明](docs/user_guide.md)。其中包含 DeepSeek、硅基流动
`Qwen/Qwen3-Embedding-4B`（1024 维）的填写方法、全部界面功能、HTTP API、备份与
常见故障处理。

如果你只有 Python 基础、希望通过本项目学习完整开发过程，请从
[从零到完整项目的代码阅读指南](docs/code_reading_guide.md) 开始，再按其中路线
阅读带教学注释的核心源码。阅读中遇到陌生 Python、FastAPI、SQLAlchemy 或
异步语法，可以查阅 [项目 Python 语法指南](docs/python_syntax_guide.md)。

## 技术栈

- 后端：Python + FastAPI
- 前端展示：Streamlit
- 数据库：SQLite
- ORM：SQLAlchemy
- 文本生成：DeepSeek V4 Pro API
- 外部 embedding：硅基流动 `Qwen/Qwen3-Embedding-4B`（1024 维）
- 模型配置：前端管理供应商、模型、端点和 API Key，`.env` 作为回退
- 向量库：本地 SQLite + NumPy 精确余弦检索（只存外部生成的向量）
- 测试：pytest

## 安装依赖

项目基线为 Python 3.12（依赖要求至少 Python 3.10）。Windows PowerShell
可先创建虚拟环境：

```powershell
py -3.12 -m venv .venv
```

Windows PowerShell 启动虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

首次使用可以直接打开侧边栏的“模型与密钥”，也可以在本地 `.env` 中填写。
页面保存的配置立即生效并覆盖 `.env`；恢复后重新使用 `.env`。API Key
不会由读取接口或页面回显。不要把密钥提交到 Git，也不要在聊天、日志或截图中公开。
未配置密钥时，资料库和正文阅读等非模型功能仍可正常使用。

“模型与密钥”页同时提供章节摘要并发调度。自动模式会根据模型从安全档位启动，
连续成功后逐步扩容，并在限流或临时服务错误时减半降速；手动模式固定使用设置值。
网络请求并发执行，SQLite 摘要保存与进度更新仍保持串行，避免本地写锁冲突。

生成请求只允许发送到 DeepSeek 官方 HTTPS 端点
`https://api.deepseek.com`（或其 `/v1` 路径）。`DEEPSEEK_MAX_TOKENS`
默认是 `8192`，可按账户限额调整。开始生成前，界面会明确列出要发送的
处理数据范围，并分别要求确认外部处理和内容处理权利。报告阶段只发送当前
章节标题、派生摘要和可选的有效人物分析，不再次发送小说正文。DeepSeek 的
缓存、留存、服务改进和数据处理规则以界面链接的官方条款与隐私政策为准。

如需使用 RAG，请在“模型与密钥”页面填写硅基流动 API Key；也可以在本地
`.env` 填写 `EMBEDDING_API_KEY`。官方 API 根地址固定为：

```text
https://api.siliconflow.cn/v1
```

模型固定为 `Qwen/Qwen3-Embedding-4B`，输出维度固定为 `1024`。建索引会把
章节标题和原文片段发送至硅基流动；提问会把问题发送至硅基流动生成查询向量，
再把问题和检索片段发送至 DeepSeek。界面会逐项披露并要求确认。
派生向量默认保存在 `VECTOR_STORE_PATH=./vector_store`；旧版
`CHROMA_PATH` 仅作为配置兼容别名，旧 Chroma 索引不会被复用，需要重建。

## 一键启动（推荐）

完成一次环境安装后，直接双击项目根目录的 `启动 NovelMind.cmd`。启动管理器会：

- 使用项目自己的 `.venv`，无需手动激活环境；
- 依次启动后端和前端，并等待健康检查通过；
- 自动打开 <http://127.0.0.1:8501>；
- 阻止重复启动，并明确提示 8000/8501 端口冲突；
- 将运行输出写入 `logs/backend.log`、`logs/frontend.log` 和
  `logs/launcher.log`。

保留启动窗口即可。使用结束后，在窗口按 `Ctrl+C`，或双击
`停止 NovelMind.cmd`。修改后端代码时使用 `开发模式启动 NovelMind.cmd`，它会
额外启用 Uvicorn 热重载；日常使用不启用热重载。

## 手动启动后端

```powershell
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

访问：

- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/docs

开发时可在命令末尾添加 `--reload`。

## 手动启动前端

保持后端终端运行，再打开一个 PowerShell 终端，进入项目目录并激活虚拟环境：

```powershell
python -m streamlit run frontend/streamlit_app.py --server.address 127.0.0.1
```

浏览器通常会自动打开 `http://127.0.0.1:8501`。页面显示“后端已连接”后，
即可导入小说、从资料库重新打开小说、阅读正文、生成章节摘要、分析人物、
使用 RAG 原文问答或生成全书分析报告。
`.streamlit/config.toml` 也固定监听 `127.0.0.1` 并保持 CORS/XSRF 防护开启；
不要为本地单用户模式改成 `0.0.0.0`。

## 当前 MVP 功能

- 已创建项目目录结构。
- 已提供 FastAPI 健康检查接口。
- 已提供 TXT 章节解析器。
- 已提供 TXT 小说上传接口。
- 已使用 SQLite 和 SQLAlchemy 保存小说及章节。
- 已提供资料库、分页章节列表、按需正文读取和小说删除接口。
- 已接入 DeepSeek V4 Pro 结构化章节摘要。
- 已提供全书或单章摘要任务、异步并发生成、串行持久化、失败恢复提示和结果分页。
- 已提供 DeepSeek 人物分页抽取、跨章节人物归并、关系推理与本地确定性统计。
- 已提供人物来源缓存、全书原子快照、搜索排序、出场记录和关系明细。
- 已在发送正文前展示处理范围、官方条款与隐私政策，并要求用户分别确认
  外部处理和内容处理权利。
- 已提供 Streamlit 上传、资料库、正文阅读、摘要和人物统计工作区。
- 已接入硅基流动外部 embedding、确定性分块和增量向量复用。
- 已提供版本化本地向量集合、SQLite 原子索引指针、失败保留旧索引和
  启动孤立集合清理。
- 已提供索引估算与授权、查询向量、原文来源复核、DeepSeek 结构化回答、
  引用白名单、证据不足标记和可恢复问答历史。
- 已提供 Streamlit RAG 工作区；答案和原文引用只按纯文本展示。
- 已提供基于当前章节摘要和可选人物快照的 DeepSeek 分层分析报告。
- 已提供报告先决条件与调用上限估算、双授权、force 二次确认、原子快照、
  章节引用白名单、旧报告保留和 Streamlit 纯文本展示。

默认数据库文件位于 `data/novelmind.db`，启动后端时自动创建。该文件已被 Git 忽略。

项目内置的原创测试文件位于 `data/samples/manual_test_novel.txt`，上传后应解析出 3 章。

## 运行测试

```bash
python -m pytest -q
```

默认测试使用假模型提供商，不访问真实付费 API。

当前完整回归基线见 `docs/dev_log.md`。人物抽取单页最多返回 8 条观察；如果
DeepSeek 声明仍有未返回人物，后端会继续调用 API（每个片段最多 16 页）。
重复页、空续页、越界 ID 或超过页数上限都会让本次任务原子失败，不会用
不完整结果覆盖旧人物快照。

RAG 测试使用假 embedding/DeepSeek 提供商和临时本地向量库，不访问真实付费 API；
会校验 1024 维响应、增量复用、原子索引、正文篡改、伪造引用和纯文本渲染。

报告测试使用假 DeepSeek 提供商，不访问真实付费 API；会校验摘要来源哈希、
正文不二次发送、分层 API 生成、章节引用白名单、失败保留旧报告、授权门禁
和纯文本渲染。
