# 黔衣有话

贵州文旅博主 AI 内容资产与运营辅助产品。

## 当前阶段目标

第一阶段已完成“画像采集 → 可信种子建库 → DeepSeek 生成 → 本地中文向量化 → 混合检索 → 决策留痕 → 长短期 Agent 记忆”的基础闭环。第二阶段实现三库体检、Agent 动态指标、证据约束评分、功能就绪判断及不可覆盖的历史比较。第三阶段实现脚本、分镜、收益约束路线、内容排期、提醒、模拟发布和手工/模拟原始指标回收。第四阶段实现反馈候选、人工确认/拒绝、三库进化、白名单经营指标和确定性经营报告。

第四阶段不实现登录/多租户、真实平台 OAuth、真实发布、真实平台取数或生产部署。反馈分析不会自动写回，只有用户明确确认的候选才会原子应用；演示页中的发布与模拟指标始终带有明确的模拟来源标识。

## 技术基线

- 后端：Python、FastAPI、SQLAlchemy、SQLite
- 演示页面：Jinja2 + 原生 JavaScript
- 大模型：DeepSeek `deepseek-v4-flash`
- 本地向量模型：`BAAI/bge-small-zh-v1.5`，GPU 优先、CPU 降级
- 开发环境：Conda `DL`

## 本地启动

1. 安装项目依赖：

   `E:\Anaconda\envs\DL\python.exe -m pip install -e ".[dev]"`

2. 首次使用时把中文向量模型下载到本地缓存：

   `E:\Anaconda\envs\DL\python.exe -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5', cache_folder='models')"`

3. 将 DeepSeek Key 放在工作区根目录 `deepseek_apikey.txt`，该文件已被 Git 忽略且不会写入数据库或日志。

4. 初始化数据库：

   `E:\Anaconda\envs\DL\python.exe -m alembic upgrade head`

5. 使用 DL 环境启动：

   `E:\Anaconda\envs\DL\python.exe -m uvicorn app.main:app --reload`

6. 浏览器访问 `http://127.0.0.1:8000`。页面只用于功能演示，不包含视觉设计。

## 第一阶段数据规则

- 博主采用可审计软删除。关联资产、地点、任务、决策和记忆历史继续保留，但所有默认业务入口都拒绝访问已删除博主；第一阶段不开放恢复 API。
- 地点的 `est_cost`、`est_benefit`、`like_level`、`fits_koc`、`fits_shoot` 只有在用户明确提供或可信来源明确记载时才赋值，未知值在数据库和 API 中均保持 `null`。
- 资产检索支持 `q`、`lib_type`、`category`、独立 `tags`、`source_type`、`source`、`min_credibility`、`max_credibility`、`page`、`page_size` 组合使用。
- 当前 Alembic head 为 `0006_phase4_feedback`；一期测试固定验证 `0002`，二期固定 `0003`，三期固定 `0005`，四期验证空库 `base → 0006`、已有三期库 `0005 → 0006`、数据保留、runtime 预建兼容及 downgrade/upgrade 往返。

## 第二阶段体检 API

- `POST /api/v1/bloggers/{blogger_id}/assessments`：以 `idempotency_key` 创建并执行体检。
- `GET /api/v1/bloggers/{blogger_id}/assessments`：分页读取历史。
- `GET /api/v1/bloggers/{blogger_id}/assessments/{assessment_id}`：读取结构、指标、就绪度和证据。
- `POST /api/v1/bloggers/{blogger_id}/assessments/{assessment_id}/retry`：安全重试失败体检。
- `GET /api/v1/bloggers/{blogger_id}/assessments/compare?left_id=...&right_id=...`：比较两次不可变快照。
- `GET /api/v1/bloggers/{blogger_id}/assessments/{assessment_id}/evidence`：读取可追溯证据。

体检状态为 `pending/running/succeeded/failed`。稳定错误码包括：`BLOGGER_NOT_FOUND`、`BLOGGER_DELETED`、`LIBRARY_EMPTY`、`THREE_LIBRARIES_INCOMPLETE`、`ASSESSMENT_NOT_FOUND`、`ASSESSMENT_ALREADY_RUNNING`、`AGENT_TIMEOUT`、`AGENT_INVALID_JSON`、`INDICATOR_RULE_VIOLATION`、`EVIDENCE_REFERENCE_INVALID`、`LIBRARY_SNAPSHOT_CHANGED`、`ASSESSMENT_PERSIST_FAILED`。跨博主资源统一返回 404，响应不包含密钥、完整提示词或内部堆栈。

体检快照只保存向量维度和摘要哈希，原始 512 维向量仅在本地 NumPy 批量分析期间使用，不进入数据库 JSON、演示 API 或 DeepSeek 上下文。服务启动时会把中断中的体检恢复为可安全重试状态。

## 第三阶段内容与执行 API

