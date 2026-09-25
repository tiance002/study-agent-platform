# Agent 工程学习规划平台

多用户 SaaS 学习助手，首个垂直领域为 **Agent 工程实践**。教学法是 project-first：围绕用户想解决的真实问题反推知识、先修关系与验收证据，而不是先摆一套固定课程。

本仓库包含设计规格、可运行的首版应用、PostgreSQL 持久化实现与生产部署资产。

---

## 当前能力与边界

首版已支持开放注册、密码登录、Cookie 会话、项目与对话、计划和任务、资料上传与持久摄取、关键词检索、学习证据，以及可持久恢复的教学运行。生产环境使用 PostgreSQL、nginx、systemd worker 和真实 DeepSeek 兼容接口；开发环境默认使用内存适配器，也可显式切换 PostgreSQL。

> 测试数量**不在本文档里硬编码**（写死会随迭代过期）。用 `pytest --collect-only -q | tail -1` 查看当前值。

下面的表格记录已经实现的边界和仍然明确受限的能力；受限能力不会由模拟结果伪装成已完成。

| 能力 | 本版现状 | 边界 |
|---|---|---|
| ~~PostgreSQL 接入应用~~ | **主要领域已实现**：身份、产品、学习闭环、摄取四组仓储都有 PostgreSQL 适配器，跑同一套参数化契约测试，各有端到端退出门 | 开发默认仍是内存适配器；审计 sink 仍写本地文件；registry / workflow 运行时尚在进程内 |
| ~~认证与授权~~ | **已实现**：签名会话令牌 + 项目归属校验；请求体不含任何身份字段 | 令牌签发的生产替代（OIDC / SAML）待接入 |
| ~~客户端自报确认~~ | **已实现**：确认是服务端记录，绑定主体 / 项目 / 工具 / 参数 / 有效期，单次消费 | 批量确认与确认疲劳限流未实现 |
| KMS / Secret Manager | 环境变量 + 开发密钥 | 密钥管理与轮换缺失 |
| 隔离沙箱（gVisor/Firecracker） | `run_in_sandbox` 为占位，**不执行任何代码** | 无代码执行能力 |
| Fetcher / Package Proxy | **已实现受限网页获取**：worker 在 URL/DNS 校验、重定向逐跳校验和地址固定连接后请求网页 | 通用包代理未实现；抓取仅支持受限的 HTTP/HTML 路径 |
| 资料解析格式 | 支持 UTF-8 纯文本、Markdown 与受限 HTML 文本抽取，内容大小有界 | 无 PDF / Office / OCR；网页解析结果受远端内容变化影响 |
| 语义检索 | **不是**：检索是 `keyword/v1` 关键词基线（NFKC 归一 + 中文 2-gram + 整数权重），结果确定性可复现 | 同义改写命中不了；无 pgvector、无 reranker、无中文分词器 |
| 模型调用（L0/L1/L2） | 已接入 OpenAI Responses 兼容 provider，并完成 DeepSeek 生产联调 | 无资料时明确降级为 `inference_only`；尚无原生 token 流 |
| ~~durable 摄取队列~~ | **已实现**：`ingestion_jobs` + lease + `FOR UPDATE SKIP LOCKED`；崩溃后租约到期可回收，重复完成幂等 | outbox（面向外部副作用的持久化派发）仍未实现 |
| ~~摄取凭据边界~~ | **已实现**：跨租户队列可见性限给 `study_worker` **数据库角色**（迁移 `0008`）；应用角色自设 `app.worker_id` 无效，且已收回队列表的 `UPDATE` | 真实的凭据轮换与最小权限审计未做过（本机 trust 认证，密码不参与验证） |
| ~~认领围栏~~ | **已实现**：每次认领产生不可复用的 `claim_token`（迁移 `0009`）；`complete` / `fail` 在**一条条件更新**里比对状态 + token + 租约期限 | 落定后 token 清空，因此"旧持有者的迟到 `complete`"与"重复投递"事后不可区分（不改变状态、不重复写片段） |
| ~~模型教学交互~~ | **已实现**：durable teaching run（`claim_token` 围栏）+ provider attempt 存证 + 整数微单位预算 + 引用三道关卡 + SSE 事件回放 | 原生 token 流未实现 |
| 前端 | React 单页工作台，由同源 FastAPI 静态托管 | 尚未拆分为独立构建产物和组件包 |
| 可观测性 | 健康检查、审计 outbox、systemd 日志，以及可选的受保护 `/metrics` 聚合指标 | 无 OTel 与告警；指标端点默认关闭，需显式配置访问令牌 |
| 学习闭环题型/评分/复测 | 有诊断、计划生成、任务流转、自报提交与证据投影 | 无题库、无自动评分、无复测分级 |

