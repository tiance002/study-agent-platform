# 第八轮：学习工作台正确性收口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复第七轮复审 R7-01 至 R7-07，并以真实浏览器、worker 和 PostgreSQL 证明普通用户最小学习闭环。

**Architecture:** 保留 FastAPI 同源 Cookie API、本地 React UMD 和现有 worker。先控制破坏性保存、异步作用域和未知写入结果，再补运行恢复、摄取状态及引用读取；最后以真实 HTTP 集成验收。不重写前端框架，不为了通过测试绕开认证、worker 或持久化。

**Tech Stack:** Python/FastAPI、PostgreSQL、React 18、Playwright Python、pytest、h11。

## 执行约束

- 适用于 5.6 Luna Max；一次只实施一个任务，完成局部验证后再进入下一个。遇到契约不支持需求，先记录差异并更新设计，不能猜字段或静默削减验收。
- 起点工作区 `E:\codex_workspace\study-plan`，先读 AGENTS.md（如有）、本审查、现有接口/测试和 git status。保留用户未提交修改。
- 下列相对路径全部相对于该工作区；行号只用于定位，不按行号机械打补丁。
- 每个修复必须先有在旧实现失败的行为测试，再做最小修改，再跑定向测试。测试失败必须是业务断言，不是环境缺依赖。
- 不把 provider disabled 当成功回答，不把内存重建当 PG 恢复，不以静态源码字符串代替浏览器行为，不以登录页截图代替登录后截图。
- 不做 UI 大改、依赖升级、检索算法变化、真实付费云调用、生产部署。未验证真实 provider 时必须保留发布阻塞说明。
- 所有测试账号/内容均为合成数据。浏览器使用邀请兑换 Cookie；不得启用生产 Bearer 或把 token 放 localStorage。
- localStorage/sessionStorage 不存邀请、Cookie、原文、答案、问题正文。恢复指针按 principal/project/conversation 隔离；账户切换不得继承旧操作。
- 不 force push，不覆盖远端 main；推送正常 `codex/round-8` 分支，遇到远端不同历史先停下记录，不绕过保护。只提交本轮文件。

## 任务 0：建立可失败的浏览器回归入口

**文件：**新增 `tools/check_round8_browser.py`、`tools/run_round8_preview.py`、`backend/tests/test_round8_http.py`；读取现有 `tools/check_round7_browser.py`、`tools/run_round7_preview.py`、`backend/tests/pg_support.py`、`backend/tests/test_teaching_api.py`、`backend/tests/test_ingestion_api.py`。

- [ ] 新脚本支持命令行 `--base-url` 和 `--artifacts`，测试服务器端口不得硬编码为已占用的 8000；启动/停止仅管理自己创建的 PID。
- [ ] 回归入口分两类：Playwright route 注入延迟/失败验证 UI；真实 Cookie/API/worker/PG 验证持久化闭环。分别报告，不混称端到端。
- [ ] 预览服务器支持内存与测试 PG；教学使用现有 provider 协议的确定性测试实现，实际经过运行入队、worker、引用校验、消息落库；不直接伪造成功 HTTP 响应替代该链路。
- [ ] 每场景使用唯一项目名；捕获 console error、pageerror、服务端日志和失败截图。预期故障应显式白名单，未知异常即失败。
- [ ] 先加入下述 R8-A 至 R8-G 的失败场景。至少保留每个场景旧实现失败的命令与关键断言到进度记录；不要要求尚未实施的全套场景一次全绿。

浏览器核心断言形状（变量由每场景自己的真实 fixture 创建）：

```python
expect(page.get_by_role("heading", name=project_b_name, exact=True)).to_be_visible()
expect(page.locator(".message-stream")).not_to_contain_text(project_a_answer)
expect(page.get_by_role("button", name="创建项目", exact=True)).to_be_visible()
assert len(created_run_ids) == 1
assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
```

## 任务 1：先堵住破坏性保存与 HTTP 协议错误

**文件：**`frontend/app.js`；`backend/app/api/product_routes.py`；测试 `backend/tests/test_round8_http.py` 和 `tools/check_round8_browser.py`。

**决策：**本轮已有计划只读，不提供假编辑。当前 PUT 是新建整版且会创建新任务身份；仅把标题拷回去仍不能保证已有学习证据关联。

