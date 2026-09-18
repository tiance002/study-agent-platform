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
| PostgreSQL + RLS | 进程内适配器，租户过滤在**应用层** | **没有数据库级隔离**，不能多租户部署 |
| 认证与授权 | `tenant_id` 由请求体提供 | 客户端可自报租户，等于没有隔离 |
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

```bash
# 1. 安装（Python 3.11+）
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows
# source .venv/bin/activate && pip install -e ".[dev]"  # macOS / Linux

# 2. 跑测试（全部为不变量证明）
.venv/Scripts/python -m pytest -q

# 3. 启动演示
.venv/Scripts/python -m uvicorn app.main:app --app-dir backend --reload
# 打开 http://127.0.0.1:8000/

# 4. 校验设计知识索引
.venv/Scripts/python tools/skills/check_manifest.py docs/skills/manifest.yaml
```

**红线：** `.env` 已在 `.gitignore` 中排除，密钥不进仓库。请从 `.env.example` 复制后再填写。

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
- **确认流程**：本版用请求参数模拟「用户已确认」，真实系统必须由服务端确认记录驱动。

以下四条来自一轮代码审查（详见 `progress.md` 的修复记录），都已修并补了回归测试：

- **原子性不能靠「两次调用都成功」**：两个维度的预算预留必须是**一次**原子操作。
  写成两次独立 `reserve` 时，第二次失败会留下第一次的残留，账户关不掉、敞口持续累积。
- **隔离必须收在数据入口**：`read_source_span` 曾在工具实现里直接遍历内部列表，绕过租户与项目过滤。
  教训是**过滤要放在拥有数据的那个类里**（`ChunkIndex.read_span`），而不是交给调用方「记得判断」。
- **路径与请求体冲突时不能「以某一方为准」**：项目 ID 以 URL 为准，请求体若携带必须一致，否则拒绝。
  让边界可协商等于没有边界。
- **审计记录需要结构化的租户/项目字段**：靠解析 `payload` 内容来过滤不可靠，因为 payload 结构由调用方决定。
