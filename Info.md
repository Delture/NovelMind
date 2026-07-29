# NovelMind 使用说明

这份文件是写给项目维护者自己的日常操作说明。它关注“怎么启动、怎么检查、开发时注意什么”，不替代 `README.md` 和 `Codex.md`。

## 项目简介

NovelMind 是一个基于 RAG 的网络小说结构分析与写作辅助系统。

当前项目按本地单用户模式开发，已经完成：

- FastAPI 后端可以启动。
- `/health` 接口可以访问。
- Streamlit 前端可以启动。
- TXT 小说章节解析。
- TXT 小说上传接口。
- SQLite 保存小说和章节。
- 小说资料库、分页章节列表、按需正文阅读和本地删除。
- DeepSeek V4 Pro API 结构化章节摘要、缓存和持久化任务。
- Streamlit 上传、资料库、正文阅读和章节摘要工作区。
- 还没有实现角色抽取、RAG 和分析报告。

本地只运行解析、存储、任务编排、检索和界面等非模型功能。摘要、角色分析、
RAG 问答、报告和 embedding 等模型能力统一调用外部 API；不得擅自加入
Ollama 或其他本地模型运行时。

## 每次开始开发前

先打开 PowerShell，进入项目目录：

```powershell
cd D:\CodexPro\NovelMind
```

激活虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

看到命令行前面出现 `(.venv)`，说明虚拟环境已经激活。

如果还没有创建虚拟环境，只在第一次执行：

```powershell
py -3.12 -m venv .venv
```

## 安装依赖

激活虚拟环境后，安装项目依赖：

```powershell
python -m pip install -r requirements.txt
```

建议使用 `python -m pip`，不要直接用 `pip`。这样更容易确保依赖安装到当前项目的 `.venv` 里。

## 启动后端

在项目根目录执行：

```powershell
uvicorn backend.app.main:app --host 127.0.0.1 --reload
```

后端默认地址：

```text
http://127.0.0.1:8000
```

验证健康检查接口：

```text
http://127.0.0.1:8000/health
```

正常结果应该是：

```json
{"status":"ok","project":"NovelMind"}
```

查看 FastAPI 自动接口文档：

```text
http://127.0.0.1:8000/docs
```

## 测试 TXT 上传接口

项目提供了一份原创测试文本：

```text
data/samples/manual_test_novel.txt
```

启动后端后，打开 `http://127.0.0.1:8000/docs`，展开
`POST /api/novels/upload`，点击 `Try it out`：

1. `filename` 填写 `manual_test_novel.txt`。
2. 请求体选择 `data/samples/manual_test_novel.txt`。
3. 点击 `Execute`。
4. 确认状态码为 `200`、`novel_id` 为正整数、`chapter_count` 为 `3`。

也可以在另一个 PowerShell 窗口运行：

```powershell
$url = "http://127.0.0.1:8000/api/novels/upload?filename=manual_test_novel.txt"
$response = Invoke-WebRequest -UseBasicParsing -Uri $url -Method Post -ContentType "application/octet-stream" -InFile "data\samples\manual_test_novel.txt"
$json = [System.Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray())
$json | ConvertFrom-Json | ConvertTo-Json -Depth 4
```

正常结果应包含：

```text
novel_id: 正整数
filename: manual_test_novel.txt
encoding: utf-8
chapter_count: 3
```

后端首次启动时会自动创建：

```text
data/novelmind.db
```

可以用 Python 查询保存结果：

```powershell
python -c "import sqlite3; db=sqlite3.connect(r'data/novelmind.db'); print(db.execute('SELECT id, filename, encoding, chapter_count FROM novels').fetchall()); print(db.execute('SELECT novel_id, chapter_index, title FROM chapters ORDER BY novel_id, chapter_index').fetchall()); db.close()"
```

## 启动前端

建议重新打开一个 PowerShell 窗口，再次进入项目目录并激活虚拟环境：

```powershell
cd D:\CodexPro\NovelMind
.\.venv\Scripts\Activate.ps1
```

启动 Streamlit：

```powershell
streamlit run frontend/streamlit_app.py --server.address 127.0.0.1
```

启动后浏览器通常会打开：

```text
http://127.0.0.1:8501
```

前端和后端必须同时运行。推荐始终使用两个 PowerShell 窗口：

1. 终端一运行 FastAPI，不要关闭。
2. 终端二运行 Streamlit，不要关闭。
3. 页面右上角显示“后端已连接”后再上传文件。

## 通过前端完整测试

使用项目内的原创样例文件：

```text
data/samples/manual_test_novel.txt
```

操作步骤：

1. 按前文说明启动后端和前端。
2. 打开 `http://127.0.0.1:8501`。
3. 确认右上角显示“后端已连接”。
4. 点击 `Browse files`，选择样例 TXT。
5. 点击“解析并保存”。
6. 确认页面出现绿色成功提示。
7. 确认“章节数”为 `3`，编码为 `UTF-8`。
8. 在“章节列表”中确认显示第一章、第二章和英文标题的第三章。
9. 切换到“正文预览”，选择不同章节并检查正文是否对应。

每点击一次“解析并保存”，数据库会新增一条小说记录。这是当前 MVP 的正常行为，
暂未实现同名文件去重。

## 运行测试

运行全部测试：

```powershell
python -m pytest -q
```

后续如果只想运行某个测试文件，可以写具体路径，例如：

