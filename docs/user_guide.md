# NovelMind 完整使用说明

本说明面向第一次使用 NovelMind 的本地单用户，涵盖安装、API 配置、启动、
全部界面功能、HTTP API、数据安全、备份和常见故障。

## 1. 先了解 NovelMind

NovelMind 在本机完成 TXT 解析、SQLite 存储、任务编排、向量检索和页面展示；
涉及语义理解或生成的功能调用外部 API，本地不运行大模型。

| 功能 | 生成模型 | 向量模型 | 发送到外部的内容 |
|---|---:|---:|---|
| 导入、资料库、正文阅读 | 不需要 | 不需要 | 不发送 |
| 章节摘要 | 需要 | 不需要 | 章节标题和原文 |
| 人物统计 | 需要 | 不需要 | 章节标题、原文和派生人物数据 |
| 建立 RAG 索引 | 不需要 | 需要 | 章节标题和原文片段 |
| RAG 提问 | 需要 | 需要 | 问题发给硅基流动；问题和检索片段发给 DeepSeek |
| 小说分析报告 | 需要 | 不需要 | 章节标题、派生摘要和可选人物分析；不再次发送原文 |

外部 API 可能收费。每个相关工作区都会先显示处理范围和预计调用步骤，并要求
确认外部处理与内容权利。详细条款与隐私链接收在“数据处理说明”中，需要时
展开查看。不要导入或发送自己无权处理的文本。

推荐使用顺序：

1. 导入 TXT 小说。
2. 在正文阅读中检查章节切分。
3. 生成全书章节摘要。
4. 按需生成人物统计。
5. 建立 RAG 索引后进行原文问答。
6. 在全部章节摘要完成后生成分析报告；人物统计是报告的可选增强输入。

## 2. 首次安装

### 2.1 环境要求

- Windows 10/11 或 macOS（Apple Silicon 已验证）。
- Python 3.12。
- Git（从 GitHub 获取或更新项目时需要）。
- 可访问 DeepSeek 和硅基流动 API 的网络。

项目目录示例：

```text
C:\Users\<用户名>\Documents\Projects\CodexPro\NovelMind
```

如果尚未下载项目：

```powershell
git clone https://github.com/Delture/NovelMind.git
cd NovelMind
```

创建并激活虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

安装依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果 PowerShell 阻止激活脚本，可执行一次：

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

随后重新运行激活命令。

## 3. 配置 API

### 3.1 创建本地配置文件

