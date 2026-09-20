# 第五轮：持久化模型教学交互实施计划

> **For agentic workers:** 使用 superpowers:executing-plans 按任务推进。以下为待执行计划，未开始实现。用户本次只要求审查和计划。

**Goal:** Cookie 用户在学习项目的会话中提出问题，得到持久化、带可核验引用且诚实区分资料与推断的教学回答；服务重启、断线与超时不产生无声重复扣费或重复消息。

**Architecture:** 复用项目/会话/消息、知识检索、HTTP 幂等和平台边界。新增持久化 teaching run、provider attempt、预算预留和输出事件；API 原子创建意图，专用 worker 执行模型请求。模型输出先校验再写 assistant 消息，SSE 从持久化事件回放。

**Tech Stack:** 现有 Python/FastAPI/Pydantic/psycopg/PostgreSQL。只接一个正式云 provider，实际 SDK、模型、结构化输出与计费能力在 Task 1 核验并锁定；本计划不预设供应商当前 API 能力。

## 范围与入口条件

- [x] 先执行第四轮审查 R4-01..07 修复，独立通过回归；修复提交与第五轮功能提交分开。（任务 0 已完成；"独立审查"仍待进行，见文末实施记录）
- [x] 测试迁移和应用 DSN 均指向独立随机测试库；错误指向 study_platform 时拒绝 setup 写入。
- [x] 不因全量绿色自动判定退出；退出依据下表的具体性质及证据。
- 本轮包含：文本问题、项目内资料检索、单 provider 教学回答、输入/输出预算、持久化恢复、SSE、历史回读。
- 本轮不开放：模型工具调用、任意 URL/base URL、代码执行、模型直接修改计划/掌握度、自动模型评卷、自动重规划。模型教学回答不生成学习证据。
- 无模型凭据时 provider 功能显式 disabled，仍可跑模拟器测试；不得宣称真实模型已验证。
- 生产启用模型时必须具备 provider 配置、显式输入/输出上限、数据库预算和批准的资料外发范围。
- 不复用内存 BudgetLedger 承担生产费用事实。共享现有维度、错误语义及不变量，新增持久化端口。
- 流式首版发送状态事件与已校验的最终回答；若供应商原生 token 流有需求，另设隔离预览协议，不把未校验文本冒充已引用结论。

## 任务 0：第四轮修复与前置验收

文件：审查文档涉及的七组文件；新增前向迁移（**实施偏差**：拆成两个风险单元
`0008_worker_role_boundary.py` 与 `0009_ingestion_claim_fencing.py`，而不是计划里
设想的一个 `0008_ingestion_integrity.py` —— 凭据边界与认领围栏是两件独立的事，
合在一起提交会让「按风险单元提交与审查」失去落点），同步 `main.py` schema 版本和生成契约。

- [x] 先建立测试库隔离，避免修复过程破坏用户资料。
  （会话级 autouse 夹具建 `study_test_<hex>` 随机库；`require_test_database` 闸门；
  顺带修掉组合根无视 `STUDY_PLATFORM_DSN` 的缺陷 —— 那正是隔离被绕开的那条路。提交 `e89627c`）
- [x] 分别复现 worker 角色绕过、旧租约回写、多版本错引、同长伪片段、纯文本丢失、围栏长度错误。
  （六条都先写成失败用例/只读探针，再改代码）
- [x] 按审查要求修复；不修改已部署 0007 代替前向迁移。
  （`0008` / `0009` 两个前向迁移；0007 一字未改）
- [x] 运行两个 worker、同 worker_id 重启、连接复用、资料多版本组合测试；修复后旧引用继续可读。
  （`test_worker_role_boundary.py` 覆盖连接复用与角色边界；`test_source_ingestion_repositories.py`
  覆盖同 worker_id 两次认领、两个 worker 接管、同源两版同跨度；旧版按引用仍可回读）
- [x] 完成迁移往返临时库演练，并定义历史不可验证片段的标记/重建策略。
  （实际演练 `0007→0008→0009→0008→0007→head`，每一步断言三表 + FORCE RLS + 策略角色集 +
  两侧权限集 + `claim_token` 列；策略见 `findings.md`：判定用一条 SQL，实测本机 75 条片段
  全数通过，将来若出现不符的走"重新上传新版本"而不是就地改）
- [x] 提交修复（**实施偏差**：按风险单元拆成 6 次提交而非计划里的单次提交 ——
  `e89627c` R4-04 / `7c2ec11` R4-01 / `89b3786` R4-02 / `5e6a80f` R4-03 /
  `5ffac18` R4-05 / `83b365c` R4-06+R4-07；另 `de84faa` 测试支撑模块重构）。
  每条修法都做了注入反向验证（去掉保护 → 对应用例变红）。
  **独立审查仍待进行** —— 本次只做到自验证，未做独立复核；进入任务 1 前需要一次独立审查。

