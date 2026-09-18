# 多租户与数据治理

## 1. 数据边界

所有项目级实体必须包含 `tenant_id` 和 `learning_project_id`。用户级偏好与经授权的跨项目能力引用单独存储，不能使项目查询自动扩大范围。

主要实体包括 User、TenantMembership、LearningProject、Session、Message、Source、ProjectSourceGrant、ProjectKnowledgeIndex、PlatformContentRelease、CompetencyComponent、EvidenceEvent、EvidenceCorrection、MasteryProjection、MasteryPresentedEvent、Plan、WorkflowRun、NodeRun、BudgetAccount、BudgetReservation、CapabilityGrant、PolicyDecision、ActionIntent、ToolOutcome 和 AuditIndex。

BudgetAccount 使用 Tenant → User/Project → Run → Node 的 ownership tree；货币、token、沙箱秒数和工具次数分别记账。Source 属于 Tenant 并由 ACL 控制，项目通过 ProjectSourceGrant 授权；原始 Source 与项目索引分离，跨租户不暴露内容存在性。

## 2. PostgreSQL 隔离

- 应用使用非表所有者、无 `BYPASSRLS` 的独立角色。
- 启用并强制 RLS；迁移使用单独角色。
- 租户和项目上下文只通过事务内 `SET LOCAL` 设置。
- 连接池归还前结束事务；后台任务同样建立租户事务上下文。
- 外键、唯一约束和索引包含适当的租户/项目列，避免仅依赖应用过滤。
- 自动化测试证明租户 A 无法通过普通查询、向量检索、ID 猜测和后台任务读取租户 B。

## 3. 向量与对象隔离

向量属于敏感派生数据，禁止进入普通日志、trace 和未经授权的双跑。向量检索在数据库内部强制 RLS/项目过滤。对象存储使用租户/项目命名空间、短期签名 URL 和服务端策略，不能仅依赖路径字符串。索引复用键至少包含 content fingerprint、normalization/parser/chunker 版本、最终 embedding input hash 和 embedding model revision。

ACL 收窄立即失效 ProjectSourceGrant、索引可见性、缓存和待装配摘要；ACL 扩大只重新计算可见性，不重新生成 embedding。provider 侧无法主动失效的缓存只能通过不再复用 cache key、合同保留期和租户策略控制。

原始资料、解析产物、chunk、embedding 和索引版本通过谱系关联。知识源删除或权限变化后，所有派生索引进入失效队列并可验证完成。

## 4. 缓存与状态

缓存通过集中式 typed cache API 构造 key，业务代码不能自行拼接租户 key。缓存 value 再携带 tenant/project 标识，读取不匹配时丢弃、告警并记录安全事件。

缓存实例允许淘汰；控制实例使用 `noeviction`。两者为独立部署，不能用 Redis DB 编号替代。所有控制状态在 PostgreSQL 有可重建事实。具体采用 Redis 或 Valkey 在上线前依据许可证、模块、托管和运维支持形成 ADR。

## 5. 加密和秘密

传输和存储使用平台级加密；敏感字段和对象使用 envelope encryption。至少分离数据加密密钥、审计签名密钥和 capability token 非对称签名密钥，并为每条密文记录 key_id/key_version。高敏感数据在线检查 key generation/revocation epoch；普通内容可采用有明确最大 TTL 的 DEK 缓存，不能宣称即时 crypto-shredding。

用户第三方密钥保存在秘密管理系统，数据库仅存引用。密钥不进入 prompt、checkpoint、trace、沙箱镜像或错误信息。工作节点按任务取得短期凭据。

## 6. DSR 与数据谱系

谱系表记录原始对象、解析物、chunk、embedding、缓存命名空间、checkpoint、影子副本和导出制品。删除流程逐项执行、对账并生成删除报告。

数据分为：可删除业务内容、需限期保留的审计主体、可加密擦除的审计个人字段和 legal hold 数据。备份按保留周期到期，并保证恢复后重放删除 tombstone。legal hold 向数据主体说明依据和期限，由独立 privacy controller 操作，普通应用无权删除审计 sink。

## 7. 保留与最小化

每类数据定义用途、保留期、访问角色、地域和删除方式。默认不保存完整模型输入输出到通用日志。调试采样必须脱敏、有 TTL、可关闭并受租户策略控制。影子双跑需要用户可见说明和 opt-out。

## 8. 并发与一致性

业务事实使用 PostgreSQL 事务、唯一约束、乐观版本和服务端 `event_seq`。Redis/Valkey 锁只用于减少竞争，不作为正确性保证。需要 fencing 时使用数据库 sequence 或资源行单调版本；跨外部系统采用 `(installation_epoch, sequence)`，恢复后由受控流程更新 epoch。投影排序以 event_seq 为主，客户端时间不得决定事件顺序。

队列只传 task id，任务内容和状态在 PostgreSQL。worker 使用 `FOR UPDATE SKIP LOCKED` 原子认领；重复投递由状态机和唯一约束吸收。规模出现明确的重试、死信、延迟或跨服务消费压力后再评估 RabbitMQ、NATS JetStream 或 Kafka。

## 9. 可观测性字段

每条 trace/metric/audit 至少关联：request、tenant、project、session、run、node instance、logical action、route decision、policy decision、budget account 和各版本标识。日志中的租户标识使用内部不可猜测 ID；正文和向量不作为默认属性。

度量包括每租户资源使用、跨项目拒绝、RLS 拒绝、缓存 scope mismatch、删除积压、unknown action、预算敞口、沙箱队列和各模型路径 P50/P95。
