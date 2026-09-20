# 07 · Round 1 产品数据模型与接口冻结

> 本文冻结 Round 1「持久化与产品底座」的数据模型、RLS 策略、接口清单与迁移边界。
> 执行计划见 `plans/2026-09-19-round-1-persistence-product-foundation.md`
> （已按本冻结稿的偏差结论修订，2026-09-19）。
>
> **冻结的含义**：本文一旦确认，`0002` 迁移与产品契约按此实现；
> 后续若要改列名、改 RLS 谓词、改接口形状，必须改本文并说明原因，
> 而不是在实现里悄悄偏离。

## 1. 本轮范围

**做**：把产品状态从内存搬到 PostgreSQL，并提供稳定的产品 API。
`projects` / `conversations` / `messages` / `learning_plans` / `milestones` /
`learning_tasks` / `sources` / `invitations` / `user_sessions` / `http_idempotency`
落库；身份从"贴令牌"改为"一次性邀请 + HTTP-only 会话 cookie"。

**不做**（留给后续轮次）：模型接入、文档解析与切块、向量检索、reranker、
React 前端、沙箱、公开注册、计费、运维后台。

不做的部分**不预留空壳**：宁可没有表，也不要建一张"看起来已经实现了"的空表。
`source_chunks` 与摄取任务状态**不在本轮建表** —— 它们随 Round 2 的文档处理迁移一起落地。

---

## 2. 与执行计划的偏差（已确认）

执行计划初稿有三处与仓库现状不符、一处自相矛盾。逐条结论如下，
**都已回写进计划文件**，这里是决策依据。

### 偏差 1 · `tools/skills/gen_sql_schema.py` 不存在（已订正）

SQL 契约实际由 `tools/skills/gen_contracts.py` 的 `sql_schema` 渲染器生成。
计划的文件清单与命令已改为 `gen_contracts.py --target sql-schema [--check]`。

### 偏差 2 · 幂等键只允许一个判定出口（已确认）

初稿的 Task 1 要求业务表带 `(tenant_id, project_id, client_key)` 唯一约束，
Task 6 又要求 `http_idempotency` 统一承担命令幂等 —— 同一个重复请求会有两个判定出口，
一个说"重放"、一个说"冲突"。

**结论（已写进 Global Constraints）**：

- `http_idempotency` 是**唯一**决定"重放 / 改体冲突"的层；**业务表从不解释客户端 key**。
- 唯一键 `(tenant_id, principal_id, command_scope, client_key)`。
- **`project_id` 不进入唯一键** —— 创建项目时它还不存在，进键就等于让"创建项目"这个命令
  永远无法幂等。`project_id` 只作为可空的**范围过滤与审计**字段。
- `request_hash` 参与比较但**在唯一标识匹配之后**：相等 → 重放或处理中；不等 → 改体冲突。
- 业务表只保留**领域唯一约束**（见 4.1 各表）：它们的职责是**报告不变量被违反**，
  永不决定 HTTP 重放语义。
- 下层的工具调用幂等（`workflow/runtime.py`）**保持不变** ——
  它防的是外部副作用重复，与 HTTP 响应重放是两件事，不能合并。

### 偏差 3 · 项目模型只能有一个（已确认）

`identity/membership.py` 已有 `ProjectRecord(project_id, tenant_id, name)`，
而 `product/models.py` 又要放一个 `Project`。两者表达同一行数据。

**结论**：`identity/models.py` 的 **`LearningProject`** 是唯一的项目身份/归属模型；
`product/models.py` **不得**定义项目模型，也不得重复项目身份与名称字段。
`MembershipStore` 从私有 `ProjectRecord` 迁移到这个类型，调用方全部更新后
**删除 `ProjectRecord`**（不留兼容别名 —— 两个类同时存在时，
总有一个会先被改坏而另一个不会）。

### 偏差 4 · `app/config.py` 与 `db/settings.py` 职责重叠（已确认分工）

`db/settings.py` 保持**唯一 DSN 来源**（它已写明"两个角色两个 DSN 不能混用"）；
`app/config.py` 只做**应用级设置聚合**（storage 选择、环境名、cookie 密钥与安全标志），
其 DSN 字段**委托**给 `db/settings.py` —— 不得出现第二个
`os.environ.get("STUDY_PLATFORM_DSN")`。

