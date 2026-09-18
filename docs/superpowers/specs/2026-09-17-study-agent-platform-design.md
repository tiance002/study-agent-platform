# Agent 工程学习规划平台最终整合设计

## 1. 产品目标

建设一个多用户 SaaS 学习助手，首个垂直领域为 Agent 工程实践。平台围绕用户希望解决的真实问题组织学习，而不是先展示一套固定课程。用户创建学习项目，平台诊断基础、分解产品与能力里程碑、推荐和摄取资料、在开发过程中提供教学支持，并通过代码、测试、运行结果、迁移任务和解释性回答验证掌握程度。

核心层级为：

```text
Tenant
└── User
    └── Learning Project
        ├── Project Knowledge Index
        ├── ProjectSourceGrant
        ├── Competency Graph and Mastery State
        ├── Product Plan and Learning Plan
        ├── Sessions
        ├── Workflow Runs
        ├── Evidence and Assessments
        └── Budget Account / Capability Grants
```

Tenant-scoped shared entities sit beside the project tree:

```text
Tenant
├── PlatformContentRelease
├── Source + ACL
├── Budget Account Tree
├── Policy Decision / Grant records
└── Independent Audit Sink Index
```

学习项目是长期记忆、检索和权限的默认边界。会话只是交互载体，不直接承担长期状态。跨项目能力复用必须由用户授权，并引用原始证据，不能复制未经验证的掌握度结论。

## 2. 首版范围

首版支持：

- Agent harness、工具调用、RAG、模型路由、评测和安全等 Agent 工程主题。
- Python、TypeScript、React、PostgreSQL、Redis/Valkey 等实现 Agent 产品所需的配套技术。
- 用户资料上传、网页或代码仓库摄取、结构化知识库和带来源的回答。
- 一个用户创建多个学习项目，每个项目创建多个会话。
- 从真实产品目标反推知识、先修关系、里程碑、练习与验收。
- 在短生命周期 Linux 沙箱内安装依赖、运行测试和构建项目。
- 本地模型承担受限路由和轻任务；高质量教学与复杂推理使用云端模型。

首版不提供：

- 用户 Agent 产品的长期生产托管。
- 任意互联网访问或任意 Shell 权限。
- 自动对外发布、支付、删除远端资源等 A2/A3 操作；这些能力在确认 UI 和授权协议完成后逐项解锁。
- 泛学科内容质量承诺。系统边界可扩展，但首版评测集只覆盖 Agent 工程。

## 3. 架构选择

### 3.1 方案比较

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 模块化单体控制面 + 独立工作节点 | 事务简单、部署成本低、模块边界可测试；沙箱可独立扩容 | 需要严格模块依赖规则，避免退化为大单体 | 采用 |
| 全微服务 | 独立伸缩和团队自治 | 分布式事务、观测和运维成本过早出现 | 达到拆分触发条件后采用 |
| 单进程全栈应用 | 原型最快 | 模型、摄取、沙箱和交互请求互相阻塞，安全边界不足 | 不采用 |

### 3.2 部署单元

```text
React Web
   │ HTTPS/SSE
FastAPI Control Plane ───── PostgreSQL + pgvector
   │       │                    │
   │       ├── Object Storage   └── durable tasks/outbox
   │       ├── Cache Valkey/Redis
   │       └── Control Valkey/Redis
   │
Async Workers ── local model / cloud model / parsers / retrieval
   │
Sandbox Broker ── isolated Linux sandbox pool
Fetcher / Package Proxy ── restricted egress zone
KMS / Secret Manager ── envelope keys and signing keys
Independent Audit Sink ── object lock / WORM / signed batches
```

控制面初期是一个模块化 Python 应用；异步工作节点和沙箱代理是独立进程/部署。沙箱位于独立网络区域，无主数据库凭据。网页/仓库抓取经过独立 Fetcher，依赖安装经过 Package Proxy，二者不能从控制面任意出网。所有外部副作用经过 Policy Gateway 和 durable action intent；审计正文写入独立 sink，PostgreSQL 只保留可删除的索引引用。

拆分微服务的触发条件是独立伸缩、故障隔离或团队所有权形成持续压力，而不是注册用户数本身。

## 4. 推荐技术路线

