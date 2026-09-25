# 新版整体改造：无邀请码认证 + 干净数据库基线 Implementation Plan

> 依据用户 2026-09-25「新版整体修改方案」；基线分支 `codex/v2-clean-baseline-20260925`（基于 `审查版2` = `a7d6fb2`）。
> **PR #3 的 head 在外部审查期间保持不动**，本批改动全部走新分支与新 PR。

**Goal：** 只保留一套代码、一套数据库契约、一套密码规则、一种登录方式——删除邀请码全链路、密码统一 6–12 码点、把 21 条迁移压成一条干净初始基线、测试夹具改为真实密码注册登录，并修掉上一轮暴露的 P0/P1 正确性问题。

**不变量：** 会话签名/撤销、CSRF、RLS、租户与项目授权、知识库、学习闭环、引用与预算语义**全部保留**；邀请码 Token 与 Session Cookie 是两回事，删前者不得动后者。

**本次不做（需用户另行确认）：** 清空/替换任何真实数据库（生产 ECS）、部署、推送 PR #3。

---

## 0. 阶段与批次

| 批次 | 内容 | 执行方式 |
|---|---|---|
| **Batch 1（原子单元）** | 阶段 1–4：邀请码移除 + 密码/认证重建 + 新基线 + 测试迁移 | 三条隔离 worktree 并行（A1 后端 / A2 前端 / B 数据库），集成后统一验证 |
| **Batch 2** | 阶段 5：P0/P1/P2 正确性修复 | Batch 1 集成后进行（与 A2 前端文件有重叠，故串行） |
| **Batch 3** | 阶段 6：空库真实用户路径验收 | 集成分支上执行，产出验收证据 |

关键耦合（决定了批次划分）：**不能先删邀请码代码再补测试夹具**——`conftest.cookie_project` 依赖邀请码兑换取会话，删功能不改夹具会让全部业务测试同时红。因此 Batch 1 必须整体完成后再判定通过。

---

## 1. 邀请码删除清单（已全仓扫描，附落点）

### 1.1 必须删除

| 层 | 位置 | 内容 |
|---|---|---|
| API | `backend/app/api/auth_routes.py:63-64,75-78,92-95,380-452` | 令牌长度上限、`ExchangeBody`、`_token_hash`、兑换端点 + 专用限流 + 审计事件 |
| 错误码 | `backend/app/core/errors.py:26-28`、`backend/app/api/routes.py:469-474` | `INVITATION_INVALID` 及其 HTTP 映射 |
| 身份模型 | `backend/app/identity/models.py:30-34,38-64` | `AuthMethod.INVITATION`、`Invitation` |
| 仓储端口 | `backend/app/identity/ports.py:150-174` | `InvitationRepository` |
| 内存实现 | `backend/app/identity/memory_store.py:36-131` | `InMemoryInvitationRepository` |
| PG 实现 | `backend/app/db/identity_store.py:247-369` | `PostgresInvitationRepository` |
| 装配 | `backend/app/platform.py:113,259,310,380,435,493` | `invitations` 字段与两条装配分支的构造/注入 |
| 主应用 | `backend/app/main.py:104-108` | 兑换路径的免登录白名单分支 |
| 工具 | `tools/issue_invitation.py`（整文件）、`tools/README.md:38` | 签发脚本与文档引用 |
| 前端 | `frontend/views.js:31,58-71`、`frontend/app.js:78-79,334-350`、`frontend/terms.js:30,33,39-40`、`frontend/api-client.js:50` | 邀请码模式、表单、`exchangeInvite`、文案、401 白名单条目 |
| 契约 | `docs/skills/contracts/protocol.md:18`、`sql-schema.md:32,170-178` | 由生成器重写（不手改） |
| 迁移 | `0002:136-150,288-294,329-335,377-383`、`0003:73-80,108-124,127-145,158-212,230-256`、`0004:78-134,194-195`、`0006:81-136,186-237,339-342`、`0012:44-47,163-273,493-543,626-674,928-970,984-987` | `invitations` 表/索引/策略/授权、`invitee_principal_id` 及组合外键、`exchange_invitation()`（被重写 4 次）、`user_sessions_auth_shape_check` 的 `'invitation'` 分支、0012 provenance 回填 DO 块 |
| 测试 | `backend/tests/test_invitation_auth.py`（整文件）+ 各文件中的邀请专属用例 | 详见 §4 |
| 浏览器工具 | `tools/run_round7_preview.py`、`tools/check_round7_browser.py`（若确认无引用则删除）；`tools/run_round8_preview.py:52,67-75`（邀请播种）、`tools/run_round8_gate.py:28,75` 与 `tools/check_round8_browser.py:109`（`--invite` 参数） | 端到端失效的邀请入口 |

