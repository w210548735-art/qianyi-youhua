# 黔衣有话开发进度

更新时间：2026-08-29
当前分支：`develop/phase-4-feedback`
阶段状态：`第四阶段已通过；画像表格快速采集与日常回归分层已完成，等待用户前端确认；未启动第五阶段`

## 2026-08-29 画像快速采集与测试分层

- 多画像采集首页改为完整表格：用户一次填写后仅调用一次 `batch-format` API 和一次 ProfileAgent，先返回格式化预览，最终确认时才创建 Blogger；原多轮问答保留为兼容入口。
- 后端状态机与批量确认均保留来源和决策审计；模型失败不创建半成品会话或 Blogger，确认请求会应用用户在预览阶段的最终修正。
- Pytest 改为四层门禁：默认 `daily` 代表性回归、`migration`、`performance`、`real_integration`；完整离线门禁仍可用 `-m "not real_integration"` 运行全部非网络测试。
- 分层实测：默认由 238 项中的 51 项关键用例组成，`51 passed, 187 deselected in 116.08s`；此前宽泛默认集为 `193 passed, 45 deselected in 443.65s`，耗时缩短约 74%。
- 独立性能门禁：`10 passed, 228 deselected in 37.44s`；完整离线门禁收集 `230/238`，真实集成 8 项继续显式执行。

## 2026-08-29 第四阶段独立门禁返修

- 根因：`ReportDataService` 与三个 traffic 指标直接聚合全部 Metric，导致 simulated 被标成 actual；报告只凭 Place 商业字段非 NULL 就形成 estimated；Feedback 确认留下 Revision 但 Route/Report 未读取其字段级来源。
- 统一流量口径：实际流量事实、互动率、趋势、实际图表和默认 traffic 指标只使用 manual Metric；simulated-only 返回 `simulation_only`，混合数据将 simulated 放入独立预览，并在 data_quality/evidence 中记录排除数量和 ID。
- 统一地点口径：新增批量 `commercial_data_policy`，路线和报告共同接受人工来源、可信来源+可信度≥3，或 applied `PlaceCommercialRevision` 中显式 `place_overrides` 字段；不改写地点原始 source/origin，未确认字段不继承信任。
- 迁移策略：复用 0006 已有 `PlaceCommercialRevision` 的 after/reason/status/confirmed_at/applied_at，不新增表或字段，不改写 0001-0006；Alembic 仍为单一 head `0006_phase4_feedback`，原迁移全路径测试继续通过。
- 新增 `test_phase4_boundary_regressions.py`，覆盖 simulated-only、混合排除、指标来源、0分母/趋势、低可信地点、可信种子、确认后路线/报告可追溯及拒绝/失败回滚；扩展专项 `69 passed`。
- 最终全量覆盖率门禁：`225 passed, 8 skipped, 39 warnings in 529.03s`，`app/services=82.46%`；Ruff app/tests/migrations 与 Mypy app 通过。
- 最终性能：1000 Metric 反馈预分析 `0.087343s`、报告聚合 `0.261454s`；1000 Feedback/Report 列表 `0.025215s/0.025791s`，CRUD `0.015531s`；1000 地点来源策略 `0.027150s`、地点报告 `0.109573s`。
- 显式真实集成：BGE/CUDA 512维与 DeepSeek v4-flash `2 passed in 35.50s`。
- Git交付：敏感扫描无新增；返修提交 `f127a7a` 已通过 SSH 推送，工作区 clean、远端 ahead/behind=`0/0`，`origin/main` 未变化；下一步仅发送独立验收摘要并暂停。

## 2026-08-29 第四阶段实现里程碑

