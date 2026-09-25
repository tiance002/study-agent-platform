# L1 契约 · protocol

> `skill_id: protocol` | version: 0 | expiry: 2027-03-01 | owner: 待指派
> 权威来源：`specs/2026-09-17-03-policy-tools-and-execution.md`、`02-routing-retrieval-and-quality.md`、总体设计 §6.2
> **生成区已启用**：由 `tools/skills/gen_contracts.py` 从 FastAPI OpenAPI、错误码枚举与 node 定义导出，并参与 CI 一致性校验。

## 1. 生成区

<!-- BEGIN GENERATED: source=FastAPI OpenAPI + ErrorCode + node 定义, source_hash=sha256:93919f22418acf048ee62fb721cf9b304ad9fc83bf46ca569ab534c5af85edf3, generated_at=2026-09-25T06:27:57Z -->
> 生成时间：2026-09-25T06:27:57Z

> 由 `tools/skills/gen_contracts.py` 从 FastAPI OpenAPI 与错误码枚举导出，请勿手工编辑本区。

### HTTP 接口

| 路径 | 方法 | 说明 |
|---|---|---|
| `/auth/invitations/exchange` | POST | Exchange Invitation |
| `/auth/login` | POST | Password Login |
| `/auth/logout` | POST | Logout |
| `/auth/logout/all` | POST | Logout All |
| `/auth/register` | POST | Register Account |
| `/healthz` | GET | Healthz |
| `/library/sources` | GET | List Library Sources |
| `/library/sources` | POST | Register Library Source |
| `/library/sources/{library_source_id}/content` | POST | Upload Library Source Content |
| `/me` | GET | Me |
| `/projects` | GET | List Projects |
| `/projects` | POST | Create Project |
| `/projects/{project_id}` | GET | Get Project |
| `/projects/{project_id}` | PATCH | Update Project |
| `/projects/{project_id}/acquisition-jobs/{acquisition_id}` | GET | Get Acquisition Job |
| `/projects/{project_id}/audit` | GET | Audit View |
| `/projects/{project_id}/budget` | GET | Budget View |
| `/projects/{project_id}/confirmations` | POST | Create Confirmation |
| `/projects/{project_id}/conversations` | GET | List Conversations |
| `/projects/{project_id}/conversations` | POST | Create Conversation |
| `/projects/{project_id}/conversations/{conversation_id}/messages` | GET | List Messages |
| `/projects/{project_id}/conversations/{conversation_id}/messages` | POST | Append Message |
| `/projects/{project_id}/conversations/{conversation_id}/teaching-runs` | POST | Create Teaching Run |
| `/projects/{project_id}/diagnosis` | GET | Latest Diagnosis |
| `/projects/{project_id}/diagnosis` | POST | Create Diagnosis |
| `/projects/{project_id}/ingestion-jobs` | GET | List Ingestion Jobs |
| `/projects/{project_id}/ingestion-jobs/{job_id}` | GET | Get Ingestion Job |
| `/projects/{project_id}/interactions` | POST | Interact |
| `/projects/{project_id}/knowledge/search` | POST | Search Knowledge |
| `/projects/{project_id}/library-sources/{library_source_id}/attach` | POST | Attach Library Source To Project |
| `/projects/{project_id}/mastery` | GET | Mastery |
| `/projects/{project_id}/plan` | GET | Current Plan |
| `/projects/{project_id}/plan` | PUT | Replace Plan |
| `/projects/{project_id}/plan/generate` | POST | Generate Plan |
| `/projects/{project_id}/plan/history` | GET | Plan History |
| `/projects/{project_id}/retrieval/chunks` | POST | Ingest |
| `/projects/{project_id}/source-candidates` | GET | List Source Candidates |
| `/projects/{project_id}/source-candidates` | POST | Create Source Candidate |
| `/projects/{project_id}/source-candidates/{candidate_id}/select` | POST | Select Source Candidate |
| `/projects/{project_id}/source-search` | POST | Search Source Candidates |
| `/projects/{project_id}/sources` | GET | List Sources |
| `/projects/{project_id}/sources` | POST | Register Source |
| `/projects/{project_id}/sources/{source_id}/content` | POST | Upload Source Content |
| `/projects/{project_id}/sources/{source_id}/span` | GET | Read Source Span (document_id required) |
| `/projects/{project_id}/tasks/{task_id}` | GET | Get Task |
| `/projects/{project_id}/tasks/{task_id}/submissions` | GET | List Task Submissions |
| `/projects/{project_id}/tasks/{task_id}/submissions` | POST | Submit Task |
| `/projects/{project_id}/tasks/{task_id}/transition` | POST | Transition Task |
| `/projects/{project_id}/teaching-runs/{run_id}` | GET | Get Teaching Run |
| `/projects/{project_id}/teaching-runs/{run_id}/events` | GET | Stream Teaching Events |
| `/registry` | GET | Registry View |