### 1.2 必须改造（保留功能，去掉邀请分支）

- `platform.py`：装配签名/字段；`main.py` 路由守卫。
- `deployment.py:50-52,136-138,252-256,345,364-367`：`EXCHANGE_*` 配置项改名/收敛为“认证尝试限流”，注册与登录共用（**限流本身保留**）。
- `identity/models.py:77-104`：`UserSession` 的 `auth_method` 语义收口（只保留密码会话），不变量里删除邀请分支。
- `db/identity_store.py:47,71-73,385-395`、`db/account_store.py:57,86-87`：会话行读写中的 `auth_method` 处理。
- 注释/文档口径：`audit/outbox.py:5-8,84`、`identity/rate_limit.py:5,18`、`platform.py:364`、`README.md:175`、`deploy/production/README.md:58-76`、`docs/operations/open-registration-runbook.md:8,37`、`progress.md` 历史段、`docs/superpowers/specs/2026-09-21-*.md`（标注为历史/归档，不改写 Git 历史）。

### 1.3 保留（不得误删）

`AuthMethod.PASSWORD`（或等价枚举收口）、`credential_id`、`security_generation`、`register_auth_attempt()` + `auth_attempt_counters`、会话签名/撤销、CSRF、RLS、`projects` 成员感知策略、`principals_tenant_principal_uq`、`PROJECT_CHILDREN` 组合外键、演示种子（`platform.py:568-576`）。

### 1.4 依赖图（删完立刻红的东西）

`conftest.cookie_project`(117-126 邀请兑换) → `test_ingestion_api:60`、`test_knowledge_search:787`、`test_acquisition_api:20`、`test_round8_http:8,20`、`test_teaching_api:98,133`、`test_teaching_events:22`；
自建邀请舞蹈：`test_product_api:29-46,189`、`test_projects_api:21-37,131,151`、`test_http_idempotency:50-67`、`test_round2_postgres_e2e:64-77`、`test_round3_postgres_e2e:58-70`、`test_round4_postgres_e2e:184-203`、`test_auth_hardening:50-71,289-312,527-620`、`test_auth_hardening_postgres:77-79,122,320,376`、`test_identity_repositories:150,239-300`；
整文件受影响：`test_invitation_auth.py`、`test_open_registration_migration.py`（依赖 0011→0012 往返）；
脚本：`check_round7_browser.py`、`run_round7_preview.py`、`run_round8_gate/preview/browser` 的 `--invite` 链路。

---

## 2. 认证模块收口

- **单一登录方式**：只保留用户名 + 密码。删除“邀请码会话 vs 密码会话”的双分支判断。
- **会话字段（保留）**：`session_id`、`tenant_id`、`principal_id`、`credential_id`、`security_generation`、`issued_at`、`expires_at`、`revoked_at`。
- **`auth_method`**：若仅用于区分两种旧来源，则从模型与新基线 schema 中删除；`credential_id` / `security_generation` 必须保留（账号禁用与会话集中失效依赖它们）。
- **前端**：`authMode ∈ {"login","register"}`，删除 `authToken`、`exchangeInvite()` 与第三种模式；`/me`、退出、退出所有设备不变。
- **OpenAPI**：不得再出现兑换端点；`test_ci_gates` 的路由/契约断言同步。

---

## 3. 密码模块重建（单一策略 6–12）