- [ ] R8-A：准备含两个里程碑、三个任务、描述、已有任务进度的计划。打开计划页应完整展示；不得有可覆盖现有计划的提交操作。切走切回不改变 version、任务身份或证据。
- [ ] 无计划时允许创建初始计划。表单草稿必须绑定项目；空/空白标题不能提交。明确 UI 是“创建计划”，不是“保存修改”。已有计划编辑排入后续独立版本化设计，不计为本轮完成。
- [ ] R8-B：为两个空结果响应增加断言 status=204、body=b''；真实 uvicorn 连续请求同一连接也成功，无 LocalProtocolError。
- [ ] 将两个 JSONResponse 替换为 Response，使用现有导入风格。

```python
from fastapi.responses import Response

# 仅替换空结果分支，不改变存在资源的响应契约。
return Response(status_code=204)
```

- [ ] 验证：`.venv\Scripts\python.exe -m pytest backend/tests/test_round8_http.py backend/tests/test_product_api.py -q`；执行浏览器 R8-A/R8-B，再检查差异、记录、提交。

## 任务 2：请求作用域和状态隔离

**文件：**`frontend/app.js`；测试 `tools/check_round8_browser.py`。必要时可新增一个同源状态辅助脚本，但不迁移构建系统。

**接口约束：**每个异步操作捕获 principalId/projectId/conversationId 和递增 epoch；响应写 state 前必须匹配当前 scope/epoch。api 接受 AbortSignal；abort 只停止等待，不宣称已取消服务端写入。

- [x] R8-C1：延迟 A 项目消息、计划与运行响应，切到 B 后再释放 A；最终仅显示 B 的消息、运行和计划。
- [x] R8-C2：同项目两个会话执行同样逆序返回；消息和运行状态不可串会话。
- [x] R8-C3：运行轮询未结束时退出，再登录另一测试用户；释放旧消息/运行响应，页面不能出现上一用户的答案、引用、通知、草稿。
- [ ] 项目/会话切换立即使旧回调无效，并清理相关 state；轮询定时器及网络请求在 effect cleanup 中取消。useRef 维护 epoch，不依赖闭包中旧 state。
- [ ] loading 按操作与作用域管理；旧操作 finally 不可解除新操作 busy。遇到任何 401 清理认证相关状态并返回登录，保留安全提示。
- [ ] 重跑 R8-A/B/C，检查差异、记录、提交。

## 任务 3：未知结果的幂等重试

**文件：**`frontend/app.js`；测试 `tools/check_round8_browser.py`、按需扩展 `backend/tests/test_product_api.py` / `backend/tests/test_teaching_api.py`。

**状态契约：**逻辑命令含 scope、method、path、不可变 payload、key、state；state 为 submitting/unknown/succeeded/rejected。重复重试复用同一 key 和 payload；修改 payload 必须作为用户明确发起的新命令。页面刷新后不自动重发。

- [ ] R8-D：让服务端已提交创建项目或 teaching run 后，浏览器收不到响应；点击“重试同一请求”，断言请求 key/body 相同，API 中仅一个资源，教学只有一个预算预留/执行。
- [ ] 两阶段资料提交分别保存 source 创建和 content 入队的命令状态；content 失败后不再盲目重新创建 source。
- [ ] 网络错误保留页面内命令对象，展示未知结果和显式重试；普通新提交不能暗中替代未知操作。不得把问题正文写入浏览器持久存储。
- [ ] 刷新丢失内存命令时从服务端刷新资源，不自动再发；如果无法判断同内容是否已提交，明确提示待确认，禁止宣称精确一次已经保证。需要跨刷新自动重试时须另行设计服务端命令查询，不能本轮临时泄露正文到 localStorage。
- [ ] 连续快速点击只产生一个逻辑命令；reconciliation_required 不创建替代运行、不自动再次消费预算。
- [ ] 重跑 A-D，记录已提交响应丢失的数据库/仓储断言，提交。

## 任务 4：运行恢复到终态

**文件：**`frontend/app.js`、`backend/app/api/teaching_routes.py`（仅在恢复缺少读接口时）；持久化扩展须同时涉及 `backend/app/teaching/ports.py`、`memory_store.py`、`backend/app/db/teaching_store.py`；测试 `tools/check_round8_browser.py`。

