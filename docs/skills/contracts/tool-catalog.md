# L1 契约 · tool-catalog

> `skill_id: tool-catalog` | version: 0 | expiry: 2027-03-01 | owner: 待指派
> 权威来源：`specs/2026-09-17-03-policy-tools-and-execution.md` §4、`2026-09-18-05-agent-skill-context-and-tool-boundary.md` §5–§6
> **机器真相源是 tool registry；本文件是它生成的人读视图。** 禁止双写：任何工具定义只在 registry 里改，本文件由导出生成。

## 1. 生成区

<!-- BEGIN GENERATED: source=tool_registry, source_hash=sha256:d9116ace94dbf0f655bc81da1595cab864e66bb0934e37a51844f682cf3c1838, generated_at=2026-09-18T16:14:54Z -->
> 生成时间：2026-09-18T16:14:54Z

> 由 `tools/skills/gen_contracts.py` 从 tool registry 导出，请勿手工编辑本区。
> registry 是机器真相源，本区是它的生成视图；**禁止双写**。

registry_version: `sha256:fd6f8b3966abfa8f44307a6d15e920d8c42dec5bd1cc3fad5c6a3076cbf1e4a5`

### 工具

| tool_id | intent_tag | sink_class | owner_module | min_authority | exclusivity | idempotency | 成本上界 | 网络边界 |
|---|---|---|---|---|---|---|---|---|
| `append_project_evidence` | `append_evidence` | fact | learning | A2 | exclusive | native | 1 | — |
| `fetch_external_url` | `fetch_external` | context | knowledge | A1c | exclusive | native | 2 | docs.python.org, fastapi.tiangolo.com |
| `query_competency_graph` | `read_graph` | context | learning | A1a | exclusive | native | 4 | — |
| `read_source_span` | `read_source_span` | context | knowledge | A1a | exclusive | native | 1 | — |
| `retrieve_project_chunks` | `retrieve_chunk` | context | knowledge | A1a | exclusive | native | 3 | — |
| `run_in_sandbox` | `execute_code` | artifact | execution | A1d | exclusive | queryable | 1 | — |
| `run_validator` | `validate_artifact` | context | learning | A1a | exclusive | native | 1 | — |

### node

| node_id | 允许工具 | 最低档位 | 权限上限 | 工具次数 | 循环 | 递归 | 并发 |
|---|---|---|---|---:|---:|---:|---:|
| `diagnose_prerequisites` | `query_competency_graph` | L0 | A1a | 4 | 2 | 1 | 2 |
| `intake_goal` | — | L2 | A0 | 0 | 1 | 1 | 2 |
| `retrieve_material` | `retrieve_project_chunks`, `read_source_span`, `fetch_external_url` | L0 | A1c | 6 | 3 | 1 | 2 |
| `validate_and_record` | `run_validator`, `append_project_evidence` | L0 | A2 | 4 | 2 | 1 | 2 |

### 重叠检测结果

注册时机械门已保证：同一 `(intent_tag, sink_class, min_authority)` 下不存在多个 exclusive 工具。
若本表出现重复意图组合，说明门被绕过，应当视为构建失败。
<!-- END GENERATED -->

## 2. 手写区 · 工具声明的必填字段

| 字段 | 含义 | 作用 |
|---|---|---|
| `intent_tag` | 唯一的「动词 + 宾语」，如 `retrieve_chunk` | 重叠检测主键 |
| `sink_class` | 结果去向：`context` / `artifact` / `external` / `fact` | 区分同名不同用途 |
| `owner_module` | 唯一负责模块 | 禁止跨模块重复实现 |
| `min_authority` | 最低权限档位（A0–A3） | 权限裁剪 |
| `exclusivity` | `exclusive` / `composable` | 是否允许多工具完成同一 intent |
| 幂等等级 | 原生幂等 / 可查询对账 / 条件写 / 可补偿 / 不可安全重试 | 决定重试策略 |
| 边界 | 网络与文件访问范围 | 决定 capability token 约束 |
| 审计级别 | 是否需签名批次 | 决定审计强度 |

工具定义写在**代码里的 registry**，字段缺失即注册失败。

## 3. 手写区 · 首批工具候选（设计期推定，实施时以 registry 为准）