这些不是「顺手没做」，而是**按设计有意延后**：先让边界可证明，再逐项换掉适配器。

---

## 快速开始

### 1. 安装依赖（Python 3.11+）

仓库已有可用 `.venv` 时可直接复用；新环境按下面的命令创建。

```bat
cd E:\codex_workspace\study-plan
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

`[dev]` 里除了 pytest/httpx，还包含 **ruff 与 mypy** —— 本地跑不通的门禁，
等推到 CI 才发现就已经晚了。

> ⚠️ **`.venv` 处于半安装状态时，不要原地修，直接删掉重建。**
> 「有 `python.exe` 但没有依赖」是最常见的坏状态，增量修复只会失败得更隐晦。
>
> ```bat
> rmdir /s /q .venv            :: cmd
> Remove-Item -Recurse -Force .venv   # PowerShell
> ```

> ⚠️ **如果 editable 安装（`-e`）反复失败**：本项目是通过 `PYTHONPATH` 加载自己的代码的，
> **并不需要**把项目本身装进环境。可以直接装依赖绕过 `-e`：
>
> ```bat
> .venv\Scripts\python -m pip install fastapi uvicorn pydantic PyYAML pytest httpx ruff mypy
> ```
>
> 换机器或想用 conda 环境时，也可以用 `STUDY_PLATFORM_PYTHON` 指定解释器，
> 见下一节 `scripts\dev.cmd` 的说明。

### 2. 跑测试

日常开发先运行受影响的测试；例如修改导入边界时：

```bash
.venv/Scripts/python -m pytest backend/tests/test_import_direction.py -q
```

具体测试文件应按本次变更选择。里程碑、发布或任务计划明确要求完整回归时，再运行全量测试；PR 的全量测试由 CI 执行：

```bash
.venv/Scripts/python -m pytest -q
```

### 3. 启动数据库（可选）

本工作站的 PostgreSQL 安装在 `E:\pgsql`，并注册为自动启动的 Windows 服务
`study-plan-postgresql`。先检查服务状态：

```powershell
Get-Service study-plan-postgresql
pg_isready -h 127.0.0.1 -p 5432
```

在未注册服务的其他 Windows 开发机上，仍可使用 `scripts\pg_start.cmd` 和
`scripts\pg_stop.cmd` 管理仓库自带的数据目录。

#### 验证数据库连通与 RLS

```bat
scripts\db_check.cmd
```

一条命令跑完六项检查，最后打印 `RESULT: ALL CHECKS PASSED` 或失败项数：

| 检查 | 期望 |
|---|---|
| 1. 服务器可达 | 打印版本号 |
| 2. 应用角色是否真的受限 | `NOSUPERUSER` + `NOBYPASSRLS` |
| 3. 装载 RLS 测试夹具 | 重建 `demo_doc`（可重复执行） |
| 4. 租户隔离 | t1 / t2 各自只看到自己那 1 行 |
| 5. **缺少租户上下文** | **返回 0 行**（不是返回全部行） |
| 6. 越权写入（t1 写 t2 的数据） | 被拒绝 |

> ⚠️ 第 4–6 项**故意用应用角色 `study_app` 连接**，而不是超级用户。
> 超级用户会绕过 RLS，用它做隔离检查等于什么都没验证 —— 这类"检查了但没检查到位"
> 比不检查更危险，因为它会给出虚假的安全感。

> ⚠️ **改 `scripts/*.cmd` 时必须保持纯 ASCII，这条不能破。**
>
> cmd.exe 是按**系统代码页**（中文 Windows 为 GBK）**逐字节**解析批处理文件的。
> 文件里一旦出现 UTF-8 中文，解析偏移就会错位，cmd 会开始把字节残片当作命令名执行，
> 报出 `'竻鐞嗭紝...' 不是内部或外部命令` / `'t' 不是...` 这类完全看不懂的错误。
>
> **在文件开头写 `chcp 65001` 救不了** —— 错位发生在这一行生效之前。
> 所以脚本里只用英文提示，中文说明一律放在这份 README 里。行尾统一 CRLF。

开发默认使用内存适配器；设置 `STUDY_PLATFORM_PERSISTENCE=postgres` 后，应用会在启动时连接数据库并核对迁移版本。生产模式强制使用 PostgreSQL，连接或版本不正确时直接启动失败。

### 4. 启动控制面

```bat
scripts\dev.cmd
```

脚本会按这个顺序挑选解释器：

1. `%STUDY_PLATFORM_PYTHON%`（显式指定，例如想用 conda 环境）
2. `.venv\Scripts\python.exe`
3. `PATH` 上的 `python.exe`

挑中之后它会**先自检关键依赖**（`fastapi` / `uvicorn` / `pydantic` / `yaml`），
缺任何一个就直接报错退出并给出修复命令。

> 为什么要多做这一步：一个半安装的 venv **有 `python.exe` 但没有包**，
> 「文件存在就放行」的检查会通过，然后服务在 uvicorn 里炸出
> `ModuleNotFoundError` —— 那个报错完全指不出问题在环境上。
> 这类失败应该在门口就拦住，而不是等到容器里才暴露。

用 conda 环境（或任意其它解释器）时：

```bat
set STUDY_PLATFORM_PYTHON=D:\Drivers\anaconda\envs\langchain1.2\python.exe
scripts\dev.cmd
```

或手动：

```bash
set PYTHONPATH=backend
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### 5. 注册与登录

页面默认显示用户名和密码登录。开发或生产环境需要显式开启注册与密码登录：

```powershell
$env:STUDY_PLATFORM_REGISTRATION_ENABLED = "true"
$env:STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED = "true"
```

用户名支持 1 到 16 个规范化 Unicode 字符。密码只通过 Argon2id 摘要保存，长度需为 6-12 个字符；平台不要求邮箱，因此忘记密码时目前需要管理员处置。注册与密码登录是唯一的认证入口。

### 6. 校验设计知识索引与契约

```bash
.venv/Scripts/python tools/skills/check_manifest.py docs/skills/manifest.yaml
.venv/Scripts/python tools/skills/gen_contracts.py --all --check
.venv/Scripts/python tools/security/scan_secrets.py
.venv/Scripts/python -m ruff check backend tools
```

**红线：** `.env` 已在 `.gitignore` 中排除，密钥不进仓库。请从 `.env.example` 复制后再填写。

#### 环境变量一览

| 变量 | 用途 | 默认值（仅限本地开发） |
|---|---|---|
| `STUDY_PLATFORM_DSN` | 应用连接串 | `postgresql://study_app@127.0.0.1:5432/study_platform` |
| `STUDY_PLATFORM_WORKER_DSN` | 摄取 worker 连接串（`study_worker` 角色，**必须与应用程序串不同**） | `postgresql://study_worker@127.0.0.1:5432/study_platform` |
| `STUDY_PLATFORM_MIGRATION_DSN` | Alembic 迁移连接串 | `postgresql+psycopg://postgres@127.0.0.1:5432/study_platform` |
| `STUDY_PLATFORM_SESSION_SECRET` | 会话令牌签名密钥 | `dev-only-session-secret-change-me` |
| `STUDY_PLATFORM_TOKEN_SECRET` | capability token 签名密钥 | `dev-only-placeholder-change-me` |
| `STUDY_PLATFORM_PYTHON` | `dev.cmd` 的解释器覆盖 | 未设置时按 `.venv` → `PATH` 探测 |

两条铁律：

- **应用 DSN 与迁移 DSN 必须是不同的角色。** 迁移需要 DDL 权限，应用角色绝不能有
  —— 拿到 DDL 就能 `ALTER TABLE ... DISABLE ROW LEVEL SECURITY`，
  租户隔离会从「数据库强制」退化成「应用自觉」。
- **两个签名密钥必须分离**（会话令牌 ≠ capability token），
  生产环境由 KMS / Secret Manager 分别注入。

依赖锁定：`requirements.lock.txt` 记录当前已验证的精确版本组合
（`pip install -r requirements.lock.txt`），权威约束仍是 `pyproject.toml`；
PostgreSQL 相关依赖在 `postgres` extra 里（`pip install -e ".[dev,postgres]"`）。

---

## 目录结构

```text
backend/app/           控制面（分层见下）
  core/                错误码、时钟、标识、哈希（零依赖）
  registry/            node / tool 注册表与三道机械门
  policy/              Policy Gateway、capability token、taint 与 endorsement
  budget/              树形预算账本与原子预留
  execution/           执行状态机、工具派发、审计前置、ChildRun
  audit/               独立审计 sink（哈希链、无删除接口）
  learning/            Evidence 事实源与可重建掌握投影
  knowledge/           资料摄取（端口 + 双适配器）、结构化切块、关键词检索与引用
  db/                  PostgreSQL 适配器（身份 / 产品 / 学习闭环 / 摄取）
  workers/             摄取 worker（独立进程入口，与 api 同为驱动层）
  tenancy/             租户上下文强制、存储端口与适配器
  workflow/            typed node 目录与交互运行时
  api/                 HTTP 接入层
backend/tests/          不变量测试
frontend/index.html     演示页
tools/skills/           manifest 校验与契约生成
docs/superpowers/       规格与实施计划
docs/skills/            设计知识（按需加载，见其 README）
docs/adr/               架构决策记录
```

### 分层与依赖方向

```text
L1 接入  api/
L2 边界  tenancy/（身份与租户上下文） · policy/（Policy Gateway）
L3 编排  workflow/ · learning/（投影写入）
L4 能力  registry/ · knowledge/ · execution/
L5 事实  各领域 ports.py 定义仓储端口，db/ 提供 PostgreSQL 适配器
旁路     audit/（审计） · learning/（Evidence → 投影）
```

**依赖检查**：`backend/tests/test_import_direction.py` 显式列出跨包导入边，拒绝未知模块和未经审查的新边；组合根回调仍列为待消除的历史依赖。
层间通信规则见 `docs/superpowers/specs/2026-09-18-06-layered-architecture-and-module-contracts.md`。

---

## 已机械证明的性质

每条都有对应测试，跑 `pytest` 即可复现。完整不变量清单见 `docs/skills/L0/invariants.md`。

| 不变量 | 证明内容 |
|---|---|
| #1 / #2 | 缺租户上下文即失败；跨租户读取被拒；项目级列表按项目过滤 |
| #3 | 未通过策略网关的调用不进入 `dispatched` |
| #4 | capability token 只能收缩；派生不得超期；篡改验签失败；撤销 epoch 生效 |
| #5 | 未注册 node / tool 默认拒绝 |
| #6 | 外部副作用前必须先写 intent |
| #8 | 预算预留失败不得执行；已派发的预留不可释放；`unknown` 不得盲目重派 |
| #9 | 外部内容只作为数据，不改变策略与权限 |
| #10 | taint 只增；模型摘要不能去污点；endorsement 面向单一 sink、单次使用、随值变化失效 |
| #13 | 注册表冻结后不可改，版本可复现 |
| #14 | 产品执行证据不形成掌握；无效裁决不进投影；失败事实保留 |
| #15 | 子 agent 深度上限 1；权限从父派生；用途决定权限上限 |
| #16 | 回传信封必须携带可校验谱系；散文摘要不得冒充有来源的结论；推断不得作为学习证据 |
| #17 | skill 声明不影响授权结果 |
| 投影性质 | 顺序无关、相同输入必得相同输出、投影期读时钟抛错 |
| 审计性质 | 哈希链可校验、篡改可发现、无删除接口、缓冲有界且恢复后回放 |
| 工具边界 | 同意图不可重复实现；A2 无幂等即拒绝；单 node 工具数 ≤ 8；工具按需加载 |
| #2（摄取与检索） | 跨项目/跨租户候选不进候选集（收窄发生在 SQL/RLS 里，**在打分之前**）；跨租户读原文/任务状态/资料列表一律 404，且与"不存在"除 `request_id` 外逐字同形 |
| 引用可核验 | 每个候选都带**不可变标识**（`document_id + span + content_hash`），按标识精确回读**那一版**原文（同一来源两版、跨度相同时不会串版）；检索默认只看每个来源的最新成功版本 |
| 片段来源 | 片段内容必须等于**持久化原文**的对应切片 —— `assert_chunks_match_document` 在**写入之前**核验（"长度对口"与"自身哈希自洽"都证明不了这一点） |
| 摄取可恢复 | worker 崩溃留下租约 → 租约期内第二个 worker 抢不到 → 到期后回收**恰好一次** → 重复完成不追加第二套片段；**回收之后旧持有者的迟到落定被拒绝**（围栏 token） |
| 检索可复现 | 冻结夹具 25 条中文查询的 recall@5 / MRR 不低于记录下限；排序 tie-break 用 `(source_id, chunk_index)` 而非随机 id |
| 测试不碰业务库 | 整套测试跑在随机会话级临时库（`study_test_*`）上，用完即删；破坏性写入前先过**库名闸门**，误指业务库时在写入之前失败 |
| 错误体形状 | 所有失败出口共用 `public_error_payload` 的五个键；422 也走规范体，且**不回显请求输入** |

---

## 设计文档

| 文档 | 内容 |
|---|---|
| `docs/superpowers/specs/2026-09-17-study-agent-platform-design.md` | 总体设计 |
| `.../2026-09-17-00-system-invariants-and-threat-model.md` | 17 条不变量与威胁模型 |
| `.../2026-09-17-01-learning-loop-and-mastery-evidence.md` | 学习闭环与掌握证据 |
| `.../2026-09-17-02-routing-retrieval-and-quality.md` | 路由、检索与质量 |
| `.../2026-09-17-03-policy-tools-and-execution.md` | 策略、工具与执行 |
| `.../2026-09-17-04-multitenancy-and-data-governance.md` | 多租户与数据治理 |
| `.../2026-09-18-05-agent-skill-context-and-tool-boundary.md` | 技能分层、上下文管理、工具边界 |
| `.../2026-09-18-06-layered-architecture-and-module-contracts.md` | 分层架构与层间通信 |
| `docs/superpowers/plans/2026-09-18-...-implementation-plan.md` | 九项任务与三批次 |
| `docs/skills/` | 按需加载的设计知识（先读 `manifest.yaml`） |
| `docs/adr/` | ADR-001 ~ ADR-014 |

---

## 已知局限（实现层面）

- **预算维度易混**：调用计数与成本必须分别记账。早期实现混用过一次，导致结算越界；
  现已拆开并有测试守住（`test_reservation_failure_blocks_execution` 等）。
- **额度回收**：run 账户必须在交互结束时关闭并回收剩余额度，否则额度会缓慢泄漏。
  关闭时只把**实际消耗**计入父账户，不是把授予额度整体算作消耗。
- **工具重叠门的能力边界**：它只能拦「同一 `intent_tag` 的重复实现」，
  **拦不住「语义相近但标签不同」的歧义**（例如召回与精确回读）。后者依赖 when-to-use 表与人工复查。
- **确认流程**：已改为服务端 `ConfirmationRecord` —— 绑定主体 / 项目 / 工具 /
  **规范化参数哈希** / **成本上界（具体数值）** / 有效期，且单次消费。客户端只能引用
  `confirmation_id`，无法声明确认。执行时会拿**实际预留**与确认的成本上界比对，
  超出即拒绝。仍缺的是**确认 UI**（本版只能通过接口创建确认记录）。
- **确认的原子性只在单进程内成立**：内存适配器用 `threading.RLock` 把「校验 + 占用」
  关进同一临界区。**多 worker 部署下这把锁等于不存在** —— 必须换成 PostgreSQL 的
  `UPDATE ... WHERE consumed_at IS NULL RETURNING`（零行返回即已被占用）。
- **并发测试不能依赖默认调度**：CPython 默认 5ms 才检查一次线程切换，而「读 → 判断 → 写」
  只有微秒级。默认设置下**一个并不原子的实现也能 100 轮全过**。实测同一脚本：
  无锁实现默认间隔下 0/100 轮穿透，把切换间隔压到 1μs 后 **92/100** 轮穿透
  （加锁实现两次都是 0/100）。所以并发测试必须用 `racy_scheduling` fixture
  显式制造切换机会 —— 否则它给出的是虚假的信心。
- **审计链的并发追加会直接断链**（已修，第七轮审查）：
  「分配序号 → 取前序哈希 → 算本记录哈希 → 更新状态 → 写文件」原本没有互斥。
  两条并发记录会拿到**相同序号与相同前序哈希**，第二条当场断链。
  实测（压小 GIL 间隔）：30 轮里 **24 轮断链**，`seqs = [1, 1]`。
  FastAPI 的同步路由跑在线程池里，所以这不是理论问题。
  现已把整段关进 `RLock`；读取也取锁 —— 否则会读到"写了一半"的行，
  把并发写变成读侧的解析错误，被误判成记录损坏。
- **审计链的格式演进**（已修检测，旧数据仍需处置）：`entry_hash` 覆盖了记录结构本身，
  所以**哈希公式一变更，此前写入的记录会全部校验失败**。之前 `verify_chain()` 只返回
  `true` / `false`，**分不清「这是旧格式记录」和「记录被篡改」** ——
  生产里一次代码升级就能让整条链报「无效」，真正的篡改会淹没在噪音里。
  现已按当初记下的修法实现：记录带 `schema_version` 并纳入哈希，
  `verify_chain_report()` 返回**第一处坏点的位置与原因**
  （`LEGACY_FORMAT` / `BROKEN_LINK` / `HASH_MISMATCH`），`verify_chain()` 退化为它的投影。
  ⚠️ **v1 旧记录本身仍无法用当前公式校验**（需要迁移工具或按旧公式单独重验），
  区别只在于：它现在被明确报成"格式问题"，而不是"疑似篡改"。
  本地若看到 `chain_valid: false`，先看 `var/audit/` 里混了什么格式的记录，
  再用 `verify_chain_report().describe()` 看原因。
- **幂等占用表的容量与淘汰**（已标注）：占用表是进程内字典，**不设上限也不淘汰**。
  这是刻意的：**不要用 LRU 之类的方式"修"它** —— 淘汰一条幂等记录意味着后续重试
  会**重新执行**，对 A2/A3 就是第二次真实副作用。正确做法是持久化 + 按风险等级
  设保留期（与 outbox / durable task 一起做）。在那之前，客户端可控的 key
  意味着它可以持续增长内存，这一点属于必需在持久化阶段解决的问题。
- **进程内互斥的适用范围**（已标注）：审计 sink 的追加临界区、预算账本的额度不变量、
  幂等占用表，三处都用进程内的锁保证原子。它们**只对单进程有效**：
  多 worker 或重启后，锁等于不存在。生产实现分别是
  「单一写入者进程 / 系统级文件锁 / 落库」（审计）、
  「数据库行锁 + 约束」（预算）、
  「数据库唯一约束 `(tenant_id, idempotency_key)` + 状态推进」（幂等）。
  这三条不是可选项 —— 它们决定了系统能不能水平扩展。

以下几条来自一轮**自查**，每一处都先复现再修（不是从代码推断的）：

- **落盘顺序：先提交内存状态、后写文件**（已修）：落盘失败时内存已经"以为"记录进去了，
  于是**下一条**记录的 `previous_hash` 指向文件里不存在的哈希 —— 之后整条链全部断掉。
  审计链是 append-only 的，**断掉就再也补不回来**。
  实测：写失败后内存 `seq=4` / 文件 3 条 → `broken_link`。现在状态只在写成功之后提交。
- **崩溃残留会把服务挡在门外**（已修）：进程写记录中途被杀会留下半行，
  而恢复链尾的代码走严格读取，于是 `AuditSink(...)` 构造失败 ——
  **一次崩溃残留就让整个平台装配不起来**，而重启恰恰是最需要它可用的时刻。已实测确认。
  现在读取区分三种态度：链校验收下坏行并报 `MALFORMED_LINE`、
  要全量数据的调用方抛 `AUDIT_LOG_CORRUPTED`、恢复链尾取最后一个**完整**记录。
  ⚠️ 遗留：**校验只覆盖到第一处坏行之前** —— 所以 `MALFORMED_LINE` 应读成
  "从这里往后都还没验过"，而不是"只有这一行有问题"；处置完那一行要重新跑一次校验。
- **残行没有换行结尾时会把下一条记录一起毁掉**（已修）：直接追加会让新记录粘在残行后面，
  垃圾从一行扩散成两行 —— 而其中一行是我们刚提交成功、已记进内存状态的记录。
  现在写入前先补一个换行，让残行独立成行。
- **缓冲回写失败会静默丢审计记录**（已修）：原本是「一次性清空缓冲、再逐个写」，
  中途失败让剩下的记录既不在文件里、也不在缓冲里，**没有任何标记**。
  实测：缓冲 3 条、第 2 条写失败 → 缓冲归零、文件只剩 1 条。现在未写入的留在缓冲并抛出。
- **「同一把钥匙开两扇门」的判定必须与状态无关**（已修）：`released`（占用者失败后放弃）
  分支原本排在指纹比较**之前**，于是同一个错误有两种结果 —— 失败后复用同键换参数会被
  **静默接受**，成功后复用同键换参数才报 `IDEMPOTENCY_VIOLATION`。
  不一致的判定比严格但一致的判定更糟：客户端据此会得出"key 复用没问题"，而事实只有一半。
- **行尾噪音**（已加机械守卫）：`.gitattributes` 要求 `* text=auto eol=lf`，
  但 `core.autocrlf=true` 会在 `add` 时把行尾差异规范化掉，**git 自己不会报**。
  用 Python 脚本改源码时忘了指定 `newline=""`，一次就把四个文件变成 CRLF
  （其中一个还是上一轮就已污染并已提交的）。它还会让契约的 `source_hash` 漂移 ——
  好在契约门禁抓住了这一点。现在 `test_line_endings.py` 守住文本文件用 LF、
  `.cmd` / `.bat` 用 CRLF 且纯 ASCII。

以下四条来自一轮代码审查（详见 `progress.md` 的修复记录），都已修并补了回归测试：

- **原子性不能靠「两次调用都成功」**：两个维度的预算预留必须是**一次**原子操作。
  写成两次独立 `reserve` 时，第二次失败会留下第一次的残留，账户关不掉、敞口持续累积。
- **隔离必须收在数据入口**：`read_source_span` 曾在工具实现里直接遍历内部列表，绕过租户与项目过滤。
  教训是**过滤要放在拥有数据的那个类里**（`ChunkIndex.read_span`），而不是交给调用方「记得判断」。
- **路径与请求体冲突时不能「以某一方为准」**：项目 ID 以 URL 为准，请求体若携带必须一致，否则拒绝。
  让边界可协商等于没有边界。
- **审计记录需要结构化的租户/项目字段**：靠解析 `payload` 内容来过滤不可靠，因为 payload 结构由调用方决定。

以下三条来自一次**自查**（不是外部审查），另四条来自第六轮外部审查，都已修：

- **`supported` 曾经没有正向证明**（已修）：规格说「核心结论有充分且一致的证据」，
  而当时的实现只在**没有任何 issue** 时返回它 —— 检查的是"检索过程没发现异常"，
  不是"核心结论已被验证"。自查时我把它标成"已知局限"并留了一条绊线测试，
  理由是"要求正向覆盖会让首版每次都判 insufficient，状态退化成常量"。
  **这个理由站不住**：状态退化成常量不是"问题被掩盖"，而是如实反映我们确实还不知道；
  真正的错误是用一个更强的词描述一个更弱的判断。
  现在 `supported` 要求 `required_claim_refs ⊆ supported_claim_refs`，
  拿不到覆盖信息时产生 `MISSING_SUPPORT` 并返回 `insufficient`。
  绊线测试按设计触发了，已按预期改成断言严格行为
  （`test_retrieval_health_clean_does_not_imply_supported_evidence`）。
- **"过程无异常"曾冒用证据状态**（已修）：它与证据充分性正交，现在独立成
  `retrieval_health`（`clean` / `degraded`）。**过程干净 ≠ 结论有据** ——
  首版正常检索就是 `retrieval_health=clean` + `evidence_state=insufficient`。
- **同一响应曾有两套互相矛盾的证据判定**（已修）：node handler 给出
  `evidence_state=insufficient`，运行时外层又按「有没有 citation」另算一个
  `evidence_sufficiency=supported`。外部抓取失败但本地有命中时必然矛盾，
  而读旧字段的客户端会据此接受缺少关键证据的答案。
  **矛盾的判定比没有判定更危险**，因为它看起来像有依据。旧字段已删除。
- **追踪 id 与幂等键曾被混为一个**（已修）：运行时拿 `request_id` 做幂等去重，
  而 `request_id` 由服务端**每请求重新生成**，客户端重试必然拿到新值 ——
  那种"幂等"从不报错，只是静默地什么都没做，比没有更糟。
  现已拆开：`request_id` 只做追踪（响应头 / 响应体 / 错误体 / 审计同一个值）；
  幂等由客户端提供的 `idempotency_key` 承担。
  重放会返回本次的追踪 id 并标注 `idempotent_replay: true`。
- **幂等还缺三样：原子占用、租户作用域、处理中状态**（已修，第七轮审查）：
  上一版的"拆开"只做到一半 ——
  ① **读占用表与写结果之间隔着整个执行流程**，两个并发同键请求会同时看到
     "未占用"并各自执行一遍（实测：压小 GIL 间隔 + handler 做 I/O，
     能稳定抓到 `handler_executions = 2`，且两次都 `idempotent_replay=false`）；
  ② 占用表只用客户端字符串做键，**租户 A 用完 `shared` 后租户 B 用同名键会被判
     `IDEMPOTENCY_VIOLATION`** —— 一个租户能"占住"另一个租户的键；
  ③ 等待中的同键请求只有一个模糊结果，无法区分"钥匙用错了"与"等一下再来"。
  现在：键是 `(tenant_id, idempotency_key)`；先到者原子占用（`pending`），
  后到者等待；完成才原子写结果并唤醒（`completed`）；失败/拒绝**释放**占用
  让重试真正再执行（`released`），而不是缓存一个旧拒绝；
  等待超时返回可重试的 `IDEMPOTENCY_IN_PROGRESS`。
- **`supported` 的覆盖约束曾只活在判定器里**（已修，第七轮审查）：
  `assess_retrieval` 会要求正向覆盖，但 `EvidenceAssessment` 这个**被多个模块共享**
  的类型本身仍允许 `EvidenceAssessment(state=SUPPORTED)` ——
  一个"声称证据充分、零证明"的对象。现已把 `required_claim_refs` 纳入该类型，
  并在 `__post_init__` 强制四级状态的不变量。
- **`单个方法原子` 不等于 `一组方法原子`**（已修，第七轮审查）：给预算账本的每个
  变动方法都加了锁之后，并发仍然稳定抛 `BUDGET_TREE_INVALID` ——
  因为 `if 账户不存在: 创建账户` 是 check-then-act，两个线程会同时通过检查，
  第二个在**父账户已经授予额度之后**才撞车，那次授予就挂在那里没人回收。
  修法是 `BudgetLedger.atomic()` 把「检查 + 创建」关进同一临界区。
  这一条是**反向验证并发测试时被抓出来的**：写测试时以为已经修好了，
  去掉保护跑一遍才发现还有一层。
- **对外错误响应体曾有四个生成点**（已修）：`to_payload()`、API 层两处手写 dict（401/404 的脱敏替换）、
  runtime 的拒绝分支。后果是新增一个字段时只落到一部分 —— `/interactions` 在"拒绝"路径返回
  3 个字段、在"失败"路径返回 5 个，**同一个端点自己就不一致**。形状不一致不是小瑕疵：
  客户端只能按「有就取、没有就跳过」来写，最终等于该字段不存在。
  现已收敛为唯一构造点 `core/errors.public_error_payload`，并有测试把**全部出口**收集起来比对键集。
- **`request_id` 从未真正生效**（已修）：`PlatformError.request_id` 没有任何 raise 点设置过，
  所以**每条错误响应的 `request_id` 都是 null**；`deny(code, msg, request_id=...)` 还会把它
  静默塞进不会对外暴露的 `details`。现在由 HTTP 中间件为每个请求生成并绑定追踪 id，
  响应体与 `X-Request-Id` 响应头同源。这类"字段在、值永远是空"的状态最危险的地方在于
  它看起来已经实现 —— 既不会有人补，也不会被测试拦住。
  ⚠️ 补记一条**自查纠正**：我当时在文档里断言"响应头、响应体、错误体、审计同一个值"，
  但实测**审计事件里根本没有 `request_id`** —— 成功路径只带 `run_id`
  （由 request_id 与 node_id 哈希而来，**不可逆**），也就是"错误响应 → 审计事件"
  之间没有可用的关联键。现已补齐：审计记录带 `request_id` 并纳入链哈希，
  由编排层显式传入（`dispatch` / `reconcile`），HTTP 请求内还可用中间件绑定的值兜底。
  **写下的断言也是断言，必须实测** —— 这条教训比修复本身更值钱。
- **契约 header 的 `source=` 曾不参与校验**（已修）：只改 `Target.source_label` 时，
  `--check` 仍通过、普通生成也因"内容未变化"跳过，旧标签会永久保留。
  现在 header 三个字段（`source` / `source_hash` / `generated_at`）都参与校验与写入判定。
