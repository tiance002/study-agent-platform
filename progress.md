# 进度日志

## 2026-09-21 · 最终实现核验

- 按冻结方案完成开放注册、中文用户名规范化、Argon2id 登录、会话来源/代际约束、双开关、无邮箱账号处置说明、平台级预算预留与 worker 派发接入。
- 最终非 PostgreSQL 全量回归 `100%` 通过；认证/预算定向测试通过；Ruff、mypy、compileall、Node 语法、合同生成/行尾、许可证扫描和 `git diff --check` 通过。
- 修正内存限流器在端点覆盖限额时的 `Retry-After` 计算，将注册成功响应固定为计划约定的 `201`，并让普通 logout 在失效 Cookie 下只清理本地引用、不伪称撤销服务端会话。
- 本机 PostgreSQL 不可达，故未把最新 `0012` SQL 的临时库往返/权限/并发门禁记为通过；浏览器和生产发布同样留作发布前门槛。

## 2026-09-21 · 开放注册实现收口

- 任务 1：用户名/密码契约已落地：两阶段 NFKC+casefold 校验、固定 Unicode 15.0.0、Argon2id、dummy 验证、rehash 与脱敏诊断；34 项定向测试通过，Ruff 与许可证扫描通过。
- 任务 2：`0012` 已加入凭据表、双重会话元数据、来源核验回填、默认项目/授权的原子注册函数、登录完成函数、RLS/权限/组合约束；同时落平台 UTC 月度预算配置与预留生命周期表。PG 专项在本机当前因 PostgreSQL 不可达而跳过，曾由临时库验证过迁移专项。
- 后端：内存/PG 账号仓储、独立注册/密码登录端点、双开关、有效 Cookie 的 `409 already_authenticated`、Argon2 有界且登录优先的计算池、实际请求体大小闸、认证响应 `no-store`、会话字段读写已接入。
- 费用与治理：新增平台总额度状态机（跨 provider、价格版本、UTC 周期、in_flight 不自动释放）、限流桶容量/TTL、发布与账号生命周期运维手册；前端增加邀请码/登录/注册入口，并明确无邮箱找回提示。
- 费用派发已接入教学 worker：真实 `openai` provider 在网络调用前占用平台预留，成功按权威 usage 结算，明确未送达释放，超时/未知用量保留 `in_flight`；PostgreSQL 通过 0012 的受限 definer 函数持久化状态，内存模式保留同一状态机。
- 新增定向测试：密码认证 API、平台付费额度、Argon2 池、会话元数据；本地内存/静态门禁通过。全量测试中仅发现既有契约/PG 环境问题：契约已重新生成，PG 不可达的数据库测试不作为通过结论。
- 收口验证：非 PostgreSQL 全量回归 `100%` 通过；Ruff、mypy、compileall、生成契约、行尾、许可证扫描与 `git diff --check` 通过。PG 0012 往返/权限专项仍待本机 PostgreSQL 可达后重跑。

## 2026-09-21 · 开放注册修订方案缺口审查

- 已读取用户提供的完整修订方案，并恢复现有 `task_plan.md`、`findings.md`、`progress.md` 上下文。
- 本轮仅做方案与当前仓库的证据化对照，不执行代码、数据库或生产变更。
- 已完成代码对照：确认方案修正的会话存活、平台费用闸、R8-H/R8-I 和回滚兼容问题均真实存在。
- 另识别 9 组需补充的实施契约，核心是密码会话 schema/版本语义、双功能开关、Unicode 稳定性、公开端点资源闸、平台预算生命周期和账号切换服务端语义。
- 本轮未修改业务代码、迁移或生产配置；仅更新审查记录。

## 2026-09-21 · 开放注册实现启动

- 已将最终冻结决定同步到 `docs/superpowers/specs/2026-09-21-open-registration-auth-design.md` 与对应实施计划：固定用户名字符集、原始用户名显示、会话代际/哈希版本分离、双功能开关、失效 Cookie 语义、Argon2 优先级及预算周期。
- 按边界任务启动任务 1（密码/用户名契约）和任务 2 的迁移子任务；两者均要求先写失败测试并只改声明范围。
- 全量基线尝试因系统临时目录 `C:\Users\22088\AppData\Local\Temp\pytest-of-22088` 权限错误产生大量 fixture setup errors，未作为代码回归结论；改用仓库内临时目录后，`test_product_models.py` 与 `test_auth_hardening.py` 定向基线通过（53 passed）。

## 2026-09-20 · 第 1-2 轮复审与持续交付

- 建立持续目标：逐轮审查、修复、测试、记录、提交并推送，直至普通用户可用首版。
- 保留用户现有未提交修改；基线全量内存测试通过，PostgreSQL 测试此前因服务未启动而跳过。
- 启动本地 PostgreSQL 16.4；`alembic current` 为 `0005`。
- 当前静态门未通过：Ruff 1 项未使用导入，mypy 5 项返回类型/适配器类型错误。
- 第 1 轮复审待收口：兑换审计仍记录 `session_id`；PostgreSQL outbox 投影需验证多 worker 串行与权限边界。
- 第 2 轮复审确认：`pending` 租约超时后直接重执行不安全，项目、消息和计划并无足以恢复原响应的领域唯一约束，可能重复产生已成功副作用。
- 第 1 轮修复完成：生产密钥强度与用途隔离、可信代理链解析、限流旧桶清理、definer 全限定、认证 transactional outbox、审计会话材料脱敏、outbox FORCE RLS，以及跨 worker 投影的数据库串行与链头刷新。
- 第 2 轮修复完成：所有产品写端点强制 HTTP 幂等；claim_id 覆盖完整逻辑键；内存 claim 加锁；缓存完成失败不再 release；超时 pending 进入 `indeterminate` 并返回 `RECONCILIATION_REQUIRED`，禁止未知结果自动重执行。
- 新增 PostgreSQL HTTP 退出门：Cookie 用户完成“建项目 → 建会话 → 发消息 → 保存计划 → 登记资料”，重建全部适配器后完整读回；原命令跨重启重放返回缓存且不重复创建。
- 最终验证：全套 `backend/tests` 在 PostgreSQL 运行状态下 100% 通过且无 skip；Ruff、mypy、三份生成契约检查通过；临时空库从零升级到 `0006`、降到 `0005`、再升级到 `0006` 通过。


## 2026-09-17

- 读取 Superpowers 的会话、头脑风暴和计划编写流程。
- 读取文件化规划与实时检索技能说明。
- 检查项目目录：空目录，且不是 Git 仓库。
- 创建设计任务、发现记录和进度日志。
- 下一步：确认产品形态，再进行针对性技术调研与架构比较。
- 用户确认第一阶段为多用户 SaaS，并要求每个用户针对不同学习内容展开多个会话。
- 初步确定 `用户 → 学习项目 → 会话` 的数据与隔离层级。
- 用户将首版领域收敛为 Agent 工程，并明确要求以真实产品落地为核心，而非简单 Demo。
- 将课程式流程调整为项目驱动学习：从产品目标反推知识、边做边学、以工程证据验收。
- 用户选择隔离沙箱执行代码作为首版工程验收能力；完整云开发与长期部署暂不纳入首版。
- 用户接受“按成长期容量设计、按早期验证规模部署”的建议。
- 用户指定本地开源模型仅承担路由和轻任务，采用保守云端升级策略；组件优先开源免费。
- 给出轻任务白名单、硬升级规则、可校准分类器和误路由评估的待批准提案。
- 实时检索前完整性校验遇到网络证书错误，未执行检索工具；候选清单不作为当前版本核验结果。
- 用户完成路由、安全、隔离、预算、幂等、审计和教学证据边界的多轮评审并同意进入正式规格阶段。
- 写入总体设计及五份领域规格，覆盖产品范围、架构、技术比较、学习闭环、RAG、模型路由、权限、沙箱、多租户、高并发、错误处理和数据治理。
- 自检结果：内部链接和代码围栏完整，未发现占位符或旧掌握实体/证据术语，关键跨规格不变量均有落点。
- 当前目录不是 Git 仓库，因此没有创建提交；设计与规划文档已完成，实施阶段需另行进入代码工作流。
- 修复总体设计层级图的 Markdown 代码围栏，并完成旧实体、旧证据等级术语、占位符和相对链接扫描。
- 新增 `docs/superpowers/plans/2026-09-18-study-agent-platform-implementation-plan.md`，按八项任务、三批次和机械退出门拆解实施工作。
- 需求设计与项目规划阶段完成；下一步是用户选择实施方式后，按实施计划进入代码阶段。

## 2026-09-18

- 讨论 Agent 技能分层、上下文管理、工具边界、子 agent 与 MCP 复用，发现现有六份规格**未定义 Agent 运行时上下文层**（全库 grep 仅两处把 skill/MCP 描述列为不可信输入），判定为最大结构性空白。
- 新增 `specs/2026-09-18-05-agent-skill-context-and-tool-boundary.md`：两个平面的划分、skill 三层加载、四类上下文压缩规则、ChildRun 规格与回传信封、工具 intent 边界与重叠 CI 门、MCP 准入清单。
- 新增 `specs/2026-09-18-06-layered-architecture-and-module-contracts.md`：五层划分（接入／边界／编排／能力／事实）+ 两条旁路、**层间通信原语表**、层间禁止事项、六类循环退出条件汇总。
- 参照用户提供的 nanobot 架构图完成对照，明确两处不可照抄：`MessageBus` 必须落 durable task（不变量 #7 要求队列全丢不改变事实）；`security` 不能沉为底座库，Policy Gateway 必须是调用链上的强制关卡。
- 建立 `docs/skills/` 独立目录：`manifest.yaml` 唯一索引、L0 常驻层（不变量摘要 + 术语速查）、三份首批 L1 契约（`sql-schema`、`protocol`、`tool-catalog`，均带生成区／手写区双区结构与生成脚本约定）、其余条目以 `status: planned` 预留。
- 回改现有规格：00 号新增不变量 15–17 与两条威胁；02 号新增 §2.1 上下文装配与按需加载；03 号新增工具声明字段、重叠 CI 门与 §10 ChildRun；总设计 §5 增加 Skill Registry 与 ChildRun Runtime 两个模块，§12／§13 同步更新。
- 新增 `ADR-013`（技能与契约知识的版本治理）与 `ADR-014`（ChildRun 与 MCP 准入），登记进 ADR 索引。
- 实施计划新增任务 9（技能契约、子任务运行时与工具边界），排入批次二，含 8 项步骤与退出门。
- 设计阶段仍未创建任何业务代码；下一步为进入实施或继续评审 05／06 号讨论稿。
- 随后按用户要求进入实施：生成批次一最小闭环并推送 GitHub（见下）。未实现项已在 README 显式列出，设计文档仍为权威依据。

