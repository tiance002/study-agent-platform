# Agent 工程学习规划平台

多用户 SaaS 学习助手，首个垂直领域为 **Agent 工程实践**。教学法是 project-first：围绕用户想解决的真实问题反推知识、先修关系与验收证据，而不是先摆一套固定课程。

本仓库当前包含**设计规格**与**批次一最小闭环的可运行实现**。

---

## 本版是什么（以及不是什么）

本版实现了「批次一上线门槛」中**最需要机械证明的那部分**：策略、预算、幂等、审计、证据、子任务边界。
它们全部是纯 Python、零第三方依赖，并由一组不变量测试逐条证明。

> 测试数量**不在本文档里硬编码**（写死会随迭代过期）。用 `pytest --collect-only -q | tail -1` 查看当前值。

**⚠️ 本版不是可上线的多租户服务。** 下面这些是设计里有、但本版**没有实现**的部分，
实现时用开发适配器占位并已显式标注 —— 请勿在未替换它们之前用于任何真实数据。

| 未实现项 | 本版现状 | 后果 |
|---|---|---|
| **PostgreSQL 接入应用** | 数据库已就绪、RLS 已验证生效，但应用仍走进程内适配器 | 隔离目前靠应用层，尚无数据库兜底 |
| ~~认证与授权~~ | **已实现**：签名会话令牌 + 项目归属校验；请求体不含任何身份字段 | 令牌签发的生产替代（OIDC / SAML）待接入 |
| ~~客户端自报确认~~ | **已实现**：确认是服务端记录，绑定主体 / 项目 / 工具 / 参数 / 有效期，单次消费 | 批量确认与确认疲劳限流未实现 |
| KMS / Secret Manager | 环境变量 + 开发密钥 | 密钥管理与轮换缺失 |
| 隔离沙箱（gVisor/Firecracker） | `run_in_sandbox` 为占位，**不执行任何代码** | 无代码执行能力 |
| Fetcher / Package Proxy | 仅域名白名单示意，**不真的出网** | 无 SSRF 防护实现 |
| 混合检索 | 进程内关键词匹配 | 非设计中的中文分词 + pgvector + rerank 管线 |
| 模型调用（L0/L1/L2） | 只有档位声明，无 provider | 无实际模型调用 |
| durable task table / outbox | 内存实现 | 进程重启即丢失 |
| 前端 | 单页演示（原生 HTML） | 非设计中的 React + Vite 应用 |
| 可观测性 | 无 | 无 OTel、无指标与告警 |
| 学习闭环题型/评分/复测 | 只有证据模型与投影 | 无题库与评分流程 |

这些不是「顺手没做」，而是**按设计有意延后**：先让边界可证明，再逐项换掉适配器。

---

## 快速开始

### 1. 安装依赖（Python 3.11+）

> ⚠️ **本项目目录下的 `.venv` 目前不存在。** 调试过程中它被 pip 弄成了半安装状态
> （有 `python.exe`、没有任何包），已删除。请在你自己的终端里重建——大概一两分钟。

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

```bash
.venv/Scripts/python -m pytest -q
```

### 3. 启动数据库（可选）

PostgreSQL 16.4 已装在本机 `E:\pgsql`。**必须在你自己的终端里启动** ——
由自动化工具启动的进程会在命令结束时被清理，无法常驻。

```bat
scripts\pg_start.cmd
```

它会启动数据库并打印连接串。停止用 `scripts\pg_stop.cmd`。

两个脚本都是**幂等**的：重复启动会提示「已在运行」，重复停止会提示「未运行」，不会报错。

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

> 应用目前**还没有连接这个数据库**（仍是进程内适配器）。数据库已经就绪、RLS 也已验证生效，
> 但正式接入属于下一步工作，见文末「下一步」。

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

### 5. 签发会话令牌

**这一步是必须的**：所有接口都要求认证，而页面上刻意没有「登录」按钮 ——
那会退化成"客户端自报身份"。签发是运维动作，只能在服务端做。

新开一个终端：

```bash
.venv/Scripts/python tools/issue_session.py --tenant tenant_demo --principal user_demo
```

复制输出的令牌，打开 **http://127.0.0.1:8000/** ，粘贴到第一个输入框，点「验证身份」。

开箱可用的演示数据：租户 `tenant_demo` / 主体 `user_demo` / 项目 `proj_demo`。

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
  knowledge/           检索（开发适配器）
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
L5 事实  tenancy/ports.py 定义的适配器
旁路     audit/（审计） · learning/（Evidence → 投影）
```

**依赖单向**：上层可依赖下层，反向依赖由测试机械拒绝（见 `test_registry_boundaries` 与 CI 计划）。
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
- **审计链的格式演进缺口**（端到端验证时发现，尚未修）：`entry_hash` 覆盖了记录结构本身
  （含 `tenant_id` / `project_id`），所以**哈希公式一变更，此前写入的记录会全部校验失败**。
  而 `verify_chain()` 只返回 `true` / `false`，**分不清「这是旧格式记录」和「记录被篡改」**。
  在生产里这很危险：一次代码升级就能让整条链报「无效」，真正的篡改会淹没在噪音里。
  待办修法 = 给记录加 `schema_version` 并纳入哈希 + 让校验返回第一处坏点的位置与原因。
  本地开发时若看到 `chain_valid: false`，先看 `var/audit/` 里是否混了旧格式记录。

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
  幂等由客户端提供的 `idempotency_key` 承担，并绑定**主体 + 项目 + 请求内容**
  （不绑主体等于把幂等缓存变成跨用户读取通道；不绑内容会让换参数复用静默返回旧结果）。
  重放会返回本次的追踪 id 并标注 `idempotent_replay: true`。
  ⚠️ **幂等缓存仍是进程内的开发适配器**：不跨重启、不跨 worker，
  生产实现要与 outbox / durable task 一起落库。
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
- **契约 header 的 `source=` 曾不参与校验**（已修）：只改 `Target.source_label` 时，
  `--check` 仍通过、普通生成也因"内容未变化"跳过，旧标签会永久保留。
  现在 header 三个字段（`source` / `source_hash` / `generated_at`）都参与校验与写入判定。