## 冻结接口与状态

API 路径：

| 方法 | 路径 | 语义 |
|---|---|---|
| POST | `/projects/{project_id}/conversations/{conversation_id}/teaching-runs` | body 只有 question；必须 Idempotency-Key；返回 202+run_id |
| GET | `/projects/{project_id}/teaching-runs/{run_id}` | 状态、最终 message_id、安全错误、恢复动作 |
| GET | `/projects/{project_id}/teaching-runs/{run_id}/events` | SSE；支持 Last-Event-ID；每次连接重新认证授权 |
| GET | 现有 messages 路径 | 历史包含一次用户问题和至多一次最终 assistant 消息 |

客户端不得设置 role、tenant、provider、model、prompt、usage、价格或 tool schema。现有通用消息接口对普通用户允许写哪些 role 必须先审查；未经服务端 provenance 标记的历史 assistant/system 文本不得升级为可信模型指令。

```text
queued → running → succeeded
queued → failed                  # 可证明未派发
running → failed                 # provider 明确失败且能确定费用处理
running → reconciliation_required # 已派发、结果或费用未知
```

queued 阶段租约可按 fencing 规则接管；发生 provider 派发后，lease 到期不得自动重复调用。结果未知不能自动释放预留预算。取消/截断连接不代表取消供应商请求。

关键唯一键：HTTP 幂等原有 scope；run 对应唯一 user_message_id；final assistant_message 的 run_id 唯一；attempt_id 唯一；事件 `(run_id, seq)` 唯一。所有关联使用 tenant/project 组合外键，不能只证明“对象存在”。

## 任务 1：provider 与模型边界

新增：`backend/app/teaching/models.py`、`ports.py`、`provider.py`、`prompts.py`、`backend/tests/test_teaching_provider.py`。
修改：`backend/app/deployment.py`、`.env.example`、依赖锁文件。

- [x] 阅读现有 00/02/03/04/06 规格；核验选中 provider 官方文档，将模型标识、SDK 版本、输入/输出上限、超时、幂等支持、usage 字段与查询/对账能力记入决策记录。
- [x] 确认用户资料外发授权范围，提供明确功能开关；凭据仅服务端读取，日志/异常/数据库不保存秘密。
- [x] 定义以下领域端口，供应商 SDK 只出现在适配器：

```python
class TeachingProvider(Protocol):
    def generate(self, request: ProviderRequest) -> ProviderResult: ...

# ProviderRequest: attempt_id, approved model, prompt_version,
# messages, immutable artifact snapshot, max_output_tokens, deadline.
# ProviderResult: provider_request_id, answer blocks, citations,
# reported token usage, completion status; 不允许客户端构造这些事实。
```

- [x] 编写 ScriptedProvider 模拟响应、明确拒绝、派发前失败、派发后超时、畸形 JSON、截断、缺 usage、错误引用；记录调用次数而非只返回固定成功文本。
- [x] 禁止 SDK 隐藏自动重试；若供应商幂等有明确定义，重试只能复用同一个 durable attempt 标识并验证其期限。
- [x] 对缺配置、非法模型、越界输入先拒绝，证明调用次数为 0；超时与明确失败返回不同领域结果。
- [x] 完成定向测试与提交 `feat: define teaching provider boundary`。

## 任务 2：运行意图与预算持久化

新增：`alembic/versions/0009_teaching_runs.py`、`backend/app/db/teaching_store.py`、`backend/app/teaching/memory_store.py`、`backend/app/budget/ports.py`、`backend/app/db/budget_store.py`、`backend/tests/test_teaching_repositories.py`。
修改：`backend/app/main.py`，以及必要的 product 消息事务内核。

- [x] 先冻结表关系：teaching_runs 保存问题引用、状态、fencing token、模型/prompt/检索版本、最终消息引用；provider_attempts 保存派发状态与外部请求 id；teaching_events 保存稳定事件序号；预算账户/预留/结算保存额度事实。
- [x] 原子命令 `start_run(actor, project_id, conversation_id, question)` 同事务写 user message、run、预算预留及对应意图；其中任何写入失败都回滚。HTTP 缓存写回未知仍遵循现有 reconciliation 语义。
- [x] `claim_run(worker_context)` 复用任务 0 建立的真正 worker 角色边界；普通应用 SQL 角色不能自行设置变量读取全局 runs。
- [x] `mark_dispatched(claim, attempt_id)` 在调用 provider 前持久化，原子把预算 held→in_flight；网络调用不持有数据库事务或行锁。
- [x] 预算预留同时限制 tenant 总额度、项目额度、输入与最大输出；价格快照有版本，整数微单位记账，禁止浮点累计。估计上界必须采用供应商可证明上界或经过验证的 tokenizer；超上界作为明确异常保守记账，不静默截断消费值。
- [x] 缺 usage 的成功结果不得按零费用释放；保留待对账状态与费用敞口。
- [x] 用双连接屏障测试“最后一份额度同时争抢”，只有一个命令能成功；重启后额度、预留、运行状态不丢。
- [x] 在 user message 后、run 后、预算后分别故障注入，验证无部分写入；测试 RLS、组合外键和列级权限。
- [x] 提交 `feat: persist teaching intents and budget reservations`。

