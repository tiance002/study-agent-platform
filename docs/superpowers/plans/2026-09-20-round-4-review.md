# 第四轮独立审查

审查基线：`9f358de`，对比 `3b2f9d4`。本次只审查，不修改业务代码、迁移或测试。

## 必须修改的地方

### R4-01 / P1：worker 策略可由普通应用角色自行开启

位置：`alembic/versions/0007_source_ingestion.py:270`、`:285`。

`app.worker_id` 是普通连接可以自行设置的自定义变量，不是身份凭据。策略未限定数据库角色，且 SELECT/INSERT/UPDATE 都适用；配合 `study_app` 的整表 UPDATE 权限，应用角色可以跨租户读取和修改队列。当前没有 HTTP 端点设置它，不代表数据库防线成立；本发现不是声称存在已验证的远程 HTTP 利用链。

只读实测，同一 `study_app` 连接：不设上下文时队列计数 0；事务内设置 worker_id 后计数 177；事务结束后变量为 `''`，无租户上下文仍读到 177。`IS NOT NULL` 连空串也会放行。

修改要求：新增前向迁移，撤销应用角色的跨租户 worker 策略和不必要的列更新权；使用独立 worker 数据库角色/连接串，或只授予 worker 的窄接口。固定 SECURITY DEFINER search_path，撤销 PUBLIC EXECUTE，明确 API 与 worker 凭据部署边界。仅改为 `NULLIF(..., '') IS NOT NULL` 不能解决自行设置变量的问题。

验收：普通应用角色任意设置 worker_id 仍不能跨租户读写；worker 能原子认领；复用连接在 COMMIT/ROLLBACK 后不残留权限；直接 SQL 权限测试与 HTTP 隔离测试分别执行。

### R4-02 / P1：旧租约仍可完成或失败新租约中的任务

位置：`backend/app/db/ingestion_store.py:374`、`:429`；`backend/app/knowledge/memory_store.py` 的同名方法。

`complete/fail` 只核对任务状态，没有比较 lease_owner、认领代次、到期时间。A 认领后超时，B 接管，此时 A 的迟到 fail 会直接把 B 的 processing 改成 failed；迟到 complete 也能抢先落结果。内存实测 A→到期→B→A.fail 后任务为 failed。

修改要求：每次认领产生不可复用的 claim_token 或单调代次；complete/fail 在同一锁/事务内比较任务身份、token、processing 状态与有效期限，条件更新失败返回稳定冲突。worker_id 可能重复，不能单独充当 fencing token。失去租约不能再调用 fail 改写新 owner 的任务。已成功请求的幂等回执与过期请求必须区分。

验收：A→到期→B→A.complete/fail 均不改变 B 的状态、片段与代次；同 worker_id 两次认领也拒绝旧代次；B 完成后重放同一成功回执不重复写入。

### R4-03 / P1：多版本资料引用回读不唯一

位置：`backend/app/knowledge/store.py:132`；`backend/app/api/product_routes.py:468`。

同一 source_id 可上传多个 document 版本，但回读只匹配 source_id+span，然后返回排序中的第一条。实测同一来源两版 `bravo`/`delta`、span 均为 [0,5)，搜索命中 delta，回读得到 bravo。片段带 document_id，引用契约和回读接口却没有使用它。

修改要求：引用标识包含不可变 document_id（或 chunk_id），并核对 content_hash/parser_version；精确读取直接使用该标识查库，禁止挑第一条。明确默认检索为最新成功版本，或显式展示并筛选历史版本；已有引用始终回读原版本。

验收：同来源同跨度、不同内容的两版均可独立精确回读；上传新版本后旧引用不变；错误 hash、版本和跨项目标识不能被静默替换成别的片段。

### R4-04 / P1：测试会修改默认业务数据库的全部在途任务

位置：`backend/tests/test_source_ingestion_repositories.py:57`；`backend/tests/test_round4_postgres_e2e.py:127`。

两处 `_drain_queue` 以 postgres 连接默认 study_platform，对所有 queued/processing 行写 TEST_DRAIN，无测试租户约束。执行测试会把用户任务终结，也掩盖队列竞争与遗留任务场景。

修改要求：测试使用独立临时数据库，应用与迁移 DSN 指向同一随机测试库；在任何写入前核验库名和测试标记。跨租户 worker 测试不应在共享业务库靠全库清理获得独占。清理只能发生在验证过的测试库中。

验收：默认业务库上预置 sentinel job，运行测试后其状态、attempt_count 和时间戳完全不变；误指业务 DSN 时在 setup 写入前失败；并行测试不会互相排空。

### R4-05 / P2：片段长度与哈希自洽不能证明来自原文

位置：`backend/app/knowledge/models.py:388`、`:439`；`backend/app/db/ingestion_store.py:374`。

构造器只核对跨度长度，complete 只核对作用域和序号，回读只比较片段自身哈希。将原文 truth 的片段内容替换为同长度 false，仍能完成并被搜索到。注释所声称的“类型保证精确原文切片”不成立。