| 层 | 首选 | 说明 |
|---|---|---|
| 前端 | React + TypeScript | 项目、计划、会话、知识库、证据和确认界面 |
| API/业务 | Python + FastAPI + Pydantic | 检索、模型、教学评估和数据生态更适合 Python |
| 业务数据 | PostgreSQL | 唯一真相源、事务、RLS、outbox、任务状态 |
| 向量检索 | pgvector | 首版减少数据库种类；与数据库内租户过滤结合 |
| 对象存储 | S3 兼容存储 | 原始资料、解析产物、沙箱制品；开发环境可用 MinIO |
| 密钥与秘密 | KMS/HSM + Secret Manager | 数据、审计签名、token 签名密钥分离；按 key version 管理 |
| 公共知识库 | PlatformContentRelease | 全局受控内容、许可、errata 和能力映射；项目显式 pin release |
| 快路径 | 两个独立 Redis/Valkey 兼容部署 | 缓存实例可淘汰；控制实例 `noeviction`，但不承担事实正确性 |
| 后台任务 | PostgreSQL durable task table + `SKIP LOCKED` | 首发规模足够；Valkey 只存加速指针 |
| Agent 工作流 | 有版本的 typed node registry；可评估 LangGraph | 框架只管理工作流，不承担权限、预算和多租户隔离 |
| 本地模型 | 小型开源模型服务 | 只允许分类、查询改写、受 schema 约束的轻量抽取 |
| 云端模型 | provider adapter | 支持模型切换、预算、超时、熔断和数据策略 |
| 沙箱 | Linux 独立节点；gVisor 候选 | 更高隔离需求再评估 Firecracker |
| 可观测性 | OpenTelemetry + 指标/日志/trace 后端 | 全链路携带 tenant、project、run、node 和版本信息 |
| 中文稀疏检索 | 外部分词 + PostgreSQL `tsvector` 起步 | 词典、normalization、chunk 和 index version 化；对比 `pg_bigm` 基准后决定 |
| Prompt Registry | 有版本的 prompt/model/retrieval 组合 | 记录评测组合、敏感扫描、owner、canary 和回滚 |

所有具体发行版、模型、解析器和许可证在锁定依赖前完成 ADR。Redis 与 Valkey 不能假设模块完全等价；若需要搜索模块，应单独做兼容性验证。

### 4.1 关键技术比较

以下比较用于确定架构方向，不代表对 2026 年具体版本、许可证或维护状态的实时背书。

| 决策 | 方案 | 优势 | 劣势 | 适用场景 | 本项目选择 |
|---|---|---|---|---|---|
| 后端语言 | Python | LLM、RAG、解析和评测生态完整；FastAPI/Pydantic 契合结构化接口 | 大型前端团队可能需要维护两种语言 | AI 密集型业务 | 采用 |
| 后端语言 | TypeScript | 前后端共享类型，Web 与实时服务体验好 | Python AI 库常需跨进程封装 | 团队 TS 能力强、AI 逻辑较薄 | 不作主后端，可用于前端/未来 BFF |
| 后端语言 | Python + TS 双后端 | 各用所长 | 早期接口、部署和调试成本翻倍 | 已有两支独立团队 | 首版不采用 |
| 向量存储 | PostgreSQL + pgvector | 事务、RLS、元数据和向量同库，运维最少 | 极大规模向量和专用检索能力受限 | 首版与中等规模多租户 SaaS | 采用 |
| 向量存储 | Qdrant 等专用向量库 | 向量过滤和独立扩展更专业 | 增加事实同步、租户隔离和运维复杂度 | 向量规模/吞吐成为独立瓶颈 | 达到压测阈值后评估 |
| 检索平台 | OpenSearch/Elasticsearch 类 | 强关键词、聚合和混合搜索 | 集群成本与运维较高，业务事务仍在 PostgreSQL | 大规模全文检索与运营搜索 | 中文全文成为瓶颈后评估 |
| 工作流 | 显式 typed runtime | 边界、预算与策略最清楚，依赖少 | durable pause/resume 需要自行实现 | 首版安全核心 | 作为领域接口 |
| 工作流 | LangGraph | Agent 状态图、暂停恢复和生态成熟度候选较好 | 易误把框架状态当权限/业务事实 | 多步 Agent 推理 | 通过 spike 后作为内部实现候选 |
| 工作流 | Temporal | 长流程、重试和 durable execution 强 | 部署与概念成本明显更高 | 大量跨小时/天可靠业务流程 | 长流程和跨服务补偿成为瓶颈后评估 |
| 任务队列 | PostgreSQL task table | 事务一致、组件少、易对账 | 极高吞吐和复杂消费拓扑有限 | 首发规模 | 采用 |
| 任务队列 | RabbitMQ/NATS JetStream | 重试、消费和事件拓扑更专业 | 增加运维及事实同步 | 多服务、多消费组和延迟任务增长 | 触发后引入 |
| 事件平台 | Kafka | 高吞吐、可重放、流处理生态 | 对首版过重 | 大规模分析事件和多下游 | 首版不采用 |
| 本地推理 | Ollama/llama.cpp | 开发简单、硬件覆盖广 | 高并发调度能力有限 | 本地开发、低并发 L1 | 首发候选 |
| 本地推理 | vLLM 类 GPU 服务 | 批处理与吞吐更适合并发服务 | GPU 和运维门槛高 | L1 流量稳定且达到盈亏平衡点 | 有数据后评估 |
| 沙箱 | 普通容器 | 成本低、生态成熟 | 不足以承载恶意多租户代码 | 可信 CI 或本地开发 | 不作为生产隔离边界 |
| 沙箱 | gVisor | 相比普通容器强化系统调用隔离，部署复杂度适中 | 兼容性和性能需按工作负载压测 | 首版多租户代码执行 | 首选候选 |
| 沙箱 | Firecracker | microVM 隔离更强 | 镜像、网络和调度复杂度更高 | 高风险代码或强隔离要求 | 后续评估 |
| 知识关系 | 关系型先修图谱 | 可解释、易版本化、查询和治理简单 | 不自动发现复杂隐含关系 | 课程/能力先修关系 | 采用 |
| 知识关系 | GraphRAG/图数据库 | 跨文档实体关系和多跳探索有潜力 | 构图成本、噪声和评测困难 | 已证明普通混合检索无法处理的多跳任务 | 不在首版引入 |

