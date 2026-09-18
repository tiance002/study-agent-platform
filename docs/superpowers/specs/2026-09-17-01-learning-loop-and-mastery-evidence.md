# 学习闭环与掌握证据

## 1. 不变量与教学原则

1. `EvidenceEvent` 是掌握相关事实源；掌握状态只能由证据投影得到，任何 API、模型或人工界面不得直接写掌握状态。
2. 首版不输出概率数值；置信度使用 `insufficient / low / medium / high` 四档，规则版本化并绑定 independence group 的数量和多样性。
3. 产品执行证据和学习掌握证据分开建模。产品运行成功不自动等于用户掌握。
4. 平台只能声明“在观测范围内的独立验证”，不能证明用户在远程环境未使用外部工具。
5. 反作弊信号只触发补充验证，不自动处罚、降级或判定作弊。

平台采用 project-first / learning-by-doing：真实产品目标驱动产品里程碑和能力里程碑，学习内容在需要时补齐并立即应用。

## 2. 能力图谱与版本

能力使用组件级 `CompetencyComponent` 表达。稳定概念与版本绑定实现分开，例如“幂等语义”与某框架 API 不得放进同一个不可迁移节点。

能力图谱包含定义、先修边、适用技术版本、有效期、任务映射和 rubric 引用。图谱、组件、边和等价关系均有 `graph_version`。

首版使用 PostgreSQL 关系表保存显式先修图谱，不引入独立图数据库。Agent 工程初始域包括 Python/TypeScript、React、API/异步任务、PostgreSQL、缓存、LLM、工具调用、RAG、harness、工作流、安全、沙箱、并发和产品验证。

图谱演进使用 `unchanged / rename / split / merge / deprecated / incompatible`。升级前 dry-run，输出受影响用户、需补测项和潜在状态变化；旧投影保留旧 graph version，新投影按等价关系重算，迁移可回滚且不打断正在进行的任务。

## 3. 学习任务模式

| 模式 | 允许帮助 | 目标 | 说明 |
|---|---|---|---|
| Guided Practice | 讲解、提示、示例、共同编码 | 学会方法 | 默认最多形成 practiced 贡献 |
| Assisted Assessment | 有限提示，全部记录 | 检查部分独立能力 | 合同允许时可形成 demonstrated 贡献，帮助程度会降低独立性贡献 |
| Independent Verification | 平台内不提供实质答案或提示 | 验证独立解决与迁移 | 仍只代表平台观测范围 |

模式只提供合同上的默认上限。实际 `help_level` 取声明、平台观测和任务界面默认内容的保守值。界面中的示例、自动补全、错误提示、上下文摘要和隐式脚手架必须在任务开始时记录。

Guided 任务只有在 `AssessmentContract` 事前声明“无提示完成可产生独立性贡献”时，才允许因实际无帮助突破默认上限；普通练习不能事后追认为 assessment。

## 4. Assessment Contract 与题目映射

`AssessmentContract` 在任务开始前冻结：

```text
contract_id / supersedes
competency_graph_version / competency_component_ids
task_version / task_structure_version
rubric_version / validator_versions
assessment_mode / allowed_help / allowed_tools
evidence_observation_scope
resource_limits / accessibility_extension
expected_evidence / impact_level
```

Contract 执行中的 `help_level_observed`、脚手架快照、环境状态和结果写入 `AssessmentExecution`，不能回填不可变 Contract。

`CompetencyTaskMapping` 是不可变版本链：

```text
mapping_id / mapping_version / supersedes_mapping_version
competency_component_id / task_version / rubric_version
evidence_type / coverage_dimension
minimum_independence / maximum_help_level
valid_from / expires_at / author / reviewer / publisher
```

无有效 mapping 的证据不能进入掌握投影。高影响 mapping 按 impact 分档要求独立 reviewer 和 publisher。

## 5. 证据模型与质量维度

核心实体：

```text
EvidenceEvent                  append-only 事实
EvidenceCorrection             追加裁决，不修改事实
EvidenceCompetencyContribution 按证据×组件裁决
AssessmentExecution            任务运行观察
ProjectionCheckpoint           可丢失性能优化
MasteryProjection              可重建读模型
MasteryPresentedEvent          用户实际被发出某视图的记录
```

每个证据×能力组件独立裁决四个维度：

```text
assessment_validity   任务是否测量目标组件
observation_strength  平台能观测多少，使用 OBS-1..OBS-5
source_reliability    参考资料的权威性、时效性与版本可信度
independence          帮助程度、跨资料综合和独立完成情况
```

四维门槛的后果不同：validity 不通过则组件贡献无效；observation 不足则限制可支撑档位；independence 不足可计 practice 但不能计独立水平；reference reliability 不足则该资料驱动的贡献为 inconclusive。不能用一个总 verdict 代替组件级结果。

## 6. Evidence 与 Mastery 投影

掌握状态拆成独立水平、迁移观测、时效、争议和用户放弃标记：

```text
independence_level: unknown | introduced | practiced | demonstrated
transfer_observation: 独立关联记录，含维度、帮助条件和证据引用
freshness: 展示层根据 last_valid_evidence_at + 复测规则计算
disputed: active / reason / review_id
withdrawn: 用户主动放弃，不表示成功或失败
confidence: insufficient | low | medium | high
```