- 常量：`MIN_PASSWORD_LENGTH = 6`、`MAX_PASSWORD_LENGTH = 12`（按 Unicode 码点计数）。
- 统一入口 `validate_password()`，被 `hash_password()` 与 `verify_password()` 共用；注册/登录/rehash 全部走同一策略（**不再有 `REGISTRATION_*` 之类的第二套规则**）。
- 具体行为：6 与 12 允许、5 与 13 拒绝；不 strip/截断/小写化/规范化；拒绝含孤立代理项等非法 UTF-8 字符串；Argon2id 摘要；未知账号统一失败响应；保留登录限流；`needs_rehash` 触发的重哈希不得对合法长度密码抛异常。
- 文案：服务端错误消息与前端提示统一为“密码长度需为 6-12 个字符”；`views.js` 的 `minLength/maxLength`（36,91,92）与 `terms.js`（38,41,310-312）同步。
- 加固（不改变长度规则）：保留限流；在条件允许时增加常见弱口令拦截（可选，若实现则必须可测且不影响既有用例）。
- **需要同步的既有口令串**（>12 或 <6，逐处改）：`test_password_auth_api.py:56,87,98`、`test_password_accounts.py:96,103,139,152,157`、`test_library_postgres.py:62`、`tools/check_library_browser.py:34`、`tools/check_round8_browser.py:147,219,625`、`tools/check_round8_process_recovery.py:409`。

---

## 4. 数据库新基线（本批 blast radius 最大项）

**决定：把 `alembic/versions/` 的 21 条迁移压成一条初始基线 `0001_initial_schema.py`（`revision="0001"`，`down_revision=None`），删除旧文件。** 旧库不做数据迁移（用户明确不需要旧数据）。

### 4.1 新基线包含

原 head（0021）的全部**有效**对象，按依赖顺序：
- 核心：`tenants`/`principals`/`projects`/`project_grants`/`confirmations`/`evidence_events`/`action_intents`（0001）
- 产品：`user_sessions`/`conversations`/`messages`/`learning_plans`/`milestones`/`learning_tasks`/`sources`/`http_idempotency`（0002，去掉 `invitations`）
- 认证：`auth_attempt_counters`（0004）、`account_credentials`（0012）、`platform_budget_config`/`platform_paid_reservations`（0012）
- 学习闭环：`task_assessments`/`task_submissions`/`diagnoses`（0005）
- 审计：`auth_audit_outbox`（0006）
- 摄取检索：`source_documents`/`ingestion_jobs`/`source_chunks`（0007-0009）、`source_candidates`/`acquisition_jobs`（0014-0015）、`source_fetch_artifacts`（0016）、`acquisition_fetch_observations`（0018）
- 教学：`teaching_runs`/`provider_attempts`/`teaching_events`/`teaching_budgets`/`teaching_tenant_budgets`/`teaching_reservations`（0010-0011、0017、0019）
- 知识库：`library_sources`/`library_documents`（0021）
- 函数：`register_auth_attempt`、`auth_audit_pending_count`、`invalidate_credential_sessions`（触发器）、平台额度 5 函数、`register_account`/`lookup_account_for_login`/`complete_account_login`、`study_metrics_snapshot()`（**只保留 0019 版**，且必须建在 `routing_decision`/`retrieval_decision`/`provider_family`/`acquisition_fetch_observations` 之后）、`learning_jsonb_string_array`
- 策略：各表 `<表>_isolation`（租户/项目/主体级谓词）、`projects` 成员感知、worker 三条策略（`TO study_worker` + `NULLIF(current_setting('app.worker_id',true),'') IS NOT NULL`）
- 授权：`REVOKE ALL ON SCHEMA public FROM PUBLIC`、`GRANT USAGE ON SCHEMA`、各表 GRANT、`GRANT SELECT ON alembic_version TO study_app`
- 种子：`INSERT INTO public.platform_budget_config (config_id) VALUES (true)`

### 4.2 新基线不得出现

`invitations` 表/索引/策略/授权、`invitee_principal_id` 及其组合外键、`exchange_invitation()`、`user_sessions_auth_shape_check` 的 `'invitation'` 分支、0012 的 provenance 回填块、任何 `auth_method='invitation'` 语义。

### 4.3 必须原样保留的运行时约定

所有 definer 函数 `SET search_path = pg_catalog` 且表名全限定；`set_config('app.*', ..., true)`（事务级）；RLS 谓词一律用 `current_setting(..., true)`（缺失→NULL→零行）；策略的角色绑定（`TO study_worker`）；角色必须已存在的断言块；`FOR UPDATE`/`FOR UPDATE SKIP LOCKED` 语义不变；迁移内不建 role、不建 extension。

