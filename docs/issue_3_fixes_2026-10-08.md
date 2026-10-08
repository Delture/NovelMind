# Issue #3 修复与验收

日期：2026-10-08。基线：main / 2405a6e。所有修改仍在本地，未提交、推送或关闭 GitHub Issue。
原评审：https://github.com/Delture/NovelMind/issues/3。
修复前基线：[macOS 环境与问题复核](mac_environment_review_2026-10-08.md)。

## 修复清单

| Issue | 本地修改 | 验证 |
| --- | --- | --- |
| H1 活动子任务 | 受理任务链前检查相关活动任务并返回 409；派发期间若复用了子任务，等待终态，等待上限一小时。 | 活动摘要不能创建失败链；已有任务链配置冻结与重复派发回归通过。 |
| M1 卡住的任务链 | 领取、执行与终态在同一异常边界内；条件更新只处理自己能领取的状态，不覆盖已完成/取消记录。领取与失败写回使用共享写锁重试。 | 领取连续四次锁异常后，独立会话仍能把 queued 写成 failed。 |
| M2 SQLite | 显式 WAL、foreign_keys=ON、默认 busy_timeout=5000；显式传入的连接 timeout 保持可覆盖。 | 文件数据库 PRAGMA 验证、锁冲突与历史迁移测试通过。原连接本来已有 5000ms 默认等待时间，未宣称从零补上。 |
| M3 阅读保存 | 位置/偏好独立更新；书签按条 PUT/DELETE；只有 pagehide 使用 keepalive。写事务先取得 SQLite 写锁，再读取并合并当前 JSON。 | 两个标签页添加、编辑、删除不同书签并保存旧位置，不覆盖另一条书签；阅读器专项浏览器验收通过。 |
| M4 本机访问 | 全局中间件检查真实客户端回环 IP 与回环 Host；Origin 端口仅接受本服务端口；删去 system 接口零散检查与 testclient/testserver 生产白名单。 | 跨端口写入被拒绝；伪造本机 Host 的非回环客户端也被拒绝；测试使用真实回环客户端身份。 |
| M5 上传阻塞 | 流式读完请求后，解码、验证、切章、模型构造与保存统一交给 run_in_threadpool。 | 上传解码运行在 AnyIO worker thread；全量上传与网页导入测试通过。 |
| M6 长任务/取消 | 四线程专用 ThreadPoolExecutor，最多另有十六项等待任务；登记 Future 与任务链归属，容量满时拒绝新派发并复用已有补偿。任务浮层提供取消按钮。 | 容量/专用线程/等待中取消及资源释放测试通过；取消与派发交错不会发出模型请求；服务退出时关闭专用池。 |
| M7 分层 | ServiceError 由 ServiceRoute 统一映射；服务使用 TaskDispatcher 协议；JobTasks 替代伪造 FastAPI BackgroundTasks。人物业务移至 services/characters.py；供应商和会话工厂集中到 dependencies.py。 | 报告/问答服务在禁止导入 API/main 的独立进程中仍完成；任务链快照、授权与密钥绑定回归通过。 |
| L1 文件权限 | 随机独占临时路径，通过 os.open(..., O_CREAT|O_WRONLY|O_EXCL, 0600) 创建，再原子替换。 | umask 022 下替换之前与最终文件均为 0600；假密钥测试。 |
| L2 重复生命周期 | analyzer/jobs.py 统一领取、进度与失败写回；core/sqlite_retry.py 统一退避及锁识别；摘要/报告/人物/RAG 索引与问答复用，移除重复实现。 | 既有锁重试、进度上界、动态人物步骤、原子快照、取消与失败保留旧结果测试通过。业务算法仍留在各自模块，本轮没有整体重写长算法函数。 |
| L3 类型 | DeclarativeBase + Mapped/mapped_column；跨模型关系使用 TYPE_CHECKING。Workflow 时间改 DateTime，新增 v12 迁移；同时修正 SDK 请求类型、同步/异步客户端属性和实际类型不一致。 | mypy backend：63 源文件无错误；历史时区时间、微秒精度、活动索引与升级幂等验证通过。 |
| L4 测试/文档 | 六个 Windows 专用测试显式 skip；新增 macOS `.command` 入口、POSIX 进程组与管道看护。修正阅读保存文案、个人路径、测试依赖分离、output/ 忽略。 | macOS 日常启动、开发重载、安全停止、强杀启动器后的所属进程回收通过；遗留平台测试未被误算为通过。 |
| L5 lint | 移除重复 logger/factory 与未使用导入；三个业务错误补 from 链；删掉 JS 未使用 sideTab/toast。 | Ruff 后端/脚本 F 类检查通过；涉及 JS 语法检查及真实浏览器验收通过。 |

新增 GitHub Actions：Ubuntu、macOS、Windows 三个平台，创建项目 `.venv` 后运行默认隔离 pytest、Ruff 与 mypy。工作流 YAML 已解析检查，尚未推送，因此没有 GitHub 远端运行结果。

## 最终验证

- 全量 `python tests/integration/run_isolated.py tests -q`：**717 个，711 通过、6 跳过、0 失败/收集错误**，约 27.2 秒；包括 139 个 legacy 测试。
- Edge 通用网页验收：16 项通过。
- Edge 阅读器专项：9 项通过，包括书签名称/备注、搜索、缓存、刷新恢复、取消旧请求与跨书隔离。
- Ruff 0.16.10：`python -m ruff check backend scripts` 通过，配置检查 F 类正确性问题；没有宣称所有宽泛风格规则均无警告。
- mypy 2.4.0：`python -m mypy backend` 无错误，默认检查配置，不是 strict 模式。
- `pip check` 与 `git diff --check` 通过；运行及开发依赖已安装到本地 `.venv`。
- 所有模型调用使用假供应商/MockTransport；数据库、向量和运行日志使用临时目录；项目 data/ 仅保留原有样例。

## 行为边界

- Windows 专用的 6 个测试在这台 Mac 上跳过；Linux/Windows 的实际运行需推送后由 CI 验证。
- 取消写入数据库终态后，等待中的 Future 可以立即取消；已运行的 worker 在下一安全点协作停止。已经发出的网络请求可能需要等到返回或供应商超时，无法用 Future.cancel() 强行中止线程。
- 旧 `PUT /reading` 保留为兼容整份状态导入；新网页使用位置与单条书签接口。旧客户端调用整份接口时仍需自行协调并发写入。
- SQLite 写锁重试有上限；持续不可写的数据库不能凭重试保证落盘，重启恢复逻辑继续保留。
- 所有提交、推送和 Issue 状态操作均未执行。

## 可复核证据

- 最终全量日志与 XML：`/var/folders/_h/nd6c16mj14x463yhxvnrdhlw0000gn/T/novelmind-acceptance-035ndad4/`。
- 独立 macOS/任务链验证：`/var/folders/_h/nd6c16mj14x463yhxvnrdhlw0000gn/T/novelmind-acceptance-bllikphg/`。
- Edge 通用：`/var/folders/_h/nd6c16mj14x463yhxvnrdhlw0000gn/T/novelmind-web-qa-34btqasu/`。
- Edge 阅读器专项：`/var/folders/_h/nd6c16mj14x463yhxvnrdhlw0000gn/T/novelmind-web-qa-6r55iuk6/`。
- 新增回归源：`tests/web/test_issue_fixes.py`、`tests/backend/test_execution.py`、`tests/integration/test_posix_launcher.py`，权限/历史时间回归追加在既有对应测试模块。

系统临时目录可能自动清理；此报告保存结论和验证范围。