`freshness` 不进入纯投影，避免读取当前时间破坏重放；展示层计算并记录计算时间。`as_of_evidence` 是按证据截止点重算，`as_of_issued` 是用户当时收到的视图。后者由 `MasteryPresentedEvent` 保存，不是掌握状态事实源。

Projection Worker 是 `MasteryProjection` 的唯一写入者；API、模型和人工界面没有该表写权限。投影输入包括 Evidence 事件和组件级裁决状态，规范化后生成 `projection_input_set_hash`。

增量重算以 `independence_group` 为最小单元。每组维护输入 hash；迟到事件或 correction 只重算受影响分组及其组件，再更新全局 hash。规则不可增量组合时必须整组全量重算，对账只用于告警。

投影记录 `event_seq_watermark`、`projection_algorithm_version`、`graph_version`、`mapping_version`、`validator_versions` 和 `projection_run_id`。投影算法禁止使用随机、当前时间、实时 ACL、外部模型和未进入 hash 的配置。

## 7. 失败筛选、归因与复测

筛选层判断证据是否有效：环境/沙箱故障、验证器故障、题面歧义、资料冲突导致 `valid / inconclusive / voided / attribution_pending`。

归因层只处理有效失败，综合任务结构、帮助程度、先修状态、任务族历史、用户当时状态和资源限制，输出知识缺口、方法选择、任务过大或其他原因。

失败事实保留；`validated_negative_contribution` 才会进入负向投影。有效执行失败的 OBS-1/OBS-2/OBS-3 可以形成负向贡献；OBS-4/OBS-5 对需要执行证据的组件通常为 inconclusive，但对有效的概念解释任务仍可形成正/负贡献。

`attribution_pending` 不计入当前投影并强制复测：

```text
复测通过 → 新证据 + 原贡献 superseded
复测失败 → 新独立组负向贡献
超时 → indeterminate，不参与投影，进入申诉/未决看板
```

低影响、有效任务且环境正常时，非故障复测最多两次；高影响或归因不确定转人工工作台，不设统一次数上限但有 SLA；环境/题目故障不消耗复测次数，连续故障触发 task family 下线修题。复测触发、人工裁决和 correction 都是 append-only 事件。

## 8. 题目生成、防伪与质量

首版只做个体内形成性评价，不排名、不输出跨用户可比的难度分数。保留 `challenge_profile_version` 记录题目涉及组件数、跨文档、迁移要求、资源限制和默认帮助，不称为难度校准。

题目按能力组件取样，不按资料量取样；题目必须回链资料片段并经过生成/评价分离的 validator。LLM 评价结果作为版本化事件，不能在投影重算时重新调用模型；人工或规则裁决追加 correction。

原创性与过程证据包括需求到修改的演进、随机化约束下重做、设计解释、迁移任务、沙箱轨迹、允许工具和 AI 使用声明。平台提供 `open-assistance / limited-assistance / independent` assessment mode；相似度、突然出现的大段代码和解释不一致只触发补测。

错题集是教学工具，不是投影输入；复测通过后追加证据和 correction，不删除原失败事实。

## 9. 诊断退出与失败干预

诊断满足关键先修覆盖、任务/时间上限、剩余关键组件缺口低于代理阈值、用户选择先做项目或连续发生任务/环境故障之一即可结束。自评只能减少题量并形成已声明假设，不能提升掌握状态。

失败响应依次区分知识缺口、方法选择、任务过大、环境故障、任务/rubric 缺陷和持续失败。降低门槛必须同时给出升门槛路径；长期开改变属于 D2，须展示原因、影响和回滚入口。归因对用户可见并允许推翻。

## 10. 影响度、公平和生命周期

D0 仅影响展示，D1 影响当前建议，D2 改变长期路径，D3 影响证书、排名或对外记录。D 轴与 A 权限轴通过策略矩阵复合，不简单取最大值。

高影响 assessment 需要人工复核、申诉、完整审计和稳定 rubric。教学实验需等价基线、停止规则、伤害监测和适用的知情同意。首版目标为 WCAG 2.2 AA，并支持低带宽、低端设备、键盘/读屏、字幕、焦点和时限延长。首版 UI、教学和评测以中文为主，保留英文技术术语和代码标识符。

Evidence、contract、mapping、投影和展示记录均有生命周期版本。DSR 采用字段级加密擦除与谱系删除；删掉个人字段后必须验证投影仍可按保留事实重建。

## 11. 必须通过的性质测试

- EvidenceEvent、Correction 只能 INSERT，应用角色不能 UPDATE/DELETE。
- 同一事件重复处理，投影不变化。
- 事件任意到达顺序、迟到 correction 与全量规范回放一致。
- pending 复测通过只 supersede 贡献，不删除失败事实。
- 复测失败归属新的 independence group。
- validator 对同一证据按组件给出不同结果时，只影响对应组件。
- `as_of_evidence` 重算不改变当前投影；`as_of_issued` 只读取展示事件。
- 删除个人字段后，投影仍可重建且原文不可见。
- 未达样本门槛不输出伪精确概率。
- `negative_evidence_invalidated_ratio`、`indeterminate_rate`、`pending_age_p95` 按原因和任务族分层监控。

