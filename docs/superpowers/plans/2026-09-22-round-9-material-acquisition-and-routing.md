# 第九轮：资料获取、混合检索与模型路由

> 状态：计划已冻结，等待第八轮剩余证据缺口（C1/C2/C3、真实 uvicorn 重启/中断矩阵）决定是否并行实施。

## 目标

让普通用户可以在一个学习项目中：

1. 输入主题或关键词，由受控的云端搜索适配器发现资料候选；
2. 选择候选后把资料下载并持久化到云端项目知识库，必要时也能上传本地资料；
3. 继续使用现有的资料状态、可引用原文和教学会话；
4. 对简单的关键词改写、分类、去重等任务优先走本地模型（可选、不可用时直接降级）；
5. 对复杂规划、资料综合和不确定问题升级到云端模型；
6. 在任何模型或抓取循环中使用明确的超时、重试次数、并发和循环次数上限，防止 agent 卡死或无界消耗。

本轮不设置平台月度金额上限。费用仍然记录并结算；单次请求的 token、时间、重试、并发、下载字节和循环上限是可靠性与成本保护边界，不是用户月度预算。

## 不变量与范围

- 未选择、未授权的候选资料不会自动进入项目；搜索结果只是候选，不是事实或引用。
- 所有下载和解析在独立 worker 执行，不在 web 请求线程中访问外网。
- 外部 URL 经过 scheme、端口、DNS、私网地址、重定向、响应大小、内容类型和总耗时检查；每次重定向重新解析并重新检查，阻断 DNS rebinding、SSRF、路径逃逸和解压炸弹。
- 原始响应、解析文档、切块、content hash、parser version、acquisition method 和错误状态可追溯；解析失败不伪称“可检索”。
- 资料正文、向量和模型 prompt 都受 tenant/project ACL 与 RLS 约束；跨项目不得命中、回读或复用缓存。
- 关键词检索继续作为确定性兜底；向量召回和 reranker 只有在冻结评测集证明没有关键回归后才打开。
- 本地模型是可选的受限适配器，只能返回闭合 schema 的路由/改写/抽取结果，不直接生成最终教学答案或执行授权动作。
- 云端模型必须复用现有 provider、预算结算、unknown/reconciliation、审计和请求快照边界；本轮不新建第二套 provider 状态机。
- 所有循环必须有 `deadline`、`max_attempts`、`max_steps` 或 `max_redirects`；达到边界后进入可恢复失败/待对账状态，不静默无限重试。

## 任务 0：冻结协议和评测集

- [ ] 设计 `SourceCandidate`、`AcquisitionRequest`、`AcquisitionJob`、`AcquisitionAttempt` 的领域模型和 HTTP 契约。
- [ ] 冻结候选状态：`discovered`、`selected`、`rejected`、`expired`；下载状态：`queued`、`running`、`succeeded`、`failed`、`unknown`。
- [ ] 明确用户操作：搜索只读；选择候选是显式写入；重新下载必须复用 acquisition idempotency key；unknown 不自动重复下载。
- [ ] 建立最小中文评测集：同义改写、标题命中失败、正文命中、跨项目同名资料、历史版本引用、无关噪声和恶意提示注入。
- [ ] 为每个样本冻结期望引用 source/document/span/hash，不把“模型说得像”作为通过条件。

**退出门：** protocol、SQL schema、错误码和评测集能在代码审查中被唯一定位；未冻结字段不进入实现。

## 任务 1：安全外部资料获取 worker

- [ ] 新增 `Fetcher` 端口和禁网/确定性测试实现；web 层只创建 durable acquisition job。
- [ ] 实现 URL canonicalization、只允许 `http/https`、显式端口策略、禁止 localhost/loopback/link-local/private/reserved/multicast、禁止凭据段和非 HTTP scheme。
- [ ] 解析域名后连接前再次校验 IP；重定向上限、跨域重定向重新走 allowlist 和 DNS 检查；禁止解析到内网的 DNS rebinding。
- [ ] 响应头先检查 content type、content length（若有），正文按 chunk 流式计数；总字节、连接超时、读取超时和总 deadline 独立限制。
- [ ] 只接受首批批准的 `text/plain`、`text/markdown`、安全 HTML；PDF/Office/OCR 单独立项，不在本轮偷偷引入解析器。
- [ ] 原始内容进入项目受控对象存储或本地持久化目录，文件名不由 URL 路径直接决定；路径使用 content hash/版本 ID。
- [ ] worker 失败写安全错误码和可重试分类；不可判定的网络中断保留 `unknown`，不自动再次产生外部请求。

**退出门：** SSRF、DNS rebinding、重定向越界、超大响应、慢响应、路径逃逸、恶意压缩和跨项目读取测试全部拒绝；真实 HTTP 测试只能访问本机受控 fixture，不访问生产或任意公网。

## 任务 2：解析、版本和可引用知识库