- `POST /api/v1/bloggers/{blogger_id}/outputs/generate/script|storyboard|route`：生成脚本、分镜或收益约束路线。
- `GET /api/v1/bloggers/{blogger_id}/outputs` 与 `GET /outputs/{output_id}`：查询不可覆盖的输出历史和详情。
- `POST /outputs/{output_id}/revisions`、`DELETE /outputs/{output_id}`：另存新版本与软删除。
- `GET /outputs/{output_id}/evidence`：回查 `OutputAsset`、`OutputPlace` 和 `AssetPlace` 证据关系。
- `POST/GET/PUT /api/v1/bloggers/{blogger_id}/schedules`：创建、查询和编辑排期；取消使用 `/schedules/{schedule_id}/cancel`。
- `POST /schedules/reminders/scan`：手工触发到期扫描；同排期同日最多一条提醒。
- `POST /schedules/{schedule_id}/publish`：本地模拟发布，写 `PublishEvent`，不调用真实平台。
- `POST /schedules/{schedule_id}/collections` 与 `POST /collections/{job_id}/retry`：手工/模拟原始指标回收及安全重试。
- `GET /api/v1/bloggers/{blogger_id}/metrics`：查询原始指标，不在本阶段做反馈判断。

回收接口只有一个幂等键：创建请求外层 `idempotency_key` 同时作为当前排期的 `CollectionJob` 与 `Metric` 幂等键；作用域为“`schedule_id + idempotency_key`”。不同排期可复用同一键，同排期同键只产生一个任务和一条指标。嵌套 `metrics` 接受 `source_type`（`manual/simulated`）、播放/点赞/评论/收藏/分享五项非负计数、可选 `collected_at`，以及仅限 `manual + user_confirmed=true` 的可空 `actual_revenue/actual_cost`。未知商业值保持 `NULL`，simulated 写实际值稳定返回 422。

路线的 `est_cost`、`est_benefit`、`like_level`、`fits_koc`、`fits_shoot` 任一为 `NULL`，或商业数据没有用户明确提供/可信来源确认时，返回 `ROUTE_COMMERCIAL_DATA_INCOMPLETE` 及具体地点/字段。排序公式由后端复算并写入 `DecisionLog`，Agent 只能生成说明。

第三阶段稳定错误码还包括：`ASSESSMENT_NOT_READY`、`OUTPUT_NOT_FOUND`、`OUTPUT_ALREADY_RUNNING`、`OUTPUT_INVALID_JSON`、`OUTPUT_EVIDENCE_INVALID`、`OUTPUT_SNAPSHOT_CHANGED`、`STORYBOARD_SCRIPT_REQUIRED`、`SCHEDULE_INVALID_STATE`、`PUBLISH_DUPLICATE`、`COLLECTION_INVALID_STATE`、`COLLECTION_SOURCE_INVALID`、`COLLECTION_PERSIST_FAILED`、`COLLECTION_FAILED`。跨博主访问统一 404。

## 第四阶段反馈与报告 API

- `POST/GET /api/v1/bloggers/{blogger_id}/feedback-runs`：按 Output + Metric 冻结快照并分析反馈、查询历史。
- `GET /feedback-runs/{run_id}/evidence|candidates`：读取冻结证据和带版本的 pending/applied/rejected 候选。
- `POST /feedback-runs/{run_id}/confirm|reject|retry`：明确确认、拒绝或安全重试；跨博主统一 404，快照变化返回 409。
- `POST /indicators/defaults`、指标定义 CRUD/停用、`POST /indicators/recompute`、`GET /indicators/{id}/observations`：只执行注册表中的白名单公式，禁止自由表达式、`eval`、任意 SQL。
- `POST/GET /reports`、`POST /reports/{id}/retry`、`GET /reports/compare` 和证据查询：生成、重试、比较不可覆盖的经营报告。

反馈分析只落 `FeedbackRun`、`FeedbackEvidence` 和 pending Revision；不会改变画像、资产、地点、三库或 active 长期记忆。确认在一个事务内复核冻结快照、写 `DecisionLog`、应用所选候选和记忆版本；任一步失败整体回滚。拒绝不产生业务变更。simulated 可以形成明确标注的方向建议，但不能写入真实商业字段。

报告数值和原生 SVG 图表点全部由后端从 Metric、Output、Place、OperationalIndicator 和不可变 Observation 确定性计算，Agent 只解释。money 状态严格区分 `actual`、`estimated`、`data_insufficient`；缺少用户确认收入/成本时不得声称实际赚钱或亏损。

流量经营口径只使用 `source_type=manual` 的 Metric。`simulated` 不进入实际播放量、互动率、趋势或默认流量指标；只有模拟数据时报告返回 `simulation_only`，混合数据时模拟值仅进入单独的“模拟预览”图表，`data_quality` 记录排除数量和 ID。实际流量事实及图表的 `source_refs` 只引用 manual 行。