- 新增 `FeedbackRun`/Evidence、四类 Revision、OperationalIndicator/Observation、Report/Evidence 及显式迁移 `0006_phase4_feedback`；Blogger 增加 `knowledge_focus`，Asset 增加可空 effect/effect_weight，Metric 增加 shares、可空 actual_revenue/actual_cost 和 user_confirmed。
- 反馈确定性预分析严格按 Output/Metric/Asset/Place 显式链路和 blogger 隔离冻结快照；任务消息、checkpoint 和候选记忆不进入业务冲突 hash，画像、Output、Metric、Asset、Place 变化会触发 409。
- FeedbackAgent 使用 Fake/DeepSeek 可注入协议，真实模型限定 `deepseek-v4-flash`；质量状态、证据和三库 target 结构最多修复一次。分析只创建 pending 候选，确认/拒绝前不修改画像、资产、地点、三库和 active 记忆。
- 用户确认在单事务内应用画像、资产效果、地点商业字段及三库进化，保留 Revision/DecisionLog/记忆版本；失败整体回滚。simulated 不能写实际商业值，未知商业字段保持 NULL。
- 默认 11 个经营指标只执行 Python 白名单注册函数，不接受表达式、eval、exec 或任意 SQL；Observation 和报告历史不可覆盖，分母为0、样本/商业值不足均返回 data_insufficient。
- 报告后端确定性生成 money/traffic/product/supplier 事实和四类图表，Agent 只解释且不能新增数字；actual、estimated、data_insufficient 严格分离。API、跨博主404、失败重试、历史比较和 Jinja2/原生JS/SVG 演示均已接通。
- 第四阶段专项：`54 passed, 2 skipped in 80.21s`；最终全量覆盖率门禁：`215 passed, 8 skipped, 39 warnings in 496.18s`，`app/services=82.35%`。
- 最终性能复核：1000 Metric 反馈预分析 `0.092204s`，报告聚合 `0.244347s`，1000 Feedback/Report 列表 `0.026366s/0.022283s`，普通 CRUD `0.019032s`。
- 真实集成显式联跑：本地 BGE/CUDA 512维与 DeepSeek v4-flash `2 passed in 83.95s`。Ruff app/tests/migrations 与 Mypy app 当前通过。
- 当前 Alembic 单一 head=`0006_phase4_feedback`；迁移专项 8 passed，覆盖 base→head、0005→0006数据保留、downgrade/upgrade、runtime预建、约束和 alembic check。
- 详细需求→实现→测试证据见 `docs/产品文档/第四阶段_开发任务清单_v1.0.md`；本地全量、覆盖率、静态、迁移、性能与真实集成门禁均已通过，敏感扫描无新增，实施提交 `54f7df3` 已通过 SSH 推送；等待独立验收。

## 2026-08-29 第四阶段开工

- 第三阶段 `develop/phase-3-output@e83cc92` 已通过独立门禁。
- 已从验收提交创建并通过SSH推送 `develop/phase-4-feedback`，upstream 指向同名远端；main 未修改。
- 开工冻结基线：Alembic `0005_phase3_metric_contract_fix` 单一head；`157 passed, 6 skipped, 26 warnings in 244.55s`；services覆盖率`80.94%`；Ruff/Mypy通过；Git clean且远端0/0。
- 本阶段严格限定为反馈候选、人工确认/拒绝、三库进化、确定性经营指标和经营报告；不实现登录、多租户、PostgreSQL、队列、真实OAuth/发布/平台取数或生产部署。
- 详细任务与验收矩阵见 `docs/产品文档/第四阶段_开发任务清单_v1.0.md`。

## 2026-08-29 第三阶段实现里程碑

- 独立验收发现并已修复 P3-F1 至 P3-F4：Metric 幂等唯一范围由全局键统一为 `schedule_id + idempotency_key`；真实数据库写入失败先回滚并用稳定 `job_id` 落为 failed；API 只保留外层任务/指标共用幂等键并透传 `collected_at`；数据库和 Schema 均只允许 `manual/simulated`。
- 新增 `0005_phase3_metric_contract_fix`，兼容已经升级到 `0004` 的数据库，重建 Metric 约束并保留现有行。若降级时已有跨排期重复键，迁移会明确拒绝而不删除、合并或改写业务数据。
- 收尾专项：回收、API、迁移共 `17 passed`；含排期、smoke、性能的第三阶段收尾组合 `25 passed`。全量覆盖率门禁为 `157 passed, 6 skipped, 26 warnings in 245.99s`，`app/services=80.94%`。
- 收尾静态与迁移：Ruff `All checks passed!`；Mypy 39 源文件无问题；单一 head=`0005_phase3_metric_contract_fix`；空库/0003/0004升级、往返和 `alembic check` 均通过且无漂移。
- 收尾性能：1000条输出查询 `0.022082s`，1000条排期查询 `0.013520s`，输出详情 `0.007451s`，排期创建 `0.016143s`。真实 smoke 本轮联网复测 `2 passed in 21.97s`（RTX 4060 CUDA/512维及 DeepSeek v4-flash）。
- 收尾实现提交 `2bd4708` 已通过 SSH 推送到 `origin/develop/phase-3-output`；最终证据文档随当前分支推送，随后向独立验收会话发送结构化摘要并暂停。

