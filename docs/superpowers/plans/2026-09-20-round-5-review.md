# 第五轮审查

日期：2026-09-20。基线：`7c5b63c`。范围：教学 API、worker、内存/PG 仓储、预算、引用交付及第五轮测试。只审查，不修改业务实现。

## 结论

不建议将第五轮标为验收完成。发现 3 项 P1、4 项 P2。模拟器链路已形成，但故障恢复、预算上界和 PG 失败路径仍有阻塞。ADR-015 已明确真实 provider 未接入，这不是隐瞒的缺陷，但普通用户发布门尚未满足。

## 要修改的地方

### R5-01 / P1：派发后崩溃无法恢复为对账

- 位置：`backend/app/workers/teaching.py:75`、`backend/app/db/teaching_store.py:295`。
- 场景：A 已提交 mark_dispatched，尚未 record_result 就退出；租约过期后 B 认领。
- B 只区分“有结果”和“没有结果”，后者直接 execute。已有 attempt、预留已是 in_flight，却再次要求 held → in_flight，抛 BUDGET_TREE_INVALID。CLI 没有捕获此错误，会退出；运行保持 running，再次过期仍重复。
- 独立内存探针：1 秒租约，mark_dispatched 后推进时钟 2 秒，再 run_once，稳定复现以上错误。此次没有发生第二次模型调用，但也没有恢复。
- 修改：持久化查询 attempt 状态；未派发才能执行，有结果才重放，已派发无结果必须转 reconciliation_required。不能用“无 result_payload”推导“未派发”。两种仓储统一。
- 验收：在派发事务提交后、网络调用中、响应到达但未存证三个位置中断；接管后不重复调用，保留费用敞口，状态可观察且 worker 能继续处理下一条。

### R5-02 / P1：PG 失败结算与 CHECK 约束冲突

- 位置：`backend/app/db/teaching_store.py:510`、`alembic/versions/0010_teaching_runs.py:224`。
- REFUSED/MALFORMED/TRUNCATED 且有 usage 时直接 fail_run，attempt 由 dispatched 改 failed，但 result_payload 仍为 NULL。
- CHECK 要求 `(status IN ('dispatched','unknown')) = (result_payload IS NULL)`；此时为 false = true，必然拒绝 UPDATE，整笔结算和失败事件回滚。
- 这是静态 SQL/调用链确认的冲突；本次 PostgreSQL 不可达，未声称已在真实 PG 复现。
- 修改：明确失败结果的存证契约，在失败提交中写入安全、可重放的结果；或用新迁移调整状态与载荷约束，确保恢复读取不会把失败结果当成功回答。不要只删除 CHECK。
- 验收：真实 PG 分别执行三种失败，有/无 usage、事务提交失败再恢复；核查状态、费用、attempt、事件、无 assistant 消息，内存/PG 结果一致。

### R5-03 / P1：预留不覆盖实际请求，预算可透支

- 位置：`backend/app/api/teaching_routes.py:130`、`backend/app/db/teaching_store.py:295`。
- 创建只按问题字符数预留输入费用；system、历史、资料未包含。execute 虽计算完整输入估计，但 mark_dispatched 的两个估计参数在两个适配器里均未用于补充预算/再次校验容量。
- 独立探针：总预算 4010，问题 9 字符、输出上限 2000，预留 4009；provider 返回输入 7000、输出 2000，均未越过运行时 token 限额，最终 succeeded，spent=11000、available=-6990。
- 修改：冻结完整请求后、派发前原子补足预留；或最初按批准的最坏输入/输出上限预留。租户和项目同时锁定与校验；不足必须在调用前拒绝。已实际发生的费用仍应如实记账，不能用截断 usage 掩盖超支。
- 验收：资料/历史增长、双并发挤占余额、租户跨项目竞争；不足时 provider 调用数必须为 0。真实模型的 token 计数不能直接把字符数当严格上界。

### R5-04 / P2：无 usage 的失败被误判为 stale，未转对账

- 位置：`backend/app/teaching/service.py:193`。
- REFUSED/MALFORMED/TRUNCATED 的 usage=None（或被 _settled_usage 判为不可采纳）仍调用 fail_run。仓储抛 ILLEGAL_STATE_TRANSITION；worker 把它一律解释为失去租约，返回 stale。
- 独立探针：合法 REFUSED、usage=None，run_once 返回 stale，持久化状态仍 running；与函数文档承诺“缺 usage → 对账”不符。随后还会进入 R5-01。
- 修改：service 显式转对账；区分真实 claim 失效与业务状态错误，不能共用一个错误码后吞掉。
- 验收：三种失败状态 × 有/无/越界 usage，逐项断言状态、预算、事件与恢复后调用次数。

