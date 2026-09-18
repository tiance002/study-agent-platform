# 路由、检索与质量

## 1. 执行档位

| 档位 | 执行者 | 允许范围 |
|---|---|---|
| L0 | 确定性程序 | 导航、状态读取、解析器、规则和数据库查询 |
| L1 | 本地小模型 | 闭集分类、查询改写候选、受约束字段抽取 |
| L2 | 云端模型 | 教学解释、规划、架构、深度代码分析、多资料综合和工具计划 |

本地模型不生成最终高质量教学内容。默认 L2，L1 是必须通过白名单与验证器的例外。模型档位与工具权限正交。

## 2. 路由流程

1. typed node 已由注册表确定，未知 node 不进入模型路由。
2. L0 能确定完成时直接执行。
3. L1 仅在任务类型、输入边界、输出 schema、数据来源、本地容量和任务级验证器全部满足时准入。
4. 其他情况进入 L2；需求缺少不可替代的用户选择时先澄清。
5. 档位 floor 只绑定 workflow run 或任务分支并设置 TTL，不绑定整个会话。

切换模型时使用结构化项目状态与会话摘要，而不是依赖某个模型的隐式上下文。档位切换成本包含 prompt cache、KV cache、上下文序列化和冷启动损失。

### 2.1 上下文装配与按需加载

上下文分四类，压缩规则不同：

| 类别 | 内容 | 可否压缩 |
|---|---|---|
| 指令 | system prompt、node 声明的 skill | 否，按 `node_registry_version` 固定 |
| 事实 | 项目状态投影、掌握度快照 | 可压缩，只保留投影结果 |
| 证据 | 检索片段、工具输出、artifact 引用 | **只替换为指针，不得改写文本** |
| 会话 | 消息历史 | 可压缩，按会话摘要机制 |

- 工具 schema 与 skill 按需加载：只向当前 node 下发被授权且可能用到的部分。**单 node 常驻工具数目标 ≤ 8**，超出说明该 node 职责过宽。
- 低权限 node 不接收无关工具描述；该规则同样适用于子 agent 与 MCP 包装层。
- **压缩证据文本会产生新的模型派生值**：它继承 taint（不变量 #10），并破坏引用溯源与 `display_policy` 判定。因此证据只能换指针，不能改写。

## 3. 检索管线

```text
原始问题
→ 保留原文的查询候选生成
→ 权限与项目过滤
→ 文档/章节粗召回（可选，不得成为必经门）
→ 候选文档内子片段检索
→ 全局关键词 + 稠密向量检索兜底
→ 融合
→ rerank（仅在通过评测门后启用）
→ 去重与版本/来源聚合
→ 证据充分性和冲突分析
→ 引用装配
```

查询改写只能增加候选，不能覆盖原问题、代码、错误信息和版本。标题短、抽象或不规范时可能无法召回，因此标题/章节召回只用于缩小高置信候选范围，**不得过滤掉全局片段检索**。父文档/章节与子片段结果使用 RRF 或经评测验证的融合方法合并；不得用未经标定的单一相似度阈值决定“有无知识”。

### 3.1 结构化摄取与标题规范

用户上传资料、Fetcher/MCP/仓库读取的资料和模型派生资料进入同一个 `DocumentProcessor` 边界。该边界只解析结构、生成元数据和切块，**不得改写证据原文**。无法解析的格式显式标记失败，不把模型猜测的标题冒充原始标题。

文档和片段至少携带：

```text
document_id / source_id / document_title
heading_path[] / heading_level / section_id / parent_section_id
chunk_index / content_type / language
version / observed_at / content_hash / parser_version
taint_sources[] / acquisition_method / derived_from[]
```

三个来源概念必须分开：

- `taint_sources[]` 直接使用 `policy.taint.TaintSource` 闭集，参与 taint 与 endorsement；禁止另造 `external_fetch`、`agent_generated` 等同义值。
- `acquisition_method` 只表示内容如何进入系统，例如 `upload`、`web_fetch`、`repo_read`、`mcp_call`；它不参与信任判断。
- `derived_from[]` 保存父 artifact/value 引用，描述谱系而不是获取渠道。

原样抓取的网页正文只带 `WEB`；模型基于网页生成的新文本同时带 `WEB` 与 `MODEL_OUTPUT`。Agent 仅负责抓取时不是新的内容来源。模型边界必须显式增加 `MODEL_OUTPUT`，通用确定性派生不得无条件增加该来源。

切块优先尊重标题、段落、代码块、表格和列表边界，并保存父章节引用；超长章节再按语义和长度约束切分，相邻块可保留受限重叠。片段引用必须能通过 `source_id + span + content_hash + parser_version` 回到原文。

### 3.2 分阶段检索与评测门

检索能力按以下顺序上线：