---

## 3. 命名约定（防止"一个东西两个名字"）

| 层次 | 项目标识的字段名 | 说明 |
|---|---|---|
| 数据库列 | `project_id` | 与 `0001` 一致 |
| identity / product 层 Python 契约 | `project_id` | 客户端看到的就是它 |
| learning 层 Python 契约 | `learning_project_id` | **映射到同一列 `project_id`** |

本表是唯一说明这个映射的地方 —— 别再各处解释一遍。

---

## 4. 数据模型

### 4.1 新增表（`0002`，共 9 张）

除 `source_chunks` 外**一律对 `tenant_id` 建外键**：
"有租户列却指向不存在租户的行"本身就是数据损坏。

#### `invitations` — 一次性邀请 · 租户级

| 列 | 类型 | 约束 |
|---|---|---|
| `invitation_id` | text | PK |
| `tenant_id` | text | NOT NULL, FK → tenants |
| `token_hash` | text | NOT NULL, **UNIQUE**（领域唯一约束） |
| `issued_by` | text | NOT NULL（签发者 principal_id） |
| `issued_at` / `expires_at` | timestamptz | NOT NULL，`CHECK (expires_at > issued_at)` |
| `consumed_at` | timestamptz | 可空 |
| `consumed_by` | text | 可空 |

- 只存 `sha256(raw_token)`；**原始令牌不进库、不进日志、不进审计 payload**。
- 消费用 `UPDATE ... WHERE invitation_id = %s AND consumed_at IS NULL AND expires_at > now() RETURNING ...`
  —— 单条语句原子占用，不靠"先查后写"。
- 未知 / 已消费 / 过期**返回同一个公开错误**（不区分，否则这个接口就成了探针）。

#### `user_sessions` — 数据库支撑的会话 · 租户级

| 列 | 类型 | 约束 |
|---|---|---|
| `session_id` | text | PK |
| `tenant_id` | text | NOT NULL, FK → tenants |
| `principal_id` | text | NOT NULL |
| `issued_at` / `expires_at` | timestamptz | NOT NULL，`CHECK (expires_at > issued_at)` |
| `revoked_at` | timestamptz | 可空（退出即写入） |

- cookie 放**签名保护的声明载荷**：`session_id` + `tenant_id` + `principal_id` +
  签发/到期时间。验签通过后才可信这些字段，用它们设置 RLS 上下文，
  再查 `user_sessions` 的撤销状态。
  ⚠️ 不再称它"不透明值" —— 它对持有者是**可见但防篡改**的，两种说法差一个威胁模型：
  "不透明"暗示内容不可见，而这里的关键是**不可伪造**，不是不可读。
  载荷里不放身份之外的任何敏感字段（没有角色、没有邮箱）。

#### `conversations` — 连续问答的容器 · 项目级

| 列 | 类型 | 约束 |
|---|---|---|
| `conversation_id` | text | PK |
| `tenant_id` / `project_id` | text | NOT NULL, FK |
| `title` | text | NOT NULL DEFAULT '' |
| `last_message_seq` | bigint | NOT NULL DEFAULT 0, `CHECK (last_message_seq >= 0)` |
| `created_at` | timestamptz | NOT NULL DEFAULT now() |

- `last_message_seq` 是**消息序号的原子分配器**：
  `UPDATE conversations SET last_message_seq = last_message_seq + 1 WHERE conversation_id = %s RETURNING last_message_seq`。
  并发追加无需重试循环；`messages` 上的唯一约束只是最后防线。

#### `messages` — append-only 消息 · 项目级

| 列 | 类型 | 约束 |
|---|---|---|
| `message_id` | text | PK |
| `tenant_id` / `project_id` / `conversation_id` | text | NOT NULL, FK |
| `seq` | bigint | NOT NULL, `CHECK (seq > 0)`，**UNIQUE (conversation_id, seq)** |
| `role` | text | NOT NULL，`CHECK (role IN ('user','assistant','system'))` |
| `content` | text | NOT NULL |
| `created_at` | timestamptz | NOT NULL DEFAULT now() |

