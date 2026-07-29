# NovelMind

NovelMind 是一个面向本地单用户的网络小说结构分析与写作辅助系统。

本地负责 TXT 解析、SQLite 存储、任务编排、统计和界面展示；生成与理解类
能力通过外部 API 完成。目前已接入 DeepSeek V4 Pro 章节摘要与人物分析，
后续将继续加入 RAG 问答和分析报告。

## 技术栈

- 后端：Python + FastAPI
- 前端展示：Streamlit
- 数据库：SQLite
- ORM：SQLAlchemy
- 文本生成：DeepSeek V4 Pro API
- 向量库：Chroma（RAG 阶段接入）
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

如需使用章节摘要或人物分析，请只在本地 `.env` 中填写
`DEEPSEEK_API_KEY`。不要把密钥提交到 Git，也不要在聊天、日志或截图中公开。
未配置密钥时，资料库和正文阅读等非模型功能仍可正常使用。

生成请求只允许发送到 DeepSeek 官方 HTTPS 端点
`https://api.deepseek.com`（或其 `/v1` 路径）。`DEEPSEEK_MAX_TOKENS`
默认是 `8192`，可按账户限额调整。开始生成前，界面会明确列出要发送的
章节标题与正文，并分别要求确认外部处理和内容处理权利。DeepSeek 的缓存、
留存、服务改进和数据处理规则以界面链接的官方条款与隐私政策为准。

## 启动后端

```powershell
uvicorn backend.app.main:app --host 127.0.0.1 --reload
```

访问：

- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/docs

## 启动前端

保持后端终端运行，再打开一个 PowerShell 终端，进入项目目录并激活虚拟环境：

```powershell
streamlit run frontend/streamlit_app.py --server.address 127.0.0.1
```

浏览器通常会自动打开 `http://127.0.0.1:8501`。页面显示“后端已连接”后，
即可导入小说、从资料库重新打开小说、阅读正文、生成章节摘要或分析人物。
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
- 已提供全书或单章摘要任务、持久化进度、失败恢复提示和结果分页。
- 已提供 DeepSeek 人物分页抽取、跨章节人物归并、关系推理与本地确定性统计。
- 已提供人物来源缓存、全书原子快照、搜索排序、出场记录和关系明细。
- 已在发送正文前展示处理范围、官方条款与隐私政策，并要求用户分别确认
  外部处理和内容处理权利。
- 已提供 Streamlit 上传、资料库、正文阅读、摘要和人物统计工作区。

RAG 问答和分析报告尚未实现。RAG 所需的 embedding 也将调用外部 API，
不在本地运行模型；提供商会在进入 RAG 里程碑前确定。

默认数据库文件位于 `data/novelmind.db`，启动后端时自动创建。该文件已被 Git 忽略。

项目内置的原创测试文件位于 `data/samples/manual_test_novel.txt`，上传后应解析出 3 章。

## 运行测试

```bash
python -m pytest -q
```

默认测试使用假模型提供商，不访问真实付费 API。

当前完整回归基线为 `360 passed`。人物抽取单页最多返回 8 条观察；如果
DeepSeek 声明仍有未返回人物，后端会继续调用 API（每个片段最多 16 页）。
重复页、空续页、越界 ID 或超过页数上限都会让本次任务原子失败，不会用
不完整结果覆盖旧人物快照。
