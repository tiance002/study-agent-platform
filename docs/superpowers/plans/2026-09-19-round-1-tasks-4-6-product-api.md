# 第一轮任务 4-6 计划：产品 API 与幂等持久化

> 状态：已定稿，未开工。
> 前置：任务 1-3 已完成（契约与迁移 0002/0003、身份仓储、邀请 Cookie 会话）。
> 本计划含一轮自审查发现的 4 个前置修复项（任务 0），先修后建，不把缺陷带进新代码。

## 自审查发现（2026-09-19，任务 2/3 回查）

| # | 发现 | 严重度 | 核实方式 |
|---|---|---|---|
| 1 | `db/identity_store.py:235` 用 `assert` 守"兑换后回读必须可见"——`python -O` 会剥掉断言，恰恰在最需要它的时候失效。铁律 5 犯第二遍（第一遍在 runtime 里修的） | P1 | grep 确认存在 |
| 2 | 邀请兑换与认证失败**零审计事件**——安全敏感路径（谁在何时用什么 token 尝试了进入、成功与否）完全无记录，与"审计是事实源"的架构前提冲突 | P1 | grep 确认 auth_routes/cookie_auth 无 audit 引用 |
| 3 | 内存版 `exchange` 的"标记消费"在锁内、"建会话"在锁外——故障时会出现"邀请已消费但会话不存在"；PG 版由函数内单事务保证，两适配器故障原子性语义不一致 | P2 | 读代码确认锁范围 |
| 4 | 测试覆盖缺口：CSRF 只在 `/auth/logout` 上测过（业务端点如 `/interactions` 的 cookie+恶意 Origin 组合没测）；内存版兑换无并发测试（PG 版有 6 路并发）；cookie 声明过期（验签过了但 expires_at 已过）无专门测试 | P2 | 读测试文件确认 |
| 5 | 两条认证路径的 `Principal` 语义不对称：bearer 带 `display_name`/`roles`，cookie 的 `SessionClaims.to_principal()` 两者皆空——将来任何 admin 判定（`is_admin()`）对 cookie 用户恒 False | P3（记账） | 读两个 `to_principal` 确认 |

不修但记录的判断：cookie 无效时不回落 bearer（语义可辩护：带凭据的请求应明确失败，静默换凭据会掩盖客户端 bug）；`Origin`/`Host` 比对在反向代理下需配置注意（部署文档事项，属任务 8）。

## 任务 0：自审查修复（先行，半天量级）

**Files:**
- Modify: `backend/app/db/identity_store.py`（assert → 显式 raise `AUDIT_LOG_CORRUPTED` 之外的合适错误码，或回读失败即抛 `PLATFORM` 内部一致性错误）
- Modify: `backend/app/api/auth_routes.py`（兑换成功/失败各追加一条审计事件，`risk=HIGH`；**载荷不含 token、哈希、cookie 值**，只含结果与租户/主体/追踪 id）
- Modify: `backend/app/identity/memory_store.py`（`sessions.create` 挪进锁内）
- Modify: `backend/tests/test_invitation_auth.py`、`test_identity_repositories.py`（补：业务端点 CSRF、内存并发兑换、cookie 声明过期）

**要点:**
- 审计事件的 `event_type` 用稳定词汇（`invitation_exchanged` / `invitation_rejected` / `auth_failed`），失败与成功同等级记录——**审计不是只记好事**。
- 兑换失败的审计不带失败原因（未知/过期/已消费同一条），与统一拒绝的语义一致：审计里也不留探针缝。

**退出标准:** `python -O` 下全量测试仍过（证明没有一条守卫依赖 assert）；审计文件里能看到兑换成功与失败两类事件。

## 任务 4：项目 CRUD API

**Files:**
- Create: `backend/app/product/memory_store.py`（`InMemoryProductRepository`：ProjectRepository 五方法）
- Create: `backend/app/db/product_store.py`（`PostgresProductRepository`）
- Create: `backend/app/api/projects_routes.py`
- Create: `backend/tests/test_projects_api.py`
- Modify: `backend/app/main.py`（装配 `product` 仓储）

**接口清单:**
- `POST /projects` — 创建。**创建即授予创建者**（一次事务语义：没有"建了项目但自己看不见"的窗口）。`idempotency_key` 走任务 6 的统一机制。
- `GET /projects` — 列表，成员感知（复用 `MembershipRepository.list_for` 的语义，返回完整契约）。
- `GET /projects/{id}` — 详情。
- `PATCH /projects/{id}` — 改名/目标，**必须带 `expected_version`**；不匹配 → 409（乐观锁，不是"最后写入者赢"）。

**TDD 失败测试先行:**
- cookie 登录 → 建项目 → 列表可见 → 改名（version 递增）。
- 两个租户各建同名项目互不可见；跨租户 GET/PATCH 一律 404（统一拒绝，与 membership.get 同语义）。
- 乐观锁：旧 version 的 PATCH → 409，且数据未被部分改动。
- 内存/PG 双适配器契约测试（延续任务 2 的参数化模式）。

**明确不做:** 项目删除（产品决策未定：学习记录的归属权）、项目归档。

## 任务 5：会话、消息、计划与资料登记 API

**Files:**
- 扩展 `backend/app/product/memory_store.py` 与 `db/product_store.py`（Conversation/Message/Plan/Source 四组仓储）
- Create: `backend/app/api/product_routes.py`
- Create: `backend/tests/test_product_api.py`