### 稳定错误码

共 48 个，一经发布不得改变语义，只能追加。

| 错误码 |
|---|
| `AUDIT_LOG_CORRUPTED` |
| `AUDIT_SINK_UNAVAILABLE` |
| `AUTH_POOL_SATURATED` |
| `AUTH_REQUIRED` |
| `BUDGET_EXCEEDED` |
| `BUDGET_RESERVATION_FAILED` |
| `BUDGET_TREE_INVALID` |
| `CAPABILITY_ESCALATION_DENIED` |
| `CAPABILITY_NOT_HELD` |
| `CHILD_RUN_AUTHORITY_ESCALATION` |
| `CHILD_RUN_DEPTH_EXCEEDED` |
| `CHILD_RUN_ENVELOPE_INVALID` |
| `CROSS_PROJECT_DENIED` |
| `CROSS_TENANT_DENIED` |
| `CSRF_DENIED` |
| `ENDORSEMENT_EXPIRED` |
| `ENDORSEMENT_REPLAY_DENIED` |
| `ENDORSEMENT_SINK_MISMATCH` |
| `EVIDENCE_IMMUTABLE` |
| `EVIDENCE_UNMAPPED` |
| `IDEMPOTENCY_IN_PROGRESS` |
| `IDEMPOTENCY_KEY_REQUIRED` |
| `IDEMPOTENCY_VIOLATION` |
| `ILLEGAL_STATE_TRANSITION` |
| `INTERNAL_CONSISTENCY_ERROR` |
| `INVITATION_INVALID` |
| `NODE_NOT_REGISTERED` |
| `OBLIGATION_UNSUPPORTED` |
| `PARAMS_INVALID` |
| `PASSWORD_LOGIN_DISABLED` |
| `POLICY_DENIED` |
| `POLICY_GATEWAY_UNAVAILABLE` |
| `PROJECTION_WRITE_DENIED` |
| `RATE_LIMITED` |
| `RECONCILIATION_REQUIRED` |
| `REGISTRATION_DISABLED` |
| `SOURCE_SEARCH_DISABLED` |
| `SOURCE_SEARCH_UNAVAILABLE` |
| `TAINT_REQUIRES_ENDORSEMENT` |
| `TEACHING_PROVIDER_DISABLED` |
| `TENANT_CONTEXT_MISSING` |
| `TOOL_DECLARATION_INVALID` |
| `TOOL_INTENT_CONFLICT` |
| `TOOL_NOT_ALLOWED_FOR_NODE` |
| `TOOL_NOT_REGISTERED` |
| `USERNAME_TAKEN` |
| `VERSION_CONFLICT` |
| `already_authenticated` |

### typed node 输入输出 schema

| node | 输入 schema | 输出 schema | 最低档位 |
|---|---|---|---|
| `diagnose_prerequisites` | `DiagnoseIn/v1` | `DiagnoseOut/v1` | L0 |
| `intake_goal` | `IntakeGoalIn/v1` | `IntakeGoalOut/v1` | L2 |
| `retrieve_material` | `RetrieveIn/v1` | `RetrieveOut/v1` | L0 |
| `validate_and_record` | `ValidateIn/v1` | `ValidateOut/v1` | L0 |
<!-- END GENERATED -->

## 2. 手写区 · 执行状态机（唯一权威转移表）

```text
planned → intent_persisted → dispatched
                          → acknowledged | failed | unknown
                          → reconciled
```

| 规则 | 要求 |
|---|---|
| 幂等键 | 同一逻辑动作的全部重试复用 `(tenant_id, run_id, node_instance_id, logical_action_id, tool_id)` |
| `attempt_id` | 只用于观测，**不进入幂等键** |
| 未授权 | 未通过 Policy Gateway 的动作不得进入 `dispatched`（不变量 #3） |
| 未知状态 | `unknown` 只能进入对账，不得盲目重派（不变量 #8） |
| 已派发 | 已派发调用不能被当作未发生；可安全取消则取消，否则进 `unknown` |

## 3. 手写区 · typed node 与接口约束

