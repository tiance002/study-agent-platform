# 开放注册与中文用户名认证实施计划

> **For agentic workers:** 使用 superpowers:executing-plans 按任务执行，逐项记录证据。当前状态：已获用户批准，按边界任务实施；不一次性混合代码、迁移和部署。

**Goal:** 用户无需邀请码即可用中文用户名与密码注册、登录，并拥有隔离的个人学习空间。

**Architecture:** 沿用 FastAPI、PostgreSQL、现有 Cookie 会话与租户隔离。新增密码凭据仓储及原子注册/登录提交边界；邀请码保持兼容。前端认证页接入同源 API。

**Tech Stack:** Python、FastAPI、psycopg、Alembic、Argon2id、React、pytest、Playwright。

## 执行状态（2026-09-21）

实现范围已完成：用户名/密码契约、`0012` 迁移、内存/PG 仓储、注册/登录/退出 API、Argon2 计算池、持久限流、平台级付费预算、前端入口及运维手册均已落地。非 PostgreSQL 全量回归、定向认证/预算测试、Ruff、mypy、compileall、前端语法、合同生成与许可证扫描已通过。

仍未宣称完成的发布门：本机 PostgreSQL 当前不可达，因此 `0012` 最新 SQL 的临时库往返、权限、并发和真实 HTTP 重启测试待数据库可达后执行；浏览器 Playwright 及生产发布也不在本轮本地执行。迁移未通过这些门槛前，两个认证开关和付费派发开关都保持关闭。

## 全局约束与设计修正

- 用户名首版仅允许 `U+4E00–U+9FFF`、`U+3400–U+4DBF`、ASCII 英文字母/数字及 `_`、`.`、`-`；仅额外接受全角 ASCII 英文字母兼容输入。原文与 `NFKC + casefold()` 结果均按允许集合、首字符和 1–16 码点检查；保存原文，规范化值使用 `COLLATE "C"` 唯一键；测试向量锁定 Unicode 数据版本。
- 密码保持原样，12–128 个码点，不做 Unicode 规范化。注册不单独收集显示名，服务端将原始用户名复制到 `principals.display_name`。
- 注册事务包括租户、主体、凭据、会话、成功审计 outbox；任何一步失败全部回滚。HTTP 响应丢失后用户可直接登录，不得再次创建身份。
- 登录读取哈希后进行密码验证；提交会话时再次锁定并检查凭据版本、禁用状态，防止验证与提交之间状态改变。`security_generation` 与 Argon2 `hash_version` 分离；`last_login_at`、会话、审计一起提交。
- 认证函数只给应用角色最小执行权，固定 search_path、撤销 PUBLIC；这些函数是应用服务信任边界，不声称持有应用数据库凭据的攻击者也无法模拟登录。
- 旧规格“主体禁用”没有独立持久字段，本轮以凭据 disabled_at 作为密码账号禁用事实，并让禁用账号的现有密码会话也被拒绝；邀请码身份兼容。禁止凭空检查不存在的字段。
- 邀请码新增严格来源检查会影响旧测试客户端；迁移测试必须显式传 Origin，并加入跨站兑换拒绝用例。
- 不收集邮箱，因此首版没有自助找回密码；注册页明确告知用户保存密码。旧邀请用户不自动绑定同名账号或转移数据。
- 有效 Cookie 调用注册/登录返回 `409 already_authenticated`；失效 Cookie 视为未登录。普通 logout 可清除失效 Cookie 但不得伪称撤销成功；`logout/all` 仅接受有效当前会话。
- `registration_enabled` 与 `password_login_enabled` 独立且默认关闭；缺失/读取失败均关闭。平台付费开关独立于注册开关。
- 每个任务只修改列出的范围；与冻结契约冲突时报告并停止相关实现，不自行改规则、放宽安全约束或顺带重构。每项交付列出修改文件、实际测试结果、未运行项和契约偏差。
- 审批后才执行代码、数据库变更和部署。生产库不能用作 pytest 测试库。

## 任务 1：冻结规则和密码边界

文件：新增 backend/app/identity/passwords.py、backend/app/identity/accounts.py、backend/tests/test_password_accounts.py；修改 pyproject.toml、requirements.lock.txt 和许可证清单。

