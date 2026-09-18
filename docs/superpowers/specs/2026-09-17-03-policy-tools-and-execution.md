# 策略、工具与执行

## 1. 两个正交轴

能力档位 L0/L1/L2 决定谁生成结果；权限轴决定允许做什么。权限细分为：

- A0：不调用工具的回答。
- A1a：项目内、非敏感读取。
- A1b：敏感读取。
- A1c：外部网络读取。
- A1d：消耗型读取。
- A2：可撤销或受控的写入与对外可见动作。
- A3：不可逆、资金、删除或无法语义等价补偿的动作。

策略同时考虑数据敏感度、修改范围、对外可见性、可逆性，以及数据主体、地域、审计和法定时限等合规硬约束。A 轴与教学影响度 D 轴通过策略表映射控制措施，不直接取数值最大值。

## 2. Policy Gateway

Policy Gateway 是无模型的确定性服务。相同 `policy_version + immutable input snapshot` 必须产生相同 decision。决策记录包含 snapshot 的哈希和可重放事实引用。

`PolicyDecision` 是显式持久实体：

```text
decision_id / policy_version / snapshot_id / snapshot_hash
sanitized_fact_refs / verdict / obligations / deny_reasons
```

快照使用规范化序列化；原始 PII 只在受控保留策略下保存。执行器必须声明支持的 obligation ID 和版本，未知或不支持的 obligation 一律拒绝执行。

策略输入包括 principal、tenant、project、node、tool、参数、资源 ACL、数据标签、预算账户、合规上下文和当前授权域。策略失败时拒绝新的特权操作。

## 3. Capability Token

token 包含 tenant、project、run、node instance、audience、允许工具、参数边界、`policy_version`、`revocation_epoch`、`resource_acl_version`、`tool_schema_digest`、`budget_account_id` 和过期时间。

当前 token 只能缩小能力。扩权必须结束当前授权域，重新评估并签发新 token；扩权只能请求注册 node 声明的工具，并有次数上限。高风险扩权需要用户确认。低权限 node 不接收无关工具描述。

外部内容和模型派生值默认带 taint。安全来源使用 `TaintSource[]`，与 `acquisition_method`、`derived_from[]` 分开保存。通用派生函数只合并父来源，并接受显式 `new_sources`；模型边界产生新内容时必须传入 `MODEL_OUTPUT`，确定性解析、拼接或规范化不得无条件增加该来源。去污点不是修改原值，而是签发绑定当前主体、操作、sink、schema、policy decision 和过期时间的 sink-specific endorsement。SQL 参数的 endorsement 不得用于 Shell 或 URL；跨 endorsement 拼接会生成新 tainted value，不能沿用旧授权。endorsement 的历史记录可审计，但持久化后不能作为未来执行授权复用。

广播和短 TTL 加速撤销，但 A2/A3 及昂贵调用在派发前在线检查紧凑授权状态。已派发调用不能被当作未发生；支持安全取消则取消，否则进入 `unknown` 对账。

## 4. Node 与工具注册表

node 定义输入/输出 schema、允许工具、最低模型档位、权限属性、验证器、最大循环/递归/并发/工具次数和预算上限。工具定义参数 schema、网络/文件边界、幂等等级、状态查询、补偿语义、审计级别，以及下列用于边界判定的字段：

| 字段 | 含义 | 作用 |
|---|---|---|
| `intent_tag` | 唯一的「动词 + 宾语」，如 `retrieve_chunk` | 重叠检测主键 |
| `sink_class` | 结果去向：`context` / `artifact` / `external` / `fact` | 区分同名不同用途 |
| `owner_module` | 唯一负责模块 | 禁止跨模块重复实现 |
| `min_authority` | 最低权限档位 | 权限裁剪 |
| `exclusivity` | `exclusive` / `composable` | 是否允许多工具完成同一 intent |

字段缺失或歧义的注册请求一律拒绝。

**工具重叠门（CI）：** 同一 `(intent_tag, sink_class, min_authority)` 组合下注册多个 `exclusive` 工具即构建失败。

**歧义修复顺序（不得跳步）：** 合并 → 降为内部函数 → 用参数区分 → 补描述（最后手段，且必须同时更新 when-to-use 表）。一发现选错工具就加长描述是反模式：描述越长，schema 越贵，选择反而越差。

**已知局限：** 该门只能拦截「同一 intent 的重复实现」，拦不住「语义相近但标签不同」的歧义。后者依赖 when-to-use 表与定期工具清单复查，不能因为有了 CI 门就取消人工复查。

注册表版本化、有 owner、变更评审、契约测试和回滚。未知 node 拒绝实质执行，只允许解释、澄清或重新规划；参数不合法可在独立的格式修正上限内重新规划。