- [ ] R8-E1：非首个项目、非首个会话的 queued/running 状态刷新后恢复选择并继续轮询，最终出现一次回答；刷新不得 POST 新运行。
- [ ] R8-E2：临时 GET 网络失败或 5xx 不删除指针；有限退避并提供继续查询操作。401 清认证，404 清该不可用指针，禁止把所有错误都当不存在。
- [ ] R8-E3：failed、reconciliation_required、succeeded 都停止轮询；前台手动刷新会读取当前运行。轮询超出时限显示可恢复状态，不把它显示成服务端失败。
- [ ] 恢复选择和运行指针按用户/项目/会话保存；读取前校验资源所属范围，不直接相信 localStorage。优先从现有持久化消息/运行关系回读；若缺少必要查询，新增最小只读分页接口，并补权限/内存-PG一致性测试。
- [ ] 重跑 A-E，记录、提交。

## 任务 5：摄取状态及原文引用真正可用

**文件：**`frontend/app.js`、按需 `frontend/app.css`；需要摄取状态查询扩展时检查 `backend/app/api/product_routes.py`、`backend/app/knowledge` 中实际端口、`backend/app/db/ingestion_store.py` 及内存实现，先定位再修改；测试 `backend/tests/test_ingestion_api.py`、`tools/check_round8_browser.py`。

**现有接口：**上传返回 `{document, job}`；`GET /projects/{project_id}/ingestion-jobs/{job_id}` 返回持久化状态。引用 `GET /projects/{project_id}/sources/{source_id}/span` 必须传 document_id、start、end，且本客户端同时传 content_hash；禁止回退到最新文档。

```javascript
const query = new URLSearchParams({
  document_id: citation.document_id,
  start: String(citation.span_start),
  end: String(citation.span_end),
  content_hash: citation.content_hash,
});
```

- [ ] R8-F1：上传后分别观察 queued/running/succeeded/failed；刷新仍能恢复任务。若 source 列表无法返回其文档/最新 job，补受项目权限控制的最小元数据读接口；不要仅靠内存保存 job id，也不要返回原文到列表。
- [ ] 失败显示安全错误码及明确状态，上传排队成功不显示“可检索”。资料已登记与处理完成必须分开。
- [ ] R8-F2：成功回答点击引用得到准确的历史文档切片；覆盖同 source 两版本相同跨度不同内容、hash 不匹配、404、跨项目拒绝。展示资料名、版本标识和原文，不只展示内部 id。
- [ ] R8-F3：inference_only 不显示“回答已核验”；来源匹配只表示引用定位校验通过，不表示核心论点真实。历史消息能找到对应运行引用，不只显示全项目最后一次运行。
- [ ] 后端只读扩展同步更新契约文档及契约生成测试；内存和 PG 同测权限与数据形状。重跑 A-F，记录、提交。

## 任务 6：移动端与已登录键盘路径

**文件：**`frontend/app.css`、`frontend/app.js`、`tools/check_round8_browser.py`。

- [x] R8-G：全新注册用户在 390x844 与 1440x900 分别完成建项目、建会话、登记资料、读取处理状态、提问、打开引用、创建计划；768px 键盘路径额外验证证据入口。
- [x] 项目创建改为可展开表单或可访问对话框；窄屏证据改为折叠区/明确入口，不能 display:none 后无替代。优先保留原有布局，不引入第二套移动逻辑。
- [x] 纯键盘完成注册、建项目、建会话、登记资料、创建计划、提问与引用读取；长中文项目名、长引用和错误在移动端无横向溢出。当前无弹层。
- [x] 已检查登录态的移动端/桌面端答案引用、计划、资料状态截图及 768px 键盘闭环截图。
- [x] 重跑 A-G 浏览器回归；记录本地测试与截图证据。

## 任务 7：真实持久化闭环及诚实退出门

**文件：**新增 `tools/run_round8_gate.py`；更新 `tools/run_round6_gate.py`（PG skip 检测）；`task_plan.md`、`progress.md`、第七轮计划完成状态及本计划。

