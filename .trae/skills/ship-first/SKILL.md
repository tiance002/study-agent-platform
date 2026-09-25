---
name: ship-first
description: Study Plan Agent 快速交付开发规范，优先完成可运行 MVP、控制调试成本、禁止提前优化与无关重构。Use when 用户要求实现功能、修复 bug、编写或运行测试、做 RAG、记忆、Token、性能或并发调优，或任务出现反复修复、scope 蔓延、过度重构时。不适用于纯文档与纯咨询。
---

# Ship First —— 快速交付开发规范

> 核心信条：**Shipping beats tuning.**
> 尽快得到一个端到端可运行、可真实使用、可继续测量和调优的 Study Plan Agent 首版。

## 1. 核心目标与优先级

当前阶段的第一目标不是"把所有指标一次做到最好"。开发优先级：

1. 首版主流程可运行
2. 核心数据正确且不会损坏
3. 用户/项目数据隔离边界正确
4. 关键接口和架构边界稳定
5. 有足够测试保证继续开发
6. 再处理边缘问题
7. 最后进行 RAG、记忆、Token、延迟、高并发等专项调优

不要为了首版前暂时无法量化的问题长期阻塞开发。

## 2. 适用范围与边界

| 阶段 | 应用方式 |
|---|---|
| 需求分析 | 只定义 Goal / Invariants（≤7 条）/ Contract / Out of Scope / Acceptance Test（§9），不写几十条约束 |
| 架构设计 | 遵守既有分层与导入方向门禁（`test_import_direction.py`）；架构性错误属 Blocker（§3）；其余设计争议记录后推进 |
| 编码实现 | MVP 最小闭环；控制 blast radius（§10）；禁止提前优化（§11）；执行模板（§13） |
| 测试验证 | 按测试金字塔分层运行（§7）；E2E 只测关键路径（§8）；完成条件（§14） |

**本技能不适用于**：纯文档编写、纯咨询问答、以及任务本身涉及安全/权限/数据一致性/不可逆迁移的场景——这些不受"先做 MVP"约束，必须按 Blocker 标准处理（§3）。

## 3. Blocker —— 必须立即修的问题

不得因为"先做 MVP"而跳过：

- **数据安全**：跨用户/租户读写、隔离失效、凭据泄漏、权限绕过
- **数据一致性**：不可恢复的数据损坏、破坏数据的 migration、核心事务部分成功、重复提交导致严重错误
- **主流程不可运行**：注册 → 登录 → 创建学习目标 → 生成计划 → 保存计划 → 查看执行，任一核心步骤完全不可用
- **架构性错误**：继续实现会导致后续大规模推倒重来

只有以上问题默认允许阻塞当前 feature。

## 4. Bug 分级

发现问题先分类，不要立刻修改：

- **P0（立即处理）**：数据泄漏、权限绕过、数据损坏、系统无法启动、主流程完全不可用
- **P1（当前 Feature 完成前处理）**：核心路径失败、API contract 错误、明显逻辑错误、稳定复现的关键测试失败
- **P2（进入技术债）**：次要边界问题、非关键性能、可接受的重复代码、非主流程 UI
- **P3（优化阶段处理）**：更好的抽象、Prompt 精调、Token 优化、极限性能、高级缓存、高并发、极端 edge cases

默认：**P0/P1 修复；P2/P3 记录后继续推进。**

## 5. AI Debug 熔断规则

避免"修 A → 产生 B → 修 B → 重构 C"的 Debug Spiral。每次测试失败时严格执行：

1. 判断失败是否由当前修改导致
2. 保存失败命令和最小错误信息
3. 提出一个明确的 root-cause hypothesis
4. 一次只验证一个 hypothesis
5. 用最小修改修复
6. 只运行最相关测试
7. 不为修一个测试进行无关重构
8. 连续两次修改仍未解决 → 停止 patch，重新分析根因
9. 若属 P2/P3，记录技术债后继续功能开发

禁止：顺手重构、顺便统一架构、为让测试变绿而改变正确业务逻辑、一次修改多个不相关模块、没找到根因就持续试错。

## 6. Debug Budget

- 普通单元测试问题：最多 **2 次**针对性修复
- Integration 问题：最多 **3 次** root-cause 尝试
- E2E 问题：先确认是否真是产品逻辑问题；若属环境/flaky/timing/浏览器行为/测试夹具，不要反复修改生产代码迎合测试

超过预算：记录问题 → 标记风险 → 提供复现方法 → 进入技术债 → 继续当前里程碑。**P0/P1 除外。**

## 7. 测试策略（对接本项目门禁）

目标不是"每改一行跑全部测试"，按金字塔分层：

- **开发中**：只跑当前模块 unit / contract / 相关 integration 测试
  （如 `.venv\Scripts\python -m pytest backend/tests/test_<模块>.py -q`）
- **Feature 完成**：当前模块完整测试 + 相邻模块关键回归
- **Milestone 完成**：全量 `pytest -q` + PostgreSQL 子集（`-m postgres`）+ `ruff check` + `mypy backend/app`
- **Release Candidate**：`tools/run_round8_gate.py` 全门禁（含三份契约 `gen_contracts.py --all --check`、三个前端脚本 `node --check`、浏览器回归、`git diff --check`）

跨模块改动应覆盖涉及的模块和边界不变量，但不自动升级为全量测试。用户要求、具体任务计划、仓库门禁或定向验证无法圈定的具体风险可以触发全量检查；说明触发依据，通常在最终检查点执行一次。同一提交的 CI 已通过全量门禁时，除非计划要求本地运行或仍有未解决风险，避免在本地重复运行。

## 8. E2E 使用原则