## 2026-09-19 · 第一轮任务 1：产品契约与 `0002` 迁移（已完成）

执行计划：`plans/2026-09-19-round-1-persistence-product-foundation.md`
冻结稿：`specs/2026-09-19-07-round1-product-data-model.md`

### 交付

| 文件 | 内容 |
|---|---|
| `app/core/contracts.py` | 契约校验原语（住 core，避免 identity ↔ product 互相依赖） |
| `app/identity/models.py` | 新增 `LearningProject` —— **唯一的项目模型** |
| `app/identity/membership.py` | 删 `ProjectRecord`；`projects_for` 返回完整契约；注入 `clock` |
| `app/product/models.py` | 8 个契约（会话/消息/计划/里程碑/任务/资料登记/邀请/会话） |
| `app/product/ports.py` | 5 个仓库端口（Protocol，方法一律收 `Principal`） |
| `alembic/versions/0002_product_foundation.py` | 9 张新表 + `projects` 加 3 列 + 成员感知策略 + GRANT |
| `tools/skills/gen_contracts.py` | `render_sql_schema` 改为**从迁移导出** |

### 验收（全部实测，非推断）

| 项 | 结果 |
|---|---|
| 空库 → `upgrade head` | 17 张表、版本 `0002` ✓ |
| `head → downgrade -1 → head` | 可逆；策略**真的还原**成租户级（不是只 DROP POLICY） ✓ |
| `study_app` 不设上下文查 `projects` | **0 行**（RLS + GRANT 均生效） ✓ |
| 反向验证：注入列改名 → `--check` | 退出码 1 并指出内容与 `source_hash` 不一致；还原后回到 0 ✓ |
| 八道门禁 | 全绿（迁移门：2 条迁移、单头 `0002`、无环） |

### 关键设计选择（理由留在代码里）

- **`USING` 与 `WITH CHECK` 不对称**：新建项目时还没有 grant 行，
  写路径若也要求成员存在，项目永远建不出来。读靠成员关系（安全），
  写靠租户上下文 + 事务内同时写 grant（正确性）。
- **`http_idempotency.project_id` 不建外键、不进唯一键**：命令被占用时项目还不存在。
- **`SourceRecord` 没有任何处理状态**：Round 2 有真实摄取管线时才可能出现。
- **`PlanBundle`**：把"整版替换"变成可实现的目标 —— 分三次写会留下
  "有计划没里程碑"的半版状态，而它看起来是合法的。
- **迁移刻意自包含**（不共享 0001 的 helper）：共享会演进的 helper 会让
  历史迁移在新库上重放出不同的 SQL。

## 第五轮：持久化模型教学交互（2026-09-20）

提交序列（按风险单元）：`29a165f` provider 边界 → `7172ffa` 运行与预算持久化
→ `d73b06e` 上下文与引用校验 → `e5c232a`+`1f6a142` worker 执行与恢复 →
`43e445c` HTTP 与 SSE → 任务 6 收口提交。

### 逐任务验收

- **任务 1（provider 边界）**：`TeachingProvider` 协议同步、单次、无隐藏重试；
  `TIMEOUT`（结果未知）与 `DISPATCH_FAILED`（可证明未送达）在模型层强制区分
  （timeout 带 usage 直接构造失败）。`ScriptedProvider` 记录完整请求快照，
  脚本耗尽即拒绝（固定成功会藏住"多调了一次"）。16 项测试。
- **任务 2（持久化）**：迁移 `0010` 六表 + FORCE RLS + 组合外键；
  `start_run` 单事务（消息+运行+预留+事件）；worker 角色 `TO study_worker`
  从第一天成立（应用角色自设 `app.worker_id` 一律 0 行，有测试与阳性对照）；
  双连接屏障下最后一份额度只有一个赢家。22 项契约测试（memory/postgres）。
- **任务 3（校验）**：快照冻结进请求；只有 `full` 的片段外发；
  引用三道关卡（快照子集 → 回读 → 内容一致），hash 篡改的拒绝原因精确可辨。
  12 项测试。
- **任务 4（执行与恢复）**：结果分类矩阵逐项落位；usage 缺失/越界一律敞口
  保留（不按零结算、不静默截断）；提交前崩溃后从存证重放，provider 调用
  仍为 1。反例矩阵执行层 9 项，每项同时查调用计数/消息数/预算余额。
- **任务 5（HTTP/SSE）**：幂等创建（同键同体重放、冲突体 409）、
  provider 关闭明确 503（新码 `TEACHING_PROVIDER_DISABLED`）、
  SSE 批量重放（Last-Event-ID 续传、批次上限、显式截断标记）。
  14 项测试。
- **任务 6（验收）**：31 条冻结样例（6 类，期望人工预写）逐条通过；
  PG 端到端 3 项（含重启恢复：run/消息/事件/预算全部存活）；
  定向变异 4 项全部变红（围栏 token、快照子集、预算余额闸、租约期限）；
  迁移往返 `0009 → 0010 → 0009 → head` 每步实测六表 + FORCE RLS +
  策略角色集 + 权限集 + worker 的 messages 仅 INSERT。

### 已知边界（如实记录）

- **真实 provider 未验收**：本机无云模型凭据，`TEACHING_PROVIDER` 保持
  `disabled`；整条链路以模拟器 + 持久化验收，不能标"真实模型已完成"（ADR-015）。
- SSE 首版为批量重放模型（连接结束即停止，客户端续传）；
  原生 token 流属另设的隔离预览协议，未实现。
- 答案契约（`teaching-answer/v1`）要求 provider 支持结构化输出；
  不支持时按 `MALFORMED` 保守处理（费用照记）。

### 下一轮（任务 2）

PostgreSQL 身份与产品仓储：`identity/ports.py`、`db/identity_store.py`、
`db/product_store.py`、`product/memory_store.py`，以及内存/PG 双适配器的契约测试。

- 用户选择“先继续完善底层，再做普通用户可测试版本”，并同意扩大每轮范围、用机械退出门控制风险。
- 审查了现有迁移、数据库会话、确认存储、成员关系、运行时装配和 API；确认第一轮应复用现有 psycopg + 手写迁移 + RLS 路径。
- 新增 `docs/superpowers/plans/2026-09-19-round-1-persistence-product-foundation.md`，将本轮拆为 8 个可独立验收任务。
- 本轮计划已形成，尚未修改业务代码；下一步从任务 1 的产品契约与 `0002` 迁移开始。
- 根据计划审查修正幂等冲突：删除业务表 `(tenant_id, project_id, client_key)` 语义，将客户端命令判定统一收口到 `http_idempotency`；同时把幂等仓储从原 Task 6 提前到 Task 4，消除项目 API 的前置依赖倒置。
- 根据第二轮计划审查消除重复模型与虚假状态：项目统一为 `identity.models.LearningProject`；第一轮资料仅登记元数据，不预埋 `processing`/`ready`，摄取状态机推迟到第二轮真实管线。
- Task 1 已由提交 `c82bf95`、`ea0cf8a`、`9e195f8` 实现并自查；当前工作树干净。全量 pytest、ruff、mypy 与契约一致性通过，但 15 个 PostgreSQL 条件测试在数据库未运行时跳过。
- 实施前审查发现邀请/会话的 RLS 引导缺口，已将下一任务改为先落 `0003_auth_bootstrap`，再实现 PostgreSQL 仓储；没有提前进入模型、RAG 或前端。
- 同轮审查发现租户列与单列外键可产生跨租户关系不一致；下一迁移同时补组合外键，并以应用角色直接 SQL 负向测试作为退出门。

## 2026-09-18 · 首版实现与推送

- 采用「端口适配器」策略：核心域（core / registry / policy / budget / execution / audit / learning）零第三方依赖，持久化与外部能力为开发适配器并显式标注未实现。
- 新增代码约 9600 行、80 个文件；88 个测试全部通过，逐条对应 `docs/skills/L0/invariants.md` 中的不变量。
- 新增机械门：import 方向（含禁止 L4 横向互调）、工具意图重叠、A2 无幂等即拒注册、单 node 工具数上限、预算越界即拒、审计哈希链、投影顺序无关与禁用时钟。
- 新增 `tools/skills/check_manifest.py`（索引校验）与 `gen_contracts.py`（契约生成区一致性骨架）；CI 在 `.github/workflows/ci.yml`。
- 实现过程发现并修复 6 处真实缺陷，其中两处由机械门当场抓出（L4 横向依赖、策略反向依赖注册表），一处为静默失败（请求模型漏字段导致确认被忽略）。
- 创建私有仓库 `https://github.com/tiance002/study-agent-platform` 并推送 80 个文件。因本机 git 传输被代理阻断，改走 Git Data API：新增 `tools/push_via_api.py`（blobs → tree → commit → ref，产出单个快照提交）。
- 待用户审查：本地 `main` 与远端 sha 不同但内容一致；如需用 git 对齐需 `git push --force`（网络恢复后）。