### 4.1 模型工具调用的确定性校验门

模型输出不是工具调用事实。运行时必须按固定顺序处理，任一校验失败都不得进入 Policy Gateway、预算预留、intent 或派发：

```text
模型原始输出
→ JSON 解码
→ tool-call 信封 schema 校验
→ 当前 node 的工具白名单校验
→ 对应 ToolSpec.params_schema 校验
→ 字段级语义规则（范围、枚举、IP/URI/工单号等）
→ 按 schema 规范化参数并固定 params_hash
→ Policy Gateway
→ 预算预留 / confirmation / intent / dispatch
```

整体结构使用 JSON Schema 或 Pydantic 等结构化验证器，禁止用正则解析 JSON。正则只用于工单号等叶子字符串；IP、URI 等优先使用标准解析器。参数 schema 默认拒绝额外字段，必须声明必填项、类型、长度/数量上限、数值范围和枚举。确认记录创建与实际执行必须调用同一个版本化规范化器，并绑定 `tool_schema_digest + params_hash`；禁止一边对原始参数哈希、另一边对补默认值后的参数哈希。

校验失败向模型返回稳定的 `PARAMS_INVALID` 结构，至少包含 `tool_id`、字段路径、原因、修正序号和剩余修正次数；不得把内部异常堆栈写入 prompt。未知工具返回当前 node 可用工具的**注册表生成视图**，不拼接不可信的远端 `tools/list` 描述。

格式修正上限定义为 `max_generation_repairs`，语义是“首次生成之外最多允许的修正次数”；默认 2，即总生成次数最多 3。达到上限后终止当前工具调用链并进入显式降级策略，不得无限循环。

### 4.2 三套独立预算

| 预算 | 计数点 | 失败处理 |
|---|---|---|
| `max_generation_repairs` | JSON、工具名或参数校验失败后请求模型修正 | 不占 `max_tool_calls`，但消耗 token、step 和模型成本 |
| `max_tool_calls` | 工具已经派发；成功、明确失败或结果未知均计数 | 受 node 总调用上限约束 |
| `max_execution_attempts` | 同一 logical action 的实际执行尝试 | 复用同一幂等键，仅按工具幂等/对账策略允许 |

派发前因 schema、白名单、策略或预算失败的候选不是工具调用；已进入 `dispatched` 的动作无论结果如何都不是格式修正。三套计数必须分别进入审计与指标，不能复用一个 `max_tool_calls` 字段表达。

`max_generation_repairs` 与 `max_tool_calls` 属于 `NodeSpec`，分别限制该 node 的模型修正和工具编排；`max_execution_attempts` 属于 `ToolSpec`，由工具幂等能力决定，并受 node/run 总 step 与成本预算的更小上限约束。

### 4.3 显式降级策略

格式修正耗尽、没有合法工具选择或工具链不能继续时，由版本化 `ToolCallFallbackStrategy` 根据 node、错误类别和证据状态选择：请求用户澄清、转为无工具解释、返回可用工具与限制、暂停等待恢复、终止并交人工处理。策略输出必须写入结果与审计，包含 `strategy_id/version`、触发原因和未完成事项；禁止静默假装成功。新增策略通过注册新实现扩展，不修改主校验流程。

## 5. 预算树

预算覆盖货币、token、沙箱时间、步骤、并发和工具调用。创建子节点时从父节点原子预留；子节点只能消费 reservation。父节点的 `completion_reserve` 不可借出。

模型格式修正同样消耗 token、step 和货币预算；即使尚未形成合法工具调用，也不得在这些维度上免费循环。`max_generation_repairs` 是附加的退出条件，不替代预算树。

昂贵调用预留成本上界并按实际结算；廉价调用也受 worker 子预算约束。reservation 使用租约和心跳，调用进入 `in_flight` 后不能仅因 TTL 到期释放。`unknown` 有 SLA、单独看板和最大风险敞口。

## 6. 幂等与执行状态机

客户端提供作用域为租户的根请求幂等键，服务端建立唯一约束并映射到 run。内部 logical action 和 node instance 由服务端创建并在派发前持久化；随机标识允许使用，因为恢复读取 durable intent，而不是重新猜测标识。

同一逻辑动作的全部重试复用：

```text
(tenant_id, run_id, node_instance_id, logical_action_id, tool_id)
```

`attempt_id` 只用于观测，不进入幂等键。

执行状态机：

```text
planned → intent_persisted → dispatched
                         → acknowledged | failed | unknown
                         → reconciled
```

工具适配器按能力阶梯处理：原生幂等、可查询对账、条件写、可补偿、不可安全重试。只读但消耗资源或只能读取一次的工具按消耗型动作管理。补偿必须标注是否语义等价；不等价动作按 A3 控制。