E2E 是最昂贵的测试。不要在小修改后反复执行完整 Playwright suite（`tools/check_round8_browser.py`）。

开发阶段只验证当前关键路径：

```text
register → login → create goal → generate plan → persist → reload
```

首版只需确保主要用户旅程真实跑通。

## 9. Feature 开始前只定义这些内容

- **Goal**：用户最终得到什么能力
- **Invariants**：最多 3～7 条真正不能破坏的约束（例如：所有查询必须含 tenant boundary；生成失败不能覆盖已有计划；API 输出 schema 保持兼容）
- **Contract**：API 输入/输出、数据 schema、关键错误语义
- **Out of Scope**：当前明确不做什么
- **Acceptance Test**：1～5 条验证首版是否完成的测试

然后直接实现。

## 10. 控制 Feature Blast Radius

一个普通功能应只修改少数模块：

- 80% 普通 feature：主要改动 2～4 个模块
- 需要修改大量层级时，先检查是否 scope 过大

禁止在同一任务中同时重构 memory、修改 RAG、换 embedding、修改 auth、调整数据库架构、增加 local LLM、重写 prompt、修改工具系统——除非是完成该功能不可缺少的。

## 11. 禁止提前优化

首版完成前，以下工作不阻塞产品功能：

- **RAG**：先做到 query → retrieve → context → answer；chunk size / top-k / reranker / embedding / hybrid weight 的极限调参需真实测试集后再做
- **Memory**：先确保写入/读取/命名空间/基本删改/隔离边界；完美长期记忆、自动压缩最优策略、memory graph 后置
- **Token**：先能记录、能知道花在哪（observability first），不要求立即最低
- **并发性能**：先验证多用户不串线、事务正确、请求不明显阻塞；复杂并发优化需真实 workload

专项调优（Phase A–E：RAG/Memory/Token/Latency/Concurrency 评估）在首版完成后单独进行，见 [references/staged-optimization.md](references/staged-optimization.md)。

## 12. 首版必须埋好的 Observability

暂时不优化，但必须让后续"能测"：

- **LLM**：model、request latency、input/output/total tokens、estimated cost、local/cloud route、fallback
- **RAG**：query、retrieval latency、top-k、retrieved IDs、similarity/rerank score、context token count
- **Memory**：namespace、project_id、user_id、memory type、retrieval count、retrieved IDs、latency
- **API**：endpoint、latency、status、request ID、error type

生产日志不记录敏感正文。**首版不需要完成所有优化，但必须能测量后续优化。**

## 13. 执行模板

局部小改动只需简述 Goal 和 Validation；多层或重要边界改动再按需补充以下字段，然后直接实现：

```text
Goal:         本次完成什么
Invariants:   最多 3～7 条
Scope:        本次修改哪些模块
Out of Scope: 这次明确不处理什么
Validation:   完成后跑哪些最小测试
```

## 14. 完成条件

Feature 满足以下条件即视为完成：

- 核心用户路径可工作
- P0/P1 问题为 0
- 相关 unit/integration test 通过
- 没有明显破坏已有核心功能
- P2/P3 问题已记录到 `docs/tech-debt.md`
- 有必要的 observability
- 可以继续开发下一个 Feature

不要因为 coverage 不够漂亮、code style 不够完美、可以进一步抽象、理论 edge case、benchmark 未达最佳而阻止 Feature 完成。

## 15. Stop Rule

- 如果继续修复的成本已明显超过问题当前价值，且问题不是 P0/P1：**停止修复、记录技术债、继续推进**
- 功能已满足 Acceptance Criteria：**不得主动扩大 scope**
- 发现值得做但不属于当前任务的改进：**记录，而不是立即实现**

## 16. 技术债记录规范

所有 P2/P3 问题记录到 `docs/tech-debt.md`（文件不存在时首次创建），格式：

```markdown
## TD-xxx 标题

- 状态：
- 影响：
- 触发条件：
- 当前临时方案：
- 为什么现在不修：
- 后续验证方法：
- 建议处理阶段：
```

TD 编号全局递增，不回收复用。

## 17. 与本项目工程门禁的衔接

- 架构边界改动（导入方向、分层）必须过 `backend/tests/test_import_direction.py`
- API 行为改动必须比较 OpenAPI/契约（`tools/skills/gen_contracts.py --all --check`），公开契约差异属 P1
- 迁移改动必须可回退且不破坏数据，属 Blocker 审查范围
- 浏览器回归证据按 `tools/check_round8_browser.py` 记录
- 既有计划文档（`docs/superpowers/plans/`）对具体任务规定的验证门禁应执行；发现数据、权限或一致性风险时按 Blocker/P0 处理

## 18. 维护机制

- **反馈渠道**：问题与改进建议记入 `docs/tech-debt.md`（TD 条目，标注 `ship-first`）或项目 review 文档
- **版本迭代**：本技能语义化演进——新增规则提升次版本，修改执行模板/完成条件提升主版本；每次修改在下方变更记录追加一行
- **文档更新**：修改 `SKILL.md` 后同步检查 `references/` 链接有效性；技能与 `docs/superpowers/plans/` 冲突时以较新评审结论为准并更新另一方
- **优化策略**：当某条规则在 3 个以上任务中持续被绕过或被证明阻碍交付，修订该规则而不是弃用技能

### 变更记录

- v1.1（2026-09-24）：明确跨模块改动的定向验证、全量测试触发条件和重复执行规则；简化局部任务模板。
- v1.0（2026-09-23）：由 fast-delivery-development 迁入并适配本项目（命名 `ship-first`，对接 round8 门禁与契约检查，调优阶段移入 references）。