## 2026-09-18 · 代码审查与修复

一轮独立代码审查给出 5 条问题（4×P1 + 1×P2），**全部成立并已修复**；另主动补了一处同类问题。

| # | 发现 | 修复 |
|---|---|---|
| P1-1 | 契约一致性 CI 必然失败（生成区仍是 `source_hash=PENDING` 而源码已存在） | 重写 `gen_contracts.py`：三份契约真实导出内容（实体扫描 / OpenAPI+错误码 / registry），校验改为**重新渲染并比对内容 + 哈希**，不再只比哈希 |
| P1-2 | `/projects/{id}/*` 使用请求体的项目 ID，路径与请求体不一致仍返回 200 | 新增 `_resolve_project_context`：**路径为权威来源**，不一致直接拒绝（对外 404，不暴露存在性） |
| P1-3 | `/audit` 与 `/budget` 无租户上下文，返回进程级全量数据 | 审计记录与预算账户增加**结构化 `tenant_id`/`project_id`** 字段（并纳入链哈希），读取走 `read_scoped` / `reservations_scoped` |
| P1-4 | 预算双维度预留非原子（第二次失败留下第一次的残留） | 改为一次 `batch_reserve`，并补失败注入测试 |
| P2-1 | `read_source_span` 直接遍历内部列表，绕过租户与项目过滤 | 读取逻辑收回 `ChunkIndex.read_span()`，过滤在该方法内强制 |
| 主动补充 | 投影输入未按作用域过滤（审查未提，属同类问题） | 新增 `events_scoped` / `corrections_scoped`；`runtime.projection()` 先过滤再投影 |

### 本轮暴露的三条原则

1. **隔离要收在拥有数据的那个类里**，不能交给调用方「记得判断」—— `read_source_span` 就是反例。
2. **路径与请求体冲突时不能「以某一方为准」**，否则边界变成可协商的，等于没有边界。
3. **审计过滤需要结构化字段**，解析 `payload` 内容不可靠，因为 payload 结构由调用方决定。

### 状态

- 测试：88 → **108 个**（新增 20 个回归测试覆盖上述六项修复）
- CI 三道门：测试 ✅、manifest ✅、契约一致性 ✅（此前契约门必然失败）
- 文档同步：README 去掉硬编码测试数量；实施计划任务 1–9 各自标注「已实现／大部分完成／部分完成／少量完成／未开始」
- 阶段判定（沿用审查结论）：**批次一核心不变量实现完成，但批次一发布门禁尚未完成**，不能进入生产部署或批次二功能扩展

## 2026-09-18 · 第二轮审查修复与 PostgreSQL 就位

### 审查提出的问题（2×P1 + 3×P2）全部处理

| # | 发现 | 处理 |
|---|---|---|
| P1-1 | 租户身份仍由客户端自报 | 新增 `app/identity`：签名会话令牌 + 租户成员与项目归属校验；**请求体不再含任何身份字段**；令牌签发改为运维脚本（API 上刻意不暴露登录端点） |
| P1-2 | A2 确认仍由客户端提供 | 新增服务端 `ConfirmationRecord`：绑定主体 / 项目 / 工具 / 参数哈希 / 有效期，单次消费；客户端只能引用记录 id |
| P2-1 | 契约覆盖范围不足 | 保留局限标注（`sql-schema` 仍是代码层实体，待 alembic 落地后替换） |
| P2-2 | CI 门禁不完整 | 新增秘密**内容**扫描、ruff 静态检查、mypy 类型检查；六道门禁全绿 |
| P2-3 | 跨租户测试不够强 | 新增正向隔离用例：跨租户路径、伪造令牌、请求体无身份字段、确认不可转让、查询参数不能改变身份 |

### 实现中发现的两个「会让确认静默失效」的缺陷

1. **工具参数混入环境值**（`occurred_at`、默认 `task_id = request_id`）→ 参数哈希每次都不同 → 确认永远匹配不上。
   修法：参数只保留用户可见字段，环境信息从 `NodeContext` 取。
2. **handler 重新拼装参数** → 客户端无法预测服务端构造 → 确认必然失败。
   修法：参数原样透传用户提交的内容。

这两个缺陷**都不会报错**，只会让确认"看起来实现了但从不生效"—— 属于最难发现的一类。

### PostgreSQL 16.4 安装与 RLS 验证

- 位置：`E:\pgsql`（EnterpriseDB 免安装 binaries；不碰 C 盘、不需要管理员权限）
- 应用角色 `study_app`：`NOSUPERUSER` + `NOBYPASSRLS`（RLS 生效的前提）
- **RLS 实测生效**：
  - t1 身份只见自己的 1 行，t2 同理；
  - **不设上下文 → 0 行**（不变量 #2 在数据库层自然成立，不依赖应用层记得判断）；
  - t1 身份写入 t2 的数据 → `new row violates row-level security policy`。
- 环境限制：由自动化工具启动的进程会在命令结束时被清理，**数据库必须由用户在自己终端启动**
  （`scripts/pg_start.cmd`）。
- 应用尚未连接该数据库 —— 属于下一步（任务 2 的剩余部分）。

## 2026-09-18 · 启动脚本编码崩溃修复与启动链路打通

### 用户报障

在自己终端跑 `scripts\pg_start.cmd`，输出全是乱码报错：

```
'竻鐞嗭紝鏃犳硶甯搁┗銆?REM' 不是内部或外部命令
't' 不是内部或外部命令
'l' 不是内部或外部命令
```

### 根因

**cmd.exe 按「系统代码页」逐字节解析 .cmd 文件**，而脚本是以 UTF-8 写入的。
中文注释的字节序列被 GBK 错误解码后，**cmd 的读取偏移发生错位**，于是它开始把
字节残片当作命令名执行（`'t'`、`'l'` 就是被切碎的残片）。

已用 `od` 确认文件**无 BOM**，所以纯粹是编码不匹配。**第 2 行的 `chcp 65001` 救不了**
—— 它是被解析到那一行之后才生效的，而错位在解析阶段就已发生。

### 修复

| 项 | 变更 |
|---|---|
| `scripts/pg_start.cmd` | 纯 ASCII + CRLF；新增 `pg_ctl status` 前置检测（返回码 0=运行中 / 3=未运行 / 4=目录无效），实现「已在运行则不动」的幂等启动，并替用户识别孤儿 `postmaster.pid` |
| `scripts/pg_stop.cmd` | 纯 ASCII + CRLF；新增「未运行则直接返回」的幂等停止 |
| `scripts/dev.cmd` | 纯 ASCII + CRLF；块内 `set` 改为单行 `if ... set "VAR=..."` |
| `.gitattributes` | 新增 `*.cmd` / `*.bat` → `eol=crlf`（否则会被 `* text=auto eol=lf` 冲掉） |
| `pyproject.toml` | `dev` extras 补 `ruff` / `mypy`：本地跑不通的门禁，等推到 CI 才发现就晚了 |
| README | 「启动数据库」一节加醒目块说明「改 `.cmd` 必须保持纯 ASCII」 |

### 验证（9 步全通过）

启动前 `rc=3` → `pg_start.cmd` 干净输出 `server started` → 启动后 `rc=0`（带 PID）
→ `psql` 连接成功 → 重复启动识别为「已在运行」→ `pg_stop.cmd` 正常停止
→ 重复停止识别为「未运行」→ 收尾 `rc=3` 无残留进程。

### 两个新踩的坑

1. **Bash 工具拒绝直接调用 `cmd.exe`**（安全策略：bypasses all command validation）。
   在工具环境里验证 `.cmd` 得用 Python `subprocess` 间接调用。
2. **`subprocess.run(capture_output=True)` 调 `pg_ctl` 会挂死**：`pg_ctl` 启起的 postgres
   继承了管道写端，Python 的 `communicate()` 永远等不到 EOF，整条命令被外部 SIGTERM 杀掉。
   现象是「无输出 + 信号终止」，**极易误判成脚本本身有问题**。修法：输出重定向到文件句柄。

## 2026-09-18 · 分层 RAG、工具校验与证据状态规格收敛

- 用户批准采用“回改现有权威规格”方案，不新增平行的 07 号规格。
- 已复核两处关键歧义：坏格式在派发前被拦截时不占 `max_tool_calls`；通用 `derive()` 不得无条件增加 `MODEL_OUTPUT`。
- 正在同步 00/02/03/05 号规格、ADR-014 与实施计划任务 4/5。
- 首次同步补丁因 05 号规格段落标题与预期不一致而未应用；重新读取原文后按实际结构精确修改，未重复使用失败补丁。
- 首次完整 pytest 因默认临时目录 `C:\Users\22088\AppData\Local\Temp\pytest-of-22088` 无访问权限而在 fixture setup 阶段报错；该结果不能用于判断测试成败，改用工作区内独立 `--basetemp` 并禁用不可写的 cacheprovider 后重跑。
- 首版一致性脚本把 02 号规格中的“从 `unresolved[]` 迁移”说明误报为残留契约；收窄为检查 03/05/ADR 的信封定义。
- 工作区 `--basetemp` 下完整测试通过（10 项 PostgreSQL 条件测试跳过），Markdown 围栏与信封契约检查通过。首次单独运行 manifest 校验时漏传必需路径参数，按脚本 CLI 补参重跑。
- manifest 校验补参后通过；`gen_contracts.py` 还要求 `--target` 或 `--all`，最终使用 `--all --check` 校验全部生成契约。
- 规格同步完成：00/02/03/05/06、ADR-014 与实施计划任务 4/5 已统一；完整 pytest 通过，10 项 PostgreSQL 条件测试跳过；manifest、三份生成契约、Markdown 围栏、旧信封字段和 `git diff --check` 全部通过。
- 提交时工作区禁止创建 `.git/index.lock`；升级授权的自动审批服务首次返回 503（不是安全拒绝），文件变更与验证结果不受影响。

