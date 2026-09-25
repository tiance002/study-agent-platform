# tools 入口索引

> 历史入口与可重建产物的整理基线（2026-09-23，对应
> `docs/superpowers/plans/2026-09-23-architecture-followup.md` 任务 7）。
> 新工作以 **Round 8 系列**为当前门禁基线；Round 6/7 脚本仅保留用于历史复现。

## 当前执行入口

### CI 门禁（`.github/workflows/ci.yml` 每次推送运行）

| 脚本 | 用途 |
|---|---|
| `skills/check_manifest.py docs/skills/manifest.yaml` | 校验 skill manifest 结构、依赖方向与复审期 |
| `skills/check_agent_skills.py` | 校验 `.agents/skills` 结构与 frontmatter |
| `skills/gen_contracts.py --target {sql-schema,protocol,tool-catalog} --check` | 三份契约与实现一致性 |
| `migrations/check_migrations.py --require-active` | Alembic 迁移链合法性与活跃状态 |
| `security/scan_licenses.py` | 依赖许可证（copyleft）扫描 |
| `security/scan_secrets.py` | 秘密扫描 |

### 本地 / 发布前总门禁

| 脚本 | 用途 |
|---|---|
| `run_round8_gate.py` | **综合门禁**：全量 pytest、PostgreSQL 子集、ruff、mypy、前端 5 个脚本 `node --check`、契约 `--all --check`、浏览器回归、进程恢复检查 |
| `run_round8_preview.py` | 启动带确定性 provider 的一次性预览（默认端口 8008），供浏览器回归使用 |
| `check_round8_browser.py` | 浏览器回归（登录/项目/会话/迟到响应门控/同键重试/窄屏），配 `--base-url` |
| `check_round8_process_recovery.py` | 中断后恢复场景验证 |
| `bench_metrics_snapshot.py` | `study_metrics_snapshot()` 容量基准（临时库，见 `docs/performance/metrics-snapshot-2026-09.md`） |

浏览器回归的标准流程：后台起 `run_round8_preview.py --port 8008` →
`check_round8_browser.py --base-url http://127.0.0.1:8008` → 停掉预览进程
（两次浏览器检查之间需重启预览：内存限流器会因重复注册报"尝试过于频繁"）。

### 开发辅助

| 脚本 | 用途 |
|---|---|
| `issue_session.py` | 生成本地调试用会话令牌 |
| `push_via_api.py` | 代理环境下经 Git Data API 推送的替代流程 |
| `pytest_gate_support.py` | 门禁共用的 JUnit XML 解析库（非直接入口） |

## 历史复现入口（不作为当前门禁）

以下脚本不再被 CI 或当前流程引用，仅被历史计划/评审文档引用，**保留原位以可追溯**；
复现历史结论时按对应文档使用：

| 脚本 | 替代者 | 历史引用 |
|---|---|---|
| `run_round6_gate.py` | `run_round8_gate.py` | `docs/superpowers/plans/2026-09-20-round-6-reliability-and-provider.md`、`progress.md` |

> `run_round7_preview.py`、`check_round7_browser.py`、`issue_invitation.py` 已随
> 2026-09-25 邀请码全链路移除一并删除（它们只依赖已删除的邀请兑换入口，
> 无法再运行）；历史引用见 `docs/superpowers/plans/` 同名计划的归档记录。

## 可重建产物与忽略规则

以下产物均已在 `.gitignore` 中忽略，**可随时删除并重建**：

- `output.json`、`pytest_html_report.html` —— pytest 运行输出，重跑 pytest 即重建；
- `.pytest_cache/`、`__pycache__/`、`htmlcov/`、`.coverage` —— 缓存，删除无副作用；
- `var/` —— 运行时数据与门禁证据（`var/round6-gate`、`var/round8-preview` 等），
  由对应 gate/preview 脚本重新生成；**只按子目录处置，不递归清空整个 `var/`**。

## 不做归档/不移动的清单

- Alembic 迁移（`alembic/versions/`）—— schema 唯一来源，含回滚护栏；
- ADR 与评审记录（`docs/adr/`、`docs/reviews/`）—— 架构决策证据；
- `INTEGRATION_SUMMARY.md`、`verify_components.py` —— 集成摘要与组件验证，保留在仓库根；
- `.trae/`、`.agents/`、`.codex/`、`.serena/` —— 工具与技能材料，不入归档流程；
- `docs/superpowers/plans/` —— 历史计划文档，历史入口引用的主要来源。
