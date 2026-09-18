# L1 契约 · protocol

> `skill_id: protocol` | version: 0 | expiry: 2027-03-01 | owner: 待指派
> 权威来源：`specs/2026-09-17-03-policy-tools-and-execution.md`、`02-routing-retrieval-and-quality.md`、总体设计 §6.2
> **设计期版本：生成脚本尚不存在，全文属手写区。** 实施时按第 5 节约定填充。

## 1. 生成区

<!-- BEGIN GENERATED: source=openapi+pydantic+node_registry, source_hash=PENDING, generated_at=PENDING -->
> 待实施后导出：OpenAPI 路径与方法、请求/响应 schema、SSE 事件类型、错误码枚举、typed node 输入输出 schema、状态机转移表。
> CI 校验：导出结果与本区不一致即构建失败。
<!-- END GENERATED -->

## 2. 手写区 · 执行状态机（唯一权威转移表）

```text
planned → intent_persisted → dispatched
                          → acknowledged | failed | unknown
                          → reconciled
```

| 规则 | 要求 |
|---|---|
| 幂等键 | 同一逻辑动作的全部重试复用 `(tenant_id, run_id, node_instance_id, logical_action_id, tool_id)` |
| `attempt_id` | 只用于观测，**不进入幂等键** |
| 未授权 | 未通过 Policy Gateway 的动作不得进入 `dispatched`（不变量 #3） |
| 未知状态 | `unknown` 只能进入对账，不得盲目重派（不变量 #8） |
| 已派发 | 已派发调用不能被当作未发生；可安全取消则取消，否则进 `unknown` |

## 3. 手写区 · typed node 与接口约束

| 项 | 约束 |
|---|---|
| node 定义 | 输入/输出 schema、允许工具、最低模型档位、权限属性、验证器、循环/递归/并发/工具次数上限、预算上限 |
| 未知 node | 拒绝实质执行，只允许解释、澄清或重新规划 |
| 参数不合法 | 可在上限内重新规划；**解析失败不得预留工作流预算** |
| 输入上限 | 请求进入时即检查大小、地域、敏感度、taint 与硬禁令 |
| 只读 vs 命令 | 读投影 API 与命令 API 分离；前端不得通过读接口改变状态 |
| 错误码 | 稳定错误码 + `request_id` + 可重试标记 + 用户可理解状态 |
| 重试 | 仅针对明确瞬时错误；指数退避 + 抖动 + 幂等键 + 总时限 |
| 流式 | 交互走 SSE；**不得同步等待沙箱完成** |
| 版本 | 每次运行固定 `policy_version`、`node_registry_version`、`tool_registry_version`、`validator_version`、`dataset_version`、`prompt_version` 与模型标识 |

## 4. 手写区 · 一次交互的固定顺序（不得调换）

1. 身份、租户、学习项目鉴权
2. 输入大小、地域、敏感度、taint/provenance 与硬禁令预检查
3. 解析成已注册 typed node（失败则不预留预算）
4. 轻量 admission quota 通过后**原子预留** workflow budget
5. Model Router 选择 L0/L1/L2（权限轴独立计算）
6. Policy Gateway 签发当前 node 的 capability token 与 policy decision
7. 模型生成回答候选或工具计划，参数携带 lineage/endorsement 约束
8. 验证契约、证据充分性、组件级 validity 与预算；三个失败分支各自处置
9. 外部动作先写 action intent 与审计事件，再幂等执行并写 outcome
10. 记录产品证据；只有事前声明的 assessment 才记录学习证据
11. 返回带来源、假设与限制的结果；**失败不得静默降级**

**为什么顺序不能换：** 第 5 步若在第 6 步之前不成立，会出现"先用昂贵模型、再发现没权限"；第 3 步若在第 4 步之后，会出现"解析失败却已占用预算"。

## 5. 待填充的生成脚本约定

| 项 | 约定 |
|---|---|
| 脚本路径 | `tools/skills/gen_protocol.py` |
| 输入 | `backend/app/api/**`、`backend/app/**/schemas.py`、`backend/app/workflow/nodes/**` |
| 输出 | 本文件第 1 节替换内容 |
| `source_hash` | 输入文件按路径排序后内容的 SHA-256 |
| CI | `--check` 模式比对，不一致即失败 |
| 禁止 | 生成区出现人工编辑内容 |

## 6. 手写区 · 常见坑

- **把 `attempt_id` 混进幂等键**：重试会被当作新动作，产生重复副作用。
- **用客户端时间排序事件**：只能以服务端 `event_seq` 为准。
- **在预测阶段就预留预算**：解析未成功就预留，失败后会留下未结 reservation。
- **只读接口顺手改状态**：掌握度、预算、授权都不得由读接口间接改写。
- **新增 node 只改代码不改 registry 版本**：违反不变量 #13，运行无法归因。
- **为省事让前端轮询代替 SSE**：延迟目标（首 token P95 < 5s）与后端并发预算都会被破坏。
