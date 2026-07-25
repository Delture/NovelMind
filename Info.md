# NovelMind 使用说明

这份文件是写给项目维护者自己的日常操作说明。它关注“怎么启动、怎么检查、开发时注意什么”，不替代 `README.md` 和 `Codex.md`。

## 项目简介

NovelMind 是一个基于 RAG 的网络小说结构分析与写作辅助系统。

当前项目还处在第一阶段，已经完成：

- FastAPI 后端可以启动。
- `/health` 接口可以访问。
- Streamlit 前端可以启动。
- TXT 小说章节解析。
- TXT 小说上传接口。
- SQLite 保存小说和章节。
- 还没有实现前端上传页面和 RAG。

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
python -m venv .venv
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
uvicorn backend.app.main:app --reload
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
streamlit run frontend/streamlit_app.py
```

启动后浏览器会打开前端页面。当前页面只显示项目标题和一句说明。

## 运行测试

运行全部测试：

```powershell
pytest
```

后续如果只想运行某个测试文件，可以写具体路径，例如：

```powershell
pytest backend/tests/test_chapter_splitter.py
```

当前阶段测试还不多。以后每实现一个核心功能，都应该补对应测试。

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

Streamlit 前端入口，目前是最小展示页面。

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
4. Streamlit 上传页面
5. 章节摘要
6. 角色抽取与统计
7. RAG 问答
8. 小说分析报告

下一步最推荐做：

```text
Streamlit 上传页面和章节列表展示
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
uvicorn backend.app.main:app --reload --port 8001
```

如果 Streamlit 默认端口被占用，可以换端口：

```powershell
streamlit run frontend/streamlit_app.py --server.port 8502
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