- 新增 `Output`、`OutputAsset`、`OutputPlace`、`AssetPlace`、`Schedule`、`PublishEvent`、`ReminderEvent`、`Metric`、`CollectionJob` 及显式迁移 `0004_phase3_output`；输出人工编辑创建不可变新版本，旧版本和排期引用不被覆盖。
- `OutputAgent` 提供脚本、分镜、排期和路线说明能力；生产使用 `deepseek-v4-flash`，离线使用 Fake。合法JSON但字段不完整同样进入唯一一次格式修复，第二次仍失败则返回 `OUTPUT_INVALID_JSON`。
- `OutputValidationService` 拒绝快照外、跨博主、软删除、低可信无来源知识引用；脚本与分镜结构、画像风格、平台、来源和地点均由后端复核。
- `OutputService` 已接入冻结快照、固定四段上下文、TaskSession/消息/Checkpoint/final_summary、DecisionLog、OutputAsset/OutputPlace 和不自动激活的决策摘要候选；支持幂等、失败重试、中断恢复、快照冲突和软删除。
- 路线顺序由后端按净收益、喜爱度、KOC、拍摄适配和画像契合度确定性计算；商业字段为 `NULL` 或来源不可信时返回具体缺失地点/字段，Agent 无权改变公式、输入或顺序。
- 排期按画像日更/周更/月更约束，提醒使用可注入时钟且同日去重；发布仅为本地模拟并写 PublishEvent；手工/模拟回收只落非负原始 Metric，不执行反馈判断或资产更新。
- 输出、排期、提醒、模拟发布、回收、证据回查 API 和 Jinja2/原生JavaScript演示区均已接通；跨博主统一404，页面明确不代表真实平台发布/取数。
- 迁移专项验证 `base → 0005`、`0003 → 0005`、`0004 → 0005` 数据保留、downgrade/upgrade 和 runtime 预建兼容；一期、二期测试继续固定到各自 revision。
- 在全新临时库升级到head后执行 `alembic check`：`No new upgrade operations detected`，ORM与0005无结构漂移。
- 原第三阶段实现基线全量回归：`152 passed, 6 skipped, 24 warnings in 258.34s`；收尾修复后的最新结果以上方 `157 passed` 记录为准。
- Ruff：`All checks passed!`；Mypy：`Success: no issues found in 39 source files`。
- 原实现性能基线：1000条输出查询 `0.025130s`，1000条排期查询 `0.012816s`，输出详情 `0.010510s`，排期创建 `0.020784s`，1000地点路线排序 `0.013698s`；收尾复测结果见上方。
- 原实现真实集成：RTX 4060 CUDA 上 BGE 512维通过；DeepSeek v4-flash 真实请求和一次结构修复通过；合计 `2 passed in 43.56s`；收尾联网复测结果见上方。
- 安全扫描未发现跟踪的Key、数据库、模型权重、任务日志或pytest临时目录；`.tmp_phase3*/` 已加入忽略。原第三阶段实现提交 `4e44134` 已通过SSH推送；本次收尾修复完成门禁后独立提交并推送，未提交或合并main。

## 2026-08-29 第三阶段开工

- 第二阶段 `develop/phase-2@6783d146` 已通过独立门禁并保持远端0/0。
- 已从该提交创建并通过SSH推送 `develop/phase-3-output`，Alembic起始head为 `0003_phase2_assessment`。
- 第三阶段范围限定为脚本、分镜、收益约束路线、排期、提醒、模拟发布、手工/模拟回收和证据链，不实现反馈学习、经营报告或真实平台发布。
- 开工基线为115项测试可收集；详细矩阵见 `docs/产品文档/第三阶段_开发任务清单_v1.0.md`。

## 2026-08-28 第二阶段实现里程碑

