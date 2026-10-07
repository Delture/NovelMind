# NovelMind 当前架构

## 单应用与职责

默认浏览器地址为 `http://127.0.0.1:8501`。`backend/app/main.py` 同时注册 API 和固定目录的静态资源。
`frontend/web_server.py` 是旧命令兼容导出，引用同一 app，不再代理到 8000。
启动器只创建一个 uvicorn 应用；开发模式由 uvicorn 管理重载子进程。
Windows 开发模式通过 `scripts/novelmind_server.py` 复用 uvicorn 的 StatReload 和共享监听 socket，
用进程间事件请求 worker 优雅退出；超时仅终止自己持有的 worker，确认退出后再启动新 worker。
此适配不依赖控制台 Ctrl+C，也不修改第三方依赖；启动器的 Job Object 继续覆盖整个子进程树。
回环 Host/Origin 门禁、端口占用检查、带 token 控制端点、Windows Job Object 和进程归属继续保留。

| 目录 | 职责 |
| --- | --- |
| `frontend/web/app.js` | hash 路由、外壳、页面生命周期 |
| `frontend/web/pages.js` | 资料库/阅读接入及各分析页面 |
| `frontend/web/tasks.js` | 处理计划、统一任务面板与轮询 |
| `frontend/web/reader.js`、`reader.css` | 独立阅读器与主题排版 |
| `frontend/web/api.js`、`ui.js`、`controls.js`、`settings.js` | HTTP、安全展示、控件和配置授权 |
| `backend/app/api/` | HTTP 参数与响应模型、依赖注入 |
| `backend/app/services/` | 路由与工作流共用校验、缓存、冲突判断和派发 |
| `backend/app/analyzer/`、`rag/` | 有界模型处理、来源校验、原子结果写入 |
| `backend/app/models/`、`core/` | 数据约束、配置、会话与迁移 |
| `legacy/streamlit/` | 隔离保留旧界面和可选依赖 |

## 请求与后台任务

HTTP 路由把已解析参数交给普通服务函数；任务链直接调用同一函数，无内部 HTTP 或主应用反向导入。
授权先于外部客户端构造。任务先提交 queued 记录，再移交后台 worker；派发失败补偿为 failed 并关闭未移交的客户端。
`services/tasks.py` 共享供应商依赖、任务响应、资源关闭和派发补偿，各业务保留自己的缓存与冲突规则。

`services/workflows.py` 仅有“摘要 → 报告”和“索引 → 提问”两条链。
受理时通过 FastAPI 依赖缓存捕获同一个不可变 Settings 及绑定工厂，授权范围同时确定。
中途保存另一组设置不会改变后续步骤目的地；敏感配置只在内存中，不写 workflow JSON 或日志。
数据库唯一索引阻止同书并行任务链；worker 原子地将 queued 改成 running，重复派发不能重复付费。
每步等待 worker 真正结束并核对持久状态，失败停止下一步、保留已有结果。
关闭页面后进程继续执行；服务重启将 queued/running 明确标失败，不自动恢复外发操作。

## 配置与外部处理边界

`.env` 是安装默认值，本机 `data/model_settings.json` 是原子保存的覆盖值。
读取接口只返回密钥是否存在。更换供应商或规范化服务地址时，生成与向量角色都要求新密钥或明确清除。
明确清除只撤销凭据，保留本次提交且校验通过的非秘密端点与模型配置；角色转为未配置。
域名大小写、默认端口、尾斜线等价；路径、协议或非默认端口不同视为目的地改变。
不接受 URL 凭据、查询参数和片段。DeepSeek/硅基流动预设限定官方地址；兼容服务要求 HTTPS（本机回环允许 HTTP）。
`workspace_settings` 的隐私选择带公开目的地范围；读取时与当前配置核对。
因此目的地变化后旧选择自动失效，无须网页再发一次撤销请求；正在执行的已批准任务继续使用旧快照。
模型设置请求验证不回显敏感输入。

## 持久化与迁移

基础小说/章节表建立后，`core/migrations.py` 在显式 SQLite 事务内执行尚未记录版本。
`run_migrations` 的账本创建、全部待应用步骤及版本记录处于同一事务。任何步骤失败，
均回滚到调用该函数前的结构、数据与版本记录；若此前没有账本，回滚后也不会留下空的 `schema_migrations`。
此边界不包含 `init_db` 在调用迁移前创建基础小说/章节表的操作。
迁移文件按版本组拆分，保留旧库升级、唯一约束、级联外键、CHECK 语义与索引保障；普通列物理顺序不参与正确性判断。

- v1–v4：章节唯一性、摘要和任务范围/活动任务唯一性。
- v5：人物逐章缓存、快照、出场和关系。
- v6–v9：RAG 索引、问答、报告，以及可配置向量模型。
- v10：workspace_settings、reading_states、workflows 进入版本账本，兼容已存在的网页预览数据。
- v11：验证并保留已有出场数据，移除占位归属默认值和填充触发器；写入时显式指定 analysis_id/novel_id。

人物算法位于 `analyzer/character_pipeline/{payloads,extraction,merge,persistence,worker}.py`。
模型负责语义，本地校验 ID 白名单、分区完整性、数量/长度上限和来源哈希，最终快照只在完整成功后替换。
向量库仅保存外部派生 float32 向量和定位元数据，检索命中回查 SQLite 原文范围与哈希。

## 网页/API 映射

所有业务路径前缀 `/api`，与网页同源。