### 4.4 验证（必须全部通过）

1. 全新随机临时库 `alembic upgrade head` 一次成功；`alembic downgrade base`（基线 downgrade 在**已有数据时拒绝**，无数据时清空）后可再 upgrade。
2. **对象级对比**：用旧链（迁移文件尚在时的 head）与旧链+人工删除邀请对象后的 `pg_dump --schema-only` 作为对照，与新基线建出的库做对象 diff，确保除邀请相关对象外**零差异**（表/列/约束/索引/策略/函数/授权）。
3. PG 子集测试（151+）在新基线上全绿——这是 RLS、组合外键、函数、授权的最强证明。
4. `platform.py` 的 `EXPECTED_SCHEMA_VERSION` 改为 `"0001"`；`test_ci_gates.py:106-115` 的“唯一 head == EXPECTED_SCHEMA_VERSION”断言仍成立。
5. `tools/skills/gen_contracts.py --all` 重新生成 `docs/skills/contracts/sql-schema.md`（head 由 `_head_revision()` 推导）并 `--check` 一致；**保持既有 DDL 书写风格**（生成器用 AST/正则解析，`CREATE TABLE public.x` 这类当前匹配不到的写法保持原状，避免契约内容意外扩张）。
6. 因压扁而失去意义的“迁移往返/数据护栏”用例（`test_metrics.py:238,257-280,318,417`、`test_library_postgres.py:301,307-321`、`test_open_registration_migration.py` 全篇）按 §5 处理。

**禁止执行**：对任何真实数据库（含 ECS）执行 drop/reset/rebuild；本批只在随机 `study_test_*` 临时库验证。

---

## 5. 测试体系迁移（不是删完邀请测试）

**方向：** 所有业务测试改用真实密码认证取得身份——`POST /auth/register` → `POST /auth/login`（或直接持注册后的会话 Cookie），不保留绕过 HTTP 的临时登录入口。

| 改造项 | 方案 |
|---|---|
| `conftest.cookie_project:112-134` | 改为 `POST /auth/register`（带 `Origin`）→ 断言 201 → 用 `default_project_id` 作为项目（注册会自动建“我的学习项目”并授权）。**返回值签名不变**，故 6 个消费者零改动 |
| `conftest.auth_headers:137-154` | 从“仓储直发会话 + 自签 Bearer”改为真实注册/登录后取会话（Cookie 或登录返回的令牌），保持返回 header dict 的签名 |
| `conftest.demo:157-159` | 从静态常量改为“注册得到的身份”（principal/tenant/project 来自注册响应），或保留常量但确保与注册身份一致 |
| 自建邀请舞蹈的 8 个测试文件 | 逐处替换为共享的“注册/登录”辅助（§1.4 清单），断言随之对齐新身份 |
| 邀请专属用例 | 删除：`test_invitation_auth.py` 全篇、`test_auth_hardening.py:289-312,527-620`、`test_auth_hardening_postgres.py:122`、`test_identity_repositories.py:239-300` |
| 共享认证语义用例 | 保留并改指密码流程：CSRF（`test_auth_hardening:77-166,246-256`）、Cookie 标志与撤销（`test_invitation_auth:59-77,127-167` → 迁到新文件）、篡改/无库会话失败闭合、logout/all、密钥轮换、审计事件、TTL/definer 限流/RLS/重启存活 |
| 会话模型不变量 | `test_session_auth_metadata.py` 按新的 `UserSession` 字段收口重写 |
| 迁移相关测试 | 删除依赖具体 revision 的往返用例；新增“空库一次初始化 + RLS 生效 + 关键函数可用”的基线测试 |
| 浏览器/门禁 | 移除 `--invite` 全链路（`run_round8_gate.py:28,75`、`check_round8_browser.py:109`、`run_round8_preview.py:52,67-75`）；`check_round7_*` 若无引用则删除；口令串按 §3 改到 6–12 |

---

## 6. 批次 2：核心正确性问题（与邀请码无关，但必须修）