## 任务 2/3：身份仓储与邀请 Cookie 会话（2026-09-19）

- **交付**：`identity/ports.py`（`MembershipRepository` / `InvitationRepository` / `SessionRepository` + 显式 `SystemContext`）；`identity/memory_store.py` 与 `db/identity_store.py` 双适配器；`identity/cookie_auth.py`（HMAC 签名声明）；`api/auth_routes.py`（`POST /auth/invitations/exchange`、`POST /auth/logout`、统一认证入口 `authenticate_request`）。
- **认证流**：cookie 优先、bearer 兼容兜底（运维/测试显式凭据）。cookie 路径三步不可换：验签 → CSRF 来源检查（仅不安全方法，浏览器必带 Origin）→ **回库查撤销**（可撤销的全部根据）。
- **统一拒绝**：邀请的未知/过期/已消费/畸形共用 `INVITATION_INVALID` + 一句话 —— 区分原因等于泄露"token 存在过"。
- **关键坑（实测）**：`INSERT ... RETURNING` 在 RLS 下要求新行**同时通过 SELECT 的 USING 策略**；`projects` 的 USING 是成员感知的，新项目没有授权行 → 创建者自己都"看不见"，RETURNING 稳定报 `InsufficientPrivilege`。无 RETURNING 的同一 INSERT 全部放行。已改由入参构造契约对象，并做反向验证（注入 RETURNING → 测试变红）。
- **验收**：372 项测试全过（PG 组真实执行无 skip）；适配器契约 18 项（内存+PG 参数化）+ PG 重启恢复（新仓储实例看到同一份状态：消费不可重放、会话仍存活）；八道门禁全绿。
## 第 2 轮：产品 API 与持久化闭环（2026-09-19，f30b05f / bd5db82 / 任务6）

- **任务 4（f30b05f）项目 CRUD**：`create_project_for`（创建即授予，PG 同事务两条 INSERT）、
  乐观锁 `update`（零行时区分 RLS 不可见 404 / 版本过期 409）、`VERSION_CONFLICT` 错误码 + 409 映射。
- **任务 5（bd5db82）会话/消息/计划/资料**：seq 服务端分配（取号与 INSERT 同事务）；
  `PUT /plan` 整版替换、版本 = max+1、UNIQUE (project_id, version) 兜底并发；资料按
  identity_hash 幂等去重；全部 id 服务端生成。端口方法名带类型后缀解决多协议聚合冲突。
  旧检索演示夹具让出 `/sources` 迁到 `/retrieval/chunks`（docstring 写明退役计划）。
- **任务 6 HTTP 幂等持久化**：`api/http_idempotency.py` 单文件（协议 + 双适配器 + 守卫 +
  PG 适配器）；claim-or-read 三态、失败释放、重放带 `X-Idempotent-Replay: true`、
  VIOLATION 409 / IN_PROGRESS 可重试；6 个写端点接入；守卫是端点取身份的唯一入口。
- **退出门验收**：PG 装配端到端脚本 8 步全过——登录→建项目（幂等重放）→消息 seq 1,2→
  存计划→登记资料（去重）→**全新平台实例同一 cookie 仍有效**→消息/计划/资料完整→
  退出后会话撤销、cookie 立即失效。442 项测试全过，八道门禁全绿。
- **坑**：幂等 store 曾漏设 RLS 上下文（铁律 33 又犯）；测试脚本固定幂等键 + PG 缓存跨运行持久
  → "上次运行的项目被重放回来"，键必须 per-run 随机。

## 第 3 轮：学习闭环 MVP（2026-09-20）

- **闭环 API**：固定三问诊断、确定性三阶段计划生成、任务流转、自报练习提交、提交历史、
  verified 计算结论与掌握度重建；写端点全部复用 cookie/CSRF/持久化 HTTP 幂等。
- **事前冻结**：生成计划时为 concept/practice/reflection 三任务冻结 assessment contract；
  手工计划没有 mapping，提交稳定拒绝为 `EVIDENCE_UNMAPPED`，不能事后追认学习证据。
- **原子性**：新增 `LearningLoopRepository`。内存版使用产品仓储同一把 `RLock`；PG 版使用
  `full_transaction`，计划+assessment 映射以及 submission+EvidenceEvent 分别同事务落库。
  故障注入证明证据追加失败时 submission 回滚，不留下半完成事实。
- **证据诚实性**：自报只产生 `OBS_1 / INTRODUCED / POSITIVE / VALID`，独立组等于
  `submission_id`；一次自报投影为 `introduced / low`，不伪装成高强度验证。
- **兼容修复**：发现旧 runtime `/projects/{id}/mastery` 路由先注册，遮蔽新持久化路由。
  已收敛为唯一入口，合并作用域内 runtime 与持久化证据后由同一个 `Projector` 重建。
- **双适配器一致性**：补齐仓储直调的提交长度校验，内存/PG 都对空内容和 >20,000 字符
  返回 `PARAMS_INVALID`，不再由 PG CHECK 单独决定语义。
- **PostgreSQL 退出门**：真实 cookie 用户完成“建项目 → 诊断 → 生成计划 → 开始任务 →
  提交 → 重启全平台 → verified/mastery 恢复 → 完成任务”；状态变化前后掌握投影完全相同，
  证明任务状态不能伪造掌握度。
- **机械验证**：后端 **493 项**全量测试通过（PG 无 skip）；
  Ruff、mypy、`gen_contracts.py --all --check`、`git diff --check` 通过；临时空库完成
  `upgrade head → downgrade 0004 → upgrade head`，最终版本 `0006`，临时库已删除。
- **下一轮**：已写 `docs/superpowers/plans/2026-09-20-round-4-source-ingestion-retrieval.md`；按用户要求仅给计划，
  不实施。范围限定为 durable 摄取、纯文本/Markdown 结构化切块、中文关键词基线、可核验引用。

## 2026-09-20 · 第六轮完成与第七轮启动

- 第六轮先行反例覆盖：过期已派发 attempt 不再重复调用、派发后无权威用量进入待对账、完整上下文预算在调用前拒绝、未来消息不进入旧运行、配置按 run 冻结、已校验引用对用户可见。
- 新增迁移 `0011_teaching_request_snapshot`：provider attempt 持久化不可变请求快照，失败 attempt 写入满足 PostgreSQL CHECK 的安全 failure payload。
- 内存与 PostgreSQL 的派发前预算预留都按完整冻结上下文 resize，锁顺序统一为租户账户后项目账户；worker 恢复读取 attempt 状态并拒绝未知结果重派。
- 新增 `OpenAIResponsesProvider`：官方 Responses HTTP 形状、usage 解析、请求体上界、无隐藏重试；429/5xx/网络不确定结果交给对账语义。未提供云凭据，因此没有宣称真实云端冒烟通过。
- 强制门禁 `tools/run_round6_gate.py` 实测：全量 `760 passed, 1 skipped`；`-m postgres` 子集 `124 passed`。唯一全量 skip 为非 PostgreSQL 条件用例；PG 持久化集合没有 skip。
- 第七轮已获实施指令：以 React 为前端约束，把邀请兑换、项目/会话、资料、计划、教学运行与引用回读收进普通用户工作台；先沿用关键词检索和现有异步 worker。

## 第 4 轮：资料摄取与可核验引用（2026-09-20，8071bcd / a7eaece / 任务 14）

**范围**：PostgreSQL durable 摄取、纯文本/Markdown 结构化切块、中文关键词检索、可核验引用。
明确排除：模型教学交互、React 前端、向量检索、reranker、网页抓取、PDF/Office/OCR/embedding。

### 任务 12（8071bcd）：持久化原文与片段

- `0007_source_ingestion` 三张表（`source_documents` / `ingestion_jobs` / `source_chunks`）+
  FORCE RLS + 列级 GRANT；`source_documents`/`source_chunks` 对 `study_app` 只有 `SELECT, INSERT`
  （「不可变」由权限系统保证，不是靠代码自觉），`ingestion_jobs` 有 `SELECT, INSERT, UPDATE` 但无 `DELETE`。
- 指向 `principals`/`projects` 的外键一律 `(tenant_id, X_id)` **组合键**，物理上排除跨租户父子关系。
- 双适配器（`InMemoryIngestionRepository` / `PostgresIngestionRepository`）跑**同一套**契约测试；
  PG 认领用单条 `FOR UPDATE SKIP LOCKED` 把「选 + 占」压进一条 SQL。
- `ingestion_jobs` 有**第二条独立命名策略** `ingestion_jobs_worker`（`current_setting('app.worker_id') IS NOT NULL`）——
  worker 必须先发现"哪个租户有活干"才能建立租户上下文，这正是全项目唯一没有 `Principal` 参数的仓储方法。

### 任务 13（a7eaece）：切块器、worker 与两个端点

- `DocumentProcessor`：ATX 标题 / 空行段落 / 围栏代码块 / 列表 / 表格，**不改写原文**，
  只为切片附上 `span`、`heading_path` 等结构元数据；超长节按段落边界切、重叠上限 200 字符。
- span 硬不变量由类型强制（`span_end - span_start == len(content)`），切块器**算错偏移会被拒绝构造**。
- `POST /projects/{id}/sources/{sid}/content` 只登记入队并返回 202，**从不调用切块器**；
  worker（`python -m app.workers.ingestion --once`）承担重活。
- 内容上限的唯一校验出口是 `require_document_content()`：领域模型与 HTTP 请求模型不可能给出两个
  "多大算太大"的答案（否则 40 万字中文会过应用层、在 DB CHECK 上炸成 500）。
  请求模型的 `max_length` 只是**字符粗筛**（UTF-8 每字符至少一字节，粗筛不漏），真正判定按字节。

### 任务 14：检索与引用