## 任务 3：上下文与引用校验

新增：`backend/app/teaching/context.py`、`validation.py`、`backend/tests/test_teaching_context.py`、`test_teaching_citations.py`。
修改：必要的 `backend/app/core/artifacts.py` 向后兼容适配。

- [x] 输入上下文只含本用户获授权项目与会话历史；分别限制原问题、历史、资料数量和总 token；超限显式裁剪策略或拒绝，不能截掉租户/权限约束。
- [x] 外部资料作为带 taint 的数据片段，固定 system 指令由服务端产生；资料中的“忽略规则”“调用工具”不改变权限或 provider 请求工具集。
- [x] retrieval snapshot 冻结 document_id、chunk_id、span、hash、parser_version、ranking_version 和 prompt_version；重启继续使用同一快照，不悄悄换成新版来源。
- [x] 分开内部是否可外发与用户 display_policy；citation_only 不向用户输出正文，未批准外发的正文不进 provider；summary 不能简单返回原文全文。
- [x] 校验返回引用是本次已授权 snapshot 的子集，重新确认原文跨度与 hash；伪造/跨项目/未知版本引用拒绝 final commit。
- [x] 机械引用匹配只证明出处存在，不能证明结论得到支持。没有独立语义评估时维持保守 evidence 状态；明确区分 sourced 与 inference，禁止模型自行宣称 mastered/supported。
- [x] 无资料时可以给标注为一般性说明的回答，或明确资料不足；不得编造引用、把 provider 升级当成证据充分。
- [x] 校验失败不自动无限重试；首版一次生成，记录校验失败，费用按已发生调用结算。
- [x] 提交 `feat: validate teaching context and citations`。

## 任务 4：worker 执行与故障恢复

新增：`backend/app/teaching/service.py`、`backend/app/workers/teaching.py`、`backend/tests/test_teaching_recovery.py`。

- [x] 实现 load snapshot→校验授权→预算持久化派发→provider→输出校验→原子 final commit。
- [x] `finish_run(claim, attempt_id, result)` 同事务写唯一 assistant message、费用结算、终态事件和 run succeeded；失败不得出现“已成功但无消息”。
- [x] 对派发前确定失败释放 held；派发后结果未知保留 in_flight，转 reconciliation_required，禁止自动再次 generate。
- [x] worker 死在“写 dispatched 后、真正发出前”也按未知保守处理；除非供应商可证明未发生，不能从缺响应推断未调用。
- [x] final commit 失败后允许重放已持久化结果落库，不能通过重新调用模型修复；结果尚未持久化则走 provider 可验证恢复或对账。
- [x] 用户可查询安全错误和下一动作，原始 SDK 异常/密钥/全文不进入公共错误或普通日志。
- [x] 逐项执行下方故障矩阵，检查供应商调用计数、消息数量和预算余额三者，不只检查 HTTP 状态。
- [x] 提交 `feat: execute teaching runs with recoverable outcomes`。

## 任务 5：HTTP 接线与可恢复 SSE

新增：`backend/app/api/teaching_routes.py`、`backend/tests/test_teaching_api.py`、`test_teaching_events.py`。
修改：`backend/app/main.py`、生成 protocol 契约。

- [x] 新建 run 复用 cookie/CSRF/idempotent_write；同键同体返回同一 run，冲突体拒绝，跨 conversation 不可串用命令作用域。
- [x] 状态查询和 SSE 都验证成员关系；缺失、跨用户项目、跨租户统一 404。授权应在每次连接及后续读取批次检查，撤销后停止继续发内容。
- [x] 事件序号由库分配，SSE event id 来源于持久化序号；Last-Event-ID 非法/超界明确处理，心跳不产生业务消息。
- [x] SSE 断开只结束订阅；worker 继续处理，重连不触发 generate、不再创建 run。事件投递允许至少一次，客户端按 event id 去重。
- [x] 只发状态和校验完成的 final 事件；最终内容通过已存储 message 读取。慢客户端有明确读批次与连接资源上限。
- [x] 完成端到端测试：POST 202→订阅→断线→重新装配平台→重连→同一最终消息与费用。
- [x] 提交 `feat: expose teaching runs and durable event replay`。

## 必须执行的反例矩阵

