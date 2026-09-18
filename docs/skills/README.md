# docs/skills —— 设计知识的分层加载目录

本目录与 `docs/superpowers/` **职责不同，不要混用**：

| 目录 | 是什么 | 谁读 |
|---|---|---|
| `docs/superpowers/` | 规格与计划，按主题成篇，需要完整阅读 | 人（设计与评审） |
| `docs/skills/` | 按需加载的设计知识，**先读索引再读条目** | Agent（设计/编码时查询） |

规格回答"为什么这样设计"；这里回答"实施时这一步该怎么做，以及哪些约束不能违反"。

## 三层结构

| 层 | 加载时机 | 单份预算 | 目录 |
|---|---|---|---|
| L0 常驻 | 每次开工 | ≤ 1.5k token | `L0/` |
| L1 领域契约 | 命中 `triggers` 时 | ≤ 4k token | `contracts/` |
| L2 任务手册 | 执行该类任务时 | ≤ 2k token | `playbooks/` |

**加载顺序固定：先读 `manifest.yaml`（小），再按触发条件读具体条目（大）。** 不允许跳过索引直接猜文件名。

## 唯一入口

`manifest.yaml` 是本目录的唯一索引。每个条目包含：

```yaml
id / layer / status / path / version / size_estimate
triggers / depends_on / expiry / generated_from / note
```

- `status: active` —— 文件已就绪，可直接读取。
- `status: planned` —— **预留位**：意图与触发条件已登记，文件后补。这是"保留扩展余地"的实现方式——想到但还没写的契约有地方待着，不会被遗忘，也不阻塞实施。

## 如何新增一份契约（成本必须是 O(1)）

1. 在 `manifest.yaml` 的 `entries` 加一个条目；
2. 新建对应文件；
3. 跑校验：`python tools/skills/check_manifest.py docs/skills/manifest.yaml`

**不需要修改 05／06 号规格，也不需要改任何业务代码。** 如果新增一份契约需要动到别处，说明分层设计有问题——先修机制，再新增。

## 契约文档的双区结构（防漂移）

手写文档必然与代码漂移，因此每份 L1 契约固定分两区：

```markdown
## 生成区
<!-- BEGIN GENERATED: source_hash=..., generated_at=... -->
（由代码导出：迁移、Pydantic schema、tool registry）
<!-- END GENERATED -->

## 手写区
（使用说明、when-to-use、坑与例外）
```

- **生成区**：机器可推导的内容一律由脚本导出，CI 比对不一致即构建失败。
- **手写区**：人写的内容不校验，但必须带 `expiry` 到期复审。
- **机器可推导的部分不许手写。**

## 校验的机械门

| 门 | 作用 | 违规后果 |
|---|---|---|
| manifest 结构校验 | 字段完整、`id` 唯一、路径存在（`planned` 除外） | 构建失败 |
| 依赖方向校验 | 只允许 L0 → L1 → L2；无环；**`active` 不得依赖 `planned`** | 构建失败 |
| 生成区一致性校验 | 文档生成区与源码导出结果一致 | 构建失败 |
| 体积预算校验 | 超出所在层预算即提示拆分 | 构建失败 |
| 到期复审校验 | `expiry` 过期未复审则告警 | 告警，超过 30 天升级为失败 |

## 首批落地范围（2026-09-18）

只先落三份，用于验证**机制本身**是否有效：`sql-schema`、`protocol`、`tool-catalog`。三者分别代表「数据库约束」「接口契约」「工具选择歧义」三类不同的可机械校验。

其余条目以 `status: planned` 登记。**机制未验证通过前不补文档。**