### 4.2 升级组件的客观触发条件

- pgvector 只有在隔离正确的基准中持续无法达到召回、延迟或吞吐目标时才拆出专用向量库。
- PostgreSQL 任务表只有在锁竞争、消费拓扑、延迟/死信需求形成持续瓶颈时才引入消息系统。
- typed runtime 的领域接口保持稳定；LangGraph 或 Temporal 通过 spike 证明恢复语义、可观测性和运维收益后才成为实现依赖。
- 本地 GPU 推理只有在真实 L1 coverage、调用量和总拥有成本达到盈亏平衡时部署。
- GraphRAG 必须在专门多跳评测集上显著优于混合检索，并覆盖构图更新成本，才进入生产。

## 5. 业务模块边界

| 模块 | 职责 | 不负责 |
|---|---|---|
| Identity & Tenancy | 登录、租户成员关系、项目范围 | 教学逻辑 |
| Learning Engine | 诊断、能力图谱、计划、掌握状态 | 工具授权、模型路由 |
| Knowledge Service | 摄取、解析、索引、检索、引用和谱系 | 决定用户是否掌握 |
| Conversation | 消息、摘要、项目状态装配、流式输出 | 保存唯一掌握状态 |
| Workflow Runtime | typed node、运行状态、预算树、暂停恢复 | 自行扩大权限 |
| Model Router | L0/L1/L2 选择、质量与成本记录 | 工具授权和数据隔离 |
| Policy Gateway | 确定性授权和 capability token | 使用模型作安全决定 |
| Tool/Sandbox Broker | 参数校验、intent、执行、对账 | 直接读取主数据库凭据 |
| Evidence & Evaluation | 产品证据、学习证据、验证器和评测集 | 用单一模型输出作真值 |
| Content Governance | 公共内容 release、来源许可、errata、creator/reviewer/publisher | 不直接改变用户掌握投影 |
| Skill Registry | 技能与契约的发布、版本化、按 node 声明下发技能和工具选择规则 | 权限判断、执行隔离 |
| ChildRun Runtime | 子任务 spawn、权限派生、深度与并发上限、回传信封校验 | 独立授权、A2/A3 副作用 |
| Support & Privacy | 脱敏 support bundle、break-glass、DSR、生命周期 | 不绕过 RLS 或审计 |

模块只通过有版本的服务接口和事件交互。禁止从检索模块直接更新掌握度，禁止从模型输出直接触发工具，禁止工作节点绕过 Policy Gateway 写业务事实。

## 6. 核心数据流

### 6.1 新建学习项目

1. 用户描述真实 Agent 产品目标、限制和期望成果。
2. 系统收集自评、既有代码/作品和诊断任务证据。
3. Learning Engine 将目标映射为产品里程碑与能力图谱。
4. 系统识别先修缺口，生成第一版可执行计划。
5. 用户确认范围；后续证据驱动计划动态调整。

### 6.2 一次交互或执行

