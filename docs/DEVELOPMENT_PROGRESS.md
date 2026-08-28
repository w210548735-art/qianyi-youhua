# 黔衣有话开发进度

更新时间：2026-08-28
当前分支：`develop/phase-2`
阶段状态：`第二阶段知识库体检与指标评估最终门禁中`

## 2026-08-28 第二阶段实现里程碑

- 新增 `Assessment`、`AssessmentIndicator`、`AssessmentEvidence` 与 `0003_phase2_assessment`，体检指标按次固化且历史不覆盖。
- 确定性分析由代码计算三库结构、可信来源、低可信/无来源/孤立资产、画像方向、跨库语义关系、核心资产、薄弱项、就绪度和快照哈希；原始向量不进入持久化快照、API 或外部模型提示。
- `AssessmentAgent` 真实运行使用 `deepseek-v4-flash`，测试使用 Fake；JSON 只修复一次，并先收敛常见指标字段别名再严格校验。
- 后端规范化权重并复算综合分，拒绝不存在、跨博主或资产/来源关系不一致的证据；关系证据两端均可从 `AssessmentEvidence` 追溯。
- 编排已接入 TaskSession、顺序消息/检查点、固定四段 Context、DecisionLog、final_summary 和不自动激活的长期记忆候选；失败不留下部分指标/证据，中断任务在服务启动后进入可重试状态。
- 六个体检 API、具体 OpenAPI response schema、历史比较和最简演示页已接通；切换博主会清空旧体检展示，跨博主访问统一 404。
- 迁移专项：一期固定到 `0002_phase1_closure`，二期验证 `base/0002 → 0003`、数据保留和 downgrade/upgrade；实际本地库已从 0002 升到 0003。
- 真实集成：BGE 在 RTX 4060 CUDA 上输出 512 维，`1 passed in 11.98s`；联网 DeepSeek 返回合法体检结构，`1 passed in 56.55s`。
- 最终全量 pytest：113 collected，`109 passed, 4 skipped, 16 warnings in 147.75s`；默认 skip 仅为真实集成开关。
- `app/services` 覆盖率 `82.38%`；Ruff `All checks passed!`；Mypy `Success: no issues found in 31 source files`。
- 最终覆盖率插桩性能：1000 条二期预分析 `0.615670s`，体检普通 CRUD `0.034175s`；一期检索 `0.148545s`，CRUD `0.034548s`。
- 安全核验：Git 跟踪敏感文件为 `NONE`，本地 Key 内容未出现在任何跟踪文本中；数据库、任务、模型和两类 pytest 临时目录均命中 `.gitignore`。
- Git 提交与远端同步证据将在本轮发布后回填。

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