- [ ] 把现有 `source_documents`/chunks 的 acquisition metadata 扩为可追溯的 `fetch_attempt_id`、`parser_version`、`content_type`、`content_hash`。
- [ ] HTML 解析保留标题层级、段落和代码块边界，去除脚本/样式/导航噪声；不得改写原文后再声称引用原文。
- [ ] 同一 content hash、parser version、source identity 的重复下载幂等；内容变化生成新 document version，不覆盖历史片段。
- [ ] 检索只看最新成功版本时继续保持该语义，并为引用回读保留历史版本。
- [ ] 内存和 PostgreSQL 适配器共享纯领域校验；PG 端补组合外键、RLS、worker claim token 和 append-only 权限测试。

**退出门：** 通过“同 URL 新旧版本、相同 hash 重试、失败版本、历史引用、跨项目读取”矩阵；每条引用都能独立通过 source/document/span/hash 回读。

## 任务 3：混合检索增量

- [ ] 保留 `keyword/v1` 作为基线；先加入查询规范化、标题/章节加权和全局片段兜底，不改变现有 API 语义。
- [ ] 增加可选 embedding 端口和版本化索引键：content hash、parser/chunker version、embedding model revision、最终 input hash 必须共同决定缓存身份。
- [ ] 优先验证 PostgreSQL `pgvector` 在目标 ECS 的可安装性、版本、RLS 查询形状和迁移回滚；未验证前不得将 pgvector 写入生产依赖。
- [ ] 以确定性 RRF/融合规则合并关键词与向量候选，保留每个候选的召回来源和版本信息。
- [ ] reranker 仅作为离线实验；只有评测集上的 recall@k、MRR/nDCG、证据覆盖和延迟/成本均达标才允许上线开关。

**退出门：** 任一索引缺失、embedding provider 超时或向量版本不匹配时，自动回退关键词检索并明确记录降级原因；不因向量失败而丢失现有可用答案。

## 任务 4：本地轻任务与云端复杂任务路由

- [ ] 定义 `RoutingDecision`：任务类型、复杂度、资料规模、不确定性、模型档位、原因、policy version、route version。
- [ ] 本地模型只支持闭合 schema 的 query rewrite、语言/主题分类、去重和轻量抽取；provider 不可用、超时或 schema 错误直接升级云端或使用确定性规则。
- [ ] 云端模型继续走现有 teaching provider 和预算/attempt/审计状态机；路由层不得直接发 HTTP 或绕过预算。
- [ ] 明确升级条件：复杂计划、跨文档综合、引用校验失败、不确定性高、用户明确要求深度解释。
- [ ] 每次路由最多执行固定数量的“本地→云端”阶段；禁止本地/云端互相循环升级。
- [ ] 不把本地模型输出当作最终教学答案；答案仍需通过现有 JSON、引用坐标、hash 和 evidence 状态规则。

**退出门：** 对每个路由样本断言调用 provider 数、顺序、状态、预算、审计和最终答案；本地模型关闭时首版功能仍可用。

## 任务 5：普通用户界面

- [ ] 在资料页新增“搜索资料”入口，显示候选标题、来源域名、摘要、更新时间、风险/状态和选择操作；候选正文默认不全部注入页面。
- [ ] 选择后显示下载 queued/running/succeeded/failed/unknown；刷新和重启后仍能恢复状态。
- [ ] 搜索、选择、下载分别有独立 idempotency key；未知结果只显示确认/继续查询，不自动重复外部下载。
- [ ] 教学回答展示检索模式（关键词/混合/降级）、资料版本和引用原文；不把“搜索到”写成“事实已验证”。
- [ ] 继续覆盖中文用户名、移动端、纯键盘、无资料、外网失败、权限拒绝和云端 provider 不可用状态。

**退出门：** 新用户不需要终端即可搜索、选择、等待资料完成、提问并读取准确引用；浏览器断网/5xx 后不会重复下载或重复收费。

## 任务 6：门禁、部署与成本观察

- [ ] 新增内存/PG 同构测试、真实 HTTP fixture、worker lease/claim、重启和 unknown 恢复测试。
- [ ] 新增安全探针：SSRF、跨租户、越权对象、未选候选、提示注入、任意文件路径和外部响应超限。
- [ ] 新增 fetch latency、bytes、attempt、provider usage、route decision、fallback 和 reconciliation 指标；正文、API key、完整 prompt 默认不进日志。
- [ ] 生产继续不设置月度金额拒绝；保留单次 token/deadline/attempt/loop/bytes/并发边界，并设置超时与人工对账告警。
- [ ] 完成备份、迁移往返、release hash、systemd restart、HTTPS 和回滚演练；真实 provider 只做经用户确认的最小 smoke。

**最终退出门：** 安全探针全部拒绝；内存/PG 语义一致；搜索、下载、解析、检索、路由、教学和引用全部可恢复；门禁没有把缺依赖、缺 PG、缺浏览器或缺 provider 凭据转成 skip 通过。

## 暂不做

- 不在本轮引入全自动网页爬虫、任意网站批量抓取、PDF/Office/OCR、浏览器自动化或长期沙箱。
- 不把 GraphRAG、reranker、Agent 工具循环当作默认路径。
- 不把本地模型作为质量兜底；本地模型不可用时必须规则化降级或升级云端。
- 不把“无月度金额 cap”解释为无 token、无超时、无并发或无限循环。
