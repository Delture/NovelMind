# NovelMind 完整使用说明

本说明面向第一次使用 NovelMind 的本地单用户，涵盖安装、API 配置、启动、
全部界面功能、HTTP API、数据安全、备份和常见故障。

## 1. 先了解 NovelMind

NovelMind 在本机完成 TXT 解析、SQLite 存储、任务编排、向量检索和页面展示；
涉及语义理解或生成的功能调用外部 API，本地不运行大模型。

| 功能 | DeepSeek | 阿里云百炼 | 发送到外部的内容 |
|---|---:|---:|---|
| 导入、资料库、正文阅读 | 不需要 | 不需要 | 不发送 |
| 章节摘要 | 需要 | 不需要 | 章节标题和原文 |
| 人物统计 | 需要 | 不需要 | 章节标题、原文和派生人物数据 |
| 建立 RAG 索引 | 不需要 | 需要 | 章节标题和原文片段 |
| RAG 提问 | 需要 | 需要 | 问题发给百炼；问题和检索片段发给 DeepSeek |
| 小说分析报告 | 需要 | 不需要 | 章节标题、派生摘要和可选人物分析；不再次发送原文 |

外部 API 可能收费。每个相关工作区都会先显示处理范围和预计调用步骤，并要求
确认外部处理与内容权利。不要导入或发送自己无权处理的文本。

推荐使用顺序：

1. 导入 TXT 小说。
2. 在正文阅读中检查章节切分。
3. 生成全书章节摘要。
4. 按需生成人物统计。
5. 建立 RAG 索引后进行原文问答。
6. 在全部章节摘要完成后生成分析报告；人物统计是报告的可选增强输入。

## 2. 首次安装

### 2.1 环境要求

- Windows 10/11。
- Python 3.12。
- Git（从 GitHub 获取或更新项目时需要）。
- 可访问 DeepSeek 和阿里云百炼 API 的网络。

项目目录示例：

