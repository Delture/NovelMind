# Codex.md

本文件用于让 Codex 在每次参与 NovelMind 开发时快速恢复项目上下文。修改代码前应优先阅读本文件，再结合 README 和 docs 下的文档判断当前任务范围。

## 项目定位

NovelMind 是一个基于 RAG 的网络小说结构分析与写作辅助系统。

当前阶段的重点不是完整 RAG，而是先把“小说文本解析、上传、存储、展示”这些基础能力逐步做好。

## 使用者背景

项目维护者是人工智能专业大二学生，熟悉 Python/C++/Java 基础语法，但工程经验较少。

代码要求：

- 结构清晰，便于初学者理解和维护。
- 不要过度抽象，不要提前设计复杂架构。
- 注释适量，只解释不直观的逻辑。
- 每次只实现一个小功能，并配套基础测试。

## 技术栈

- 后端：Python + FastAPI
- 前端展示：Streamlit
- 数据库：SQLite
- ORM：SQLAlchemy
- 向量库：Chroma，当前只保留接口和占位设计，不急着完整接入
- 测试：pytest

## 当前项目结构

```text
NovelMind/
  backend/
    app/
      api/          # 后续存放接口路由
      core/         # 后续存放配置、数据库连接等基础能力
      models/       # 后续存放 SQLAlchemy 数据模型
      parser/       # 小说章节解析逻辑
      rag/          # RAG 和向量库相关代码，当前先占位
      analyzer/     # 摘要、角色抽取、结构分析等逻辑
      main.py       # FastAPI 应用入口
    tests/          # pytest 测试
  frontend/
    streamlit_app.py
  docs/
    product_requirements.md
    architecture.md
    rag_design.md
    dev_log.md
  data/
    samples/
  .env.example
  .gitignore
  README.md
  requirements.txt
```

## 已有最小功能

- FastAPI 应用入口：`backend/app/main.py`
- 健康检查接口：`GET /health`
- Streamlit 最小页面：`frontend/streamlit_app.py`
- TXT 章节解析：`backend/app/parser/chapter_splitter.py`
- TXT 小说上传接口：`POST /api/novels/upload`
- SQLite 保存小说和章节：`backend/app/core/database.py`、`backend/app/models/novel.py`
- 基础文档：`README.md` 和 `docs/`

`GET /health` 应返回：

```json
{"status":"ok","project":"NovelMind"}
```

## 常用启动命令

进入项目目录：

```powershell
cd D:\CodexPro\NovelMind
```

激活虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

安装依赖时优先使用：

```powershell
python -m pip install -r requirements.txt
```

启动后端：

```powershell
uvicorn backend.app.main:app --reload
```

启动前端：

```powershell
streamlit run frontend/streamlit_app.py
```

运行测试：

```powershell
pytest
```

## 开发守则

1. 不要一次性实现完整系统。
2. 每次只处理一个清晰的小功能。
3. 新功能应尽量添加 pytest 测试。
4. 测试通过后再建议提交。
5. 不要上传真实商业小说文本。
6. 不要提交 `.env`、数据库文件、向量库文件、上传文件和日志。
7. 不要引入复杂依赖；确有必要时先说明理由。
8. 先做解析、上传、存储和展示，再做 RAG。
9. Streamlit 当前用于快速展示，不急着切换到 Vue/React。
10. 每次完成实质功能后，更新 `docs/dev_log.md`。

## Codex 修改代码时的约束

- 优先遵循现有目录结构。
- 不要修改与当前任务无关的文件。
- 不要重写已有简单实现，除非当前任务确实需要。
- 后端接口放在 `backend/app/api/`，基础配置放在 `backend/app/core/`。
- 数据模型放在 `backend/app/models/`。
- 章节解析逻辑放在 `backend/app/parser/`。
- RAG 相关代码放在 `backend/app/rag/`，当前阶段保持轻量占位。
- 分析类功能放在 `backend/app/analyzer/`。
- 测试文件放在 `backend/tests/`，文件名使用 `test_*.py`。

## 推荐 Commit 规范

格式：

```text
类型(模块): 简短说明
```

常用类型：

- `chore`：初始化、配置、杂项
- `feat`：新功能
- `fix`：修 bug
- `docs`：文档
- `test`：测试
- `refactor`：重构

示例：

```text
feat(parser): add txt chapter splitter
docs: update development log
test(parser): add chapter splitter tests
```

## 下一阶段路线

建议按以下顺序推进：

1. TXT 章节解析（已完成）
2. 小说上传接口（已完成）
3. SQLite 保存小说和章节（已完成）
4. Streamlit 上传页面
5. 章节摘要
6. 角色抽取与统计
7. RAG 问答
8. 分析报告

当前最推荐的下一步是实现 Streamlit 上传页面和章节列表展示。

## TXT 小说上传接口

接口：

```text
POST /api/novels/upload?filename=小说文件名.txt
Content-Type: application/octet-stream
```

请求体是 TXT 文件的原始字节。接口当前：

- 只接受 `.txt` 文件名。
- 支持 UTF-8、带 BOM 的 UTF-8 和 GB18030 编码。
- 文件最大为 20 MB。
- 调用章节解析器并返回 `novel_id`、章节标题、正文和字符数。
- 不保存原始上传文件。
- 使用同一个数据库事务保存小说和全部章节。

## SQLite 数据库

- 默认地址：`sqlite:///./data/novelmind.db`
- 环境变量：`DATABASE_URL`
- 后端启动时自动创建缺少的表。
- `novels` 表保存小说基本信息。
- `chapters` 表保存章节顺序、标题、正文和字符数。
- 测试必须使用独立数据库，不得写入开发数据库。

## TXT 章节解析模块目标

建议文件：

```text
backend/app/parser/chapter_splitter.py
backend/tests/test_chapter_splitter.py
```

核心函数：

```python
def split_chapters(raw_text: str) -> list[Chapter]:
    ...
```

章节标题应支持：

- `第1章 xxx`
- `第001章 xxx`
- `第一章 xxx`
- `第十七章 xxx`
- `Chapter 1 xxx`
- `CHAPTER 001 xxx`

基本要求：

- 尽量保留原文段落。
- 去除首尾多余空白。
- 如果没有章节标题，则把全文作为第 1 章。
- 不要把正文中的“第x章”误判为章节标题，章节标题一般应单独成行。
- 至少测试阿拉伯数字章节、中文数字章节、英文章节标题、无章节标题、空文本、正文误判情况。

## RAG 设计原则

RAG 不要过早实现。后续设计方向：

```text
用户问题
  ↓
检索相关章节片段
  ↓
把片段作为证据交给大模型
  ↓
生成基于证据的回答
  ↓
返回答案 + 引用来源
```

未来需要考虑：

- 章节文本 chunk 切分大小。
- 中文网络小说章节标题识别。
- 人物、地点、事件抽取。
- Chroma 向量数据和 SQLite 章节数据之间的关联。
- RAG 回答中的引用来源展示。

## 已知依赖注意事项

如果 Streamlit 启动时报错找不到 `altair.vegalite.v4`，应确保 `requirements.txt` 中保留：

```text
altair<5
```

这是为了兼容部分 Streamlit 版本对 Altair v4 API 的依赖。