- **append-only 由权限保证**：应用角色只有 `SELECT, INSERT`。
- 本轮 `assistant` 消息只在受信应用服务提供时写入；Round 1 没有模型，
  正常路径只产生 `user` 消息。**必须写进契约文档** ——
  否则前端会以为"发了消息就该收到回复"。

#### `learning_plans` — 学习计划 · 项目级

| 列 | 类型 | 约束 |
|---|---|---|
| `plan_id` | text | PK |
| `tenant_id` / `project_id` | text | NOT NULL, FK |
| `version` | integer | NOT NULL, `CHECK (version > 0)`，**UNIQUE (project_id, version)** |
| `goal` | text | NOT NULL |
| `status` | text | NOT NULL，`CHECK (status IN ('draft','active','archived'))` |
| `created_at` | timestamptz | NOT NULL DEFAULT now() |

- 一个项目可有多版；"当前计划" = `version` 最大的那一行。
  **不做 `is_current` 布尔列** —— 它需要"改这个必须同时改那个"的成对更新，
  是另一处 check-then-act。

#### `milestones` / `learning_tasks` — 计划的结构 · 项目级

`milestones`：`milestone_id` PK、`tenant_id`/`project_id`/`plan_id` FK、
`order_index` integer `CHECK (order_index >= 0)`、`title`、`description`，
**UNIQUE (plan_id, order_index)**。

`learning_tasks`：`task_id` PK、`tenant_id`/`project_id`/`milestone_id` FK、
`order_index`、`title`、
`status` `CHECK (status IN ('pending','in_progress','done','skipped'))`，
**UNIQUE (milestone_id, order_index)**。

- 顺序用**显式 `order_index`**，不用插入顺序：序号是内容的一部分，且未来要支持拖动排序。

#### `sources` — 资料**登记**元数据 · 项目级

| 列 | 类型 | 约束 |
|---|---|---|
| `source_id` | text | PK |
| `tenant_id` / `project_id` | text | NOT NULL, FK |
| `display_name` | text | NOT NULL |
| `media_type` | text | NOT NULL DEFAULT '' |
| `identity_hash` | text | NOT NULL（服务端算：`sha256(规范化 uri)`） |
| `acquisition` | jsonb | NOT NULL DEFAULT `'{}'`（来源方式、uri 等获取元数据） |
| `registered_at` | timestamptz | NOT NULL DEFAULT now() |

**UNIQUE (project_id, identity_hash)** —— 同一项目内同一资料不重复登记。

⚠️ **本轮没有任何处理状态**。不定义也不返回 `processing` / `ready`：
Round 1 只登记元数据，`registered_at` 之后什么都不发生。
Round 2 增加摄取任务、切块与它们的状态机时，**那些状态才可能出现**。
理由与 `EvidenceIssueCode` 的三级诚实标注一致：
**一个永不触发的状态比缺失的状态更糟，因为它看起来已经实现。**

#### `http_idempotency` — 命令幂等（唯一出口）· 租户级

| 列 | 类型 | 约束 |
|---|---|---|
| `claim_id` | text | PK |
| `tenant_id` | text | NOT NULL, FK → tenants |
| `principal_id` | text | NOT NULL |
| `command_scope` | text | NOT NULL（命令标识，如 `POST /projects`） |
| `client_key` | text | NOT NULL（客户端提供，≤ 200 字符） |
| `request_hash` | text | NOT NULL |
| `project_id` | text | **可空** —— 仅范围过滤与审计，**不参与唯一键** |
| `state` | text | NOT NULL，`CHECK (state IN ('pending','completed','released'))` |
| `status_code` | integer | 可空 |
| `response_body` | jsonb | 可空（**有界**，超限只存摘要） |
| `claimed_at` | timestamptz | NOT NULL DEFAULT now() |
| `completed_at` | timestamptz | 可空 |

**UNIQUE (tenant_id, principal_id, command_scope, client_key)**