### R5-05 / P2：历史包含本次问题之后的消息

- 位置：`backend/app/teaching/service.py:243`。
- _history 只排除当前 user_message_id，没有按该消息的 seq 截止。连续提交 A、B，再执行 A，B 会成为 A 的“历史”；多 worker 下还可能包含后续回答。
- 独立探针：同会话连续创建 first question、FUTURE_QUESTION，执行第一条，其 provider 请求包含 FUTURE_QUESTION。
- 修改：按当前问题的 seq 限定历史，定义同会话并发策略；把实际使用的历史和资料快照在派发前持久化。后续追加消息不能改变已冻结请求。
- 验收：同会话两个排队问题、乱序完成、重启前后请求哈希一致。

### R5-06 / P2：引用核验结果未交付，用户无法回读证据

- 位置：`backend/app/teaching/service.py:185`、`backend/app/db/teaching_store.py:359`。
- validate_citations 返回 accepted/rejected，但 finish_run 只收到 answer_text、grounding、usage。公开 run、消息、SSE 均没有已验证引用坐标或拒绝理由；原始 provider payload 也不能作为已验证结果替代。
- 独立探针：有效引用成功后 grounding=sourced，公开消息仅有 `Answer [1]`，事件只有 answer_message_id/grounding/usage_reported，无任何 [1] 到原文的映射。
- 修改：最终提交原子保存已验证引用及稳定引用标识，公开受授权保护的引用结果与原文回读入口；拒绝引用不得继续表现为可点击的有效证据。不要把原始 provider payload 整包公开。
- 验收：仅通过 Cookie HTTP 获取回答、列引用、回读对应 document/hash/span；部分拒绝、全部拒绝、重复引用、撤权后访问均覆盖。

### R5-07 / P2：运行记录的配置与实际派发配置可不同

- 位置：`backend/app/teaching/service.py:129`、`backend/app/workers/teaching.py:67`。
- run 保存 model_id/prompt_version，但 execute 使用当前 worker 的 self.model_id/self.prompt_version/max_output_tokens。排队期间部署改配置，实际请求与运行记录、原始预算不一致。
- 独立测试环境探针：run 记录 test-model-v1，实际请求为 unconfigured-model。生产中在创建与执行之间更新配置也走同一路径。
- 修改：冻结模型、provider、prompt 内容/版本、输出上限及价格版本；执行/重放使用运行快照。旧配置不可执行时明确拒绝，不能悄悄替换。
- 验收：创建 A 配置运行，切到 B 后再执行；实际仍使用 A，或显式终止且不调用 B。历史价格结算不随部署变更。

## 验证记录与边界

- 第五轮九个测试文件：96 passed、16 skipped、2 warnings。15 个跳过源于 PG 不可达，另一个是内存参数不适用 PG 双连接场景。
- 命令：`.venv\Scripts\python.exe -m pytest backend/tests/test_teaching_provider.py backend/tests/test_teaching_repositories.py backend/tests/test_teaching_context.py backend/tests/test_teaching_citations.py backend/tests/test_teaching_recovery.py backend/tests/test_teaching_api.py backend/tests/test_teaching_events.py backend/tests/test_round5_acceptance.py backend/tests/test_round5_postgres_e2e.py -o addopts='' -q -rs -p no:cacheprovider --basetemp=var/review5-pytest-second`。
- 独立探针复用了测试 world 构造器，但输入、故障时点和断言由审查单独设计；未改业务数据、未调用云模型。
- PG 约束问题为静态确认；本次不能确认真实数据库迁移/恢复验收。没有以 skip 代替通过。
- 未进行完整安全重审、真实云调用、浏览器端验收；本报告不是全项目无缺陷保证。

## 为什么测试全绿仍有缺陷

不是单凭这些问题就能判断你“做得不好”。主要是测试的验收对象偏离了用户承诺：成功路径和单个函数覆盖较多，跨事务故障窗口、运行中的配置变化、通过 HTTP 交付证据覆盖不足。PG 失败路径还有真实约束与内存实现分歧。

更详细的文字计划本身不保证正确率。有效约束是：先列可观察不变量及反例，再实现；每个修复先证明旧版本会失败；相同状态机矩阵跑内存和 PG；每个持久化提交边界做中断；验收需同时检查 provider 调用数、状态、消息、账本、事件；必需基础设施缺失必须使发布门失败。模拟器测试与真实 provider 测试分别记账，不互相替代。