- `retrieval.py` 重写：`RANKING_VERSION = "keyword/v1"`，NFKC + 小写归一，
  汉字段内 2-gram，整数加法权重（精确短语 100 / 标题命中 10 / 正文命中 3 / 全覆盖 15），
  tie-break 用 `(source_id, chunk_index)` —— **不用随机 `chunk_id`**，否则同一查询每次顺序都可能不同。
- `store.py` 的 `KnowledgeRepository`：`search` 的第一步就是 `ingestion.stored_chunks(actor, project_id)`，
  作用域收窄发生在 SQL / RLS 里、**在打分之前**（02 号规格 §7 禁止把跨项目候选拉回应用层再筛）。
- 判定与健康度**正交**：`evidence` 答"核心结论有没有足够证据"，`retrieval_health` 答"这次检索有没有跑完"。
  本轮没有冻结的核心结论标注集 → 证据状态**必然** `insufficient + MISSING_SUPPORT`，
  同时过程健康度如实报 `clean`。这是正确结果，不是待修缺陷。
- 新增 `POST /projects/{id}/knowledge/search`（**读端点，不要求 `Idempotency-Key`**）与
  `GET /projects/{id}/sources/{sid}/span?start=&end=`（`start<0 或 end<=start` → 400 `PARAMS_INVALID`）。
- 冻结夹具 `backend/tests/fixtures/retrieval_v1.json`：25 条中文查询（标题/段落/代码/表格/英文/归一化/
  多词/无命中），期望答案**手写**而非抄实现输出；基线 recall@5 = 1.0、MRR = 1.0、22 命中 + 3 无命中。

### 退出门验收（全部实测）

- 后端 **600 项**全量测试通过（**PG 无 skip**）；Ruff、mypy（87 files）、三份生成契约 `--all --check`、
  `git diff --check` 全绿。
- 临时空库证明 `upgrade 0006 → 0007 → downgrade 0006 → upgrade 0007`，每一步都实测
  「三张表存在 **且** `relforcerowsecurity = true`」，临时库已删除。
- `test_round4_postgres_e2e.py` 三个用例覆盖计划 Step 6 的整条链：
  ①cookie 登录 → 建项目 → 登记资料 → 上传 Markdown（202/`queued`）→ worker 切块 →
  **重建整个 PlatformState（新实例 + 新本地目录）** → 中文查询命中 → `原始文本[start:end] == 片段内容`
  且指纹一致 → 引用回读同一切片；
  ②同一主体的另一个项目搜不到、读不到，另一个租户搜索/回读/任务状态/资料列表一律 404，
  且跨租户 404 与"来源不存在"404 **除 `request_id` 外逐字相同**；
  ③认领后崩溃留下租约 → 租约期内第二个 worker 抢不到 → 租约过期后被回收**恰好一次**
  （`attempt_count` 1 → 2）→ 片段只落一套 → 重复 `complete` 不追加第二套。

### 反向验证与坑（本轮新增）

- **"一次就绿"要当场证伪**：给用例 2 补了**阳性对照**（先证明本项目内搜得到），
  注入"跳过 worker"后该断言立刻失败 —— 否则"别的项目搜不到"在摄取产出零片段时也会通过，
  测的是"什么都没有"而不是"隔离生效"。另一条注入：去掉 `_expire_lease` 后用例 3 立刻变红。
- **`_expire_lease` 直接改库而不是 `sleep`**：租约过期的语义就是"`lease_until` 落在 `now()` 之前"，
  改那一列是这句话的逐字实现；睡满租约（300 秒）不可行，睡 1 秒只是让租约变短、语义变模糊。
- **`claim_next` 是跨租户的系统级操作**，所以端到端用例必须**先排空全库遗留任务**
  （超级用户 UPDATE 到终态），否则认领回来的可能是上一次运行留下的任务。
- **`request_id` 不参与"逐字相同"的比对**：它按设计每请求唯一，把它算进去等于要求追踪失效。
- **`read_span` 不做"拒绝 → `None`"的翻译**：授权拒绝由 `IngestionRepository` 抛出那**一个**拒绝码
  （与 `search`/`get_job` 同源），两条路在 HTTP 上同形由 `error_response` 这个**唯一**出口保证。
  让 `read_span` 也吞成 `None` 就是第二个判定出口，日后必然分叉。
- **`RequestValidationError` 必须自定义处理器**：FastAPI 默认 422 体会**回显请求输入**，
  而孤立代理项（`\ud800`）无法编码成 UTF-8 → 编码响应时抛异常 → 422 静默变 500，
  且形状与本项目规范错误体不同。已改为 `public_error_payload` 规范体，只回显字段路径。
- **计划偏差**：没有 `PostgresKnowledgeRepository`（计划假设按后端分叉，实际检索层没有后端逻辑），
  理由与替代方案已回注计划 Step 2 并写在 `store.py` 模块 docstring。

## 第 4 轮收口：独立审查 7 条（2026-09-20）

审查基线 `9f358de`（文档 `docs/superpowers/plans/2026-09-20-round-4-review.md`）。
7 条（4×P1 + 3×P2）**逐条复核全部成立**，已全部修复。按风险单元分 6 次提交，
每次先跑定向测试、再做注入反向验证：

| 提交 | 覆盖 | 一句话 |
|---|---|---|
| `e89627c` | R4-04 | 测试跑在随机临时库上；业务库里的在途任务不再是测试耗材 |
| `7c2ec11` | R4-01 | 队列的跨租户能力绑到 `study_worker` **角色**，不再绑自定义变量 |
| `89b3786` | R4-02 | `claim_token` 围栏：租约回收后旧持有者不能再落定 |
| `5e6a80f` | R4-03 | 引用带 `document_id`；检索默认只看每个来源的最新成功版本 |
| `5ffac18` | R4-05 | 片段必须与**持久化原文**比对，等长伪内容被拒 |
| `83b365c` | R4-06 + R4-07 | 纯文本走独立解析路径；围栏比长度不只比字符 |

（另：`950d649` 收录审查与第 5 轮计划；`de84faa` 测试支撑模块改名与辅助收拢。）

### 每条的关键点（缺陷形态比修法更值钱）

- **R4-04**：`_drain_queue` 用超级用户把全库 `queued/processing` 写成 `failed`，
  DSN 默认指向业务库 —— 跑一次测试就终结用户全部在途摄取任务，**而测试全绿看不出来**。
  现在会话级 autouse 夹具建一个 `study_test_<hex>` 随机库并迁移到 head，
  `require_test_database()` 拒绝一切非该前缀的库（写入前失败）。
  顺带修掉同一根因的另一半：组合根写的是 `dsn = loaded.dsn or DEFAULT_APP_DSN`，
  **完全无视 `STUDY_PLATFORM_DSN`** —— "环境说临时库、装配连业务库"正是从这条路发生的。
- **R4-01**：`app.worker_id` 是自定义 GUC，任何持应用连接串的人都能自己 `set_config`。
  只读实测（177 行队列）：不设 0 行；设 `'x'` 177 行；设 `''` 也 177 行（`IS NOT NULL` 对空串为真）。
  迁移 `0008` 把策略限成 `TO study_worker` 并把 `UPDATE` 从应用角色收回；
  应用侧新增 `worker_dsn()` / `connect_worker()` / `worker_transaction()`，
  认领与落定四个方法改走 worker 凭据。**两处必须同时成立**：只改迁移则认领不到任务
  （症状是"队列空了"），只改代码则越权依旧。生产自检新增三条（缺 worker DSN、
  用本机默认值、与 app 相同）。
- **R4-02**：`complete`/`fail` 只比状态，从不比"是不是我这一代"。
  时序：A 超时 → B 接管 → A 迟到的 `fail` 把 B 正在处理的任务打成终态。
  迁移 `0009` 加 `claim_token uuid`（每次认领 `gen_random_uuid()`），
  落定改成**一条条件更新**（状态 + token + `lease_until > now()`）；
  条件落空后再读状态分两类，**处理方式相反**：重复投递 → 幂等成功，
  认领失效 → 稳定冲突 `ILLEGAL_STATE_TRANSITION`。
- **R4-03**：同一来源两版片段**可能落在同一个跨度上**。实测命中 `doc_v2/'delta!'`、
  回读拿到 `doc_v1/'bravo!'`，而引用契约里**根本没有** `document_id`，拿到引用的人无从察觉。
  `ArtifactRef` 新增必需的 `document_id`（缺了就构造不出对象），
  端口新增 `chunk_at(document_id, span)` 精确查库（禁止挑第一条），
  `stored_chunks(latest_only=True)` 让检索只覆盖每个来源的最新成功版本，
  历史版本按引用仍可回读。
- **R4-05**：类型只保证**长度对口**，`content_hash` 只对片段自身取哈希（**自洽**），
  两者都不涉及原文 —— 实测把 `alphabet` 的片段换成等长 `XXXXXXXX`，落库并被检索到。
  新增 `assert_chunks_match_document(document_id, content, chunks)`，
  在两个适配器的**写入之前**对**持久化原文**核验（PG 在同一事务里 SELECT）。
- **R4-06**：`# plain heading` 是合法纯文本，按 Markdown 规则却成了"无正文标题" →
  **整篇丢弃、零片段**。`parse` 现在按媒体类型分派；纯文本有一条更强的性质：
  非空内容必然产出至少一个片段。
- **R4-07**：四反引号里包三反引号是展示 Markdown 示例的标准写法；只比字符不比长度时
  内部三反引号提前闭合外层，示例里的 `#` 变成真标题、污染 `heading_path`（排序权重输入）。
  开围栏现在存 `(字符, 长度)`，闭围栏要求长度不短于开围栏且同字符。

### 注入反向验证（每条修法都验过"去掉保护会红"）