地点估算与路线共用字段级商业来源策略：人工录入地点、可信来源且可信度不低于 3 的地点，或已应用 `PlaceCommercialRevision` 中由用户在 `place_overrides` 明确覆盖的字段才可参与计算。反馈确认不改写地点原始来源；Revision 保留字段、运行、确认时间和理由。低可信 generated 地点即使字段非 NULL 也不会直接进入估算，未确认字段仍保持不可信。该策略复用 0006 已有 Revision，无需新增迁移。

## 测试与质量门禁

- 日常快速回归（默认命令，51 项代表性用例）：

  `E:\Anaconda\envs\DL\python.exe -m pytest -q --basetemp E:\Guikesong\.pytest-fast`

  默认覆盖画像采集、博主生命周期、资产、记忆、第二至四阶段关键 API、来源边界和页面烟雾测试。实测约 2 分钟；详细 Service 组合、历史迁移往返、千条性能门禁和真实模型集成改为独立门禁，不在每次小改动后重复执行。

- 完整离线回归与覆盖率（提交前或阶段验收）：

  `E:\Anaconda\envs\DL\python.exe -m pytest -m "not real_integration" -q --cov=app\services --cov-report=term-missing --cov-fail-under=80 --basetemp E:\Guikesong\.pytest-run`

- 迁移门禁（仅修改 `app/models` 或 `migrations` 时运行）：

  `E:\Anaconda\envs\DL\python.exe -m pytest -m migration -q --basetemp E:\Guikesong\.pytest-migration`

- 完整离线门禁（合并前或阶段验收运行，230 项，包含详细服务、迁移和性能）：

  `E:\Anaconda\envs\DL\python.exe -m pytest -m "not real_integration" -q --basetemp E:\Guikesong\.pytest-full`

- 静态检查：

  `E:\Anaconda\envs\DL\python.exe -m ruff check app tests migrations`

  `E:\Anaconda\envs\DL\python.exe -m mypy app`

- 性能验收（仅性能相关修改或阶段验收运行）：

  `E:\Anaconda\envs\DL\python.exe -m pytest -m performance -q -s --basetemp E:\Guikesong\.pytest-performance`

- 默认离线、显式启用的真实集成 smoke：

  `set RUN_REAL_EMBEDDING=1 && E:\Anaconda\envs\DL\python.exe -m pytest tests\test_real_integrations.py -m real_integration -q -rs`

  `set RUN_REAL_DEEPSEEK=1 && E:\Anaconda\envs\DL\python.exe -m pytest tests\test_real_integrations.py -m real_integration -q -rs`

  `set RUN_PHASE2_REAL_BGE=1 && E:\Anaconda\envs\DL\python.exe -m pytest tests\test_phase2_real_integration.py::test_phase2_real_bge_cuda_and_dimension -m real_integration -q -rs`

  `set RUN_PHASE2_REAL_DEEPSEEK=1 && E:\Anaconda\envs\DL\python.exe -m pytest tests\test_phase2_real_integration.py::test_phase2_real_deepseek_assessment_structure -m real_integration -q -rs`

  `set RUN_PHASE3_REAL_INTEGRATIONS=1 && E:\Anaconda\envs\DL\python.exe -m pytest tests\test_phase3_real_integration.py -m real_integration -q -rs`

  `set RUN_PHASE4_REAL_INTEGRATIONS=1 && E:\Anaconda\envs\DL\python.exe -m pytest tests\test_phase4_real_integration.py -m real_integration -q -rs`

  未设置开关时不会加载真实模型或调用外部 API。真实 DeepSeek smoke 仅在本地密钥和网络均可用时执行，测试不会输出密钥。

## 运行时数据边界

以下内容均被 Git 忽略：DeepSeek 密钥、SQLite 数据库、`data/tasks/` 任务日志、本地模型权重、测试缓存和覆盖率文件。

## 当前进度

详见 [开发进度](docs/DEVELOPMENT_PROGRESS.md)。

## 前端工作台

前端位于 [`guikesong-creator-agent-main`](guikesong-creator-agent-main/)，与 FastAPI 后端保持独立部署。前端采用 Next.js App Router、React、TypeScript、Vite/vinext 和原生 CSS；后端采用 Python、FastAPI、SQLAlchemy、SQLite、Alembic，并由后端统一调用 DeepSeek。

前端默认请求 `/api/v1`，本地开发服务器通过 `API_PROXY_TARGET` 代理到 `http://127.0.0.1:8000`。普通多轮对话使用 `POST /api/v1/bloggers/{blogger_id}/chat`，默认演示博主为 `id=2`；画像、三库、体检、脚本继续使用对应的后端资源接口。完整前端启动、接口和验证说明见 [`guikesong-creator-agent-main/README.md`](guikesong-creator-agent-main/README.md)。

## 发布安全检查

提交前必须确认以下内容没有被加入暂存区：`deepseek_apikey.txt`、`.env*`、SQLite 数据库、`data/`、本地模型缓存、测试缓存、日志和构建产物。仓库根目录及前端目录的 `.gitignore` 已覆盖这些本地文件；DeepSeek Key 只从后端本地文件读取，不在前端代码中保存。