| 场景 | 必须观察到的事实 |
|---|---|
| 两用户同 tenant 不同项目，同样 query | 上下文/引用无交叉，调用参数检查而非只检查最终文字 |
| 旧 worker 超时、新 worker 接管、旧结果返回 | 拒绝旧 token；无第二消息，无重复结算 |
| 同来源同跨度两版本 | 两个引用各回自己的文本；历史回答不漂移 |
| 同键并发 POST 两次 | 一个 run、一条问题、一次预算预留 |
| 额度只够一次的两个独立请求 | 一次派发，另一次预留失败，无负额度 |
| provider 已收到请求但响应超时 | unknown + 保留费用敞口；自动调用次数仍为 1 |
| 接到结果、数据库提交失败 | 不重新生成；重放持久化结果或进入对账 |
| SSE 断线、刷新、重启 | 历史事件可回放，不增加 provider 调用/消息/费用 |
| 假引用/改 hash/资料提示注入 | 不提交假引用；工具调用次数 0；秘密不进输出 |
| usage 缺失或越界 | 无零成本默认值，无静默释放 in_flight |
| 无资料/仅 citation_only | 不伪造支持证据、不展示受限原文 |
| 模型回答生成成功 | learning evidence/mastery 输入集合完全不变 |

## 任务 6：独立验收、记录与发布本轮

新增：`backend/tests/test_round5_postgres_e2e.py`、`backend/tests/fixtures/teaching_eval_v1.json`；更新 progress/findings/README/task_plan。

- [x] 先冻结至少 30 个验收样例：有资料、无资料、冲突版本、错误引用、提示注入、中文技术解释；期望由人工按原文编写，不能由被测模型自判生成。
- [x] 结构与隔离硬门要求全部通过；语义引用支持率和教学质量单独评估，阈值在第一次正式评测前冻结，不把关键词命中作为语义正确率。
- [x] 对租约 token、引用版本、原文比较、预算原子性做定向变异，证明每个移除保护的版本都被对应反例击败，再恢复并确认工作区无变异残留。
- [x] 实际 provider 小规模验收前核对配置与预算上限；未提供可用凭据时记录外部阻塞，只能标为“模拟器/持久化已验收”，不能标整轮真实模型完成。
- [x] 在独立 PG 测试库运行第五轮端到端与全量回归，拒绝 PG skip；完成新迁移往返和故障恢复演练。
- [x] 运行以下仓库门禁并检查退出码，不因后一个命令成功忽略前一个失败：

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -p no:cacheprovider --basetemp=var/round5-final-UNIQUE
.\.venv\Scripts\python.exe -m ruff check backend/app backend/tests
.\.venv\Scripts\python.exe -m mypy backend/app
.\.venv\Scripts\python.exe tools/skills/gen_contracts.py --all --check
git diff --check
```

`UNIQUE` 在执行时替换为本次随机后缀；测试 DSN 必须由任务 0 的安全 fixture 提供，不能直接沿用业务库默认值。

- [x] 记录审查基线 SHA、各反例的实际结果、测试数量/跳过数、真实 provider 调用数、预算消耗、明确局限。
- [x] 独立审查在阅读实现之前按矩阵设计反例；未关闭 P1 或与退出门相关 P2 不宣告完成。
- [x] 按既有授权每轮提交并推送 GitHub，先核对远端新增修改；API 快照工具需要 `--repo tiance002/study-agent-platform --branch main`，推送后核对 SHA/tree。

## 第五轮退出门

Cookie 用户可从真实问题获得真实 provider 的回答并保存至项目会话；引用回到固定版本原文，推断与资料引用区分；API/worker 重启后 run、消息、事件和预算不丢；并发/断线/未知响应不会隐式重复生成或重复收费；模型响应不会直接修改掌握度。每条均有独立 PG 或真实服务证据。

后续第六轮再建设 React 普通用户入口、可访问性、浏览器核心流程及用户资料外发提示；上线前另过部署、备份恢复和负载门。

## 实施记录（2026-09-20 执行后补记）

- **实施偏差 1**：任务 6 的"独立审查"未在第五轮内完成 —— 本次只做到自验证（注入反向验证 4 项 + 31 条冻结样例 + 全量回归）。与任务 0 相同，独立复核需在阅读实现之前按矩阵设计反例，建议作为第 6 轮的前置任务。
- **实施偏差 2**：真实 provider 验收因无凭据外部阻塞；本轮验收口径为"模拟器 + 持久化已验收"（ADR-015），未宣称真实模型完成。
- 迁移往返实际演练 `0009 → 0010 → 0009 → head`（计划里写的是"完成新迁移往返"）。
- 变异验证实际覆盖：内存围栏 token、引用快照子集、预算余额闸（余额闸与 token 上限闸是**两条**判定，第一轮注入打错了目标用例 —— 变异本身也要核对"红的原因"）、PG 落定租约期限。