1. 身份、租户和学习项目鉴权。
2. 输入大小、数据地域、敏感度、taint/provenance 和硬禁令预检查；外部内容持续带 taint，不由 L2 自动去除。
3. 解析成已注册 typed node；解析失败不得预留工作流预算。
4. 通过轻量 admission quota 后原子预留 workflow budget。
5. Model Router 选择 L0/L1/L2；权限轴独立计算。
6. Policy Gateway 按最小权限签发当前 node 的 capability token 和 policy decision。
7. 模型生成回答候选或工具计划；每个参数携带 lineage/endorsement 约束。
8. 验证契约、证据充分性、组件级 validity 和预算；契约错、证据不足、越权分别进入修复/检索/拒绝分支，并受重规划上限约束。
9. 外部动作先写 action intent 和审计事件，再幂等执行并写 outcome；审计 sink 不可用时高影响动作 fail-closed。
10. 每次产品执行记录 ProductEvidence；只有事前声明的 assessment 才记录 LearningEvidence；projector 异步从证据更新 MasteryProjection。
11. 返回带来源、假设和限制的结果；失败不得静默降级。

## 7. 容量、并发和延迟

架构边界面向约 10 万注册用户、500 峰值同时活跃用户；首发资源按约 1,000 注册用户、20 峰值同时活跃用户部署。

- API 无状态横向扩展；所有长任务进入 durable task table。
- 按租户、用户、项目、工具和模型分别限流与计费。
- 模型、摄取、评测和沙箱使用独立并发池，避免相互耗尽。
- 沙箱池使用队列、公平调度、每租户并发上限和资源配额。
- 压测必须同时覆盖会话轮数、上下文增长/压缩、项目资料与向量规模、检索类型、沙箱比例、think time、稳态/突发和依赖故障。
- 在目标数据规模下用合成负载验证 500 峰值活跃用户，而不是等真实用户增长后再验证。
- 成本按租户、项目和单位任务记录 token、模型、检索、沙箱秒数、存储与 provider invoice；预算是累计上限，限流是速率上限。
- 本地模型拥堵时直接升级云端或排队，不能为了“本地优先”增加总延迟。
- 云端不可用时仅对可重试错误有限重试；之后排队、暂停或显式失败。

首版服务目标用于压测和告警，不作为外部 SLA：L0 路径 P95 小于 200ms；路由开销 P95 小于 300ms；检索 P95 小于 1s；云端回答首 token P95 小于 5s；交互请求不得同步等待沙箱完成。目标按真实区域、模型和数据量校准。

## 8. 错误处理

所有错误使用稳定错误码、`request_id`、可重试标记和用户可理解的状态。重试仅针对明确的瞬时错误，并使用指数退避、抖动、幂等键和总时限。

| 故障 | 默认动作 |
|---|---|
| 鉴权、RLS 上下文或 Policy Gateway 不可用 | 拒绝特权操作；已授权 A0 回答可继续 |
| KMS/密钥服务不可用 | 新写入和签名 fail-closed；仅允许仍在有效授权 TTL 内的缓存解密，不能绕过撤销 |
| 对象存储不可用 | 停止摄取；在途制品保留 intent，重试或进入对账 |
| 审计 sink 不可用 | 高影响操作 fail-closed；低风险事件进入有界缓冲，超限拒绝 |
| typed node registry 不可用 | 现有运行使用签名且未过期的固定版本；新 node 无有效缓存则拒绝 |
| 控制缓存不可用 | 回源 PostgreSQL；昂贵调用拒绝或排队 |
| 检索不可用 | 仅在现有证据充分时受限回答，否则说明无法下结论 |
| 验证器不可用 | 高影响结论拒绝；低影响内容标注未验证或升级人工/云端验证 |
| 本地模型不可用 | 升级云端或排队，不影响权限策略 |
| 云端模型不可用 | 有限重试后排队/暂停/显式失败，不降级到本地生成 |
| 沙箱状态未知 | 保留预算和 intent，进入对账，不重新盲目派发 |

## 9. 测试与发布门槛

- 单元测试：能力状态转换、路由规则、策略矩阵、预算账本、幂等状态机。
- 契约测试：模块接口、typed node schema、工具 schema、模型结构化输出。
- 隔离测试：跨租户和跨项目读取、向量检索、缓存、checkpoint、对象存储。
- 安全测试：提示注入、参数走私、路径逃逸、出网、权限扩大和确认绕过。
- 恢复测试：工作节点崩溃、重复投递、未知外部状态、数据库/缓存/模型故障。
- 数据与恢复测试：备份恢复、删除 tombstone、KMS key version、taint lineage、孤儿沙箱、in-flight action 和未结预算 reservation 对账。
- 统计与质量测试：校准、risk-coverage、中文分词/融合检索、成本和故障注入。
- 回滚演练：prompt/model/retrieval/index/policy/node/validator 版本均可回退。
- 教学评测：近迁移、远迁移、新情境、间隔复测和真实项目交付。
- 发布采用版本化规则、策略、node、validator、数据集和模型配置；canary 超阈值自动回滚。