- 新增 `Assessment`、`AssessmentIndicator`、`AssessmentEvidence` 与 `0003_phase2_assessment`，体检指标按次固化且历史不覆盖。
- 确定性分析由代码计算三库结构、可信来源、低可信/无来源/孤立资产、画像方向、跨库语义关系、核心资产、薄弱项、就绪度和快照哈希；原始向量不进入持久化快照、API 或外部模型提示。
- `AssessmentAgent` 真实运行使用 `deepseek-v4-flash`，测试使用 Fake；JSON 只修复一次，并先收敛常见指标字段别名再严格校验。
- 后端规范化权重并复算综合分，拒绝不存在、跨博主或资产/来源关系不一致的证据；关系证据两端均可从 `AssessmentEvidence` 追溯。
- 编排已接入 TaskSession、顺序消息/检查点、固定四段 Context、DecisionLog、final_summary 和不自动激活的长期记忆候选；失败不留下部分指标/证据，中断任务在服务启动后进入可重试状态。
- 六个体检 API、具体 OpenAPI response schema、历史比较和最简演示页已接通；切换博主会清空旧体检展示，跨博主访问统一 404。
- 迁移专项：一期固定到 `0002_phase1_closure`，二期验证 `base/0002 → 0003`、数据保留和 downgrade/upgrade；实际本地库已从 0002 升到 0003。
- 真实集成：BGE 在 RTX 4060 CUDA 上输出 512 维，`1 passed in 11.98s`；严格指标字段协议下联网 DeepSeek 返回合法体检结构，`1 passed in 42.82s`。
- 最终全量 pytest：115 collected，`111 passed, 4 skipped, 16 warnings in 144.06s`；默认 skip 仅为真实集成开关。
- `app/services` 覆盖率 `82.34%`；Ruff `All checks passed!`；Mypy `Success: no issues found in 31 source files`。
- 终审加固：核心/薄弱/就绪/缺失结论均落 Evidence；任务完成文件与数据库失败可回滚；不完整指标只修复一次；每库进入模型的资产最多50条。
- 最终覆盖率插桩性能：1000 条二期预分析 `0.615670s`，体检普通 CRUD `0.034175s`；一期检索 `0.148545s`，CRUD `0.034548s`。
- 安全核验：Git 跟踪敏感文件为 `NONE`，本地 Key 内容未出现在任何跟踪文本中；数据库、任务、模型和两类 pytest 临时目录均命中 `.gitignore`。
- 实现提交 `2cc1c37` 已通过 SSH 推送 `origin/develop/phase-2`；未提交或合并 `main`。最终文档提交随后独立推送，最终哈希以交付报告为准。

## 2026-08-28 第二阶段开工

- 第一阶段最终提交 `3c2e4ca` 已与 `origin/develop/phase-1` 同步。
- 已从该提交创建并通过 SSH 推送独立分支 `develop/phase-2`。
- Alembic 起始 head：`0002_phase1_closure`；第二阶段迁移必须衔接为 0003。
- 第二阶段严格限定为三库体检、动态指标、证据评分、功能就绪和历史比较，不实现内容产出、路线、发布、反馈或经营报告。
- 详细任务与证据矩阵见 `docs/产品文档/第二阶段_开发任务清单_v1.0.md`。

## 2026-08-28 第一阶段收尾里程碑

- 已确认画像支持编辑和可审计软删除；编辑写入决策并生成画像长期记忆新版本，旧版本保留为 `superseded`。
- 资产已补齐按博主作用域的手工新增、详情、编辑、软删除，以及独立标签、来源类型、来源和可信度范围组合过滤。
- 地点库已实现完整 CRUD、过滤、软删除和可信种子同步；未由用户或可信来源给出的商业字段始终保持 `NULL`。
- Alembic `0001_phase1_initial` 已冻结为显式操作；新增 `0002_phase1_closure`，空库升级、既有 0001 数据升级及降级/再升级测试均通过。
- 画像采集已接入可注入 `ProfileAgent`；单元测试使用 Fake，真实运行使用 `deepseek-v4-flash`，失败保留会话并可重试。
- 当前分组回归证据：地点/迁移/资产 API `14 passed`；任务记忆与建库 `14 passed`；长期与短期记忆入口 `12 passed`。
- 最终全量 pytest：`72 passed, 2 skipped in 93.69s`；`app/services` 覆盖率 `83.31%`。
- Ruff：`All checks passed!`；Mypy：24 个源文件无问题。
- 性能：1000 条资产完整组合检索 `0.059433s`；普通 CRUD `0.039980s`。
- Alembic：空库、标准 0001 旧库及 runtime 预建 Place 的 0001 旧库均可升级到 `0002_phase1_closure (head)`；本地实际库升级成功。
- 真实 BGE smoke：CUDA、512维，`1 passed in 14.99s`。真实 DeepSeek smoke 已显式启用，但当前网络不可用，按规则记录为 skip/阻塞，未伪造成功。
- 实现提交 `dd9bb79` 已通过 SSH 推送到 `origin/develop/phase-1`；未提交或推送到 `main`。