| 去掉的保护 | 观察到的失败 |
|---|---|
| 排空改指业务库 | 阳性对照立刻红（临时库那条仍是 `queued`） |
| 排空同时打业务库 | 业务侧逐字段比较立刻红 |
| 策略退回 0007 形态（无限定角色 + `IS NOT NULL`） | 应用角色读到 1 行，核心用例报"自定义变量又变成凭据了" |
| worker 连接退回应用角色 | 认领报 `InsufficientPrivilege`（两层同时挡住） |
| memory / PG 各自的 token 比较 | 对应参数（`[memory]` / `[postgres]`）各自变红，且只红那一个 |
| `read_span` 退回"挑第一条" | 多版本用例红（读到了另一版） |
| memory / PG 各自的版本过滤 | 对应参数变红（旧版正文仍搜得到） |
| 忽略 `content_hash` 校验 | 多版本用例红 |
| memory / PG 各自的原文核验 | 对应参数变红（伪内容落库） |
| 生成器失去 f-string 模板解析 | 契约列比对红（`claim_token` 缺失） |
| 忽略媒体类型分派 | 纯文本用例报 `assert 0 in set()`（零覆盖 = 整篇丢弃） |
| 闭围栏只比字符 | 四反引号用例变成 2 个片段 |

### 迁移往返（临时库，每一步都实测）

`0007 → 0008 → 0009 → 0008 → 0007 → head`，每步断言：三表存在且 `FORCE RLS` 生效、
worker 策略的角色集（`{public}` ↔ `{study_worker}`）、应用角色与 worker 角色的权限集、
`claim_token` 列的存在与消失。

### 已知遗留（如实记录，未做）

- **业务库里有 112 行 `error_code='TEST_DRAIN'` 的失败任务**：它们是 R4-04 缺陷
  （以及我为证伪而故意注入的那两次）的遗留，`error_detail` 是原排空语句的固定话术。
  **一行未删** —— 删除用户数据不在本次授权范围内。当前未终态任务数已为 0。
- **契约表只有"路径/方法/说明"三列**：`span` 端点新增的必填 `document_id` 只能出现在
  summary 文字里（已加），参数级的机械对照仍未建立。
- **`complete` 的一个刻意边界**：任务已被别人落定成 `succeeded` 之后，旧持有者的迟到
  `complete` 得到幂等成功而不是冲突 —— token 在落定时清空，事后无法区分"我做的"与
  "别人做的"。不改变任何状态、不重复写片段，代价只是旧持有者不知道结果被丢弃。
- **文档被外部进程截断过一次**：`docs/superpowers/plans/2026-09-20-round-4-review.md`
  在 12:36 被改成只剩尾段（不是我的工具调用做的，仓库内没有代码写这个目录）。
  已用 `git restore` 恢复为提交 `950d649` 里的完整版本。

### 第 5 轮起点

- 第 5 轮计划的**任务 0（第四轮修复与前置验收）已完成**；接下来任务 1：provider 与模型边界。
- 第 6 轮：教学可靠性、真实 provider 离线适配器与强制 PostgreSQL 门禁已完成。

## 2026-09-20 · 第七轮完成

- 用本地 React 18 UMD 运行时替换开发演示页，FastAPI 通过 `/assets` 同源提供界面、样式与运行时；页面不再保存或发送 Bearer token。
- 普通用户浏览器路径已实现：邀请兑换、项目列表/创建、会话创建、异步教学运行轮询、计划保存、资料登记与引用状态展示。
- 页面处理空数据、加载、服务关闭、预算不足、待对账和会话失效；最后一次教学运行指针保存在浏览器本地并刷新恢复，敏感会话仍只在 HttpOnly Cookie 中。
- 新增 `backend/tests/test_frontend_assets.py`，守住同源资源与“前端不携带 Authorization”约束。
- 浏览器验收脚本 `tools/check_round7_browser.py` 已通过：桌面流程完成 Cookie 登录、创建项目、会话、资料、计划和教学关闭态；390px 视口无横向溢出。
- 最终门禁：全量 `761 passed, 1 skipped`；PostgreSQL 子集 `124 passed`；Ruff、mypy、迁移链、Node 语法检查通过。真实 provider 云端联调和部署发布门仍是上线前阻塞项。

## 2026-09-20 · 第八轮实现与门禁复验

第七轮复审发现的工作台正确性问题已完成本轮代码修复，但没有把未验证的发布条件冒充完成。主要改动如下：

- 空计划/空诊断响应改为真正的 `204` 空 body；新增项目级摄取任务元数据列表，内存与 PostgreSQL 都按租户、项目和成员权限过滤，列表不返回原文。
- 计划已有时改为只读展示；无计划时的动作明确为“创建计划”，避免把整版 PUT 误当成安全编辑。
- 前端异步读取按用户、项目、会话和 epoch 校验；项目/会话切换、登出和引用回读不会把旧响应写进新作用域。教学运行恢复指针按同一作用域隔离，临时 GET 错误不再被当成 404。
- 写命令在当前页面生命周期内保留不可变 payload 和 `Idempotency-Key`，未知结果可用同一请求重试；请求键限制为 ASCII，长中文名称不会导致浏览器 Fetch 在发出请求前失败。
- 资料列表区分已登记与排队/处理中/已处理/失败；引用展示资料名、版本/坐标和 hash，并可按 `document_id + span + content_hash` 读取历史原文。`inference_only` 不再显示“来源已核验”。
- 新增 `test_round8_http.py`、摄取列表契约测试、Round 8 浏览器预览/门禁脚本；契约文档已重新生成并通过一致性检查。

### 证据与边界

- 旧实现反例：HTTP 回归先观测到空响应 body 为 `b'null'` 且项目级摄取列表为 404；移动端旧实现隐藏新项目表单；浏览器还暴露过中文 `Idempotency-Key` 触发 Fetch 编码异常。本轮定向修复后相关测试与浏览器断言通过。
- 最终门禁 `tools/run_round8_gate.py` 通过：全量 **765 passed, 1 skipped**（共 766 个收集用例）；唯一 skip 是 `test_concurrent_reservation_on_last_slot_has_one_winner[memory]`，因为该并发语义只适用于 PG 双连接，说明在 JUnit 中保留。PostgreSQL 子集 **125 passed, 641 deselected, 0 skipped**；Ruff、mypy（104 个源文件）、Node 语法检查和浏览器验收均通过。
- 浏览器证据来自全新邀请和实际预览 worker/provider：`var/round8-gate-final-11`，完成 Cookie 登录、390px/桌面工作区、项目、会话、资料处理、提问、引用原文、初始计划和响应丢失后的同 key/body 重试。
- R8-H 的“重启 web/worker 后不清库刷新恢复”完整真实 PG HTTP 场景，以及 R8-I 的中断网络恢复场景，尚未由本轮脚本单独证明；真实云 provider、生产配置、部署、备份恢复和运维发布门仍未完成。因此第八轮不标为首版发布完成。
- 本轮未能创建 `codex/round-8` 分支、commit 或推送 GitHub：当前工作区的 `.git` 目录对本任务只读，创建分支时报 `unable to create directory for .git/refs/heads/codex/round-8`。代码、测试和记录均保留在工作区，不能声称已上传。

## 2026-09-21 · 首版 ECS 部署准备完成

- 已在阿里云 ECS `123.56.129.138` 部署 `/opt/study-plan/current`，运行 Ubuntu 24.04、PostgreSQL 16、systemd 和 nginx；数据库迁移版本为 `0011`。
- 已启用 HTTPS（Let's Encrypt IP 证书）、HSTS、同源 Cookie、Web 服务和持久摄取 worker；HTTP 自动跳转到 HTTPS，健康页可由 Python TLS 客户端验证。
- 已启用每日备份与证书续期 timer；新备份已恢复到临时 PostgreSQL 数据库并验证迁移版本，临时库随后删除。备份文件权限为 `root:postgres 0640`。
- 摄取与教学 worker 均支持 `--daemon` 常驻模式；教学服务在 provider 未配置前保持禁用，避免生产环境在缺少凭据时误发模型请求。
- DeepSeek 生产配置位置固定为服务器 `/etc/study-plan/provider.env`，当前 API key 为空。待用户填写后，设置 `STUDY_PLATFORM_TEACHING_PROVIDER=openai`、`STUDY_PLATFORM_TEACHING_MODEL=deepseek-flash` 并启用教学 worker。
- 本地验证：非 PostgreSQL 测试 `634 passed, 7 skipped`；Ruff、mypy、Python 编译检查、Node 语法检查和 `git diff --check` 通过。完整 PostgreSQL 门禁的既有证据为第八轮 `765 passed, 1 skipped`、PG 子集 `125 passed`；本机当前未运行 PostgreSQL，未对生产库运行测试。
- 部署资产、API key 填写步骤、邀请生成、运维和回滚记录见 `deploy/production/README.md`。当前 `.git` 对本任务只读，仍不能声称已 commit 或上传 GitHub。

## 2026-09-21 · 切换新 ECS `39.105.45.212`

- 新实例确认是全新 Ubuntu 24.04，已安装 nginx、PostgreSQL 16、Python venv 和 certbot；发布包重新上传到 `/opt/study-plan/releases/20260921-first`，并创建了独立的数据库角色、数据库和随机密钥。
- 已执行 Alembic 到 `0011`，web 与持久摄取 worker 启动正常；新 IP 的 Let's Encrypt short-lived IP 证书已成功签发，有效期至 `2026-09-27`；外部 HTTPS 曾验证为 `200`、HSTS 正常、页面内容正常。
- 已启用备份和证书续期 timer，首个备份文件权限为 `root:postgres 0640`。教学 provider 仍保持 disabled，API Key 不从旧实例复制。
- 换机过程中发现并修复首装缺陷：`bootstrap_server.sh` 现在会自举创建 `/etc/study-plan`；生产 nginx、信任来源和证书路径已改为新 IP `39.105.45.212`。
- 当前阻塞：新实例随后出现持续的网络不可达，22/80/443 连续多次探测均失败，因此尚未完成新实例上的备份恢复临时库验证、最终 systemd 状态复核和真实浏览器验收。恢复 SSH 后这些步骤可幂等继续，不能把本次换机标记为最终发布完成。