| 项 | 约束 |
|---|---|
| node 定义 | 输入/输出 schema、允许工具、最低模型档位、权限属性、验证器、循环/递归/并发/工具次数上限、预算上限 |
| 未知 node | 拒绝实质执行，只允许解释、澄清或重新规划 |
| 参数不合法 | 可在上限内重新规划；**解析失败不得预留工作流预算** |
| 输入上限 | 请求进入时即检查大小、地域、敏感度、taint 与硬禁令 |
| 只读 vs 命令 | 读投影 API 与命令 API 分离；前端不得通过读接口改变状态 |
| 错误码 | 稳定错误码 + `request_id` + 可重试标记 + 用户可理解状态 |
| 重试 | 仅针对明确瞬时错误；指数退避 + 抖动 + 幂等键 + 总时限 |
| 流式 | 交互走 SSE；**不得同步等待沙箱完成** |
| 版本 | 每次运行固定 `policy_version`、`node_registry_version`、`tool_registry_version`、`validator_version`、`dataset_version`、`prompt_version` 与模型标识 |

### 3.1 错误响应体的形状（手写区 · 尚未被生成器覆盖）

所有对外错误响应体必须有**同一组字段**，一个不多一个不少：

```text
{ code, message, retryable, request_id, next_action }
```

| 字段 | 语义 |
|---|---|
| `code` | 稳定错误码。401/404 对外替换成 `UNAUTHENTICATED` / `NOT_FOUND`，避免用内部码差异探测存在性 |
| `message` | 用户可理解描述，不含内部细节 |
| `retryable` | 仅对明确瞬时错误为真。**「结果未知」恒为假** |
| `request_id` | 追踪 id，服务端生成，与响应头 `X-Request-Id` 同源 |
| `next_action` | 由 `code` 推导。`RECONCILIATION_REQUIRED` → `reconcile`，其余为空串 |

三条约束：**① 形状只有一个定义点**（代码里是 `core/errors.public_error_payload`），
任何调用方都不得手写这个 dict；**② `next_action` 由 `code` 推导**，不由 raise 点填写，
否则新增 raise 点时会漏；**③ `retryable=true` 与「先去对账」不能同时出现** ——
那会让客户端在不解决未知状态的情况下重放请求，产生第二次副作用。

状态码映射：`AUTH_REQUIRED` → 401；跨租户/跨项目 → 404（不暴露存在性）；
`RECONCILIATION_REQUIRED` → 409（不是 403「你不被允许」，也不是 5xx「服务故障」——
后者会被当故障重试，而盲目重试正是这条错误要阻止的行为）；审计或策略组件不可用 → 503。

### 3.2 交互结果的证据字段与幂等（手写区）

交互响应里与证据相关的字段**只有一套**，且各答一个问题：

| 字段 | 回答的问题 | 取值 |
|---|---|---|
| `output.evidence_state` | 核心结论有没有足够证据 | `supported` / `partially_supported` / `conflicting` / `insufficient` |
| `output.issues[]` | 缺口具体是什么 | `EvidenceIssueCode` 闭集 + `claim_refs` / `retryable` / `next_action` |
| `output.retrieval_health` | 这次检索有没有按预期跑完 | `clean` / `degraded` |

三条不可违反的约束：

1. **`supported` 需要正向覆盖证明**：必须满足 `required_claim_refs ⊆ supported_claim_refs`。
   拿不到必需结论集合时（首版的常态）输出 `MISSING_SUPPORT` + `insufficient`。
2. **`retrieval_health=clean` 不代表证据充分**。两者正交：首版正常检索就是
   `clean` + `insufficient`。展示层不得把 `clean` 渲染成"结论可靠"。
3. **不得存在第二套证据判定**。历史上曾并存一个按「有没有 citation」计算的
   `evidence_sufficiency`，会与 `evidence_state` 直接矛盾（外部抓取失败但本地有命中时
   必然如此）。该字段已删除，**不提供兼容投影**——它不是历史契约，而是错误判定的遗迹。

**幂等键复用判定与占用状态无关。** `IDEMPOTENCY_VIOLATION` 只看「同一作用域内的
同一把键有没有被用于不同内容」，**不区分上一次是成功还是失败**。
占用者失败后（占用已被释放）复用同键换参数，同样是 `IDEMPOTENCY_VIOLATION` ——
客户端应当改用一把新键，而不是指望"上次没成功所以这次可以复用"。

这条曾经是不一致的：`released` 分支排在指纹比较之前，于是**失败后复用会被静默接受**，
只有成功后复用才报错。客户端据此会得出"key 复用没问题"，而事实只有一半 ——
**不一致的判定比严格但一致的判定更糟。**

追踪与幂等是两个字段，不得混用：

