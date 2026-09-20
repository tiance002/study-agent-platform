# ADR-015：教学 provider 边界与供应商选型

日期：2026-09-20　状态：已接受（第六轮更新）

## 背景

第五轮引入"带可核验引用的教学回答"。需要一个外部模型服务，但本机当前
没有可用的云 provider 凭据；同时反例矩阵（超时、缺 usage、错误引用、
提示注入……）必须**确定性**地可复现。

## 决策

1. **协议先行，适配器隔离**：`teaching.ports.TeachingProvider` 是唯一边界。
   `ScriptedProvider` 用于确定性反例，`OpenAIResponsesProvider` 使用一次
   Responses HTTP 请求、关闭隐藏重试，并把未知传输结果交给对账状态。
2. **目标 provider：OpenAI 兼容接口**（`STUDY_PLATFORM_TEACHING_PROVIDER=openai`）。
   选型理由：结构化输出（JSON mode）与 usage 回报有明确文档；计费可按
   token 对账；SDK 成熟。**凭据到位前不宣称"真实模型已验收"** ——
   适配器有本地 HTTP 契约测试；真实云冒烟仍需部署凭据和单独费用确认，
   没有凭据时不能标记真实联调通过。
3. **禁止 SDK 隐藏自动重试**：适配器关闭一切自动重试；重试是编排层的
   显式决策，且只能复用同一个 durable `attempt_id`。一次调用 ↔ 一次计费。
4. **超时 ≠ 未送达**：`TIMEOUT`（结果未知，费用敞口保留）与
   `DISPATCH_FAILED`（可证明未送达，可安全释放）是两个状态，处理相反。
5. **模型与 prompt 版本由服务端批准**：客户端不能选模型、不能改 prompt；
   system 指令版本随内容冻结（`teaching-sys/v1`），run 行记录所用版本。
6. **资料外发范围**：只有 `display_policy=full` 的片段进入 provider 上下文；
   `summary` / `citation_only` 的正文不出服务端。用户上传到项目内的资料
   即视为同意在**本项目内**用于检索与教学回答；不外发到项目之外。

## 后果

- 无凭据环境下整条链路（意图 → 预算 → 派发 → 校验 → 落定 → SSE）仍可
  端到端测试与验收；接入真实 provider 时只需补适配器与配置，
  不改领域层与持久化。
- 缺点：答案 JSON 契约（`teaching-answer/v1`）要求 provider 支持
  结构化输出；不支持时按 `MALFORMED` 处理（保守，费用照记）。
