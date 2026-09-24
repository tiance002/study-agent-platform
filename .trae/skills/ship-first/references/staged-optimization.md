# 分阶段调优（首版完成后）

功能首版完成后，不要混在日常 Feature 开发中调优。单独进入以下阶段，并在每个阶段内**每次只改变一个主要变量**。

## Phase A — RAG Evaluation

建立真实问题集（50～200 条），测：Recall@K、Precision@K（如适用）、MRR / NDCG（如适用）、answer groundedness、retrieval latency、context tokens。

依次实验：

1. chunk strategy
2. embedding
3. top-k
4. metadata filter
5. hybrid retrieval
6. reranker
7. context compression

## Phase B — Memory Evaluation

必须重点测试隔离：

- **用户隔离**：User A 写入"A 的考试是 10 月 10 日"；User B 查询"我的考试是什么时候？"——必须不能召回 User A 信息
- **项目隔离**：Project A 写入内容，Project B 用相似 query 查询，验证是否发生 cross-project recall
- **污染测试**：故意加入相似名称/课程/任务、可复用模板、其他项目内容；测 false recall rate、namespace leakage、stale memory rate、incorrect personalization rate

## Phase C — Token Evaluation

将请求拆成 system / memory / RAG / conversation / tools / user / output 分别统计 Token。找最大的消费源再优化。

优先顺序一般为：

1. 无用历史上下文
2. RAG 返回过多
3. memory 返回过多
4. system prompt 过大
5. tool schema 过大
6. 输出冗余

不要凭感觉优化。

## Phase D — Latency

记录 request → intent → memory → retrieval → rerank → LLM → tools → response 的 latency breakdown，优先优化占比最大的部分。

## Phase E — Concurrency

首版稳定后再压测：10 / 50 / 100 concurrent users 及目标生产负载。观察 p50/p95/p99 latency、throughput、error rate、DB pool、CPU、memory、LLM rate limit、queue time。

发现瓶颈后再决定是否需要 Redis、cache、async queue、worker、connection pool 调整、batching、rate limiting、horizontal scaling。**不要提前加入复杂基础设施。**

# 开发阶段划分

## Stage 0 — Foundation

只建设首版真正需要的基础设施。完成即可停止扩张。

## Stage 1 — Functional MVP

目标：一个用户可以真实使用 Study Plan Agent 完成完整学习流程。优先实现：注册/登录、学习目标、学习信息、计划生成/保存/修改、基础任务与进度、基础 Agent 对话、基础 memory、基础 retrieval。

这一阶段：**功能推进 > 架构完美 > 性能极限。**

## Stage 2 — Hardening

处理 P2 技术债中值得修的问题、权限、数据一致性、migration、error handling、recovery、E2E。

## Stage 3 — AI Quality Evaluation

开始真实测 RAG、memory、hallucination、planning quality、context engineering。

## Stage 4 — Cost & Performance

测 token、cost、latency、local/cloud routing。

## Stage 5 — Scale

最后测 concurrency、load、rate limit、cache、queue、horizontal scaling。