| 字段 | 来源 | 用途 |
|---|---|---|
| `request_id`（响应头 `X-Request-Id` 同值） | 服务端每请求生成 | 全链路追踪。响应头、响应体、错误体、**审计记录**必须是同一个值 |
| `idempotency_key`（请求体可选，≤ 200 字符） | **客户端**提供 | 表达"这是我上一次那个请求的重试"。作用域为 `(tenant_id, idempotency_key)`；指纹另绑主体 + 项目 + 请求内容 + 确认记录 |

幂等键的三种非成功结果必须区分开：

| 错误码 | 含义 | 客户端应当 |
|---|---|---|
| `IDEMPOTENCY_VIOLATION` | 同一租户内这把键被用于**不同内容** | 换一把键（这是用错了） |
| `IDEMPOTENCY_IN_PROGRESS` | 同键请求**仍在处理中**，等待超时 | **重试**（可重试；占用者完成后就会返回重放结果） |
| `output.idempotent_replay = true` | 命中已有结果，本次未重新执行 | 正常使用 |

把前两者混为一谈会让客户端把并发重试当成参数冲突，从而放弃一个本来会成功的请求。

⚠️ 审计记录带 `request_id` 并纳入链哈希（记录格式 `schema_version=2`）。
在此之前审计只带 `run_id`（由 `request_id` 与 `node_id` 哈希而来，**不可逆**），
也就是说**从一次错误响应无法反查到对应的审计事件** —— 而全链路追踪正是它存在的理由。
链校验用 `verify_chain_report()`：它区分 `LEGACY_FORMAT` / `BROKEN_LINK` / `HASH_MISMATCH`，
不要用只返回布尔的入口排障。

重放时响应带 `output.idempotent_replay: true`（**重放必须可观测**，否则客户端分不清
「重试没生效」和「命中了缓存」），且 `request_id` 仍是本次请求的。

## 4. 手写区 · 一次交互的固定顺序（不得调换）

1. 身份、租户、学习项目鉴权
2. 输入大小、地域、敏感度、taint/provenance 与硬禁令预检查
3. 解析成已注册 typed node（失败则不预留预算）
4. 轻量 admission quota 通过后**原子预留** workflow budget
5. Model Router 选择 L0/L1/L2（权限轴独立计算）
6. Policy Gateway 签发当前 node 的 capability token 与 policy decision
7. 模型生成回答候选或工具计划，参数携带 lineage/endorsement 约束
8. 验证契约、证据充分性、组件级 validity 与预算；三个失败分支各自处置
9. 外部动作先写 action intent 与审计事件，再幂等执行并写 outcome
10. 记录产品证据；只有事前声明的 assessment 才记录学习证据
11. 返回带来源、假设与限制的结果；**失败不得静默降级**

**为什么顺序不能换：** 第 5 步若在第 6 步之前不成立，会出现"先用昂贵模型、再发现没权限"；第 3 步若在第 4 步之后，会出现"解析失败却已占用预算"。

## 5. 生成脚本约定（已实现）

| 项 | 约定 |
|---|---|
| 脚本 | `tools/skills/gen_contracts.py --target protocol` |
| 输入 | `backend/app/api/**`、`backend/app/core/errors.py`、`backend/app/workflow/catalog.py` |
| 输出 | 本文件第 1 节生成区（HTTP 路径、稳定错误码、node 输入输出 schema） |
| `source_hash` | 输入文件按路径排序后内容的 SHA-256 |
| CI | `--check` 重新渲染并与文件比对（不只看哈希） |
| 禁止 | 生成区出现人工编辑内容 |

**已知局限：** 当前导出的是「有哪些路径/错误码/node schema」，**不包含**请求体字段级的 schema 细节，
也**不包含**错误响应体的字段级形状（§3.1 因此暂列手写区）。
字段级契约应随 Pydantic 模型与错误体构造器导出补齐 —— 那属于实施期的增量工作。

**注意：** §3.1 是手写区，与生成区之间**没有自动一致性检查**。
代码侧的机械守卫是 `tests/test_error_payload_contract.py` 里的键集比对
（它把所有出口收集起来比对形状）。两者的关系是：测试守代码内部一致，
本节守对外契约声明；改动错误体时两处都要看。

## 6. 手写区 · 常见坑

- **把 `attempt_id` 混进幂等键**：重试会被当作新动作，产生重复副作用。
- **用客户端时间排序事件**：只能以服务端 `event_seq` 为准。
- **在预测阶段就预留预算**：解析未成功就预留，失败后会留下未结 reservation。
- **只读接口顺手改状态**：掌握度、预算、授权都不得由读接口间接改写。
- **新增 node 只改代码不改 registry 版本**：违反不变量 #13，运行无法归因。
- **为省事让前端轮询代替 SSE**：延迟目标（首 token P95 < 5s）与后端并发预算都会被破坏。