```text
D:\CodexPro\NovelMind
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

`.env` 已被 Git 忽略。真实密钥只能保存在本机 `.env`，不要提交到 GitHub，
也不要放进截图、日志、聊天或前端代码。

### 3.2 获取并填写 DeepSeek API

DeepSeek 用于章节摘要、人物统计、RAG 答案和分析报告。

1. 登录 [DeepSeek 开放平台](https://platform.deepseek.com/)。
2. 在 [API Keys](https://platform.deepseek.com/api_keys) 创建并立即保存密钥。
3. 确认账户有可用余额。
4. 在 `.env` 中填写：

```dotenv
DEEPSEEK_API_KEY=sk-你的真实密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_TIMEOUT_SECONDS=660
DEEPSEEK_MAX_ATTEMPTS=3
DEEPSEEK_MAX_TOKENS=8192
```

注意：

- 不要把示例中的 `sk-你的真实密钥` 原样保留。
- `DEEPSEEK_BASE_URL` 只接受 `https://api.deepseek.com` 或其 `/v1` 路径。
- 本项目按 `deepseek-v4-pro` 的结构化 JSON 能力实现。
- 超时、重试次数和最大输出通常保持默认值即可。
- 官方调用方式可参考
  [DeepSeek API 文档](https://api-docs.deepseek.com/)。

### 3.3 获取并填写阿里云百炼 API

百炼仅用于 RAG 的文档向量和问题向量。项目固定使用：

- 地域：中国（北京）。
- 模型：`text-embedding-v4`。
- 维度：`1024`。
- 协议：工作空间专属 OpenAI 兼容端点。

操作步骤：

1. 登录阿里云百炼，并切换到中国（北京）地域。
2. 在目标工作空间创建 API Key。
3. 保存创建成功页面显示的 API Key 和 API Host。
4. API Key 与 API Host 必须属于同一个北京区工作空间。
5. 在 `.env` 中填写：

```dotenv
EMBEDDING_API_KEY=sk-你的百炼密钥
EMBEDDING_BASE_URL=https://你的WorkspaceId.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024
EMBEDDING_TIMEOUT_SECONDS=120
EMBEDDING_MAX_ATTEMPTS=3
```

`EMBEDDING_BASE_URL` 中必须替换真实的 WorkspaceId，不要保留大括号或中文。
例如：

```dotenv
EMBEDDING_BASE_URL=https://ws123456.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

本项目会拒绝以下地址：

- 非北京区地址。
- 通用 DashScope 地址。
- 缺少 `/compatible-mode/v1` 的地址。
- 带端口、查询参数、账号密码或其他路径的地址。

本说明采用标准百炼工作空间 Key 与专属 API Host，不要混用 Token Plan Key、
试用地址或其他地域凭据，即使它们的地址外观相似。

`EMBEDDING_API_KEY` 与 `EMBEDDING_BASE_URL` 必须同时填写或同时留空。
不要把新索引的维度改为其他值，否则与本地索引契约不兼容。

官方说明：

- [如何获取百炼 API Key](https://help.aliyun.com/en/model-studio/get-api-key)
- [text-embedding-v4 与 OpenAI 兼容调用](https://help.aliyun.com/en/model-studio/embedding)

### 3.4 完整 `.env` 示例

```dotenv
APP_NAME=NovelMind
DATABASE_URL=sqlite:///./data/novelmind.db
BACKEND_URL=http://127.0.0.1:8000

DEEPSEEK_API_KEY=sk-替换为DeepSeek真实密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_TIMEOUT_SECONDS=660
DEEPSEEK_MAX_ATTEMPTS=3
DEEPSEEK_MAX_TOKENS=8192

EMBEDDING_API_KEY=sk-替换为百炼真实密钥
EMBEDDING_BASE_URL=https://替换为真实WorkspaceId.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024
EMBEDDING_TIMEOUT_SECONDS=120
EMBEDDING_MAX_ATTEMPTS=3

VECTOR_STORE_PATH=./vector_store
```

通常不要修改：

- `DATABASE_URL`：本地 SQLite 数据库位置。
- `VECTOR_STORE_PATH`：本地派生向量目录。
- `APP_NAME`：后端应用名称。
- `BACKEND_URL`：Streamlit 连接的后端地址。

修改 `.env` 后必须停止并重新启动后端；如果修改了 `BACKEND_URL`，也要重启
Streamlit。

## 4. 启动与配置检查

建议使用两个 PowerShell 窗口。

### 4.1 启动后端

终端一：

```powershell
cd D:\CodexPro\NovelMind
.\.venv\Scripts\Activate.ps1
uvicorn backend.app.main:app --host 127.0.0.1 --reload
```

检查：

- 健康检查：<http://127.0.0.1:8000/health>
- 配置状态：<http://127.0.0.1:8000/api/system/status>
- Swagger API 文档：<http://127.0.0.1:8000/docs>

PowerShell 检查命令：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/api/system/status |
    ConvertTo-Json -Depth 5
```

配置正确时，状态中应出现：

```json
{
  "generation": {
    "provider": "deepseek",
    "model": "deepseek-v4-pro",
    "configured": true
  },
  "embedding": {
    "provider": "aliyun_model_studio",
    "model": "text-embedding-v4",
    "dimensions": 1024,
    "region": "cn-beijing",
    "configured": true
  }
}
```

这里的 `configured` 只表示配置已加载，不会发送测试请求，也不能证明密钥余额
或权限有效；首次真实任务会完成外部验证。

### 4.2 启动前端

终端二：

```powershell
cd D:\CodexPro\NovelMind
.\.venv\Scripts\Activate.ps1
streamlit run frontend/streamlit_app.py --server.address 127.0.0.1
```

浏览器通常自动打开 <http://127.0.0.1:8501>。页面显示后端已连接后即可使用。

不要将 FastAPI 或 Streamlit 绑定到 `0.0.0.0`。当前产品只按本地单用户设计，
没有账号系统、多人权限隔离或公网部署防护。

## 5. 全部界面功能

### 5.1 导入小说

1. 在左侧选择“导入小说”。
2. 选择 `.txt` 文件。
3. 点击“解析并保存”。
4. 成功后会自动打开新小说。

输入限制：

- 只接受 TXT。
- 支持 UTF-8、带 BOM 的 UTF-8 和 GB18030。
- 最大 20 MB。
- 文件名最长 255 个字符。
- 单本最多 10,000 章。
- 拒绝含异常二进制控制字符的伪 TXT。
- 没有可识别章节标题时，全文保存为第 1 章。

支持的常见章节标题包括：

```text
第1章 初见
第一章 初见
第十七章 风暴
Chapter 1 Arrival
CHAPTER 001 Arrival
```

原始上传文件不会另存一份，但解析后的完整章节正文会保存在本地 SQLite。
重复导入同一个文件会创建新的小说记录。

### 5.2 资料库

“资料库”显示已导入小说的文件名、章节数、正文字符数、编码和导入时间。

- “打开小说”：进入该小说工作区。
- “删除”：二次确认后永久删除本机的小说、章节、摘要、人物、RAG 元数据、
  报告和对应本地向量。
- 删除本地数据不能撤回外部平台已经处理、缓存或留存的数据。

### 5.3 正文阅读

1. 打开小说。
2. 选择“正文阅读”。
3. 选择章节；章节很多时使用“上一批章节/下一批章节”。

正文只在打开当前章节时读取，不会在资料库列表中一次加载整本小说。

### 5.4 章节摘要

前提：已填写 DeepSeek API。

1. 选择“章节摘要”。
2. 查看全书字符数、预计生成步骤和最多请求尝试。
3. 选择要查看的章节，可读取已有摘要。
4. 勾选外部处理同意与内容权利确认。
5. 选择“生成全书摘要”或“仅生成当前章节”。
6. 任务创建后使用“刷新任务状态”查看进度。

每章结果包含：

- 简明摘要。
- 关键事件。
- 剧情推进。
- 章节悬念；没有则为空。

正常生成会复用来源、模型和提示版本完全一致的摘要。勾选 `force` 会重新发送
正文并产生新的 API 调用；只有确实需要重算时使用。

### 5.5 人物统计

前提：已填写 DeepSeek API。

1. 选择“人物统计”。
2. 查看待分析章节、可复用章节、人物合并与关系推理的预计步骤。
3. 阅读数据处理说明。
4. 勾选外部处理同意与内容权利确认。
5. 点击“生成人物统计”。
6. 完成后可搜索、排序和分页浏览人物。

可以查看：

- 标准人物名和别名。
- 人物简述。
- 首次/末次出现章节。
- 出场章节数和提及数。
- 分章出场记录。
- 人物关系、方向、说明和证据章节。

普通更新会复用未变化章节的抽取缓存；强制刷新会重新分析整本小说并重复产生
费用。任何中途失败都会保留上一份完整人物快照。

### 5.6 RAG 原文问答

RAG 分为“建立索引”和“提问”两步。

#### 建立或更新索引

前提：已填写百炼 API Key 和北京区工作空间地址。

1. 选择“RAG 问答”。
2. 查看分块数、待生成向量数、复用数量和预计请求批次。
3. 勾选同意将章节标题与原文片段发给百炼。
4. 确认拥有内容处理权利。
5. 点击“建立或更新索引”。
6. 等待页面显示“RAG 索引已就绪”。

普通更新会复用未变化片段的向量。强制重建不复用旧向量，会重新发送全部原文
片段，必须额外确认。

本地向量库只保存百炼返回的 `float32` 向量和定位元数据，不复制小说正文；
命中后会回到 SQLite 校验原文范围与哈希。

#### 基于原文提问

前提：

- RAG 索引为当前版本。
- DeepSeek 和百炼都已配置。

操作：

1. 输入不超过 2,000 字符的问题。
2. 选择检索片段数量，范围为 3–10，默认 6。
3. 确认将问题发给百炼生成查询向量。
4. 确认将问题和检索片段发给 DeepSeek。
5. 确认拥有内容处理权利。
6. 点击“提交 RAG 问题”并刷新状态。

答案会显示原文章节、片段和引用。证据不足或互相冲突时，系统会明确提示，
不会把推测伪装成原文事实。

### 5.7 小说分析报告

前提：

- 已填写 DeepSeek API。
- 每一章都有与当前正文、模型和提示版本匹配的摘要。
- 人物统计可选；没有人物统计时，人物部分只依据摘要并标明限制。

1. 选择“分析报告”。
2. 查看摘要覆盖、人物档案数量和预计 API 步骤。
3. 勾选外部处理同意与内容权利确认。
4. 点击“生成或更新分析报告”。
5. 完成后刷新并阅读已保存报告。

报告包含：

- 执行摘要。
- 情节结构。
- 人物动态。
- 章节/阶段节奏。
- 有证据支持的主题、优势、风险和修改建议。
- 每项对应的证据章节。

报告使用派生摘要和可选人物数据，不再次发送小说原文，因此不能可靠评价原文
句式、修辞或真实段落节奏。证据不足的可选栏目可以为空。

当章节摘要、人物分析、模型或提示版本变化时，旧报告会标记为过期。强制刷新
会重复生成整份报告并产生费用。

## 6. 后台任务、缓存与失败恢复

常见任务状态：

- `queued`：已创建，等待执行。
- `running`：正在调用外部 API 或保存结果。
- `succeeded`：完成。
- `failed`：失败，页面会显示可读错误。

使用建议：

- 创建任务后不要连续重复点击。
- 同一小说已有相关任务运行时，新任务可能被禁用或返回 `409`。
- 长篇小说需要多次 API 调用，耐心等待并使用“刷新状态”。
- 应用异常退出后，未完成任务会在下次启动时转为失败，可重新提交。
- 摘要、人物和报告只在完整成功后替换当前结果；失败会尽量保留旧结果。
- `force` 会绕过正常复用，通常意味着更高费用。

## 7. HTTP API 使用

日常使用优先选择 Streamlit。开发、自动化或排错时可打开
<http://127.0.0.1:8000/docs>，Swagger 中的请求和响应模型是最准确的接口说明。

### 7.1 接口总览

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/system/status` | 查看模型配置状态，不返回密钥 |
| POST | `/api/novels/upload` | 上传 TXT 原始字节；文件名使用 `filename` 查询参数 |
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
$url = "http://127.0.0.1:8000/api/novels/upload?filename=sample.txt"
Invoke-WebRequest `
    -Uri $url `
    -Method Post `
    -ContentType "application/octet-stream" `
    -InFile "data\samples\manual_test_novel.txt"
```

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
    -Uri "http://127.0.0.1:8000/api/novels/1/summaries" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body

$job
Invoke-RestMethod "http://127.0.0.1:8000/api/analysis-jobs/$($job.id)"
```

所有确认字段都必须是真正的 JSON 布尔值，不能写成字符串 `"true"`。

## 8. 本地数据、备份与隐私

默认本地数据：

```text
data/novelmind.db    # 小说正文、任务和分析结果
vector_store/        # 百炼生成的派生向量
.env                 # API 密钥与本地配置
```

备份方法：

1. 停止 FastAPI 和 Streamlit。
2. 复制 `data/novelmind.db`。
3. 如果需要保留现有 RAG 索引，同时复制整个 `vector_store`。
4. `.env` 可单独加密备份，不要放进普通云盘或 Git 仓库。

恢复时保持数据库与向量目录来自同一次备份。只恢复数据库也能保留小说、摘要、
人物和报告，但 RAG 向量缺失时需要重新建立索引并产生百炼调用。

安全边界：

- 应用只监听 `127.0.0.1`。
- 公共状态接口只返回“是否配置”，不会返回密钥。
- 日志不应记录 API Key。
- 本地删除不等于撤回外部平台数据；使用前阅读页面提供的服务条款和隐私政策。
- 外部平台的价格、配额、缓存和留存规则可能变化，以其官方页面为准。

## 9. 常见问题

### 页面显示“后端未连接”

确认后端终端仍在运行，并访问：

```text
http://127.0.0.1:8000/health
```

如果后端改为 8001 端口：

```dotenv
BACKEND_URL=http://127.0.0.1:8001
```

然后重启 Streamlit。

### 修改 `.env` 后仍显示未配置

停止后端并重新启动。`.env` 只在进程启动时加载。再检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/system/status
```

### DeepSeek 认证失败、余额不足或限流

- `401/403`：检查 API Key 是否复制完整、是否已失效。
- `402`：检查 DeepSeek 余额。
- `429`：等待后重试，避免同时重复创建任务。
- `5xx/超时`：稍后重试；系统已做有限次数退避，不会无限调用。

### 百炼显示未配置

检查：

- Key 与端点是否都填写。
- 是否为同一北京区工作空间。
- 地址是否以 `/compatible-mode/v1` 结束。
- 是否误填了通用、试用、新加坡或 Token Plan 地址。
- 修改后是否重启后端。

### RAG 无法提问

依次确认：

1. DeepSeek 与百炼状态均为已配置。
2. 已成功建立索引。
3. 页面没有提示索引过期。
4. 三个授权框均已勾选。
5. 没有其他 RAG 问答任务正在运行。

### 报告按钮不可用

报告要求每一章都有当前版本摘要。先进入“章节摘要”，生成或更新全书摘要。
人物统计不是必需条件。

### 任务失败或应用中途关闭

重新启动后端，查看任务错误。中断任务会被标记为失败，可重新提交；旧的完整
分析结果通常仍会保留。

### TXT 章节切分不正确

- 确认章节标题单独成行。
- 使用“第1章”“第一章”或“Chapter 1”等格式。
- 超过 255 字符的行不会作为章节标题。
- 无章节标题时全文会成为第 1 章。

### 端口被占用

后端改用 8001：

```powershell
uvicorn backend.app.main:app --host 127.0.0.1 --reload --port 8001
```

同时把 `.env` 的 `BACKEND_URL` 改成 8001 并重启前端。

前端改用 8502：

```powershell
streamlit run frontend/streamlit_app.py `
    --server.address 127.0.0.1 `
    --server.port 8502
```

### 依赖安装失败

```powershell
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

确保命令行前有 `(.venv)`，并用 `python --version` 确认为 Python 3.12。

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
python -m pytest -q
```

编译检查：

```powershell
python -m compileall -q backend frontend
```

如果只想使用本地资料库和阅读功能，可以让两组 API 配置都保持为空；不要为了
消除“未配置”提示而填入伪造密钥。