## 10. 分阶段交付

### 批次一：上线门槛

租户/RLS 隔离、KMS/Secret Manager、typed node registry、Policy Gateway、capability token、预算账本、失败矩阵、durable task/outbox、独立审计 sink、Fetcher/Package Proxy、最小验证器、学习证据模型 v0。默认只开放 A0/A1 的明确子类。退出必须机械证明拒绝路径无外部副作用、掌握可由证据重建、跨租户隔离和恢复对账通过。

### 批次二：受控开放

L1 本地路由、离线 risk-coverage、数据集/验证器版本化、DSR 谱系、确认 UI。完成后按工具逐项解锁 A2/A3。

### 批次三：数据驱动优化

在线 canary、校准与序贯检验、ANN 选择性调优、模型升级重标定、近迁移/远迁移评测深化。任何优化不得放松跨规格不变量。

## 11. 公共内容、生命周期与访问治理

公共内容使用不可变 `PlatformContentRelease`。普通 content update 只进入新 release；普通 errata 通知用户；安全或实质错误使用可审计的 critical correction overlay，并触发相关评测和掌握投影复核。来源许可和 `display_policy`（全文、摘要、仅引用）参与检索展示。

Source 属于 Tenant，带 owner 和 ACL；项目通过 `ProjectSourceGrant` 授权。索引复用键至少包含 content fingerprint、normalization/parser/chunker 版本、最终 embedding input hash 和 embedding model revision。跨租户去重默认不暴露存在性。

生命周期策略本身版本化，分别定义原始资料、派生索引、消息/摘要、沙箱制品、产品证据、学习证据、调试日志和审计主体的用途、TTL、地域、删除方式和 legal hold。DSR 通过 privacy controller、谱系删除、字段级擦除和删除报告完成。

支持人员默认使用用户生成的脱敏 support bundle；break-glass 访问要求 JIT、双人审批、时限、字段脱敏、会话录制和独立审计。Creator、Reviewer/Publisher、Learner、Support、Tenant Admin 和 Privacy/Security Auditor 分权。

Provider adapter 根据数据类别、地域、保留期、训练使用、subprocessor、租户 allowlist 和密钥策略决定是否可发送；秘密、原始凭据和不允许出境数据在 adapter 前拒绝。

首版 UI、教学和评测以中文为主，允许英文技术资料和代码标识符；跨语言学习效果不在首版承诺内。目标为 WCAG 2.2 AA，并覆盖低带宽、低端设备、键盘、读屏、字幕、焦点和时限延长。

## 12. ADR 与跨规格不变量

`docs/adr/README.md` 维护后端、中文检索、Redis/Valkey、工作流、沙箱、KMS、审计、备份恢复、Provider 策略、skill 治理与子任务运行时的 ADR；每项包含 owner、版本、到期复审日期、重新评估触发条件和回滚方案。

跨规格不变量包括：未授权工具不得 dispatch；用户数据默认敏感；taint 只通过 sink-specific endorsement 使用；capability token 只减不增；Redis 丢失不改变事实；审计缺失时高影响操作拒绝；Evidence 是掌握事实源；负证据失效率、pending 和 indeterminate 必须可观测；**子 agent 权限从父 run 派生且只减不增**；**子 agent 回传必须携带谱系**；**skill 与契约文档不承载权限**；任何失败不得静默降级。

## 13. 关联规格

- [系统不变量与威胁模型](./2026-09-17-00-system-invariants-and-threat-model.md)
- [学习闭环与掌握证据](./2026-09-17-01-learning-loop-and-mastery-evidence.md)
- [路由、检索与质量](./2026-09-17-02-routing-retrieval-and-quality.md)
- [策略、工具与执行](./2026-09-17-03-policy-tools-and-execution.md)
- [多租户与数据治理](./2026-09-17-04-multitenancy-and-data-governance.md)
- [Agent 技能分层、上下文管理与工具边界](./2026-09-18-05-agent-skill-context-and-tool-boundary.md)
- [分层架构、层间通信与模块契约](./2026-09-18-06-layered-architecture-and-module-contracts.md)
- [跨规格词汇表](./2026-09-17-glossary.md)
- [设计知识索引](../../skills/manifest.yaml)
- [ADR 索引](../../adr/README.md)
- [实施计划](../plans/2026-09-18-study-agent-platform-implementation-plan.md)