- [ ] 明确接口 normalize_username(raw: str) -> str、hash_password(raw: str) -> str、verify_password(raw: str, encoded: str) -> bool；用成熟 Argon2id 库，锁定兼容版本并检查许可证。追加原始用户名保存接口和 Unicode 版本常量。
- [ ] 先写边界测试：张、张三、16 字通过；空串、17 字、空格、换行、零宽与双向控制字符拒绝；全角Ａ与 a 冲突；casefold 扩展后超长拒绝；密码不能被 trim 或 Unicode 规范化。
- [ ] 运行测试确认行为失败，再实现。哈希参数集中配置，初始采用 memory_cost=19456 KiB、time_cost=2、parallelism=1；上线机基准验证延迟与并发内存后可提高，不静默降低。
- [ ] 缺失用户也执行预生成 dummy hash 验证；哈希损坏对外统一失败，内部记录脱敏诊断；成功登录支持 check_needs_rehash。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest backend/tests/test_password_accounts.py -q -p no:cacheprovider`，保留真实哈希/验证证据，不以“字符串含 argon2”替代验证。

## 任务 2：原子仓储与迁移

文件：新增 alembic/versions/0012_open_registration_auth.py、backend/app/db/account_store.py、backend/app/identity/account_memory_store.py、backend/tests/test_account_repositories.py、backend/tests/test_account_postgres.py；修改 identity/accounts.py 和 main.py 装配。

- [ ] 定义 AccountCredential（主体、租户、原始用户名、规范化用户名、哈希、hash_version、security_generation、disabled_at）与仓储接口 register_and_create_session、lookup_for_login、complete_login。具体参数使用服务端生成 ID、期限、预期凭据版本，禁止 HTTP 传入身份字段。
- [ ] 在随机 study_test_* 临时库先写失败用例：规范化同名并发注册只有一次成功；会话/outbox 故障时五类写入全部回滚；不同账号空间隔离；禁用/版本变化后完成登录被拒绝。
- [ ] 新表增加全局唯一 `username_normalized COLLATE "C"`、每主体唯一凭据、组合外键、1–16 长度 CHECK、`hash_version` 与 `security_generation`；`user_sessions` 增加 `auth_method`、可空 `credential_id`、`security_generation` 及对应 CHECK/FK。存量会话先核验来源，全部确认为邀请后才回填；应用/worker 不得直接查询密码表。
- [ ] definer 函数分为注册、精确凭据查找、登录提交；输入长度、会话 TTL、凭据版本和主体归属在数据库再次检查。返回值不进入 HTTP 序列化。
- [ ] 内存实现用同一临界区和共享会话/outbox 完成对应原子操作，不写入私有字典绕过仓储接口。
- [ ] 验证空库到 head、0011 → 0012 → 0011 → 0012；降级仅在临时库演练，检查函数授权、RLS 和现有邀请数据未损坏。
- [ ] PostgreSQL 测试必须实际执行且零意外 skip；保留并发结果和回滚行数，不仅检查最终 HTTP 状态。

## 任务 3：注册登录 API 和反滥用

文件：修改 backend/app/api/auth_routes.py、backend/app/deployment.py、backend/app/main.py、backend/app/core/errors.py；新增 backend/tests/test_password_auth_api.py；复用 identity/rate_limit.py、db/identity_store.py 与 audit/outbox.py。

- [ ] POST /auth/register 成功 201，POST /auth/login 成功 200；返回主体 ID 和到期时间，使用既有 Secure/HttpOnly/SameSite Cookie。用户名占用 409，非法输入 422，错误账号/密码/禁用统一 401，限流 429 + Retry-After。
- [ ] 顺序固定为请求大小限制、严格来源检查、持久限流、密码计算、原子提交、Cookie 响应；成功响应不得因 outbox 投递失败变成账号已创建却返回失败。
- [ ] 默认注册每 IP 每小时 5 次；登录每 IP 每 10 分钟 30 次、每规范化账号每 10 分钟 10 次，独立桶均需通过；账号桶使用摘要，不能只用 IP+账号组合让攻击者轮换一端绕过。
- [ ] Argon2 每进程并发上限初始为 2，覆盖注册/登录/dummy/rehash；登录优先、注册低优先，队列上限和等待时限显式配置。策略限流返回 429，计算池饱和返回 503 + `Retry-After`。生产 PostgreSQL 限流重启不清零，保留受信代理解析。
- [ ] 有效已有会话调用注册/登录返回 `409 already_authenticated`；失效 Cookie 视为未登录。登录/注册换发新 session_id，检查禁用密码账号时同步拒绝既有会话；普通 logout 可清除失效 Cookie，logout/all 不得由失效会话触发。
- [ ] 测试跨站登录/注册/邀请、缺 Origin、伪造 XFF、注入 tenant_id、错误消息统一、响应与审计无密码/哈希、请求验证失败不回显输入。
- [ ] 回归 logout、logout/all、TTL、密钥轮换、生产 Bearer 禁用、邀请码单次兑换及有效会话；测试客户端显式传同源 Origin。

## 任务 4：开放注册的费用上限

文件：检查并按缺口修改 backend/app/teaching/service.py、backend/app/teaching/ports.py、backend/app/teaching/memory_store.py、backend/app/db/teaching_store.py、backend/app/deployment.py；新增 backend/tests/test_public_registration_budget.py；需要数据库变更时独立新增 0013_public_budget_cap.py。

- [ ] 先证明现有项目预算是否允许通过新建项目/账号增加总支出；记录当前真实计费单位，禁止凭字段名猜测货币。
- [ ] 在真实 provider 派发前原子预留平台总预算，所有账号和项目共同受限；并发最后一份额度只准一次派发，进程重启不重置。
- [ ] 与已有未知计费语义一致：超时或用量不明不能释放为免费额度；用量明确后结算。不得新增自动付费重试。
- [ ] 平台总额度缺失时拒绝付费调用；首版所有 provider 共享、按数据库 UTC 自然月归属，预留固定 `billing_period`、provider、price_version、最大可支出和结算状态；跨月不自动释放陈旧 `in_flight`，降额只阻止新派发。独立 `paid_dispatch_enabled` 缺失默认关闭；注册/登录开发与测试不依赖具体预算数值。

## 任务 5：普通用户认证界面

文件：修改 frontend/app.js、frontend/app.css、backend/tests/test_frontend_assets.py；新增 tools/check_registration_browser.py。

- [ ] 登录为默认页，注册为并列切换，邀请码为次级入口；中文标签、正确 autocomplete、密码确认、提交中/错误/成功状态、可访问关联标签。
- [ ] 客户端支持 1–16 码点用户名，显示服务端规范化冲突；密码只保留在当前表单内存，切换模式/成功后清空，不写本地存储。
- [ ] 登录接口 401 显示“用户名或密码错误”；工作区 401 返回登录页，修正原“重新兑换邀请”文案。
- [ ] 账号切换递增 epoch，清除旧项目/会话/草稿/请求重试状态；迟到响应不得进入新账号。
- [ ] 前端所有功能使用现有 React 与 CSS 风格；不重构整个工作台。

## 任务 6：独立验收门

文件：新增 tools/run_registration_gate.py、backend/tests/test_registration_restart.py；修改 tools/check_round8_browser.py 兼容新的邀请次级入口；用 tools/pytest_gate_support.py 隔离数据库。

- [ ] 定向单元、HTTP、内存/PG 契约通过后运行现有全量门；生成 JUnit，PG 不可用就是阻塞，不把 skip 写成成功。
- [ ] 静态检查：`.venv\Scripts\python.exe -m ruff check backend tools`、`.venv\Scripts\python.exe -m mypy backend/app`、`node --check frontend/app.js`、`git diff --check`；契约使用 tools/skills/gen_contracts.py 的现有参数生成并核对。
- [ ] Playwright 在 390×844、768×1024、1440×900 实际注册“张”、退出、重新登录、建项目、会话、登记资料、保存计划；检查截图与水平溢出。
- [ ] 两个账号、两个浏览器上下文证明跨租户读写拒绝；重复用户名并发、登录错误限流、邀请码兼容分别留证。
- [ ] 临时 PostgreSQL + 真实 HTTP 重启 web/worker 后重新登录，原项目/计划/资料仍完整；注册响应丢失后用相同账号登录，只有一份身份。
- [ ] 使用确定性 provider 完成教学路径，不把 fake 回答当真实 DeepSeek 联调。付费联调只在预算配置就绪后单独记录。
- [ ] 做反向注入：移除来源检查、唯一约束、限流、事务回滚各使对应测试变红，随后恢复；独立审查不得只重复实现者测试。

## 任务 7：生产发布与记录

文件：修改 deploy/production/README.md、deploy/production/bootstrap_server.sh、task_plan.md、progress.md；记录发布版本与文件摘要。

- [ ] 发布前轮换此前错误信息中已暴露的迁移数据库密码，同步 admin.env 的迁移/备份 DSN 与 bootstrap 秘密文件，验证迁移和备份；不回显新旧凭据。
- [ ] 备份并恢复验证；构建只含运行资产的发布包，root 拥有、目录 0755/普通文件 0644/脚本 0755，不包含 PEM、env、测试输出。
- [ ] 生产通过功能开关暂时关闭注册，迁移后切换新 release，重启并检查 nginx/web/worker；待合成账号 smoke 和费用上限验证通过后开启注册。
- [ ] 检查外部 HTTPS GET、真实 Cookie 登录、CSRF 拒绝、中文单字注册、退出再登录。失败则关闭注册并切回兼容旧应用版本，保留新增账号数据，不在生产 downgrade 删表。
- [ ] 用户自己首次建立的个人账户不与既有邀请码账户自动合并；需要迁移历史数据另行明确归属。
- [ ] 每阶段提交/推送前检查工作区，保留用户改动；若 .git 权限仍受限，记录未提交原因，不能报告上传成功。
- [ ] 交付说明包含可访问地址、注册方式、已通过证据、密码找回限制、费用上限及尚未验证项。

## 审批范围与退出条件

本次审批覆盖以上开发、测试和发布步骤；平台付费总预算的具体值在上线前单独确定。用户名长度已按用户要求固定为 1–16，不再请求重复确认。

全部完成的判定：普通用户无需终端或邀请码即可注册并再次登录；每人空间隔离、重启保留数据；安全与 PG 门实际通过；平台付费总额不能通过新增项目或账号绕过；生产浏览器完成核心流程。仅页面出现注册按钮或测试全绿不构成完成。

当前只提交计划供审批，尚未实现上述功能或操作生产环境。
