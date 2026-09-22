# 开放注册与用户名密码认证设计

## 状态

已获用户确认的产品方向：用户名 + 密码、开放注册、注册后自动创建个人学习空间。
本文是实现前规格；当前线上仍保留邀请码兑换，未认证用户默认界面尚未切换。已冻结为 `0012` 开工基线。

## 目标与非目标

### 目标

- 未注册用户可通过用户名和密码注册；首版不单独收集显示名称，服务端将原始用户名复制到 `principals.display_name`。
- 注册事务自动创建一个个人租户和一个主体；不要求邀请码、邮箱或管理员介入。
- 用户可使用用户名和密码登录，服务端创建可撤销的数据库会话并签发 HttpOnly Cookie。
- 登录、注册、邀请码兑换共用严格来源校验、未认证限流和认证审计。
- 已有邀请码用户和已有 Cookie 会话继续可用；迁移不删除或重解释现有身份。
- 内存适配器与 PostgreSQL 适配器通过同一组行为契约测试。

### 非目标

- 本轮不实现邮箱验证、自助密码重置、社交登录、二次认证或管理员后台；运维处置流程另行交付，不得临时手工改库冒充恢复。
- 账号生命周期基线：密码遗失时首版不恢复、不手工改 `password_hash`；禁用、数据导出、删除请求由受理人、核验材料、处置结果和审计事件组成独立运维流程。若日后增加管理员重置，必须使用一次性恢复凭证、递增 `security_generation`、撤销旧会话并保留审计，不能只凭用户名操作。
- 本轮不允许客户端提交 `tenant_id`、`principal_id` 或项目归属字段。
- 注册会创建一个空的个人默认学习空间及授权，不灌入示例资料、对话或计划；用户可在工作台继续创建更多学习项目。
- 本轮不移除邀请码兑换端点；它保留给内测、管理员邀请和兼容旧用户。

## 用户流程

### 注册

1. 浏览器打开同源认证页，默认显示“注册”与“登录”切换。
2. 用户提交 `username`、`password`。
3. 服务端做长度、规范化和密码强度校验；请求来源必须是可信同源。
4. 服务端在一个事务中创建个人租户、主体和密码凭据。
5. 服务端创建 `user_sessions` 行并签发 `study_session` HttpOnly Cookie。
6. 前端进入项目工作台；项目列表为空时显示创建项目动作。

### 登录

1. 用户提交 `username` 和 `password`。
2. 服务端按规范化用户名查找凭据，在应用层用 Argon2id 验证密码。
3. 用户名不存在、密码错误、主体禁用和凭据禁用返回同一公共认证错误，不暴露账号是否存在。
4. 成功后创建数据库会话并签发与邀请码兑换相同格式的 Cookie。
5. 失败尝试按客户端和规范化用户名组合限流，并写入认证失败审计；日志和审计不得包含密码、密码哈希或原始 Cookie。

### 邀请兼容

`POST /auth/invitations/exchange` 保持现有一次性令牌语义。前端可放在“使用邀请访问码”次级入口，不作为普通用户默认入口。已兑换的主体、租户和会话不迁移、不重建。

## 数据模型与迁移

新增迁移 `0012_open_registration_auth.py`，迁移必须可逆，并继续使用现有 RLS、组合外键和安全函数模式。

### `account_credentials`

- `credential_id text primary key`
- `tenant_id text not null`
- `principal_id text not null`
- `username text not null`：保留用户输入的原始用户名用于展示和回填
- `username_normalized text not null unique`：NFKC 后再 `casefold()`，作为唯一查找键；唯一索引使用 `COLLATE "C"`
- `password_hash text not null`：Argon2id PHC 字符串；只存哈希，不存密码
- `created_at timestamptz not null`
- `updated_at timestamptz not null`
- `last_login_at timestamptz null`
- `disabled_at timestamptz null`

凭据通过 `(tenant_id, principal_id)` 组合外键绑定 `principals`。用户名唯一约束是全局唯一，避免同一个登录名在不同个人租户下产生歧义。账号注册使用服务端生成的租户和主体 ID。

### 未认证数据库访问

应用角色不能直接读取凭据表。迁移新增两个 `SECURITY DEFINER` 函数，并固定 `search_path`、显式限定对象、撤销 `PUBLIC` 执行权限，只授予 `study_app`：

- `register_account(...)`：在同一事务中插入个人租户、主体和凭据；冲突只返回稳定的用户名占用错误。
- `lookup_account_for_login(username_normalized)`：返回登录所需的主体字段和 Argon2id 哈希；找不到时返回空结果。HTTP 层统一对外认证失败。

函数不接受客户端租户或主体身份。应用层验证密码后，只能用返回的服务端主体数据创建会话。

## HTTP 契约

新增：

- `POST /auth/register`
  - 请求：`username`、`password`
  - 成功：HTTP `201`，创建会话 Cookie，返回 `principal_id`、`expires_at` 及默认项目标识
  - 失败：稳定的校验错误、用户名占用、限流或统一认证错误；不回显密码
- `POST /auth/login`
  - 请求：`username`、`password`
  - 成功：HTTP `200`，创建会话 Cookie，返回 `principal_id`、`expires_at`
  - 失败：统一 `AUTH_REQUIRED` 语义，不区分用户名不存在和密码错误

现有 `/auth/logout`、`/auth/logout/all`、所有产品端点和 Cookie 属性保持不变。

