# NovelMind 开发约束

开始前读本文件、README 和 `git status`。用户当次明确授权的修复范围优先。

- 在已有工作上继续；保留未提交文件，不用 reset/checkout 覆盖或清理未跟踪内容。
- 不提交、推送、安装升级依赖、调用收费 API，除非用户明确授权。
- 保护 `.env`、`.venv`、`data`、`vector_store`、`logs`、`output`；测试仅临时目录与假提供商。
- 本地只做解析、存储、统计、检索、编排和界面；语义生成与 embedding 使用外部 API。
- 代码面向有 Python 基础的本科生。解释公开边界、复杂流程与不直观原因，保留教学资料；不要用注释字数作质量指标。
- 保持小而明确的模块，不引入通用工作流框架、新前端构建链或万能基类。
- 端点和密钥绑定，读取与错误不暴露密钥；任务持有受理时配置。显式授权、失败保留旧结果、来源复核、外键与原子性不能削弱。
- Windows 移动/删除前确认完整绝对路径在项目内，用 PowerShell LiteralPath 操作。
- 修改后更新 `docs/dev_log.md`，按实际运行结果报告验证边界。

## 入口

- `backend/app/main.py`：单应用，默认网页/API 共用 8501。
- `backend/app/api/`：HTTP 声明；`services/`：共享请求处理、派发与明确的两种任务链。
- `analyzer/character_pipeline/`：人物结构、抽取、归并、持久化与 worker。
- `core/migrations.py`：版本账本；`core/migration_steps/`：按历史版本分组。
- `frontend/web/`：原生模块；`legacy/streamlit/`：可选旧界面。
- `tests/{backend,web,launcher,legacy,integration,support}/`：功能分组和共享替身。
- `design/current/`：有效设计参考；`design/history/`、`design/artifacts/`：历史与验收。

## 命令和文档

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8501
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts/check_web.py
.\.venv\Scripts\python.exe tests/integration/run_isolated.py tests -q
```

README 是快速开始和导航；`docs/user_guide.md` 管操作，`architecture.md` 管实现。
`rag_design.md`、`report_design.md` 保留业务算法约束；两份教学指南继续维护。
历史 HANDOFF/SAVEPOINT 位于 `docs/archive/`，不能当作当前待实现清单。