| id | intent_tag | sink | min_authority | 说明 |
|---|---|---|---|---|
| `retrieve_project_chunks` | `retrieve_chunk` | context | A1a | 项目知识库混合检索，结果在数据库内过滤 |
| `read_source_span` | `read_source_span` | context | A1a | 按 `source_id + span` 精确回读片段 |
| `fetch_external_url` | `fetch_external` | context | A1c | 经独立 Fetcher，强制 taint |
| `query_competency_graph` | `read_graph` | context | A1a | 读先修关系与组件定义 |
| `run_in_sandbox` | `execute_code` | artifact | A1d | 一次性环境，默认断网，消耗型读取 |
| `install_package` | `install_package` | external | A1d | 只经白名单 Package Proxy，禁用 lifecycle scripts |
| `run_validator` | `validate_artifact` | context | A1a | 确定性验证器 |
| `append_project_evidence` | `append_evidence` | fact | A2 | 只追加，不可修改 |
| `spawn_child_run` | — | — | — | **编排原语，不是普通工具**；只有 L3 可调用 |

## 4. 手写区 · 重叠检测（CI 机械门）

**规则：** 同一 `(intent_tag, sink_class, min_authority)` 组合下注册了多个 `exclusive` 工具 → **构建失败**。

**已知局限（必须知道）：** 这套规则只能拦「同一 intent 的重复实现」，**拦不住「语义相近但标签不同」的歧义**。例如上表中 `retrieve_chunk` 与 `read_source_span` 的标签不同、不会被拦截，但两者都能拿到内容片段，模型仍可能选错。

这类歧义只能靠两件事处理：第 5 节的 when-to-use 表，以及每季度的工具清单复查。**不要以为有了 CI 门就不需要人看。**

### 歧义修复顺序（不得跳步）

1. **合并** —— 最常见且正确的答案：它们本就该是一个工具。
2. **降为内部函数** —— 其中一个不该暴露给模型。
3. **用参数区分** —— 而不是用工具区分。
4. **补描述文字** —— 最后手段，且必须同时写入 when-to-use 表。

**反模式：** 一发现模型选错工具就加长描述。描述越长，schema 越贵，选择反而越差。

## 5. 手写区 · when-to-use 表（随 node 声明下发，不写死在 prompt 里）

| 场景 | 该用 | 不该用 |
|---|---|---|
| 需要「哪些资料提到了 X」 | `retrieve_project_chunks` | `read_source_span`（它要求已知确切位置） |
| 已知 `source_id` 与行范围，需要原文 | `read_source_span` | `retrieve_project_chunks`（会引入无关候选） |
| 需要外部最新资料 | `fetch_external_url` | 任何检索工具（检索只覆盖已摄取内容） |
| 需要运行用户代码验证 | `run_in_sandbox` | 直接执行（不存在该工具，这是刻意的） |
| 需要判断产物是否合格 | `run_validator` | 让模型自己说「看起来对」 |

## 6. 手写区 · MCP 包装层要求

复用 MCP 时，**协议复用、边界自建**：

1. MCP 工具名映射为内部 `tool_id`，参数 schema 以内部契约为准，超集参数一律剔除。
2. MCP 返回的一切文本视为外部内容，默认 `tainted`。
3. **无幂等声明的工具最高只给 A1**，不得进入 A2/A3。
4. 响应大小与超时上限在 registry 中声明。
5. `tools/list` 描述不可信：不得作为策略输入，也不得直接进入 system prompt。
6. schema 按需加载：只向被授权且可能使用的 node 下发，避免一个 server 的几十个工具定义挤占主对话上下文。
7. 不接受：远程代码执行类、无边界文件系统类、自动发布/支付类、无法固定版本类、需长期凭据且无法收窄 scope 类。

## 7. 手写区 · 常见坑

- **两个工具都能返回内容片段**，却都不写 when-to-use —— 模型只能随机选，且无法测试。
- **用工具名区分用途**（`search_v2` / `search_improved`）—— 名字不是语义，进不了重叠检测。
- **把 `spawn_child_run` 当普通工具开放给模型** —— 子任务派发是编排决策，不是模型自由选择项。
- **只给模型下发全部工具 schema** —— 单 node 常驻工具数应 ≤ 8；超出说明 node 职责过宽。
- **改了 registry 不改版本** —— 违反不变量 #13，运行无法归因。
