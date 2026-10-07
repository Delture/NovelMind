# NovelMind

本地单用户小说阅读与分析工作台：TXT 导入、阅读书签、章节摘要、人物、原文问答和全书报告。
默认界面是 `frontend/web/`，一个 FastAPI 应用提供网页与 API。生成和向量模型调用外部 API；本地负责解析、存储、编排与检索。

## 快速开始

使用 Python 3.12，在项目根目录创建虚拟环境并安装默认依赖（已有环境无需重复安装）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

双击 `启动 NovelMind.cmd`，打开 [阅读工作室](http://127.0.0.1:8501)。双击 `停止 NovelMind.cmd` 安全停止；开发入口支持后端热重载。
手动启动同一应用：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8501
```

导入与阅读无需密钥。在“设置”填写生成/向量服务；`.env` 可提供初始默认值，页面设置保存到本机 `data/model_settings.json`。
DeepSeek 与硅基流动是预设，也支持 OpenAI 兼容服务。更换供应商或服务地址需新密钥或明确清除，并重新确认外部处理目的地。
点击处理计划中的“开始处理”才发送内容。关闭页面任务继续，停止服务后未完成任务会在重启时标记失败。

## 文档导航

| 需要了解 | 权威入口 |
| --- | --- |
| 安装、配置、网页操作、API、备份与排障 | [使用指南](docs/user_guide.md) |
| 当前需求与范围 | [产品需求](docs/product_requirements.md) |
| 模块职责、接口映射、持久化与任务链 | [架构](docs/architecture.md) |
| 检索与来源验证 | [RAG 设计](docs/rag_design.md) |
| 派生数据报告与原子保存 | [报告设计](docs/report_design.md) |
| 从项目结构开始学习 | [代码阅读指南](docs/code_reading_guide.md) |
| Python / FastAPI / ORM 语法 | [Python 语法指南](docs/python_syntax_guide.md) |
| 当前原型、历史源稿与验收产物 | [设计目录](design/README.md) |
| 旧版 Streamlit 可选入口与依赖 | [兼容说明](legacy/streamlit/README.md) |
| 开发约束与历史记录 | [Codex.md](Codex.md)、[开发日志](docs/dev_log.md) |

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 含可选 legacy 的全部测试（需兼容依赖）
.\.venv\Scripts\python.exe -m pytest tests -q
# 已有 Playwright 模块和 Edge 时
.\.venv\Scripts\python.exe scripts/check_web.py
```

默认测试包含 `tests/backend`、`tests/web`、`tests/launcher`、`tests/integration`；集成组覆盖端点、任务链、历史迁移与真实启动器生命周期。`tests/legacy` 为独立的兼容入口测试，完整 `pytest tests` 会包含它；假提供商与数据工厂在 `tests/support`。
验收使用临时数据库与假提供商，浏览器输出默认在系统临时目录，可用 `QA_OUTPUT` 指定。
真实供应商网络、收费及生成质量需要用户另行验收。不要提交 `.env`、数据库、向量库、密钥或用户文本。

集成验收隔离入口：`.\.venv\Scripts\python.exe tests/integration/run_isolated.py tests -q`。