修改要求：完成事务内读取持久化原文，检查 `document.content[start:end] == chunk.content`、边界及 parser/version 关系，再原子写入；内存执行同一规则。核验对象是原文，不是重新计算片段自身哈希。修正相关过强注释。

验收：同长伪内容、越界 span、错误 document 绑定均拒绝；无片段部分落库、无成功状态；正常内容和重复完成不受影响。

### R4-06 / P2：纯文本误用 Markdown 解析导致内容丢失

位置：`backend/app/knowledge/processor.py:283`、`:289`。

parse 不区分 media_type，text/plain 的 `# plain heading` 被识别为无正文标题并丢弃，返回零片段；这可以是合法纯文本内容，不应被 Markdown 规则删除。

修改要求：按已校验媒体类型选择解析路径；纯文本保留 #/围栏作为文字，空白输入与零片段成功语义单独定义。

验收：同一 `# plain heading` 在 text/plain 下可检索；普通段落、前导缩进、空白文档均有明确结果。

### R4-07 / P2：Markdown 围栏不保存开头长度

位置：`backend/app/knowledge/processor.py:109`、`:114`。

开围栏只保存字符，闭围栏任意 >=3 个同字符就接受。四反引号包裹三反引号示例时，内部三反引号错误关闭外层；后续代码中的 # 被当标题，实测 heading_path 被污染。

修改要求：保存开围栏字符及长度，闭围栏不得短于开围栏，并遵循缩进规则；优先使用有源码位置支持的成熟解析器，若保留受限解析器则明确语法子集并增加对应测试。

验收：四/五字符围栏内三字符示例、反引号/波浪号混用、代码内 #、未闭合围栏都不产生错误标题。

## 本次验证范围

- 45 项现有 DocumentProcessor/上传 API 测试通过。
- 命令：`.\.venv\Scripts\python.exe -m pytest backend/tests/test_document_processor.py backend/tests/test_ingestion_api.py -q -p no:cacheprovider --basetemp=var/review-r4-20260920-a --tb=short`。
- 首次测试因默认临时目录权限失败，改工作区独立临时目录后通过；该环境错误不算业务缺陷。
- R4-01 为真实 PG 只读探针；R4-02/03/05/06/07 为内存适配器独立脚本复现，PG 对应实现已静态核对，不声称全部经过 PG 故障注入。
- 没有运行会全库排空队列的 PG 套件，没有修改现有数据库业务行。R4-04 为明确 SQL 路径确认。
- 项目内全量片段加载与较小检索语料的性能/泛化局限仍存在，本次未测量，不作为已复现性能故障。

## 为什么全绿仍漏错

全绿证明已有断言成立，不证明未写出的性质成立。本轮漏的是组合场景：过期后旧 worker 恢复、多个版本相同跨度、普通应用角色伪装 worker、同长错误内容。单独测“租约能回收”“版本能递增”“哈希相等”覆盖不了这些组合。

原计划也有具体责任：要求租约却未定义 fencing；要求多版本和精确引用却未定义引用唯一键；要求跨租户认领却未定义数据库权限身份；接口示例与后续签名不一致，worker 所需 load_document 也未在早期端口完整列明。此前把这些称为完整退出门过于乐观。实现中的自设 worker 策略、危险测试清理和过强注释又扩大了问题。不能据此判断使用者个人能力不足。

## 提高正确率和效率的执行约束

1. 每个任务先给“性质→反例→观察结果→测试名”，然后实现；反例必须针对能写出来的错误实现。
2. 每个带租约状态机必须定义旧持有者、超时、接管、迟到结果、同身份重启和终态重放。
3. 每个不可变引用必须定义完整唯一键、版本选择、原文核验和授权检查，禁止从展示字段推断身份。
4. 安全权限分别验证 HTTP 层、普通应用 SQL 角色、worker SQL 角色；注释不能替代数据库拒绝证据。
5. 测试库隔离是执行前置条件；没有隔离就不运行跨租户后台任务测试。
6. 针对关键性质做定向变异：移除 token 比较、忽略 document_id、关闭原文核验时，对应测试必须变红；数量和覆盖率不能代替鉴别力。
7. 不为修绿删除断言、扩大权限、改写评测答案或自动降低阈值；必要的规格变更先写影响与反例，再改代码。
8. 每个风险单元独立提交与审查；定向门通过后再跑全量，未改业务代码时不重复全量套件。
9. 独立审查先从契约写攻击/故障时序，再看实现与现有测试，减少实现和测试共享同一盲点。
10. 交付声明区分“代码具备”“测试证明”“真实服务实测”；未执行、跳过、模拟 provider 明确列出。

更详细的计划只有把这些性质和反例写清才有用。提前抄完全部实现代码会让测试与实现一起继承计划错误；下一轮采用冻结接口、状态图、事务边界和反例矩阵，具体内部代码保留给实现期验证。