| 优先级 | 问题 | 定位 | 修复边界 |
|---|---|---|---|
| **P0** | 换账号后旧账号知识库残留 | `frontend/project-state.js:57-67` 的 `clearProjectData` 未清 `librarySources`；`frontend/app.js:203-222,372-401` 同样漏 | 在 `clearProjectData` 中清空 `librarySources` 与相关 busy/loading；补浏览器用例断言换号后知识库为空 |
| **P1** | 知识库同名不同内容被静默丢弃 | `library_routes.py:121-141` 身份只由 acquisition 计算（`core/hashing.py:51-62`），命中幂等即忽略新 `content`（`db/library_store.py:97,112-127`、`memory_library_store.py:54-61`） | 让**携带原文**的材料身份包含内容哈希（库侧实现，不动共享 `hashing.py` 算法），使不同内容成为不同材料；同内容重复登记仍幂等；响应明确 `created` |
| **P1** | 自报抬高 confidence | `learning/projector.py:151-181`（NONE 仍 `count+=1`、`groups.add`，confidence 由组数推导 96-104,187） | 投影时跳过 `Direction.NONE`（不计入计数与独立组）；`verified` 判定保持 VALID+POSITIVE |
| **P1** | 资料关联半成品 | `library_routes.py:182-248` 先 `register_source` 再 `enqueue`/抓取，无事务无补偿 | 首选单事务（`tenant_transaction` + `register_source_in_conn` + 新增 ingestion `enqueue_in_conn`）；退路是失败补偿删除刚建 source，并测试“失败后项目内不残留资料” |
| **P2** | 库内版本语义 | `db/library_store.py:192-201,215-226`：一版一行、取最新、无冻结概念；关联时项目副本在摄取时冻结 | 明确并记录语义（“关联即冻结到项目副本，库侧后续更新不回溯”），必要时在 attach 响应中带版本号；不引入新的版本系统 |
| **P2** | 计划个性化 | `learning/plan_builders.py:809-882` 仅用 weekly_hours + 领域关键词 | 只记录路线（v0 模板 → 后续按真实目标个性化），本轮不改算法 |

---

## 7. 批次 3：验收（空库真实用户路径）

在全新临时库上走：注册 → 登录 → 项目 → 诊断 → 生成计划 → 任务流转 → 自报反馈 → 关联知识库 → 检索/引用 → 退出 → 登录另一账号（不得看到前一账号数据）。
证据要求：迁移一次成功日志、浏览器截图/脚本输出、PG 重启读回、`ROUND8_GATE_PASSED` 全量门禁（本地，全新预览实例单次运行）。

---

## 8. 验收标准（对齐用户方案第九节）

- **认证**：公开入口只有注册与登录；`/me`、退出、退出所有设备保留；OpenAPI 中不存在兑换端点；任何测试/模块都不得依赖邀请码取得身份。
- **代码**：全仓扫描 `invitation|invite|邀请码|exchange_invitation` 在运行时代码、数据库基线、配置与有效测试中零残留；历史文档明确标注为历史。
- **数据**：全新库无邀请表/函数；账号与项目授权正常；RLS 用例通过；`EXPECTED_SCHEMA_VERSION == "0001"` 且单头。
- **密码**：5 拒绝、6 允许、12 允许、13 拒绝；前后端一致。
- **产品**：新账号可完成全部核心流程；换账号后看不到前一账号数据（含知识库）。

---

## 9. 风险与停止条件

| 风险 | 停止条件与处理 |
|---|---|
| 压扁迁移漏对象（策略/授权/函数） | `pg_dump` 对象 diff 不为空即停止，逐项补齐后再继续；PG 子集不全绿不得进入 Batch 2 |
| 测试夹具改造导致“假通过”（绕过真实认证） | 若发现仍有测试用仓储直发会话取得身份，视为未完成；`auth_headers` 必须是真实注册/登录 |
| 口令改 6–12 后旧夹具失效 | 逐处改成合法长度；若出现“为了通过而放宽断言”的改法，视为未完成 |
| 破坏身份隔离/项目归属 | 任何 RLS、membership、会话签名相关测试变红都按 Release Blocker 处理，不允许放宽断言 |
| 真实数据库被误操作 | 本批禁止对真实库执行任何破坏性命令；生产库重建须用户单独确认目标环境 |