**接口清单:**
- `POST /projects/{id}/conversations`、`GET /projects/{id}/conversations`
- `POST /projects/{id}/conversations/{cid}/messages`（**seq 由服务端分配**：PG 用 `UPDATE ... SET last_message_seq = last_message_seq + 1 RETURNING`，并发追加无空洞无重复；请求体不得带 seq）
- `GET /projects/{id}/conversations/{cid}/messages`（按 seq 升序）
- `PUT /projects/{id}/plan`（**PlanBundle 整版替换**：要么整版换掉要么什么都不变；版本号服务端递增）
- `GET /projects/{id}/plan`（当前版 = version 最大；无计划返回 204 而非空对象）
- `POST /projects/{id}/sources`（登记元数据；`identity_hash` 同项目去重，重复登记返回既有记录而非报错）
- `GET /projects/{id}/sources`

**TDD 失败测试先行:**
- 消息并发追加（`racy_scheduling` + 多线程）：seq 连续无重复——**反向验证**：把 PG 分配器换成"先查后写"必须变红。
- 计划替换的原子性：构造中途失败（注入异常）→ 库里还是旧版完整计划。
- 会话/消息/计划/资料的跨租户访问全部 404。
- messages append-only：应用角色对 messages 表无 UPDATE/DELETE 权限（迁移已给，补测试钉住）。

**明确不做:** 计划的逐任务状态流转 PATCH（属第二轮与掌握证据联动）、资料内容解析（第二轮 DocumentProcessor）。

## 任务 6：HTTP 幂等持久化

**Files:**
- Create: `backend/app/db/idempotency_store.py`（`http_idempotency` 表的适配器）
- Create: `backend/app/api/idempotency.py`（命令幂等中间件/依赖）
- Create: `backend/tests/test_http_idempotency.py`
- Modify: 全部写命令端点（POST /projects、POST messages、PUT plan…）接入

**语义（与 runtime 进程内幂等对齐，最终替换它）:**
- 唯一键 `(tenant_id, principal_id, command_scope, client_key)`；`command_scope` = 方法+路径模板（不含路径参数值——项目 id 进了 scope 会让"建项目"的幂等键随每次创建变化）。
- `claim-or-read` 原子语义，三态 `pending` / `completed` / `released`：首个请求 INSERT pending（唯一约束兜底并发）；等待者看到 pending → `IDEMPOTENCY_IN_PROGRESS`（可重试，与 VIOLATION 严格区分）；失败释放占用不缓存结果（缓存旧拒绝会让修好参数的重试永远拿旧拒绝——铁律 17）。
- 重放命中返回**缓存的响应体 + 本次追踪 id**，标注 `idempotent_replay: true`（重放必须可观测）。
- **进程内 runtime 幂等的去留**：HTTP 层落地后，runtime 的幂等缓存职责收窄为"交互执行层的业务幂等"（同一命令在执行引擎层的防重放）。两者不合并——HTTP 命令幂等与工具副作用幂等是两层（铁律 27）。

**TDD 失败测试先行:**
- 同键同内容重试 → 返回缓存响应，不执行第二次（handler 计数 = 1）。
- 同键换内容 → `IDEMPOTENCY_VIOLATION`。
- 并发同键（压小 GIL + 屏障）→ 恰好一次执行，其余 IN_PROGRESS 或等待后拿缓存。
- 进程重启（新建适配器实例）后重试仍命中缓存——**持久化的定义**。
- 失败后的重试真的重新执行（占用被释放）。

## 任务 7：生产装配切换与重启恢复（原计划保留）

- `build_platform` 增加 PG 装配分支（环境变量驱动）；启动自检报告各组件是内存还是 PG。
- 端到端：邀请登录 → 建项目 → 发消息 → **重启服务** → 会话仍有效、数据仍在。
- README「已知局限」同步收缩（哪些"内存适配器"标注可以摘掉）。

## 任务 8：第一轮整体验收（原计划保留）

- 空库从零迁移 → 全链路冒烟 → `downgrade` 往返。
- 八道门禁全绿 + PG 组零 skip（脚本化，一条命令可复跑）。
- 三件套（task_plan / findings / progress）收尾更新。
- 组织一次"外部视角"审查（用户或另一轮 review），通过后宣布第一轮完成。

## 执行顺序与合并纪律

1. **任务 0 → 4 → 5 → 6 → 7 → 8**，严格串行合并；每个任务一个提交，提交前八道门禁全绿。
2. 数据模型（契约/迁移）不动——0002/0003 已冻结本轮所需全部表；若任务 5/6 实现中发现表结构缺口，**停下来补规格与迁移评审**，不允许"先写代码后补表"。
3. 每个新仓储必须同时交付内存与 PG 两个实现 + 同一套参数化契约测试（任务 2 建立的模式）。
4. 所有 SQL 参数绑定、事务边界即方法边界、数据库错误翻译成平台错误（`db/identity_store.py` 的三条纪律）。

## 本阶段退出标准（汇总）

- [ ] 自审查 4 项修复落地且有反向验证
- [ ] cookie 登录用户全程（无需终端/令牌）完成：建项目 → 改名（乐观锁）→ 开会话 → 发消息（并发无空洞）→ 换计划（原子）→ 登记资料
- [ ] 同一幂等键跨进程重启重试返回缓存响应；失败重试真实重执行
- [ ] 跨租户/跨项目访问在**每一组**新端点上被测试拦截
- [ ] PG 组测试零 skip；内存/PG 契约一致
- [ ] `python -O` 全量测试通过（无守卫依赖 assert）
- [ ] 审计覆盖认证与兑换的成功/失败路径
