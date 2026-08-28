# 黔衣有话开发进度

更新时间：2026-08-29
当前分支：`develop/phase-3-output`
阶段状态：`第三阶段实现门禁已通过，等待独立验收；禁止启动第四阶段`

## 2026-08-29 第三阶段实现里程碑

- 新增 `Output`、`OutputAsset`、`OutputPlace`、`AssetPlace`、`Schedule`、`PublishEvent`、`ReminderEvent`、`Metric`、`CollectionJob` 及显式迁移 `0004_phase3_output`；输出人工编辑创建不可变新版本，旧版本和排期引用不被覆盖。
- `OutputAgent` 提供脚本、分镜、排期和路线说明能力；生产使用 `deepseek-v4-flash`，离线使用 Fake。合法JSON但字段不完整同样进入唯一一次格式修复，第二次仍失败则返回 `OUTPUT_INVALID_JSON`。
- `OutputValidationService` 拒绝快照外、跨博主、软删除、低可信无来源知识引用；脚本与分镜结构、画像风格、平台、来源和地点均由后端复核。
- `OutputService` 已接入冻结快照、固定四段上下文、TaskSession/消息/Checkpoint/final_summary、DecisionLog、OutputAsset/OutputPlace 和不自动激活的决策摘要候选；支持幂等、失败重试、中断恢复、快照冲突和软删除。
- 路线顺序由后端按净收益、喜爱度、KOC、拍摄适配和画像契合度确定性计算；商业字段为 `NULL` 或来源不可信时返回具体缺失地点/字段，Agent 无权改变公式、输入或顺序。
- 排期按画像日更/周更/月更约束，提醒使用可注入时钟且同日去重；发布仅为本地模拟并写 PublishEvent；手工/模拟回收只落非负原始 Metric，不执行反馈判断或资产更新。
- 输出、排期、提醒、模拟发布、回收、证据回查 API 和 Jinja2/原生JavaScript演示区均已接通；跨博主统一404，页面明确不代表真实平台发布/取数。
- 迁移专项验证 `base → 0004`、`0003 → 0004` 数据保留、downgrade/upgrade 和 runtime 预建兼容；一期、二期测试继续固定到各自 revision。
- 在全新临时库升级到head后执行 `alembic check`：`No new upgrade operations detected`，ORM与0004无结构漂移。
- 最终工作树全量回归：`152 passed, 6 skipped, 24 warnings in 258.34s`。带覆盖率全量门禁：同为152通过/6跳过，耗时 `281.15s`，`app/services` 聚合覆盖率 `80.92%`，且 `--cov-fail-under=80` 通过；默认跳过项仅为需显式开关的真实集成。
- Ruff：`All checks passed!`；Mypy：`Success: no issues found in 39 source files`。
- 性能实测：1000条输出查询 `0.025130s`，1000条排期查询 `0.012816s`，输出详情 `0.010510s`，排期创建 `0.020784s`，1000地点路线排序 `0.013698s`。
- 真实集成：RTX 4060 CUDA 上 BGE 512维通过；DeepSeek v4-flash 真实请求和一次结构修复通过；合计 `2 passed in 43.56s`。
- 安全扫描未发现跟踪的Key、数据库、模型权重、任务日志或pytest临时目录；`.tmp_phase3*/` 已加入忽略。最终提交、SSH推送和独立验收摘要尚待执行。

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