已派发的写操作不得由模型直接生成一次“新调用”来重试。只有状态机和执行适配器可以在 `max_execution_attempts` 内复用原 logical action 与幂等键：`unknown` 必须先对账；明确失败是否可重试由幂等等级、错误类别和剩余预算共同决定；不可安全重试的动作立即终止并进入降级或人工处理。

## 7. 人工确认

确认界面显示确切工具、参数、目标资源、影响范围、成本、是否可撤销和补偿含义。只允许对同质、同策略范围的动作批量确认。授权具有范围、次数和时间限制；高风险动作需要重新认证。系统监控确认频率和无脑确认信号，超过阈值改为暂停而非继续弹窗。

## 8. 沙箱

沙箱在独立 Linux 执行节点运行，使用一次性文件系统、CPU/内存/磁盘/进程/时长限制和默认断网。依赖下载走允许域名、内容缓存和恶意软件扫描。沙箱只获得短期制品 URL 与 capability token，不持有主数据库、对象存储主密钥或平台内部网络权限。

网页/仓库 Fetcher 和 Package Proxy 独立于控制面。Fetcher 解析一次并固定经过 IP 校验的连接，阻断 loopback、link-local、RFC1918、IPv6 私网、云元数据地址和不允许端口；限制重定向、响应/解压大小、文件数、协议和内容类型。归档在沙箱内解压，禁止路径穿越与符号链接逃逸。Package Proxy 只允许白名单仓库和包类型，代理日志按租户隔离；沙箱默认不能把资料或制品直传外部。

普通容器不作为恶意多租户代码的充分隔离。首选评估 gVisor；需要更强内核隔离或监管证明时评估 Firecracker。

## 9. 审计与对账

外部动作使用 outbox：先提交 action intent，再由 worker 认领、幂等执行、写 outcome，最后对账未知状态。高风险 intent 包含用户确认和策略快照。

审计事件进入独立存储账户和 sink，应用无删除权；PostgreSQL 只保存索引和事件引用。按风险采用签名批次、哈希/Merkle 链、对象锁和可选的第三方时间锚定。执行节点日志同样进入证据链，但先做秘密和个人数据过滤。审计 sink 不可用时，高影响动作 fail-closed，低风险事件只能进入有界、完整性保护的缓冲。

## 10. 子任务执行（ChildRun）

ChildRun 是 `WorkflowRun` 的下级执行单元，**不是新的授权主体**。它继承父 run 的 tenant、project、node 上下文，只在父 reservation 内消费，并记录 `parent_run_id`、`parent_node_instance_id`、`depth`、`purpose`。

### 10.1 权限派生（只减不增）

- capability token 从父 token 派生，只允许收缩；默认只持 A0/A1。
- 需要 A2/A3 的动作必须回到主对话走确认流程，子 agent 不得持有。
- 不得扩大并发、不得延长超时、不得调用父 node 未声明的工具。
- 不得自行 spawn 子 agent；首版深度硬上限为 1，放宽需 ADR。

### 10.2 回传信封（强制）

回传必须是结构化信封，**不是散文摘要**：

```text
artifacts[]  source_id / span / content_hash / parser_version / display_policy
claims[]     text / evidence_refs / kind: sourced | inference
taint        inherited / new_sources
status       ok | partial | failed | unknown
issues[]     code / claim_refs / source_refs / retryable / next_action
```

四条硬规则：自然语言只能是 `claims[].text` 且必须挂 `evidence_refs`；无来源推断必须标 `kind: inference` 且不得进入学习证据；taint 只能增加；`status: unknown` 时主对话不得推断结果，必须走对账。`issues[].code` 使用 knowledge 域 `EvidenceIssueCode`，不得用 prose 或 `core.ErrorCode` 代替证据问题。

**上下文可以丢，谱系不能丢。**

### 10.3 隔离范围

| 维度 | 是否隔离 |
|---|---|
| 主对话上下文 | 隔离（这是目的：省 token） |
| 证据谱系 | 不隔离（信封携带完整引用） |
| 审计记录 | 不隔离（旁路写入，不因压缩丢失） |
| 预算账本 | 不隔离（从父 reservation 派生并实时结算） |

### 10.4 适用边界

适合下放：上下文量大且只需结论、可并行且互不依赖、输出可结构化校验、失败可整体丢弃重来、只读（A0/A1）。

不下放：需要与用户多轮澄清、需要实时确认的副作用、需要连续上下文的教学对话、延迟敏感路径（L0 目标 P95 < 200ms）。

子 agent 启动开销约为「system prompt + 工具 schema + 必要上下文」的重建，量级 1–3k token；**预计任务本身消耗低于约 3k token 时不下放**。