- [x] R8-H：PG + 真实 HTTP + ingestion/teaching worker + 确定性 provider，从 Cookie 注册完成全闭环。重启 web/worker，不清库，刷新后项目、会话、消息、计划、资料状态、引用仍正确；无重复运行或重复预算记录。
- [x] R8-I：截断真实 HTTP 响应并用同一 key 重放只创建一个 run；强杀已派发 provider 请求的 worker 后恢复进入待对账，不自动重跑。测试在隔离 PG 库进行。
- [x] gate 读取 pytest JUnit XML 的 tests/failures/errors/skipped：PG 定向组 tests > 0、failures=errors=skipped=0 才通过；普通全套的单个 memory-only skip 有测试名及原因。
- [x] 最终门禁运行并记录真实退出码与统计：

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -q
.venv\Scripts\python.exe -m pytest backend/tests -m postgres -q --junitxml=var/round8-postgres.xml
.venv\Scripts\python.exe -m ruff check backend tools
.venv\Scripts\python.exe -m mypy backend/app
node --check frontend/app.js
.venv\Scripts\python.exe tools/check_round8_browser.py --base-url http://127.0.0.1:8008 --artifacts var/round8-browser
.venv\Scripts\python.exe tools/run_round8_gate.py
git diff --check
```

端口 8008 是示例，必须先检查是否空闲并让测试服务器、浏览器参数一致。TEMP/TMP 不可写时指定工作区下独立临时目录；环境失败只记 blocked，不能改成 skip 后通过。

- [x] 从用户损害反向复核旧计划保护、会话/账户串读、断线重试、刷新恢复、手机新用户与引用回读；证据在下方最终验收记录。
- [x] 记录核心场景、测试层级、旧行为缺陷、通过证据与未验证边界；详细测试输出和截图路径见 `progress.md`。
- [ ] 更正第七轮过度勾选，不删除原历史事实；注明由第八轮补齐的日期与证据。没有通过的项保持未勾选。
- [ ] 所有必需场景通过后才提交最终记录并普通推送 `codex/round-8`。报告本地 commit、远端 commit、测试统计及仍未完成的云 provider/部署门；上传失败明确报告，不能声称已上 GitHub。

## 交给执行模型的开场指令

阅读本计划和同目录第七轮复审。先确认当前 HEAD、未提交变更和接口实际形状；按任务 0 至 7 顺序执行，不做计划外重构。每次先证明旧行为失败，再修复，再验证。任务不满足退出条件不得勾选或进入发布。遇到缺少凭据或不可用工具，继续做可验证部分并明确阻塞，不伪造通过。完成后停在第八轮验收报告，暂不开始第九轮。

## 后续边界

第八轮通过后再规划第九轮发布准备：真实 provider 小额授权 smoke、生产配置/迁移/备份恢复、运维对账流程与监控。已有计划的保留任务身份编辑及多人并发版本冲突需单独契约设计，不夹带进本轮 UI 修补。

## 执行记录（2026-09-20）

已完成代码修复和门禁脚本，未把未单独验证的条目勾选为完成：

- 已实现 R8-B 的 `204` 空 body、R8-A 的已有计划只读保护、R8-C 的作用域/epoch 防旧响应写回、R8-D 的页面生命周期幂等命令重试、R8-E 的作用域化运行恢复、R8-F 的摄取列表/状态与历史引用读取、以及 R8-G 的移动端可见入口和实际浏览器闭环所需修复。
- 反例证据已保留在 `backend/tests/test_round8_http.py`、`tools/check_round8_browser.py` 及本轮 `progress.md`：旧实现的 `b'null'`、项目级列表 404、隐藏移动端项目表单和中文幂等键 Fetch 异常均曾真实失败。
- `tools/run_round8_gate.py --base-url http://127.0.0.1:8008 --invite round8-final-11` 最终通过：全量 `765 passed, 1 skipped`；PG `125 passed, 641 deselected, 0 skipped`；Ruff、mypy、Node 和浏览器验收通过。唯一全量 skip 是内存实现不适用的 PG 并发争抢测试，原因写入 JUnit 和 `progress.md`。
- 当前仍未勾选 R8-H 的 web/worker 重启后真实 PG HTTP 恢复、R8-I 的中断网络恢复，以及 R8-G 中尚未单独提供证据的完整纯键盘/768px场景。C1/C2/C3 的浏览器逆序矩阵已在 2026-09-23 补验，证据见 `progress.md`；其余缺口仍不得由现有绿灯替代。

### 补充验收（2026-09-23）