## 第一阶段交付状态

| 模块 | 状态 | 实际证据 |
|---|---|---|
| GitHub 与 SSH 基线 | 已完成 | 私有仓库 `w210548735-art/qianyi-youhua`；SSH 鉴权成功；开发分支独立于 `main` |
| DL/GPU 环境 | 已完成 | Python 3.10.18；PyTorch 2.7.1+cu118；RTX 4060 CUDA 可用 |
| 贵州文旅测试种子 | 已完成 | 20 条权威测试数据，来源为 UNESCO、政府官网和国家级非遗资料 |
| Python/FastAPI | 已完成 | FastAPI、Jinja2、原生 JavaScript 演示页；服务烟雾测试 HTTP 200 |
| 数据模型与迁移 | 已完成 | SQLAlchemy 模型与 Alembic `0001_phase1_initial`；空库升级到 head 成功 |
| 画像采集 | 已完成 | 多轮采集、一次澄清、确认前修正、必填校验、重复确认幂等 |
| 三库建库 | 已完成 | 20 条知识、5 条素材、3 条算法；来源、可信度、标签、理由与决策留痕完整 |
| DeepSeek 接入 | 已完成 | `deepseek-v4-flash` 真实请求成功生成 8 条支持资产；协议单测覆盖密钥和 JSON 校验 |
| 本地向量化 | 已完成 | `BAAI/bge-small-zh-v1.5` 本地离线加载；CUDA；512 维；CPU 自动降级 |
| 混合检索 | 已完成 | 关键词加权 + 余弦相似度；多条件、分页、稳定排序、软删除过滤 |
| 长期记忆 | 已完成 | 画像、可信资产、来源、决策融合；候选晋升；版本历史；博主隔离；向量失败回滚 |
| 短期任务记忆 | 已完成 | 数据库与标准任务目录双持久化；消息、决策、检查点、产物、摘要与恢复 |
| Agent 上下文 | 已完成 | 固定四段顺序；近期短期记忆裁剪；当前博主相关 active 长期记忆召回 |
| 演示页面 | 已完成 | 可查看任务日志、恢复状态、final_summary、长期记忆及召回上下文 |

## 最新质量证据

- 全量测试与覆盖率门禁：`38 passed in 68.61s`，核心服务覆盖率 `80.53%`。
- Ruff：`All checks passed!`
- Mypy：`Success: no issues found in 19 source files`
- 性能：1000 条资产混合检索 `0.070407s`；普通 CRUD `0.036077s`；覆盖率插桩下检索 `0.225892s`。
- Alembic：`0001_phase1_initial (head)`。
- 本地真实向量模型：`DEVICE=cuda`、`COUNT=2`、`DIM=512`，离线加载成功。
- 服务烟雾：健康检查 HTTP 200、页面 HTTP 200、页面包含记忆演示区、OpenAPI 暴露 11 个记忆路径。

## 已知风险

- SQLite + NumPy 全量扫描适合第一阶段演示和小规模数据；资产量显著增长后应迁移到专用向量索引。
- DeepSeek 属于外部服务，仍会受网络、额度、响应格式变化影响；当前已提供明确错误码与事务回滚。
- Conda `DL` 环境存在 Requests 依赖版本警告和历史无效发行版警告，本次测试未受影响，建议后续单独清理环境。
- 演示页按要求只保证功能，不提供鉴权、权限角色和视觉设计；生产部署前必须补齐认证授权。

## 提交规则

- 所有第一阶段代码只提交并推送到 `develop/phase-1`，不直接提交 `main`。
- 密钥、数据库、模型权重、任务运行日志、测试临时文件不得进入 Git。