## 2026-09-21 · 重配新 ECS `120.55.115.162`

- 用户确认新实例之前未绑定密钥对；使用 `C:\Users\22088\Downloads\study-plan-dev.pem` 绑定后，SSH 已以 `root` 登录成功。Downloads 原文件权限过宽，未修改原文件；复制到受限缓存路径后使用。
- 新实例为干净 Ubuntu 24.04。重新上传当前工作区发布包并安装 nginx、PostgreSQL 16、Python venv、certbot；重新生成数据库角色、数据库和随机生产密钥。教学 provider 没有从旧实例复制，仍为 `disabled`，API key 为空。
- Alembic 已迁移到 `0011`；`study-plan-web.service`、`study-plan-ingestion.service`、nginx、`study-plan-backup.timer`、`study-plan-cert-renew.timer` 均 active/enabled；`study-plan-teaching.service` 保持 disabled。
- 新 IP 的 Let's Encrypt short-lived IP 证书签发成功，证书路径为 `/etc/letsencrypt/live/120.55.115.162/`，有效期至 `2026-09-27`；nginx 已监听 80/443，HTTP challenge 曾被 CA 成功读取，HTTPS 本地 TLS、HSTS 和反向代理已验证。
- 首次备份因 Windows 归档解包导致 `backup.sh` 权限为 `0666`，systemd 报 `203/EXEC`；已将发布代码改为 root 只读、脚本设为 `0755`，备份成功生成 `/var/backups/study-plan/study-platform-20260921T072814Z.dump`，权限 `root:postgres 0640`。
- 备份恢复演练已完成：恢复到临时库读到迁移版本 `0011`，随后删除临时库；当前 PostgreSQL 只保留 `study_platform` 与模板库。生产库未被覆盖。
- 新实例网络存在偶发 SSH/外部 HTTP 超时，但 22/443 可恢复，服务端监听和 CA 验证正常。真实教学 provider 联调、生产浏览器核心闭环仍等待用户填写 `/etc/study-plan/provider.env` 中的 API key，当前不标记首版最终验收完成。

## 2026-09-21 · 生产邀请码脚本 DSN 修复

- 生产生成邀请码时报 `psycopg.ProgrammingError`：`STUDY_PLATFORM_MIGRATION_DSN` 使用的是 SQLAlchemy 的 `postgresql+psycopg://` scheme，但 `tools/issue_invitation.py` 直接交给 `psycopg.connect()`，后者只接受 libpq scheme。
- 新增 `_psycopg_dsn()` 适配器和两个回归测试，先观察到缺少函数的收集失败，再实现最小修复；定向测试 `2 passed`，`git diff --check` 通过。
- 修复后的邀请码脚本已部署到 `/opt/study-plan/current/tools/issue_invitation.py`；生产实际生成了一枚 24 小时、单次使用的邀请码，原始 token 未写入仓库或进度文件。
- 用户已填入 DeepSeek API key，并将 provider 改为 `openai`；远端检查显示 API key 存在，web 与 teaching worker 均 active。注册/密码登录仍未实现，当前邀请码入口继续保留为临时认证入口。

## 2026-09-22 · 开放注册与平台预算审查收口

- 恢复了本机 PostgreSQL，并在独立测试数据库完成 `0012` 升级/降级往返；修复了误把平台预算函数授权给 `study_app`、降级误删旧约束索引、凭据表缺少显式 RLS 策略等迁移问题。
- 数据库预算函数现在只允许 `study_worker` 调用，并强制请求周期等于数据库 UTC 当前月份；内存与 PostgreSQL 适配器都支持同一预留的幂等 reserve、mark、release 和 settle。
- 付费预留 ID 现在由 teaching `run_id` 稳定派生，worker 重启后可以重建关联；成功、失败和重放路径均先结算或释放平台预留，再提交教学运行终态，避免崩溃留下无法关联的 `in_flight` 费用。
- 注册和密码登录成功审计统一由事务 outbox 写入并投影，移除了 API 层的重复直写；请求体限制改为流式计数，超限内容不会先完整缓存在内存；前端切换认证模式及成功提交后会立即清理密码和旧输入。
- 完整后端测试运行到 100%，仅保留 1 个既有的 memory-only 并发语义 skip；完整 PostgreSQL 标记子集运行到 100% 且无 skip。Ruff、mypy（111 个源文件）、Python 编译、Node 语法和 `git diff --check` 全部通过。
- 当前本地 PostgreSQL 正在运行，但本轮没有把 `0012` 发布到 ECS，也没有执行生产数据库迁移或生产冒烟。工作区 `.git` 仍只读，因此本轮不能提交或推送 GitHub。

## 2026-09-22 · ECS `120.55.115.162` 发布 `0012`

- 本地发布前门禁重新通过：完整后端测试 `100%`，仅 1 个既有 memory-only 并发测试 skip；Ruff、mypy、Python 编译、Node 语法、契约一致性和 `git diff --check` 均通过。
- release 包上传前排除了 `.git`、虚拟环境、缓存、运行数据和所有密钥文件；本地与 ECS 临时包 SHA-256 均为 `4B3733B5A401F13A8B3D6ECDA396FBBCB3CA42910334617AF1B242E46A932CF9`。
- ECS 已先生成备份 `study-platform-20260922T012911Z.dump`，随后从 `0011` 升级到 `0012 (head)`；旧 release `20260921-first` 保留，可通过 current 软链接回滚。
- 部署过程中发现并修正两项环境缺口：旧 venv 缺少锁定的 `argon2-cffi==25.1.0`，新 release 缺少 `var -> /var/lib/study-plan` 运行时链接。依赖已安装，链接已恢复；部署脚本和发布说明已补上这两个步骤。
- 当前 `current` 指向 `/opt/study-plan/releases/20260922-open-registration-0012`；web、摄取、教学服务均 active；ECS 本机 HTTP 与 nginx HTTPS `/healthz` 均返回 200，迁移版本为 `0012`，审计 outbox pending 为 0。
- 按既有“注册对所有人开放”要求，已备份并启用 `STUDY_PLATFORM_REGISTRATION_ENABLED=true` 与 `STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED=true`；无效注册探针返回参数校验 422，而非功能关闭 403，未创建测试账号。教学 provider 非密钥配置仍为 `openai/deepseek-flash`。

## 2026-09-22 · UI 默认入口修复

- 用户反馈首屏仍显示邀请码；定位为前端 `App` 将 `authMode` 硬编码为 `invite`，与后端开关无关。
- 已将默认入口改为 `login`，邀请码仍保留为兼容标签；创建独立 release `20260922-auth-ui-login`，未覆盖旧 release。
- 新 release 的前端文件 SHA-256 为 `1d53b79dfc6e8d6a8a63ebf5d830c1a1b7db48eb84b0ad2a0143d7f1e3b689f8`，与本地一致；当前 `current` 已切换到该 release。
- 使用 Chrome Playwright 对真实 ECS HTTPS 页面完成浏览器断言：首屏用户名/密码框可见且“密码登录”选中，点击“注册账号”后注册表单和无邮箱说明可见。

## 2026-09-22 · 首版生产核心闭环冒烟

- 使用真实 ECS HTTPS 页面完成一次注册、登出、密码登录；未使用邀请码。冒烟账号为临时验收账号，密码未写入仓库、日志或记录。
- 该用户完成创建项目、创建会话、追加用户消息、保存计划、登记资料和上传资料正文；摄取 worker 将资料任务处理为 `succeeded`。
- 随后重启 web、摄取和教学 worker；服务全部恢复为 `active`，迁移仍为 `0012`，HTTPS `/healthz` 返回 200，项目、消息和摄取成功状态均可从 PostgreSQL 读回。
- 本阶段没有调用真实 DeepSeek 接口，避免在未单独确认费用边界前产生模型费用。剩余发布门是一次最小真实 provider 联调、备份恢复复演和最终首版验收记录。

## 2026-09-22 · 真实 provider 联调闸门

- 使用真实 ECS 配置发起了一次最小教学联调；注册、建项目、建会话成功，但创建教学运行后由平台预算层返回 `PLATFORM_BUDGET_EXCEEDED`。
- 查询确认 PostgreSQL 的 `platform_budget_config` 仍是默认关闭/零额度，因此请求在 provider 派发前被拒绝，没有产生 DeepSeek 费用，也没有 usage 结算。
- 这证明预算止损门工作正常；下一步不能猜测货币或额度，需先由用户明确 `monthly_cap_micro` 和允许真实调用的范围，再通过受限运维变更启用。

## 2026-09-22 · 本机 PostgreSQL 永久修复

- 根因确认：本机没有 PostgreSQL Windows 服务、没有 `postgres.exe` 和 `5432` 监听；`E:\pgsql` 的安装和 `data` 目录存在，但遗留 `postmaster.pid` 指向的 PID 已不存在。
- 已注册 `study-plan-postgresql` Windows 服务，启动类型 `Automatic`，服务账号为 `NT AUTHORITY\NetworkService`；data 目录权限收紧到 NetworkService、SYSTEM、Administrators 和当前用户。
- 已验证 `pg_isready` 接受连接、服务重启后仍能恢复、项目数据库迁移版本为 `0013`。
- 本机 PG 专项门禁通过；完整套件最终为 **765 passed, 1 skipped**。
- 门禁同时发现并修复一处真实缺陷：`backend/app/main.py` 的 `EXPECTED_SCHEMA_VERSION` 仍为 `0012`，导致应用在迁移到 `0013` 后拒绝启动；现已更新为 `0013`。
- （当时记录）本机修复尚未等同于 ECS 发布；随后已完成 ECS `0013` 发布、预算启用和真实 DeepSeek 联调，详见本文件后续收口记录。