三重作用域缺一不可：少主体 → 猜到 key 就能读到别人的响应；
少命令 → 同一把钥匙在两个端点上互相串味；
少租户 → 跨租户可用性干扰。

- `state` 三态与运行时幂等表同构；失败与拒绝**不缓存**（`released`），
  否则修好参数的请求会永远拿到旧拒绝。
- 判定顺序：**唯一标识匹配 → 再比 `request_hash`**。相等即重放/处理中，不等即改体冲突。
- **不给 DELETE**：retention 只在 schema 上留 `completed_at` 与索引，
  清理任务不属于本轮（写进 README 已知局限）。

### 4.2 对 `projects` 表的增量修改

`0001` 的 `projects` 只有 `(project_id, tenant_id, name, created_at)`。本轮补：

| 列 | 类型 | 约束 |
|---|---|---|
| `goal` | text | NOT NULL DEFAULT '' |
| `version` | integer | NOT NULL DEFAULT 1, `CHECK (version > 0)` |
| `updated_at` | timestamptz | NOT NULL DEFAULT now() |

`version` 是**乐观锁**：`PATCH` 必须带版本，旧版本返回 409。
不能靠"最后写入者赢" —— 那会让两个人的编辑互相静默覆盖。
默认值让既有行无需回填（PG 11+ 的 `NOT NULL DEFAULT` 是元数据操作，不重写表）。

### 4.3 RLS 策略矩阵（本轮的关键变化）

`0001` 只有两级：租户级、项目级。本轮新增**第三级：成员感知的项目级**。

| 表 | 策略级别 | 谓词要点 |
|---|---|---|
| `tenants` | 不隔离 | 全局字典表，只读 |
| `principals` | 租户 | `tenant_id = app.tenant_id` |
| `project_grants` | 租户 | 同上（**不能**引用 `projects`，否则自引用递归） |
| `projects` | **成员感知** | 见下 |
| `confirmations` / `evidence_events` / `action_intents` | 项目 | `tenant_id` + `project_id` |
| `invitations` | 租户 | `tenant_id`（消费前**没有**主体，见 9.1） |
| `user_sessions` / `http_idempotency` | 租户 + **主体** | `tenant_id` AND `principal_id` |
| `conversations` / `messages` / `learning_plans` / `milestones` / `learning_tasks` / `sources` | 项目 | `tenant_id` + `project_id` |

`projects` 的策略：

```sql
USING (
    tenant_id = current_setting('app.tenant_id', true)
    AND EXISTS (
        SELECT 1 FROM project_grants g
        WHERE g.project_id = projects.project_id
          AND g.principal_id = current_setting('app.principal_id', true)
    )
)
WITH CHECK (
    tenant_id = current_setting('app.tenant_id', true)
)
```

**为什么 `USING` 与 `WITH CHECK` 不对称**：新建项目的那一刻还没有 `project_grants` 行，
若 `WITH CHECK` 也要求成员存在，**项目永远创建不出来**。
读权限靠成员关系（安全），写权限靠租户上下文 + 事务内同时写 grant（正确性）。

**代价（必须承认）**：`USING` 里的 `EXISTS` 让项目列表多一次扫描，
需要 `(principal_id, project_id)` 索引；策略也比纯列比较更难被读懂。
换来的是"同租户内的用户 A 看不到用户 B 的项目" —— 这正是 `0001` 的租户级策略做不到的。

**未做的部分（诚实标注）**：`tenant_admin` 目前**不能**看到租户内全部项目，
策略只认 `project_grants`。管理视图属于运维后台那轮，本轮不做，也不假装支持。

### 4.4 新增索引

```sql
-- 成员感知策略的支撑索引（缺了它，项目列表会退化成全表扫 + 逐行 EXISTS）
CREATE INDEX project_grants_principal_idx ON project_grants (principal_id, project_id);
-- 会话清理与存活查询
CREATE INDEX user_sessions_expiry_idx ON user_sessions (tenant_id, expires_at);
CREATE INDEX user_sessions_live_idx ON user_sessions (session_id) WHERE revoked_at IS NULL;
-- 邀请清理（只索引未消费的）
CREATE INDEX invitations_live_idx ON invitations (expires_at) WHERE consumed_at IS NULL;
-- 消息按会话稳定排序
CREATE INDEX messages_conversation_seq_idx ON messages (conversation_id, seq);
-- 计划取最新版本
CREATE INDEX learning_plans_project_version_idx ON learning_plans (project_id, version DESC);
-- 资料按项目列出
CREATE INDEX sources_project_idx ON sources (project_id, registered_at);
```