1. 结构化标题路径 + 中文关键词检索，建立原始查询、相关文档、相关片段和核心结论的标注集。
2. 增加稠密向量候选并以 RRF 等确定性方法融合；保留关键词和全局片段兜底。
3. 只有离线标注集证明 recall@k、nDCG/MRR 或面向结论的证据覆盖率提升，且关键分层无显著回归，才接入 reranker。

每次分词器、embedding、切块、融合、reranker、索引或提示词版本变化都必须重跑冻结评测集。GraphRAG 仅在跨文档实体关系成为可测瓶颈后评估，不能替代普通混合检索。

知识库主检索位于 PostgreSQL/pgvector；中文关键词索引方案需在自有语料基准上确定。显式先修关系使用关系表。

## 4. 证据状态

检索结果输出结构化状态：

- `supported`：核心结论有充分且一致的证据。
- `partially_supported`：明确列出已支持结论、推测和缺失证据。
- `conflicting`：展示冲突、来源权威性、版本和时间，不能静默选择。
- `insufficient`：扩大检索、换查询、请求资料或拒绝下结论。

证据不足不通过升级更强模型解决。只有证据足够而推理/表达能力不足时才升级执行档位。

状态不能用“是否存在 hit”直接决定，至少综合候选相关性、核心结论覆盖率、来源一致性、新鲜度与必需检索步骤是否完成。每个状态同时返回结构化 `issues[]`：

```text
EvidenceIssue
  code: EvidenceIssueCode
  claim_refs[]
  source_refs[]
  detail
  retryable
  next_action
```

`EvidenceIssueCode` 是 knowledge 域闭集，不复用 `core.ErrorCode`，因为“没有候选”和“证据冲突”不是平台执行错误。首批至少包含：

- `NO_CANDIDATES`
- `LOW_RELEVANCE`
- `MISSING_SUPPORT`
- `SOURCE_CONFLICT`
- `FRESHNESS_UNKNOWN`
- `SOURCE_FETCH_FAILED`
- `SCOPE_BLOCKED`
- `TOOL_RESULT_UNKNOWN`

`retryable` 不能统一从工具 outcome 推导：`SOURCE_FETCH_FAILED`、`TOOL_RESULT_UNKNOWN` 等执行类问题按 `ToolOutcome.status` 和错误策略映射；其中 `unknown` 在对账完成前必须为不可自动重试。无候选、低相关、缺少支撑、冲突和新鲜度问题按各自检索恢复规则判定。`SCOPE_BLOCKED` 不得泄露未授权资源是否存在。

现有自然语言 `unresolved[]` 迁移为 `issues[]`；展示层可以生成说明文字，但稳定判断、指标和审计只读取结构化字段。只要未完成步骤影响核心结论，就不能返回 `supported`；可明确隔离已支持结论与缺口时返回 `partially_supported`。

## 5. L1 准入与验证

L1 每种 node 单独定义数据集、验证器、风险和 coverage，不设置统一“复杂度分数”。主指标包括：

- `unsupported_node_accept_rate`
- `intent_misclassification_rate` 与拒识率
- `rewrite_retrieval_regression_rate`
- `extraction_field_error_rate`
- `escalation_loop_rate`

在冻结的数据集与验证器版本上，要求风险上置信界不超过该 node 的 `epsilon`，然后最大化 coverage。置信度校准使用 reliability curve、Brier score 或 log loss；risk-coverage 是选择目标，不是校准工具。

低样本高风险类别不合并放宽，保持 L2。模型版本、量化方式、提示词或 schema 变化均触发重新标定。

## 6. 在线质量闭环

质量信号组合：确定性验证、任务 rubric、人工盲审、行为信号和少量经授权的双跑。行为信号只能作为错误下界和告警，不能计算准确率。双跑不是金标准，并须脱敏、预算控制、隐私说明和 opt-out。

抽样按规则、node、模型、语言和上下文桶分层，并考虑同一用户/项目请求相关性。序贯监控使用适当的重复检验控制，达到最小样本前不自动放宽。

## 7. ANN 与精确检索选择

租户和项目过滤必须在数据库内执行。基于作用域行数估计、过滤选择性、历史候选数、延迟和召回选择精确或 ANN；不得将跨租户候选拉到应用层再过滤。

切换采用双阈值、冷却期和分区数硬上限。多数小项目共享有限哈希分区；只有持续超大作用域才离线迁移到专用分区。ANN 使用 iterative scan 等数据库能力补偿过滤后的候选不足。

## 8. 缓存

只缓存可明确作用域和失效条件的结果。缓存键和值均携带 tenant、project、知识库版本、模型和提示词版本；读出不匹配立即丢弃并告警。默认禁止跨项目语义缓存。高影响掌握结论、权限决定和外部动作计划不使用语义缓存。