- Playwright 在响应已由真实本机 HTTP 服务取得后暂缓交给应用，主动忽略 AbortSignal；这验证 scope/epoch guard，而不只验证浏览器取消请求。
- C1：A 项目的 plan、messages、completed run status 响应均晚于切换到 B；B 的计划标记保持可见，A 消息和运行条不进入 B。
- C2：同项目会话一的 messages/run 响应晚于切换到会话二；会话二的问题保持显示，会话一内容不串入。
- C3：用户一的 completed run（含已验证 citations）和消息响应先由本机服务返回并被暂扣；用户退出、用户二登录并建立自己的项目/会话后才释放。旧答案、引用入口、成功通知、运行条和草稿均未出现在用户二页面。
- 首次测试失败是测试钩子自身把空 targets 初始化成数组，GET 过滤调用 `.has()` 触发 TypeError；修正为空 Set 后矩阵通过。另确认开放注册不会自动创建项目，用户二测试按普通流程显式创建项目和会话。
- 独立浏览器脚本输出 `ROUND8_BROWSER_PASSED`。未修改产品前端逻辑。
- 新增 `tools/check_round8_process_recovery.py` 并接入 `tools/run_round8_gate.py`：真实 Uvicorn web 与独立 ingestion/teaching worker 均启动为独立 OS 进程，使用 `pg_support.create_test_database()` 创建迁移到 head 的随机 `study_test_*` 数据库。
- 真实 Cookie 注册后走 HTTP 项目默认空间、会话、计划、资料上传/摄取、教学、run 状态与 citation span；停止并重启 web/worker 后使用原 cookie 读回完整状态，原教学 Idempotency-Key 返回原 run，预算与 provider attempt 各一条。
- 本地 TCP 故障代理先将教学 POST 转发并确认上游 `202`，再向客户端返回截断响应体；客户端读响应失败后同 key 重试得到同一 run，数据库未重复创建。
- worker blocker 在 `provider.generate` 已开始（此时 dispatched attempt 与 in-flight 预算已提交）后被强杀；测试库中将该 run 的 lease 置过期，恢复 worker 将其落为 `reconciliation_required`，attempt 为 unknown，教学/平台预留保持 in-flight，provider call count 不再增加。
- 独立运行结果 `ROUND8_PROCESS_RECOVERY_PASSED`，summary 位于 `var/round8-process-recovery/run-74c15d2c10/summary.json`。这是真实本机 PG + socket HTTP + Uvicorn/worker 子进程，provider 本身是确定性本地 fixture，没有外网调用。
- 当前工作区 `.git` 只读，无法创建 `codex/round-8` 分支或 commit/push；因此本轮没有 GitHub 上传记录，不能声称已上传。代码和验收记录仍在工作区。

### 最终补验（2026-09-23）

- 390x844：全新注册用户完成项目、会话、资料上传与处理状态、计划、教学回答和原文引用；额外覆盖长中文项目名、长引用和 503 错误提示，均无横向溢出。
- 1440x900：同一新用户在独立项目重新完成上述核心路径；登录态截图分视图覆盖答案/引用、计划、资料状态。768x900 使用纯键盘完成注册至精确引用读取。
- 浏览器输出 `ROUND8_BROWSER_PASSED`；截图位于 `var/round8-g-final-browser/`，已人工查看移动端、桌面端和键盘截图。
- 最终统一 gate（代码与桌面/键盘回归版本）通过：全量 JUnit `924 tests, 0 failures, 0 errors, 1 skipped`；PG `143 tests, 0 failures, 0 errors, 0 skipped`；Ruff、mypy（125 个源文件）、compileall、Node、契约生成检查、浏览器与进程恢复通过。memory-only skip 是 `test_concurrent_reservation_on_last_slot_has_one_winner[memory]`，PG 对应用例执行且通过。最终增加的移动端截图/布局断言随后单独重跑浏览器并通过。
- 独立进程恢复 summary：`var/round8-process-recovery/run-01493a9f26/summary.json`。确定性 provider，无外部付费调用。
- 当时保留的 UI 缺口“项目创建表单常驻侧栏”已在 2026-09-23 补齐，详见下方记录。生产 provider 凭据、发布部署与运维门属于第九轮/首版验收，不由本地 gate 冒充完成。

### 项目表单收起状态补验（2026-09-23）

- 真实截图发现 `<details>` 在项目创建成功后仍保持展开；新增浏览器断言先以可见表单失败，确认覆盖实际缺陷。
- 将 disclosure 的 `open` 状态提升到 React，并且只在当前作用域的创建请求得到服务端确认后关闭。失败或响应未知时保留表单，允许继续恢复原幂等命令。
- 修复后浏览器矩阵输出 `ROUND8_BROWSER_PASSED`；移动端计划截图确认表单收起且长标题/引用仍可读。全新预览进程下统一 gate 通过：全量 `923 passed, 1 skipped`、PG `143 passed, 0 skipped`；Ruff、mypy、compileall、Node、契约、浏览器与真实进程恢复均通过。首次复跑因复用带有限流状态的预览进程而在第二账号注册处失败；没有改变限流设置，改用全新进程后通过。