`http_idempotency` 与各领域唯一约束用 `UNIQUE` 约束承载（自带索引），不再重复建。

### 4.5 权限（GRANT）

| 表 | 应用角色权限 |
|---|---|
| 一般表 | `SELECT, INSERT, UPDATE, DELETE` |
| `messages` | **只有 `SELECT, INSERT`** —— append-only 由权限保证 |
| `http_idempotency` | `SELECT, INSERT, UPDATE`（要写回结果）—— **不给 DELETE** |
| `evidence_events` | 保持 `0001` 的只读 + 插入 |

---

## 5. 接口清单（Round 1 冻结）

认证：除 `/auth/invitations/exchange` 与 `/healthz` 外全部要求已认证。
变更类接口（POST/PATCH/PUT）**要求 `Idempotency-Key` 请求头**。

| 方法 | 路径 | 用途 | 幂等键 |
|---|---|---|---|
| POST | `/auth/invitations/exchange` | 一次性邀请换会话 cookie | 否 |
| POST | `/auth/logout` | 撤销会话并清 cookie | 是 |
| GET | `/me` | 当前身份（cookie 优先） | 否 |
| GET | `/projects` | 我被授权可见的项目 | 否 |
| POST | `/projects` | 创建项目 + 所有者授权（同一事务） | **是** |
| GET | `/projects/{project_id}` | 项目详情 | 否 |
| PATCH | `/projects/{project_id}` | 改名/改目标，带 `version`，旧版本 409 | **是** |
| GET/POST | `/projects/{project_id}/conversations` | 会话列表 / 新建 | POST 是 |
| GET/POST | `.../conversations/{conversation_id}/messages` | 消息列表 / 追加 | POST 是 |
| GET/PUT | `/projects/{project_id}/plan` | 读当前计划 / 整体替换（产生新版本） | PUT 是 |
| GET/POST | `/projects/{project_id}/sources` | 资料列表 / **登记**元数据 | POST 是 |
| GET | `/projects/{project_id}/sources/{source_id}` | 资料登记详情 | 否 |
| GET | `/healthz` | 适配器类型与迁移就绪度（不泄露 DSN） | 否 |

**契约硬约束**：

1. 请求体**永不接受** `tenant_id` / `principal_id` / 项目归属字段
   （`extra="forbid"` + 不定义这些字段）。
2. 响应 DTO **不含**内部字段：存储路径、策略快照、审计内部、taint 背书。
3. 跨租户 / 跨项目访问一律 **404**（不暴露存在性）。
4. 未认证 → 401；未授权但资源存在 → 404；版本冲突 → 409；
   幂等键被复用于不同内容 → 409（`IDEMPOTENCY_VIOLATION`）。
5. **`sources` 的响应不含任何"处理状态"** —— 本轮没有它（见 4.1）。

**保留的兼容面**：Bearer 令牌认证**保留**，但降级为"仅测试与运维的显式兼容适配器"，
不再是产品路径。README 里"贴令牌"的说明在 Task 8 从用户路径删除。

---

## 6. 迁移边界

**`0002_product_foundation.py` 只做**：

1. 建 4.1 的 **9 张**新表 + 约束 + 索引；
2. 给 `projects` 加 3 列；
3. 把 `projects` 的策略换成成员感知版，并引入会话变量 `app.principal_id`；
4. 对新表 `ENABLE` + `FORCE ROW LEVEL SECURITY` 并建策略；
5. 按 4.5 发放 GRANT。

**不做**：数据回填（新列有默认值）、表重命名、`0001` 已有表的其它改动、
`source_chunks` 与摄取状态（Round 2）。