在项目根目录执行：

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` 已被 Git 忽略。它是首次启动和恢复配置时的默认来源。真实密钥只能
保存在本机配置中，不要提交到 GitHub，也不要放进截图、日志、聊天或前端代码。

### 3.2 在设置弹窗管理模型与密钥

应用启动后，点击侧栏“设置”打开“模型与设置”工作区。表单独立滚动，底部保存操作区和顶部关闭按钮持续可见，窄屏也不会遮挡末尾字段。供应商内部枚举仅作为请求状态，不占用只读展示栏。工作区分为“文本生成”“文本向量”“外观”三个标签：

- 生成模型：摘要、人物、RAG 回答和报告共用。
- 向量模型：RAG 建索引和问题向量共用。

生成与向量标签分别显示当前已保存地址、模型、配置状态，以及未保存草稿。预设提供 DeepSeek、硅基流动、OpenAI、OpenRouter 和自定义兼容服务中的适用选项；切换预设只填写接口类型与地址，模型、维度、密钥不自动更换。每部分都可手动填写模型与 API 地址。密钥输入框
永远为空：目的地不变时留空表示保留现有密钥，勾选“清除已保存的密钥”才会删除。读取接口只
返回“是否已设置”，不会返回密钥正文。
清除密钥只删除该角色的凭据，保留本次提交并校验通过的服务地址、模型等非秘密配置。
没有密钥时该角色显示为未配置，不能发起对应外部模型调用。

生成模型卡片还可以设置章节摘要并发：

- “自动调整”根据模型从推荐值启动，稳定完成后逐步提高；遇到限流、超时或
  临时服务错误会降低并发并重试失败章节。
- “手动设置”始终使用填写值。
- 实际并发不会超过待处理章节数、填写上限和识别到的模型上限。DeepSeek
  V4 Flash 按 2500、V4 Pro 按 500 作为已知上限；未知模型使用保守档位。

保存后，新任务立即使用新配置，不需要重启。已在运行的任务继续使用启动时的
配置；已有分析结果保留其模型标记。更换向量供应商、模型或维度后，现有 RAG
索引会显示为过期，必须重建。

页面配置原子保存在本机 `data/model_settings.json`，该文件已被 Git 忽略。
点击“恢复 .env 配置”后会先说明影响，再明确确认。这会删除页面保存的双角色覆盖配置（包括密钥覆盖）并放弃草稿，后续任务重新使用 `.env`；已在运行的任务仍使用原快照。未保存时关闭面板可选择继续编辑、放弃更改或保存并关闭。保存失败会保留草稿并显示错误，不伪装成成功。

供应商选项：

- `DeepSeek`：只接受 DeepSeek 官方 HTTPS 地址。
- `硅基流动`：只接受官方 API 地址，并固定
  `Qwen/Qwen3-Embedding-4B`、1024 维。
- `OpenAI 兼容服务`：填写服务商提供的模型名和 API 根地址；外部地址必须
  使用 HTTPS，本机 `localhost` 服务可使用 HTTP。

切换供应商或服务地址时必须同时填写新 API Key，防止把旧供应商密钥误发给
另一个端点；如果同时勾选“清除密钥”，则允许切换为未配置状态，之后必须
补填新密钥才能运行模型任务。

### 3.3 主动连接与模型目录

“测试连接”和“获取模型列表”都会主动连接所填服务，只执行 `GET /models`；不会发送小说、调用生成/向量接口，也不会保存草稿。成功仅表示模型目录可访问，不代表所选模型支持生成/embedding、输出质量合格或账户具备所有权限。

目录探测最长6秒，响应上限1MiB，最多显示200个模型；重定向不会跟随，认证失败、目录未支持、限流、超时或过大响应分别提示。服务没有目录或列表被截断时，可继续手动填写模型名称。
同角色、同供应商、同规范地址的空密钥输入可用已保存密钥；新地址必须输入对应新密钥，勾选清除时不会借用已保存密钥。显示开关只显示本次输入，不能取回旧密钥。

硅基流动向量预设继续限定 `Qwen/Qwen3-Embedding-4B` /1024维。其目录中仅该模型可选；点击明确标有“1024维”的选项才填写模型与维度。其他型号使用自定义兼容服务，并手动核对模型和维度。预设切换本身不改写这些值。

实现依据：[OpenAI模型目录](https://developers.openai.com/api/reference/resources/models/methods/list)、[DeepSeek目录](https://api-docs.deepseek.com/api/list-models/)、[硅基流动目录](https://api-docs.siliconflow.cn/docs/api/models-get)、[OpenRouter目录](https://openrouter.ai/docs/api/api-reference/models/get-models)。界面“已保存 · 已配置”只表示本机配置完整，与主动探测的网络/认证状态分开显示。

### 3.4 获取并填写 DeepSeek API

DeepSeek 用于章节摘要、人物统计、RAG 答案和分析报告。

1. 登录 [DeepSeek 开放平台](https://platform.deepseek.com/)。
2. 在 [API Keys](https://platform.deepseek.com/api_keys) 创建并立即保存密钥。
3. 确认账户有可用余额。
4. 在 `.env` 中填写：

```dotenv
DEEPSEEK_API_KEY=sk-你的真实密钥
GENERATION_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_TIMEOUT_SECONDS=660
DEEPSEEK_MAX_ATTEMPTS=3
DEEPSEEK_MAX_TOKENS=8192
SUMMARY_CONCURRENCY_MODE=auto
SUMMARY_MAX_CONCURRENCY=2500
```

注意：

- 不要把示例中的 `sk-你的真实密钥` 原样保留。
- `DEEPSEEK_BASE_URL` 只接受 `https://api.deepseek.com` 或其 `/v1` 路径。
- 本项目按 `deepseek-v4-pro` 的结构化 JSON 能力实现。
- 超时、重试次数和最大输出通常保持默认值即可。
- 官方调用方式可参考
  [DeepSeek API 文档](https://api-docs.deepseek.com/)。

### 3.5 获取并填写硅基流动 API

硅基流动仅用于 RAG 的文档向量和问题向量。项目固定使用：

- API 根地址：`https://api.siliconflow.cn/v1`。
- 模型：`Qwen/Qwen3-Embedding-4B`。
- 维度：`1024`。
- 协议：OpenAI 兼容 Embeddings API。

操作步骤：

1. 登录[硅基流动控制台](https://cloud.siliconflow.cn/)。
2. 在 API 密钥页面创建并保存 API Key。
3. 在 `.env` 中填写：

```dotenv
EMBEDDING_API_KEY=sk-你的硅基流动密钥
EMBEDDING_PROVIDER=siliconflow
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B
EMBEDDING_DIMENSIONS=1024
EMBEDDING_TIMEOUT_SECONDS=120
EMBEDDING_MAX_ATTEMPTS=3
```

本项目会拒绝以下地址：

- 非硅基流动官方域名。
- 缺少 `/v1` 的地址。
- 带端口、查询参数、账号密码或其他路径的地址。

不要把模型名或维度改为其他值，否则与本地索引契约不兼容。旧百炼配置首次
加载时会自动切换到硅基流动预设并清空旧密钥，避免跨供应商误用凭据；随后需
点击侧栏“设置”，在“模型与设置”弹窗填写硅基流动 API Key。

官方说明：

- [硅基流动快速开始](https://docs.siliconflow.cn/cn/userguide/quickstart)
- [Embeddings API](https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings)

### 3.6 完整 `.env` 示例

```dotenv
APP_NAME=NovelMind
DATABASE_URL=sqlite:///./data/novelmind.db

GENERATION_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-替换为DeepSeek真实密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_TIMEOUT_SECONDS=660
DEEPSEEK_MAX_ATTEMPTS=3
DEEPSEEK_MAX_TOKENS=8192
SUMMARY_CONCURRENCY_MODE=auto
SUMMARY_MAX_CONCURRENCY=2500

EMBEDDING_PROVIDER=siliconflow
EMBEDDING_API_KEY=sk-替换为硅基流动真实密钥
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B
EMBEDDING_DIMENSIONS=1024
EMBEDDING_TIMEOUT_SECONDS=120
EMBEDDING_MAX_ATTEMPTS=3

VECTOR_STORE_PATH=./vector_store
```

通常不要修改：

- `DATABASE_URL`：本地 SQLite 数据库位置。
- `VECTOR_STORE_PATH`：本地派生向量目录。
- `APP_NAME`：后端应用名称。

直接修改 `.env` 后必须停止并重新启动后端；通过“模型与设置”弹窗修改供应商、
模型、端点或密钥会立即生效。网页使用同源 API。

### macOS 安装与启动

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/novelmind_launcher.py
# 独立终端安全停止
.venv/bin/python scripts/novelmind_launcher.py --stop
# 开发热重载
.venv/bin/python scripts/novelmind_launcher.py --dev
```

macOS 双击入口使用 `.command`。开发与测试可安装 `requirements-dev.txt`。
前端滚动保存只写阅读位置与偏好；书签按条保存、编辑或删除，可避免其他标签页的位置更新吞掉书签。
任务浮层可取消任务。任务在下一安全检查后不再提交新模型请求；已经发出的网络请求可能需要等待返回，已生成的数据保留。

## 4. 启动与检查

双击项目根目录的 `启动 NovelMind.cmd`。启动器检查 8501，启动一个应用并验证 API 与网页后打开浏览器。
重复启动复用自己管理的实例；端口被其他程序占用时报告冲突，不结束未知进程。
`停止 NovelMind.cmd` 只联系带所有权 token 的管理进程。开发模式开启 Python 热重载；JS/CSS 修改后刷新网页。

手动启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8501
```

网页、`/health`、`/api/system/status`、`/api/system/model-settings` 和 `/docs` 共用 8501。
健康响应为 `{"status":"ok","project":"NovelMind"}`。上传与阅读无需密钥。
默认不需要第二个前端进程；`frontend.web_server:app` 兼容命令也指向同一应用。
若旧工具需要 8000，可手动将上面端口改为 8000；不必同时运行两套应用。

## 5. 网页操作

- **资料库**：真实 TXT 选择/拖入、搜索、继续阅读、删除确认。上传和阅读无需密钥。
- **阅读**：连续目录、标题/序号搜索、前后章、主题、字号、行距、段间距、宽度、
  专注和书签。位置按章节、段落及字符偏移保存到本机 SQLite。
- **章节梳理**：全部章节列在同一目录，展示已保存摘要与缺失状态；全书补齐复用有效缓存，
  单章重新生成时在处理计划中提示新的调用费用。
- **人物**：按姓名/别名搜索，展示简介、统计、关系和出场记录；章节证据按需打开。
- **问答**：历史和回答独立显示，引用只按纯文本展示；原文未准备好时一次操作完成
  索引和提问。引用可以进入正文，再返回对话。
- **报告**：先补齐缺失或过期摘要，再生成报告。任务失败不会清空旧报告。
- **任务**：全资料库最近任务集中显示，每 2.5 秒更新。结果完成后当前页面自动更新。
- **设置**：真实读取和保存供应商、端点、模型、密钥、维度与摘要并发；密码框不回显旧密钥。
  服务目的地不变时留空保留密钥，勾选清除后删除已有密钥。兼容服务切换时需同时核对端点和密钥。
- **隐私**：首次集中保存外部处理和内容权利选择，设置中可修改；改变服务目的地后重新确认。
  后续业务页面只展示本次处理范围、估算和服务条款，不重复勾选。

点击处理计划的“开始处理”才会发起真实模型请求。代码测试和浏览器验收使用假模型，
不代表真实供应商网络、账户余额或生成质量已经验收。


### 阅读器：查找、书签与返回位置

目录搜索仍只查章节标题或序号。工具栏“搜索全文”（快捷键 Ctrl/⌘ + Shift + F）打开本机正文搜索：输入1至200个字符，区分大小写，标点和 `%`、`_` 等按字面匹配。
每章展示前3处非重叠匹配，页面中的片段数不是全书出现总次数；“更多结果”继续后续片段。结果为安全纯文本，点击“打开章节中的此处”定位到对应章节、段落和字符。支持 emoji、缩进及空行；支持浏览器会高亮精确匹配，其余浏览器提示匹配段落。浮动“返回搜索前位置”恢复搜索前锚点。

章节正文按需读取，当前章成功后只预读下一章；缓存最多4章、合计30万UTF-16字符，单个超大章节不常驻缓存。快速翻章、重试和离开时取消无用请求，旧响应不会替换当前正文。

B键或书签按钮保留即时添加/移除行为。在“书签”标签可搜索名称、备注、摘录与章节标题，编辑名称（最多80字符）及备注（最多500字符），或删除/跳转。编辑名称不改变原锚点与摘录，旧书签无需迁移；每本书最多500个书签。

### 外观、透明度与动态效果

设置的“外观”标签及阅读设置均提供界面主题、降低动态效果、降低透明度。偏好即时保存在本浏览器的非秘密 `novelmind.appearance.v1`；阅读纸面继续使用原来的纸白/暖纸/夜间主题。
系统“减少动态”始终优先，系统“减少透明度”或“增加对比度”会强制更实的控件背景。降低动态会停用指针光效及其待执行动画帧，关闭页面过渡；正文没有持续动画。浏览器不支持背景模糊时回退到实色。

玻璃材质用于浮动导航、阅读控制栏和浮层，正文保留高可读纸面。视觉方向参考 Apple 的[Liquid Glass公开设计说明](https://developer.apple.com/videos/play/wwdc2025/219/)，保留本项目暖纸色与陶土色。

## 6. 后台任务、缓存与失败恢复

常见任务状态：

- `queued`：已创建，等待执行。
- `running`：正在调用外部 API 或保存结果。
- `succeeded`：完成。
- `failed`：失败，页面会显示可读错误。

使用建议：

- 创建任务后不要连续重复点击。
- 同一小说已有相关任务运行时，新任务可能被禁用或返回 `409`。
- 长篇小说需要多次 API 调用；任务面板每 2.5 秒自动更新，完成后当前页面自动更新。关闭页面后可重新打开查看。
- 应用异常退出后，未完成任务会在下次启动时转为失败，可重新提交。
- 摘要按章节成功即持久化；任务后续失败仍保留已完成章节的结果，重新发起可复用有效摘要。
- 人物分析和报告在完整快照成功后才替换当前结果；失败保留旧快照。
- `force` 会绕过正常复用，通常意味着更高费用。

## 7. HTTP API 使用

日常使用优先选择网页工作室。开发、自动化或排错时可打开
<http://127.0.0.1:8501/docs>，Swagger 中的请求和响应模型是最准确的接口说明。

### 7.1 接口总览

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/system/status` | 查看模型配置状态，不返回密钥 |
| GET | `/api/system/model-settings` | 查看可编辑配置与密钥是否已设置 |
| PUT | `/api/system/model-settings` | 本机更新供应商、模型、端点和密钥 |
| DELETE | `/api/system/model-settings` | 本机恢复 `.env` 配置 |
| POST | `/api/novels/upload` | 以 `application/octet-stream` 上传 TXT 原始字节；文件名使用 `filename` 查询参数 |
| GET | `/api/novels` | 分页列出小说 |
| GET | `/api/novels/{novel_id}` | 小说统计 |
| GET | `/api/novels/{novel_id}/chapters` | 分页列出章节元数据 |
| GET | `/api/novels/{novel_id}/chapters/{chapter_index}` | 读取章节正文 |
| DELETE | `/api/novels/{novel_id}` | 永久删除本地小说及派生数据 |
| GET | `/api/novels/{novel_id}/summaries/estimate` | 摘要调用估算 |
| POST | `/api/novels/{novel_id}/summaries` | 生成全书摘要 |
| POST | `/api/novels/{novel_id}/chapters/{chapter_index}/summary` | 生成单章摘要 |
| GET | `/api/novels/{novel_id}/summaries` | 分页读取摘要 |
| GET | `/api/novels/{novel_id}/chapters/{chapter_index}/summary` | 读取单章摘要 |
| GET | `/api/novels/{novel_id}/characters/estimate` | 人物分析估算 |
| POST | `/api/novels/{novel_id}/characters` | 生成人物分析 |
| GET | `/api/novels/{novel_id}/characters` | 搜索、排序、分页列出人物 |
| GET | `/api/novels/{novel_id}/characters/{character_id}` | 人物详情 |
| GET | `/api/novels/{novel_id}/characters/{character_id}/appearances` | 出场记录 |
| GET | `/api/novels/{novel_id}/characters/{character_id}/relationships` | 人物关系 |
| GET | `/api/novels/{novel_id}/rag/estimate` | RAG 索引估算 |
| GET | `/api/novels/{novel_id}/rag/index` | 当前索引状态 |
| POST | `/api/novels/{novel_id}/rag/index` | 建立或更新索引 |
| GET | `/api/novels/{novel_id}/rag/index/jobs` | 最近索引任务 |
| POST | `/api/novels/{novel_id}/rag/questions` | 提交 RAG 问题 |
| GET | `/api/novels/{novel_id}/rag/questions` | 问答历史 |
| GET | `/api/novels/{novel_id}/rag/questions/{question_id}` | 单次问答状态 |
| GET | `/api/novels/{novel_id}/report/estimate` | 报告生成估算 |
| POST | `/api/novels/{novel_id}/report` | 生成报告 |
| GET | `/api/novels/{novel_id}/report` | 读取当前报告 |
| GET | `/api/novels/{novel_id}/analysis-jobs` | 分析任务历史 |
| GET | `/api/analysis-jobs/{job_id}` | 单个摘要/人物/报告任务状态 |

分页接口通常使用 `offset` 和 `limit`；具体上限、搜索和排序参数以 Swagger 为准。

### 7.2 PowerShell 上传示例

```powershell
$url = "http://127.0.0.1:8501/api/novels/upload?filename=sample.txt"
Invoke-WebRequest `
    -Uri $url `
    -Method Post `
    -ContentType "application/octet-stream" `
    -InFile "data\samples\manual_test_novel.txt"
```

上传成功响应只包含小说 ID、编码和章节元数据，不回传章节正文；正文需通过单章
读取接口按需获取。其他 Content-Type 会返回 `415 Unsupported Media Type`。

### 7.3 生成任务请求体

摘要或人物分析：

```json
{
  "confirm_external_processing": true,
  "confirm_content_rights": true,
  "force": false
}
```

RAG 索引：

```json
{
  "confirm_external_processing": true,
  "confirm_content_rights": true,
  "force": false,
  "confirm_force_refresh": false
}
```

RAG 提问：

```json
{
  "question": "主角为什么决定离开故乡？",
  "top_k": 6,
  "confirm_embedding_processing": true,
  "confirm_generation_processing": true,
  "confirm_content_rights": true
}
```

分析报告：

```json
{
  "confirm_external_processing": true,
  "confirm_content_rights": true,
  "force": false,
  "confirm_force_refresh": false
}
```

示例：为 ID 为 1 的小说启动全书摘要：

```powershell
$body = @{
    confirm_external_processing = $true
    confirm_content_rights = $true
    force = $false
} | ConvertTo-Json

$job = Invoke-RestMethod `
    -Uri "http://127.0.0.1:8501/api/novels/1/summaries" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body

$job
Invoke-RestMethod "http://127.0.0.1:8501/api/analysis-jobs/$($job.id)"
```

所有确认字段都必须是真正的 JSON 布尔值，不能写成字符串 `"true"`。

## 8. 本地数据、备份与隐私

默认本地数据：

```text
data/novelmind.db    # 小说正文、任务和分析结果
vector_store/        # 硅基流动生成的派生向量
.env                 # API 密钥与本地配置
```

备份方法：

1. 停止 NovelMind 应用。
2. 复制 `data/novelmind.db`。
3. 如果需要保留现有 RAG 索引，同时复制整个 `vector_store`。
4. `.env` 可单独加密备份，不要放进普通云盘或 Git 仓库。

恢复时保持数据库与向量目录来自同一次备份。只恢复数据库也能保留小说、摘要、
人物和报告，但 RAG 向量缺失时需要重新建立索引并产生硅基流动调用。

安全边界：

- 应用只监听 `127.0.0.1`。
- 公共状态接口只返回“是否配置”，不会返回密钥。
- 日志不应记录 API Key。
- 本地删除不等于撤回外部平台数据；使用前阅读页面提供的服务条款和隐私政策。
- 外部平台的价格、配额、缓存和留存规则可能变化，以其官方页面为准。

## 9. 常见问题与运维

- **页面无法连接**：访问 `http://127.0.0.1:8501/health`，检查启动窗口、`logs/backend.log` 与 `logs/launcher.log`。默认网页没有单独代理或 BACKEND_URL 设置。
- **PowerShell 无法激活**：直接使用 `.\.venv\Scripts\python.exe`，无需改变系统执行策略。
- **端口占用**：启动器不会结束未知服务。确认已有实例，或设置 `$env:NOVELMIND_PORT='8502'` 后运行启动器；同一终端启动与停止使用相同配置。
- **临时验收**：可注入 `NOVELMIND_RUNTIME_DIR`、`NOVELMIND_LOG_DIR`、`NOVELMIND_APP`。生产默认值不变；验收必须另配临时 DATABASE_URL、VECTOR_STORE_PATH、MODEL_SETTINGS_PATH 和假提供商。
- **认证失败或余额/限流问题**：核对供应商、端点、模型与新密钥，检查供应商账户；减少摘要并发。失败不会清空旧快照。
- **设置看似没生效**：页面覆盖优先于 `.env`；改 `.env` 后需重启或通过配置 API 恢复。已受理任务继续使用旧配置。
- **索引过期或缺失**：更新原文索引；更换向量供应商/模型/维度会使旧索引失效。
- **报告缺少摘要**：网页“补齐摘要并更新报告”按顺序处理。人物分析是可选输入。摘要未齐时报告准确调用量尚不能确定。
- **任务中断**：关闭页面可重新连接查看；停止服务会中断，重启明确标为失败，手动重新发起可复用有效缓存。
- **章节切分不对**：检查 TXT 编码和独占一行的章节标题。支持 UTF-8、BOM UTF-8、GB18030，文件上限 20 MB。
- **安装或解释器问题**：用项目解释器执行 `python -m pip` 对应命令，先核对 Python 版本。旧界面缺 Altair/requests 时按兼容目录依赖安装，不修改默认依赖。

项目原创样例在 `data/samples/manual_test_novel.txt`。通过 `/docs` 或下面的 API 上传示例检查返回的小说 ID、编码和章节数。
重复导入会新增记录，当前没有按同名文件去重。日常数据库检查应先停止应用再备份；不在运行中的用户库上做实验迁移。

## 10. 停止、更新与测试

停止服务：分别在两个终端按 `Ctrl+C`。

更新项目：

```powershell
git pull
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果自己修改过代码，先用 `git status` 检查，避免 `git pull` 覆盖或冲突。

运行全部测试：

```powershell
python -m pytest tests -q
```

编译检查：

```powershell
python -m compileall -q backend frontend
```

如果只想使用本地资料库和阅读功能，可以让两组 API 配置都保持为空；不要为了
消除“未配置”提示而填入伪造密钥。

## 11. 验证边界

生产网页已连接真实持久 API；设计目录中的示例仍是假数据原型。
`python scripts/check_web.py` 使用临时库和假模型，默认输出到系统临时目录（可设置 QA_OUTPUT）。
任务、偏好、锚点和书签保存到本机数据库。多窗口同时修改遵循最后保存覆盖。
万章目录虚拟化、分页排版、划线批注和跨设备同步尚未实现。全文搜索当前按每章前3处、分页offset上限10000提供有界片段，不能作为全文词频统计；本地预读限下一章。
真实收费 API、账户余额和模型质量不在隔离验收范围内。


### 阅读状态与任务取消接口

- `PUT /api/novels/{id}/reading/position`：仅提交 `prefs` 与 `position`。
- `PUT /api/novels/{id}/reading/bookmarks/{bookmark_id}`：新增或替换一条书签，请求的 `id` 必须与路径一致。
- `DELETE /api/novels/{id}/reading/bookmarks/{bookmark_id}`：删除单条书签，成功返回 204。
- `POST /api/workspace/tasks/{task_id}/cancel`：取消活动任务，成功返回 204。为兼容既有数据库约束，取消记录的状态为 `failed`，原因明确写“用户已取消”。

原 `PUT /reading` 保留为旧客户端整份状态导入接口；网页已改用上述细粒度接口。旧客户端整份导入仍应自行协调并发修改。