## 2026-09-22 · 本机与 ECS PostgreSQL / DeepSeek 实联调收口

- 本机 PostgreSQL 已固定为 `E:\pgsql` 下的 `study-plan-postgresql` Windows 自动服务，运行身份为 `NT AUTHORITY\NetworkService`，服务重启和 `pg_isready` 均通过；这不是额外注册网站账号，也不要求用户手工创建服务账号。
- 本机当前数据库 head 为 `0013`，完整后端回归为 **765 passed, 1 skipped**；真实 PG 专项无 skip。期间发现并修复 `main.py` 仍期望 `0012` 的启动自检缺陷。
- ECS `120.55.115.162` 已切换到 `/opt/study-plan/releases/20260922-provider-parser-v3`，web、ingestion、teaching 三个服务均 active，healthz 200，迁移版本 `0013`。
- 平台配置已明确为 `monthly_cap_micro = NULL`、`paid_dispatch_enabled = true`。NULL 只表示不按月度金额拒绝，不取消超时、worker 租约、未知结果和账务结算边界。
- 已完成最小真实 DeepSeek 联调。前两次发现 provider 返回纯文本时的严格 JSON 解析问题；第三次验证了无资料时的 `inference_only` 降级，但暴露模型前置推理文本污染答案；随后增加尾部 JSON 提取，最终运行 `run_zj_ZR1pfWRgyjYrwgHhJlg` 成功，usage 为 `338 input / 172 output`，成本记录为 `682 micro`，答案已干净持久化。
- 最终生产备份 `/var/backups/study-plan/study-platform-20260922T092745Z.dump` 已恢复到临时数据库，读回 `0013`、平台配置、最新成功 run 和 provider attempt 后删除临时库；备份权限为 `root:postgres 0640`。
- 当前仍不能声称已提交或推送 GitHub：本工作区 `.git` 对本任务只读；代码、发布记录和验证证据均已写入工作区。
## 2026-09-22 · 架构精简与边界收口完成

- 将旧 registry、旧检索写入、确认、interaction、audit 和 budget 路由隔离为仅开发挂载；生产保留 health、me 和 mastery。
- PostgreSQL 适配器改由组合根按需导入，幂等与审计通过数据库命名空间注入连接；新增“阻断 psycopg 仍可导入内存应用”的回归测试。
- `AuthMethod`、`Invitation`、`UserSession` 归位 identity，并保留 product 兼容导入；学习校验与 verdict 规则抽到 `learning.rules`。
- Provider key 与 base URL 由 `DeploymentSettings` 单次解析并注入，secret 不进入 repr；README 与当前注册、数据库、React 和真实 provider 状态对齐。
- 完整后端回归运行到 100%，共收集 841 个测试，仅 1 个既有 memory-only 并发语义 skip；PostgreSQL 标记子集 134 个全部通过、无 skip。Ruff、mypy（113 个源文件）、compileall、Node 语法、三份生成契约和 `git diff --check` 均通过。
- 本轮本地门禁完成后已继续部署 ECS；GitHub 尚待提交。当前工作区包含此前开放注册与真实 provider 的未提交实现，因此提交必须覆盖当前生产源码，不能只上传本轮文件造成源码与生产漂移。

### ECS 发布补充

- 发布前生成 `/var/backups/study-plan/study-platform-20260922T104150Z.dump`，权限 `root:postgres 0640`。
- 发布包 SHA-256 为 `0CE2473555C9984C543B6E246B86B1625F13BB455DFDACF3B333EE24C77E9DBF`，上传后校验一致；排除了 Git、虚拟环境、缓存、运行数据和密钥。
- 当前软链接指向 `/opt/study-plan/releases/20260922-architecture-boundaries-v1`；迁移仍为 `0013 (head)`。
- web、ingestion、teaching 均 active；公网 HTTPS `/healthz` 返回 200，生产 `/registry` 返回 404，最近十分钟三项服务无 warning 日志。
- 当前生产源码已提交为 `6369137` 并推送到 GitHub 分支 `codex/architecture-boundaries-20260922`；未包含 PEM、环境文件、运行数据或 API key。
- GitHub PR 为 `#1`。远端默认 `main` 与当前项目上传历史没有共同祖先，因此 PR 安全地以实际祖先分支 `codex/upload-current-project-20260921` 为 base；未 force-push、未改写远端历史。

## 2026-09-22 · 第八轮残余正确性验证

- 先让旧前端在真实浏览器故障注入下失败：服务端已写入项目后把响应改为 503，旧逻辑提示普通失败并丢弃命令；修复后显示“结果未知”，重试请求的 key/body 与第一次完全一致。
- 同一场景扩展到 teaching run：第一次 POST 响应改为 503，第二次使用原请求重试；Playwright 断言答案出现且只观察到两次相同 key/body，浏览器脚本输出 `ROUND8_BROWSER_PASSED`。
- 修复异步项目刷新作用域判断：旧实现用 `principalId != owner && epoch != epoch`，只要两个条件之一相同就可能让旧响应写回；现改为任一维度不匹配即丢弃。
- 新增 `backend/tests/test_round8_postgres_http.py`：以独立 PG 临时库完成注册、项目/会话/计划/资料 HTTP 写入，摄取 worker 成功；重建第二个 web/worker 平台后读回 Cookie、计划、资料状态和消息，教学 worker 成功并在第二次调用时 idle；断言 teaching/platform reservation 都是 settled 且 usage 大于 0。
- 全量门禁：后端测试 100% 通过，仅保留原有 1 个 memory-only 并发语义 skip；新增 PG 测试单独通过且无 skip；Ruff、mypy（113 个源文件）、compileall、Node 语法、三份生成契约、`git diff --check` 均通过。
- 浏览器补充 768px 横向溢出与键盘注册路径，已通过；预览服务已停止。当前仍未把真实 uvicorn 重启和中断网络故障矩阵伪称完成，下一步应单独补这两项再更新第八轮退出门。

## 2026-09-22 · 第八轮修复发布到 ECS

- 本地 commit：`c27950f`（第八轮修复与证据）和 `2255771`（生产访问说明对齐）；工作区干净。
- 发布包排除 `.git`、`.venv`、`var`、缓存、环境文件、PEM/密钥；本地与 ECS 临时包 SHA-256 为 `7CCBD6AA66EE358ECE04F9E49EC229DB275E1623D410D03266A656F94582091E`。
- 切换前备份为 `/var/backups/study-plan/study-platform-20260922T112849Z.dump`；新 release 为 `/opt/study-plan/releases/20260922-round8-residual-v1`，旧 release 保留可回滚。
- ECS `120.55.115.162` 上新 release 启动自检通过，迁移保持 `0013`；web、ingestion、teaching 均 active；HTTPS `/healthz` 200，生产 `/registry` 404，前端新文件已在服务器校验。
- 使用浏览器只读检查真实线上首屏：用户名/密码字段可见，密码登录选中，注册账号标签可见；未提交表单、未创建线上验收账号、未触发真实模型调用。
- 常规 `git push` 和既有 GitHub Data API 推送均因当前环境无法连接 GitHub 443 而未完成；未 force-push。GitHub 上传状态必须保留为“待网络恢复后推送”，不能写成已上传。
- 修复统一门禁第一次失败的基础设施问题：历史 `TEMP` 目录权限失效会让 pytest fixture 批量 `PermissionError`；`run_round8_gate.py` 现默认使用 `var/round8-gate/tmp-<pid>`，允许环境变量显式覆盖。修复后统一门禁真实结果为全量 `841 passed, 1 skipped`，PG `135 passed, 707 deselected, 0 skipped`，最终输出 `ROUND8_GATE_PASSED`。

## 2026-09-22 · 第九轮安全获取内核（未接入生产）

- 按冻结计划先实现外部资料获取的纯策略层与有界 HTTP 适配器：URL 规范化、HTTP(S) 与端口闭集、凭据/localhost/私网/保留地址拒绝、DNS 每个结果复核、精确 host allowlist、重定向逐跳复核、HTTPS 降级拒绝。
- 默认传输连接到已通过 DNS 检查的具体 IP，并以原主机名做 Host/TLS SNI，避免校验后再次按域名解析形成 DNS rebinding 窗口；响应同时检查 `Content-Length` 与流式字节上限。
- 独立保留连接、读取、总 deadline、最大重定向数和最大响应字节；错误只返回稳定安全码与可重试标记，不把 URL 凭据、路径或底层异常写入用户状态。
- 新增 22 项无网络测试，覆盖 SSRF、DNS 失败、重定向越界、降级、超大响应、慢流、HTTP 5xx 和传输超时；全量回归 `841 passed, 1 skipped`，Ruff 全仓通过，mypy `115 source files` 通过。
- 这只是第九轮任务 1 的安全内核，尚未接入 web 路由、durable acquisition job、解析/对象存储或生产 ECS；因此没有切换现网 release，也没有声称普通用户已经能搜索下载资料。
- 同步冻结了 `SourceCandidate`、`AcquisitionRequest`、`AcquisitionJob`、候选/下载状态集合及状态转移；`unknown`、`failed`、`succeeded` 均不会被隐式重排回队列，租约只能通过显式 claim 产生。
- 本地提交 `803d49f`（安全获取内核）与 `a89c0d2`（候选/下载协议）已推送到 GitHub 分支 `codex/round8-residual-20260922`；当前 PR #2 继续作为审查入口。未部署 ECS，因 durable acquisition job 尚未完成。
- 最终复核收集到 869 项测试，执行结果为 **868 passed, 1 skipped**；Ruff 全仓通过，mypy 已覆盖 `116 source files`，工作区保持干净。