**`downgrade()` 必须真的能回滚**：删新表与索引，把 `projects` 的策略
**还原成 `0001` 的租户级版本**（而不是只 `DROP POLICY` 留一张无策略的表 ——
无策略 + `FORCE RLS` 等于拒绝一切访问），再用 `DROP COLUMN` 删掉 3 个新列。

**迁移角色与应用角色**：沿用 `0001` —— 要求 `study_app` 已存在，
不存在就显式失败（不跳过 GRANT）。

---

## 7. 退出标准（可机械验收）

1. **数据不丢**：重建应用实例后，通过公开 API 能读到之前创建的项目 / 会话 / 消息 / 计划 / 资料。
2. **边界由数据库兜底**：用 `study_app` 连接、**故意不设置** `app.tenant_id`，
   任何租户表查询返回 **0 行**（而不是全部）；换成另一个租户同样 0 行。
3. **同租户内的用户隔离**：用户 A 看不到用户 B 的项目（成员感知策略的验收点）。
4. **一次性邀请**：同 token 交换两次 → 第二次失败；过期 → 失败；
   未知 / 已消费 / 过期**返回同一个公开错误**。
5. **幂等**：同键同体重放返回原响应；同键异体 → 409；
   两个并发同键请求只有一个真正执行；**创建项目时 `project_id` 尚不存在也能幂等**。
6. **迁移可逆**：`upgrade head` → `downgrade -1` → `upgrade head` 全通过。
7. **门禁**：`pytest`（非 PostgreSQL 组全过）、`pytest -m postgres` 全过、
   ruff、mypy、manifest、契约 `--check`、迁移门、许可证、秘密扫描 —— 九道全绿。

---

## 8. 已确认的三个决策（记录结论，供后续轮次查阅）

1. **幂等只有一个出口**：`http_idempotency`，唯一键
   `(tenant_id, principal_id, command_scope, client_key)`；
   `project_id` 可空、不进唯一键；业务表只保留领域唯一约束。
2. **项目模型只有一个**：`identity/models.py` 的 `LearningProject`；
   删除 `ProjectRecord`；`product/models.py` 不定义项目模型。
3. **`sources` 本轮无处理状态**：只存登记元数据与 `registered_at`，
   不定义也不返回 `processing` / `ready`；Round 2 随摄取状态机引入。

---

## 9. 自查补充（任务 1 完成后按审查者标准回查）

实测抓到一处**真实的隔离缺口**，并补了一条机械守卫。

### 9.1 `user_sessions` 与 `http_idempotency` 曾只受租户级保护

漏的是**主体维度**。实测（`study_app` 角色，同一租户两个主体）：

| 探测 | 改前 | 改后 |
|---|---|---|
| A 看自己的会话 / 幂等记录 | 1 / 1 | 1 / 1（正向对照：没变成"谁都看不到"） |
| **B 看 A 的会话 / 幂等记录** | **1 / 1** | **0 / 0** |
| **B 能读到的 `response_body`** | **1** | **0** |

`http_idempotency.response_body` 存的是**命令响应**（业务载荷），不是元数据 ——
同租户的另一个用户可以把它整条读走。

应用层当然会按 `principal_id` 过滤，但 **RLS 存在的意义就是"应用层写错也不泄露"**：
只按租户过滤时它没兜住。修法：这两张表加主体维度。

`invitations` **刻意**保持租户级：消费前它没有主体（`consumed_by` 是消费后才写），
加主体维度会让邀请根本没法被认领。泄露面是 `issued_by` 与邀请数量 ——
属租户内管理信息，可接受，但记录在此。

### 9.2 声明的隔离级别必须有机械守卫

契约里的"隔离级别"来自迁移里的**人工声明**，而真正生效的是 `pg_policy`。
两者漂移时契约就在撒谎 —— 这次缺陷正是这样藏住的：声明的常量写着"租户级"，
**没有任何地方问过"这张表有 `principal_id` 列，为什么不约束它"**。

新增 `tests/test_rls_policy_matches_declaration.py`（PostgreSQL 组），
规则**完全从数据库推导**：

> 表里有 `tenant_id` / `project_id` / `principal_id` 且 **NOT NULL**，
> 策略就必须真的约束对应的会话变量。