```powershell
pytest backend/tests/test_chapter_splitter.py
```

每实现一个核心功能都应补齐正常路径、边界、失败恢复、并发和安全回归测试；
只有完整测试通过后才能提交。

## 当前文件用途

```text
backend/app/main.py
```

FastAPI 后端入口，负责创建应用并注册接口路由。

```text
backend/app/api/novels.py
```

TXT 小说上传接口，负责文件校验、编码识别和调用章节解析器。

```text
backend/app/core/database.py
```

SQLite 引擎、数据库会话和自动建表逻辑。

```text
backend/app/models/novel.py
```

小说与章节的 SQLAlchemy 数据模型。

```text
backend/app/parser/chapter_splitter.py
```

TXT 章节解析器，负责识别章节标题并切分正文。

```text
frontend/streamlit_app.py
```

Streamlit 前端入口，负责上传控件、结果指标、章节列表和正文预览。

```text
frontend/api_client.py
```

前端 HTTP 客户端，负责健康检查、上传请求和用户可读的错误提示。

```text
frontend/ui_styles.py
```

集中保存 Streamlit 页面样式。

```text
docs/
```

项目文档目录，包括产品需求、架构说明、RAG 设计和开发日志。

```text
Codex.md
```

给 Codex 看的长期上下文文件。让 Codex 修改项目前，应该优先参考它。

```text
Info.md
```

给自己看的项目启动和维护说明，也就是当前文件。

## 开发注意事项

1. 不要一次性做完整系统。
2. 每次只让 Codex 做一个小功能。
3. 写新功能时尽量同时写测试。
4. 测试通过后再提交 Git。
5. 不要提交 `.env`、数据库文件、向量库文件、上传文件和日志。
6. 不要上传真实商业小说文本。
7. 不要急着做 RAG，先完成章节解析、上传、存储和展示。
8. 代码优先简单清晰，不要为了“高级”而过度封装。
9. 每完成一个阶段性功能，更新 `docs/dev_log.md`。

## 推荐开发顺序

当前建议按这个顺序推进：

1. TXT 网络小说章节解析（已完成）
2. TXT 上传接口（已完成）
3. SQLite 保存小说和章节（已完成）
4. Streamlit 上传页面（已完成）
5. 章节摘要（已完成）
6. 角色抽取与统计
7. RAG 问答
8. 小说分析报告

下一步最推荐做：

```text
角色抽取与统计
```

## 常见问题

### PowerShell 不能激活虚拟环境

如果运行 `.\.venv\Scripts\Activate.ps1` 报执行策略错误，可以执行：

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

然后重新激活虚拟环境。

### Streamlit 报 altair.vegalite.v4 不存在

说明 Streamlit 和 Altair 版本不兼容。确认 `requirements.txt` 里有：

```text
altair<5
```

然后重新安装：

```powershell
python -m pip install -r requirements.txt
```

### Streamlit 报 No module named 'frontend'

请确认使用的是项目当前代码，并从项目根目录运行：

```powershell
cd D:\CodexPro\NovelMind
streamlit run frontend/streamlit_app.py --server.address 127.0.0.1
```

不要把 `frontend/streamlit_app.py` 中的同目录导入改成
`from frontend.api_client ...`。如果终端仍显示旧错误，按 `Ctrl+C` 停止
Streamlit，再重新执行启动命令。

### 页面显示“后端未连接”

先确认另一个终端中的 FastAPI 仍在运行，再访问：

```text
http://127.0.0.1:8000/health
```

如果后端改用了其他端口，请在项目根目录的 `.env` 中配置，例如：

```text
BACKEND_URL=http://127.0.0.1:8001
```

然后重启 Streamlit。

### 依赖安装失败

可以先升级 pip 工具链：

```powershell
python -m pip install --upgrade pip setuptools wheel
```

再重新安装：

```powershell
python -m pip install -r requirements.txt
```

### 端口被占用

如果后端 `8000` 端口被占用，可以换端口：

```powershell
uvicorn backend.app.main:app --host 127.0.0.1 --reload --port 8001
```

如果 Streamlit 默认端口被占用，可以换端口：

```powershell
streamlit run frontend/streamlit_app.py --server.address 127.0.0.1 --server.port 8502
```

## Git 日常流程

查看当前改动：

```powershell
git status
```

添加改动：

```powershell
git add .
```

提交改动：

```powershell
git commit -m "docs: add project info guide"
```

推送到 GitHub：

```powershell
git push
```

提交信息建议格式：

```text
类型(模块): 简短说明
```

常用类型：

- `feat`：新功能
- `fix`：修复问题
- `docs`：文档
- `test`：测试
- `chore`：配置或杂项
- `refactor`：重构

## 给 Codex 下任务的建议

不要写太宽泛的任务，例如：

```text
帮我完善项目。
```

更推荐写清楚范围：

```text
请只实现 TXT 章节解析模块。

修改范围：
- backend/app/parser/chapter_splitter.py
- backend/tests/test_chapter_splitter.py

要求：
1. 实现 split_chapters(raw_text: str) 函数。
2. 支持“第1章”“第一章”“Chapter 1”等章节标题。
3. 如果没有章节标题，则把全文作为第 1 章。
4. 添加 pytest 测试。
5. 不要修改其他模块。
```

这样 Codex 更不容易改偏，也方便你检查结果。