### 输入约束

- `username`：首版仅允许基本汉字 `U+4E00–U+9FFF`、CJK 扩展 A `U+3400–U+4DBF`、ASCII 英文字母/数字及 `_`、`.`、`-`；仅额外接受全角 ASCII 英文字母作为兼容原文输入。原文先按允许集合、首字符和 1–16 码点检查，再计算 `NFKC + casefold()`，规范化结果再次按同一集合、首字符和长度检查。字符范围、规范化实现和 Unicode 数据版本由固定测试向量锁定，禁止依赖 `isalpha()` 或静默改变既有唯一性规则。密码不进行这些规范化操作。
- `password`：12–128 个 Unicode 字符；不强制字符类别组合，避免只制造可预测规则。
- 所有请求模型 `extra="forbid"`；客户端不能带身份、租户、角色、项目或凭据状态字段。

### 来源与限流

- 注册和登录都必须通过可信 `Origin`，无 `Origin` 时必须通过可信 `Referer`；两者缺失或不可信均拒绝。注册、登录和认证错误响应统一 `Cache-Control: no-store`。
- 注册按客户端键限流；登录按客户端键与规范化用户名组合限流，并设置独立配置上限，不能复用邀请码兑换的单一桶语义。
- 用户名占用错误可以返回 409 供正常用户修正，但登录失败必须统一，不得成为账号探测接口。

### 运行与发布开关

- `registration_enabled` 与 `password_login_enabled` 是两个独立的、默认关闭的开关；缺失或读取失败均关闭。开关由唯一事实源提供，并在多实例启动/健康检查中核对一致性。
- 有效 Cookie 调用注册/登录返回 `409 already_authenticated`；失效 Cookie 视为未登录。普通 logout 可清除失效 Cookie，但不得声称撤销了有效服务端会话；`logout/all` 仅允许当前会话仍有效时执行。
- Argon2 计算池覆盖注册、真实登录、dummy 验证和重哈希；初始每进程并发 2，注册低优先级、登录优先，策略限流返回 429，计算池饱和返回 503 + `Retry-After`。

## 后端组件

- `identity` 增加凭据领域模型、规范化函数和注册/查找端口。
- `db/identity_store.py` 增加 PostgreSQL 凭据适配器；所有未认证查找走迁移函数。
- `identity/memory_store.py` 增加等价内存实现，测试只用于开发和契约验证。
- `api/auth_routes.py` 增加注册、登录入口，复用现有 Cookie 签发、会话仓储、审计和错误响应。
- 密码哈希使用 Argon2id 库的 PHC 字符串格式；参数由单一模块集中定义，验证支持未来参数升级，不把哈希算法散落在路由中。
- `main.py` 生产装配提供新仓储和限流器；生产继续关闭 Bearer 兼容通道。

## 前端行为

- 未认证页默认展示“登录”和“注册”两个模式切换。
- “邀请访问码”作为次级入口保留，兑换成功后与登录/注册进入同一个工作台。
- 注册成功和登录成功不把密码、邀请令牌或 Cookie 写入 localStorage/sessionStorage。
- 401 仍清理认证相关页面状态并回到认证页；错误文案不泄露账号存在性。
- 移动端 390px 与桌面视口都必须可完成注册、登录和进入空项目工作台。

## 测试与退出门

### 单元和契约

- 用户名规范化、大小写重复、Unicode/长度边界、密码长度和额外字段拒绝。
- Argon2id 哈希不可逆、不同密码不相等、错误密码拒绝；响应和审计不含密码或哈希。
- 内存与 PostgreSQL 都覆盖注册、重复用户名、登录成功、错误凭据统一错误、禁用账号、限流和会话撤销。
- 注册事务故障不会留下只有租户、只有主体或只有凭据的半成品。
- Cookie 属性、CSRF、Origin/Referer、会话 TTL、退出和退出所有设备沿用现有安全门。

### PostgreSQL

- `0002 → 0012 → 0011 → 0012` 可逆迁移验证。
- 未认证应用角色不能直接读取 `account_credentials`，只能执行明确的 definer 函数。
- 跨租户组合外键、RLS、用户名唯一约束和并发注册只允许一个成功。
- `user_sessions` 增加 `auth_method`、可空 `credential_id`、`security_generation`；密码会话必须关联同主体凭据，邀请会话不得关联凭据。Argon2 参数版本独立于安全代际。既有会话仅在核验全部为邀请认证后回填为 `invitation`，发现未知来源即中止迁移。

### 浏览器

- 新用户在 390px 和桌面视口完成开放注册、自动进入工作台、建项目和登出。
- 已注册用户完成登录、刷新恢复、登出后再次登录。
- 邀请兑换仍能进入同一个工作台。
- 旧邀请码、密码和 Cookie 不出现在网络日志、localStorage 或错误响应中。

## 部署与回滚

1. 先运行内存门禁、静态检查和 PostgreSQL 迁移/契约门禁。
2. 备份生产库，执行 `0012`，再部署 web 代码并重启 web worker。
3. 先用合成账号完成注册/登录浏览器 smoke，再开放普通用户入口。
4. 若应用回滚，必须保留 `0012` 数据兼容；不删除凭据表，不把密码转换成明文或旧格式。
5. 迁移和 web 代码未同时通过门禁前，不将生产入口切换为开放注册。