| 界面 | API |
| --- | --- |
| 资料库/导入/删除 | `/novels`、`/novels/upload`、`/novels/{id}` |
| 目录和正文 | `/novels/{id}/chapters`、`/novels/{id}/chapters/{index}` |
| 阅读状态 | `GET/PUT /novels/{id}/reading` |
| 摘要 | `/novels/{id}/summaries`、`/chapters/{index}/summary`、摘要估算 |
| 人物 | `/novels/{id}/characters`、详情、appearances、relationships、估算 |
| 问答 | `/novels/{id}/rag/index`、`/rag/questions`、`/rag/estimate` |
| 报告 | `/novels/{id}/report`、`/report/estimate` |
| 串联任务 | `POST /novels/{id}/workflows`（report / ask） |
| 统一任务面板 | `GET /workspace/tasks` |
| 隐私 | `GET/PUT /workspace/privacy` |
| 模型设置 | `GET/PUT /system/model-settings` |


## 验收组织与运行隔离

`tests/backend`、`tests/web`、`tests/launcher`、`tests/integration` 为默认测试，legacy 单独分组（全量 `pytest tests` 包含 legacy）；`tests/support` 提供共享 fakes 和种子数据。
浏览器测试服务 `tests/web/web_browser_server.py` 只接受隔离环境，使用临时 SQLite、向量目录、配置和假模型。
`check_web.py` 仅创建一个应用进程，输出默认在系统临时目录，`QA_OUTPUT` 可覆盖。
启动器支持 `NOVELMIND_PORT`、`NOVELMIND_RUNTIME_DIR`、`NOVELMIND_LOG_DIR`、`NOVELMIND_APP` 注入，验收无需修改生产默认值。

手动使用旧 API 端口可把同一应用启动在 8000；默认启动器不会另起第二个常驻代理。
生产资源不提供 `design/`，原型不连接真实数据库。测试不接触用户浏览器存储或真实密钥。

集成验收统一位于 `tests/integration/`：`run_isolated.py` 创建隔离运行环境，`test_launcher_lifecycle.py` 使用 `launcher_probe.py` 验证真实启动、重载与进程归属。活动测试目录中不再使用临时 review 分类。


## 阅读器与设置增强

- `services/text_search.py` 与 `api/novels.py` 的 `GET /api/novels/{id}/search` 使用 SQLite instr +逐章字面扫描；按章节/非空段/位置排序，每章3个非重叠命中，分页limit1–50/offset0–10000，不把全文一次传给浏览器。查询1–200字符，拒绝空白和换行。
- 搜索结果包含chapter_id/index/title、paragraph、offset/match_length、snippet及snippet_match_start/length。所有字符偏移/长度为UTF-16 code units，段落切分与reader的非空行规则一致。前后空行和缩进不改变锚点语义。
- `frontend/web/reader-data.js` 管理一次阅读挂载内的LRU（4章/30万字符）、在途去重、取消、失败重试与下一章预读；reader自身请求代号保护实际渲染和阅读位置。
- `reader-search.js` 管理查询节流、取消、分页和搜索前锚点；CSS Highlight API高亮不修改正文Text节点，避免破坏DOM Range偏移。不支持时只标记段落。
- 书签JSON新增可选label/note（默认空，80/500字符）；不需要数据库迁移，旧锚点、ID、摘录和阅读prefs保持兼容。
- `provider-settings.js` 提供按角色设置工作区、保存/草稿状态、主动目录探测与模型选择、关闭草稿提示和.env恢复确认；`settings.js` 继续负责外部处理授权。
- `POST /api/system/model-probe` 接收role/provider/base_url/api_key/use_saved_key/confirm_connection，经与设置相同的URL验证后仅GET /models。旧凭据仅同角色/同供应商/同规范地址可复用；不写配置、不跟随跳转、禁止环境代理、6秒总时限、1MiB响应和200条目录上限。远端错误映射固定本机状态，不回显原文或key。
- HTTP transport通过 `api.system.get_model_probe_transport` 注入；测试使用 httpx.MockTransport。模型目录成功不构成生成/embedding兼容性保证。SF embedding预设仍限定Qwen/Qwen3-Embedding-4B/1024，UI只提供该可保存选项，其余走自定义兼容配置。
- `appearance.js` 与 `materials.css` 集中管理主题、控件玻璃材质、弹层进入退出和事件驱动高光。`novelmind.appearance.v1` 只保存theme/reduceMotion/reduceTransparency；系统减少动态/透明度与高对比优先，阅读主题作用于阅读纸面。高光最多一个待执行RAF，无常驻循环；禁用效果会取消。
- `ui.js` 的dialog生命周期AbortSignal统一清理探测/搜索请求和事件，退出动画有界；新浮层不会保留旧遮罩或抢回已离开页面的焦点。

开发和回归使用隔离数据。2026-09-27经总控授权，以项目managed launcher安全停止并隐藏启动正常模式，真实8501已加载增强版后端；只读确认381章、阅读状态和配置在加载前后不变，未调用用户真实模型探测。

弹窗焦点通过 `captureModalFocus` 在异步请求前捕获，`modal(..., {returnFocus})` 保存原始归属；接替时不指向即将移除的旧弹窗控件。关闭后仅对仍连接、可见且可用的原触发元素恢复焦点，已有新弹窗时不抢焦点。通用动作绑定使用aria-busy/aria-disabled及在途WeakSet抑制重复操作，保留触发按钮的可聚焦性。