限定 NOT NULL 是必要的：`http_idempotency.project_id` 可空（创建项目的命令被占用时
项目还不存在），加项目维度会让那条幂等记录**永远读不出来**。
规则粗糙时只能用豁免去补，而豁免清单会自己长大。

两类例外分开记，性质完全不同：

- **`EXEMPT`** —— 规则在这里**本不适用**（`project_grants` 的两个维度、
  `projects` 的 project 维度），每条附理由；
- **`KNOWN_GAP`** —— 规则适用、**现在还没做**。当前一条：`confirmations`
  （项目级策略下同项目成员能读到彼此批准了什么；补它需要 `tenant_transaction`
  支持设置 `app.principal_id`，属任务 2/3 的连带改动）。

并且有测试断言"缺口仍然是缺口"、"豁免仍然会被规则命中"：
**修好之后它会失败，提醒你来删条目** —— 否则这两份清单只会自我繁殖。

### 9.3 契约展示改为"叠加了几层"

隔离级别不再是一个词，而是 `租户` / `租户+项目` / `租户+主体`。
一个笼统的"租户级"正是漏掉主体维度的原因：它看起来已经描述完了。

---

## 10. 审查第二轮补充（`0003_auth_bootstrap`）

第二轮审查实测确认两个漏洞、一个流程要求，`0003` 逐一落地：

### 10.1 跨租户组合引用没人拦（已修）

各表的 `project_id` 外键是单列 —— 审查实测「`tenant X` 的行指向 `tenant Y`
的项目」超级用户与应用角色**都能插进去**。RLS 是读的边界，替代不了写的组合完整性。

修法：`principals` / `projects` 加 `(tenant_id, …)` 组合唯一键，
全部指向它们的单列外键替换为组合外键（含 `http_idempotency` 的
`(tenant_id, principal_id)` 与 `(tenant_id, project_id)`）。
组合外键下 NULL 不受约束，所以 `project_id` 可空的幂等表无需特判。

⚠️ **测试位置揭示了外键的真实防线**：项目级表的 `WITH CHECK` 同时校验
租户与项目维度，跨租户组合会被 RLS **先**拦下——轮不到外键。
组合外键真正的防线在 RLS 看不见的两处：超级用户/运维连接，
以及只有"租户+主体"策略的 `http_idempotency`。测试按这两个场景写，
并由反向验证证明：拆掉外键后错配组合**真的能插进去**。

### 10.2 邀请兑换没有引导通道（已修）

兑换发生在**还没有任何身份**的时刻，而邀请表受租户 RLS 保护——
实测 `study_app` 无上下文查它是 0 行；且邀请没绑定被邀请主体，
只能让客户端自报"我是谁"。

修法：
- 邀请**签发时**绑定 `invitee_principal_id`（NOT NULL + 组合外键）；
- `public.exchange_invitation(token_hash, session_id, expires_at)` 以
  `SECURITY DEFINER` 原子完成「消费邀请 + 建会话」，
  **不收租户、不收主体** —— 客户端无身份可自报。
- 并发兑换由行锁串行化，测试验证 6 路并发恰好产生 1 条会话。

**权限面**（这是它安全的原因）：只收哈希、只做两件事、`SET search_path`、
REVOKE PUBLIC / 只 GRANT `study_app` —— 应用能"调用它"，
拿不到"无上下文读表"的能力。
⚠️ 运维前提：函数 owner 必须拥有表且能绕过 FORCE RLS（本机为超级用户），
否则兑换被静默拦成 0 行。

### 10.3 Cookie 载荷修正（见 4.1 的修订）

`user_sessions` 加了主体维度后，只有 `session_id` 无法建立查询上下文。
cookie 改为签名保护的声明载荷（`session_id` + `tenant_id` + `principal_id` +
时间），验签 → 设置上下文 → 查撤销。**不再称"不透明"** ——
它可见但防篡改，两种说法差一个威胁模型。

### 10.4 退出门新增流程要求

**PostgreSQL 组的测试被 skip 就等于这一关没过**：任务 2 起的每次退出门，
都必须实际启动数据库并确认对应测试真实执行（不看"通过数"，看"执行数"）。
