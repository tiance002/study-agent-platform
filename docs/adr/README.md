# Architecture Decision Records

每项 ADR 必须包含：状态、owner、决策日期、候选方案、已选方案、证据、版本/许可证、到期复审日期、重新评估触发条件和回滚方案。具体版本在依赖锁定前实时核验。

## 首批 ADR 索引

- ADR-001：Python/FastAPI 控制面与 React/TypeScript 前端
- ADR-002：PostgreSQL + pgvector 与中文稀疏检索基准
- ADR-003：PostgreSQL durable task table 与消息系统升级条件
- ADR-004：Redis/Valkey 双实例、许可证与模块兼容性
- ADR-005：typed workflow runtime 与 LangGraph/Temporal spike
- ADR-006：gVisor/Firecracker 沙箱隔离与 Linux 执行节点
- ADR-007：KMS、Secret Manager、数据/审计/token 签名密钥分离
- ADR-008：独立审计 sink、对象锁、签名批次与恢复对账
- ADR-009：Fetcher/Package Proxy、SSRF 和依赖出网策略
- ADR-010：Provider 数据策略、地域、保留期和训练使用限制
- ADR-011：PlatformContentRelease、errata overlay 与项目 pin
- ADR-012：Evidence projection、checkpoint 和 as-of 视图
- ADR-013：技能与契约知识的版本治理（skill registry、manifest 索引、生成区／手写区）— [文档](./ADR-013-skill-and-contract-versioning.md)
- ADR-014：子任务运行时（ChildRun）、权限派生、回传信封与 MCP 准入 — [文档](./ADR-014-child-run-and-mcp-admission.md)

