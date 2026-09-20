# 第 3 轮：学习闭环 MVP（2026-09-19）

## 目标

打通：目标采集 → 基础诊断 → 计划生成 → 任务状态流转 → 练习提交 → 证据记录 → 掌握度投影。

**退出门**：一个用户能从学习目标走到首个已验证任务，掌握度只由合法证据更新。

## 不动的前提

- `EvidenceEvent` 是掌握事实源；`Projector` 是 `MasteryProjection` 唯一写入者（已有，本轮只接 PG 落库与 API）。
- 产品执行证据不进入掌握投影；只有 kind=LEARNING 且带事前冻结 contract 的证据计入。
- 数据模型改动全部进 0005 迁移，一次到位；幂等/认证/RLS 规则沿用第 1-2 轮成果。

## 任务 9：证据落库 + 任务状态流转

- **迁移 `0005_learning_loop`**：
  - `task_assessments`：计划生成时**事前冻结**的评估契约（task_id → component_id + contract_id + mapping_version）。没有它，学习证据无法合法进入投影。
  - `task_submissions`：练习提交记录（主体维度组合外键 + 项目维度组合外键）。
  - `diagnoses`：基础诊断记录（jsonb 答案 + 确定性摘要）。
- **`learning/ports.py`**：`EvidenceRepository` 协议；内存 / PostgreSQL 双适配器（PG 回放 = 从 payload jsonb 重建事件；append = INSERT，seq 由 identity 列生成）。
- **任务状态机**：pending → in_progress → done｜skipped、pending → skipped；done/skipped 终态。非法迁移 `ILLEGAL_STATE_TRANSITION`。PG 用 `UPDATE ... WHERE status = expected` 的行数判定保证并发安全。
- **端点**：`POST /projects/{id}/tasks/{task_id}/transition`、`GET /projects/{id}/mastery`（PG 证据回放 → Projector）。
- verified 是**计算结论**：任务有 ≥1 条有效正向学习证据即 verified——不存库、随投影与证据实时算。

## 任务 10：练习提交 + 证据追加

- `POST /projects/{id}/tasks/{task_id}/submissions`（body: content + mode=self_report）。
- 前置：任务必须 in_progress；无冻结评估契约 → 拒绝（EVIDENCE_UNMAPPED）。
- **提交行与证据事件同事务**（PG 版单事务两个 INSERT；失败一并回滚，不留"有提交无证据"）。
- MVP 裁决（诚实标注）：平台只能观测到自报 → `observation_strength=OBS_1`、`independence_level=INTRODUCED`、`direction=POSITIVE`、`assessment_validity=VALID`。低观测强度自然压低置信度——不伪造强证据。
- independence_group = submission_id（每次提交独立成组）。

## 任务 11：目标采集 + 诊断 + 计划生成 + 退出门

- 目标采集：复用 `PATCH /projects/{id}` 的 goal（第 2 轮已有），不另起炉灶。
- `POST /projects/{id}/diagnosis`：固定 3 问自评 → 确定性摘要。
- `POST /projects/{id}/plan/generate`：**确定性模板**（内置 graph/v1：concept/practice/reflection 三组件；3 里程碑，goal 与诊断摘要拼进任务标题），生成即冻结 task_assessments；替换现有计划走 replace_plan。诚实注明：模板是占位，第 4 轮接真实模型后替换。
- **退出门端到端**（PG 装配脚本）：目标 → 诊断 → 生成计划 → 首任务 in_progress → 提交 → 证据落库 → done → mastery 显示该组件 `introduced/low` → **反证：改动任务状态/重试投影不改变掌握度**（掌握度只随证据变）。

## 执行顺序

9 → 10 → 11 严格串行，每任务一提交，TDD（先契约测试后实现），PG 组不得 skip。
