# macOS 环境建立与 Issue #3 本地复核

本报告是修复前基线。修复后的结果见 [Issue #3 修复与验收](issue_3_fixes_2026-10-08.md)。

日期：2026-10-08（Asia/Shanghai）。基线：main / 2405a6e。
Issue：https://github.com/Delture/NovelMind/issues/3。用户确认评审由 grokbot 通过 Delture 账号提交。
本轮仅建立环境、检查与报告，未修复业务代码，未提交或推送。

## 本地环境与启动

- Apple Silicon / arm64，Python 3.12.14。
- 独立环境 `.venv`，安装 `requirements.txt` 原有固定版本，补齐 pip 26.2.1；依赖一致性检查通过。
- FastAPI 0.140.13 / Starlette 1.3.1 / Uvicorn 0.39.0 / SQLAlchemy 2.0.51。
- 前端为原生 JS，无需 npm 构建。网页验收复用 Codex 附带的 Playwright 与本机 Edge，未向项目安装 npm 依赖。
- 默认网页入口不需要 legacy Streamlit；本轮未安装或测试可选 legacy 依赖。

从项目目录手动运行：

```sh
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8501
# 开发热重载
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8501 --reload
# 停止上述前台服务：Ctrl+C
```

打开 http://127.0.0.1:8501。首次正常运行会使用项目默认数据路径；本轮所有服务验证均使用临时数据库、向量目录及模型配置，没有配置真实密钥或调用收费 API。当前没有保留常驻服务。

**macOS 兼容缺口**：根目录三个 `.cmd` 为 Windows 入口；`scripts/novelmind_launcher.py:40,432` 固定查找 `.venv/Scripts/python.exe`，即使本地 `.venv/bin/python` 存在，原启动器仍会报告环境缺失。本轮直接使用 Uvicorn 验证运行，没有改启动器或添加伪造 Windows 路径。开发热重载与 macOS 下强杀后子进程归属尚未验收。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| 默认 pytest 四组 | 564 个：558 通过，6 失败，无收集错误 |
| 六个失败 | 1 个 CTRL_BREAK_EVENT，4 个 STARTUPINFO，1 个 CREATE_NO_WINDOW；均为未跳过的 Windows 专用测试 |
| Edge 网页验收 scripts/check_web.py | 16 项通过；临时原创数据和假供应商，包含摘要→报告、索引→问答、页面关闭后任务完成 |
| 实际 backend.app.main:app | /health、/、/api/novels、/api/workspace/tasks 全部 200；临时服务已退出，日志确认应用关闭完成 |
| 针对 Issue 的独立复现测试 | 5 通过；断言当前错误行为，用于证明问题存在，不是修复验收 |
| 依赖一致性 | uv pip check、pip check 通过 |
| Ruff backend scripts tests | 当前工具与规则下 722 条，其中 F401 189、F811 4；未执行自动修复 |

默认 pytest 不含 `tests/legacy`，因此本轮的 564 个不能直接与 Issue 所报全量 703 个比较。Ruff 的范围/版本/规则也未与 grokbot 对齐，不能以条数差异认定问题增加或减少。没有重新运行 mypy、bandit 或漏洞审计。

## Issue 逐项判断

| 项目 | 判断与证据 | 建议优先级 |
| --- | --- | --- |
| H1 活动子任务导致任务链失败 | 已复现：插入运行中的章节摘要任务后，报告任务链返回 202，随后变为 failed。`services/workflows.py:85-90` 没有等待复用任务终态。RAG 同类路径有相同结构，但未分别动态复现。 | 高 |
| M1 任务链卡住 | 已用领取会话故障注入复现：异常向外抛出，记录保持 queued；领取在 try 之外（workflows.py:44），失败写回也没有锁重试（:98-103）。没有用实际持续 SQLite 写锁复现。局部唯一索引会阻止同书新任务链。 | 高 |
| M2 SQLite 设置 | 文件数据库实测 journal_mode=delete，确实没有 WAL；但 busy_timeout 实测已为 5000ms，是 sqlite3 默认值。“未开启 busy_timeout”表述不准确。WAL 也不能自动替代所有并发事务处理。 | 中 |
| M3 阅读保存 | 已复现：A 标签页新增书签，B 使用旧状态保存阅读位置后，书签列表变为空。pages.js:128-136 每次 PUT 整份状态并带 keepalive。大请求体的浏览器失败未动态复现。 | 高（数据丢失） |
| M4 本机门禁 | 已复现：Host 127.0.0.1:8501 接受 Origin http://127.0.0.1:8000 的写入。local_web.py 检查 Host，不是全局检查客户端 IP；system.py:196 含 testclient。实际服务绑定回环，尚无证据证明公网可直接访问；跨站网页完整利用未测试。 | 中 |
| M5 上传阻塞 | 已验证：直接在事件循环调用真实上传函数，10,200,017 字节原创 UTF-8 文本耗时约 0.312s，10ms 心跳最大间隔约 0.321s。解码/切章/提交均同步执行。该测量不含真实网络上传，未测试多客户端延迟。 | 中 |
| M6 长任务线程池与取消 | 同步 summary/report/RAG worker 由 BackgroundTasks 派发，使用共用线程池，结构判断成立；未做线程池耗尽压测。“无法取消”需限定为没有统一用户取消入口，人物 worker 已有源记录/状态失效的协作停止检查。仅 Future.cancel() 无法停止已开始的线程任务。 | 中，需设计明确停止语义 |
| M7 分层 | services 直接使用 HTTPException、BackgroundTasks；RAG 从 tasks 导入 get_session_factory 后又重定义，Ruff 确认 F811。workspace 中实际名称是 get_workflow_session_factory，并非三个完全同名定义。未使用导入确实存在。属于结构维护问题，不宜与数据损失同级。 | 低到中 |
| L1 密钥临时文件权限 | 已验证：用假密钥及 umask 022，os.replace 前临时文件为 0644，最终为 0600。初始创建时需限制权限；固定临时文件名是否产生并发冲突本轮未测试。 | 中 |
| L2 生命周期与重复重试 | 四处重试/长函数由代码确认。统一小范围逻辑有价值，但不应与修复混成大重构。 | 低 |
| L3 ORM 类型 | declarative_base/Column 和 Workflow.created_at 的 String 由代码确认；297 个 mypy 错误与噪声分类未复核。 | 低 |
| L4 测试与文档 | 六个 Windows 测试失败已确认；文档 Windows 路径、reader.js 的浏览器保存文案、运行时 requirements 中 pytest、gitignore 未含 output/ 均存在。macOS 启动器解释器路径问题比仅补 skip 标记更直接。 | macOS 入口中，其余低 |
| L5 lint | 重复 logger、RAG 重定义、未使用导入确实存在；本轮未运行 ESLint。Ruff 警告不等于运行故障，需逐条判断后再改。 | 低 |

## 本轮另发现的验证工具问题

`tests/integration/run_isolated.py:52` 假设测试 classname 至少有两个点分片段。传入项目外复现测试文件时，5 个测试均通过并生成 pytest.xml/pytest.log，但汇总阶段抛 IndexError，使包装进程最终非零退出。本轮直接读取 pytest 日志确认测试结果；不把包装器失败算作业务复现失败。

## 后续修复顺序建议

1. H1、M1：修任务链复用/领取/终态，分别补真实行为测试。
2. M3：避免阅读位置整份覆盖书签，验证双标签页场景。
3. macOS 启动入口与平台测试：解释器路径、启动/停止/重载及自己持有的子进程回收；不能只跳过 Windows 测试就宣布支持 macOS。
4. M4、L1：收窄来源与初始文件权限。
5. M5、SQLite 并发策略；有证据后再讨论专用线程池与服务层整理。

## 本地证据

- 默认测试：`/var/folders/_h/nd6c16mj14x463yhxvnrdhlw0000gn/T/novelmind-acceptance-2zjqln0t/`（run.json、pytest.log、pytest.xml）。
- 复现测试：`/tmp/novelmind-review/test_issue_review.py`；结果 `/var/folders/_h/nd6c16mj14x463yhxvnrdhlw0000gn/T/novelmind-acceptance-07fvu4fq/pytest.log`。
- Edge 网页验收：`/var/folders/_h/nd6c16mj14x463yhxvnrdhlw0000gn/T/novelmind-web-qa-r3hpmliq/`。
- 实际应用启动与关闭：`/tmp/novelmind-review/production-smoke.log`。
- 上传事件循环探针：`/tmp/novelmind-review/upload_probe.py`、`upload-probe.log`。
- Ruff 原始结果：`/tmp/novelmind-review/ruff.json`。

临时证据可能被系统清理；本报告保存结论与验证边界。
