# L1 契约 · sql-schema

> `skill_id: sql-schema` | version: 0 | expiry: 2027-03-01 | owner: 待指派
> 权威来源：`specs/2026-09-17-04-multitenancy-and-data-governance.md`、`03-policy-tools-and-execution.md`、`01-learning-loop-and-mastery-evidence.md`
> **生成区已启用**：由 `tools/skills/gen_contracts.py` 从代码导出，并参与 CI 一致性校验。
> 生成区**从 alembic 迁移导出**（迁移是数据库的权威定义），不再扫描代码实体 —— 改迁移，本视图随之更新。

## 1. 生成区

<!-- BEGIN GENERATED: source=由 alembic 迁移导出（数据库的权威定义）, source_hash=sha256:f9481e1a6f69251efc571f5426d80874306cde87c8bb45d75754b56fa879354f, generated_at=2026-09-25T10:39:11Z -->
> 生成时间：2026-09-25T10:39:11Z

> 由 `tools/skills/gen_contracts.py` 从 **alembic 迁移**导出，请勿手工编辑本区。
> 迁移是数据库的权威定义，本区是它的生成视图。

当前 head：`0001`

### 表总览

| 表 | 来源迁移 | 隔离级别 | 应用角色权限 | worker 角色权限 | 列数 |
|---|---|---|---|---|---:|
| `acquisition_jobs` | 0001 → 0001 改写 | 租户+项目 | SELECT, INSERT | SELECT, UPDATE | 20 |
| `action_intents` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE, DELETE | — | 11 |
| `auth_attempt_counters` | 0001 | 系统级（无租户，认证前设施） | 无表权限（仅 SECURITY DEFINER 函数 EXECUTE） | — | 4 |
| `auth_audit_outbox` | 0001 → 0001 改写 | 系统级（认证前审计事实中转；应用 INSERT/SELECT/UPDATE，无 DELETE） | SELECT, INSERT, UPDATE | — | 9 |
| `confirmations` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE, DELETE | — | 12 |
| `conversations` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE, DELETE | — | 6 |
| `diagnoses` | 0001 | 租户+项目+主体 | SELECT, INSERT | — | 7 |
| `evidence_events` | 0001 | 租户+项目 | SELECT, INSERT | — | 12 |
| `http_idempotency` | 0001 | 租户+主体 | SELECT, INSERT, UPDATE | — | 13 |
| `ingestion_jobs` | 0001 → 0001 改写 | 租户+项目 | SELECT, INSERT | SELECT, UPDATE | 14 |
| `learning_plans` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE, DELETE | — | 7 |
| `learning_tasks` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE, DELETE | — | 16 |
| `library_documents` | 0001 | 租户+主体 | SELECT, INSERT | — | 9 |
| `library_sources` | 0001 | 租户+主体 | SELECT, INSERT | — | 8 |
| `messages` | 0001 | 租户+项目 | SELECT, INSERT | INSERT | 8 |
| `milestones` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE, DELETE | — | 7 |
| `principals` | 0001 | 租户 | SELECT, INSERT, UPDATE, DELETE | — | 4 |
| `project_grants` | 0001 | 租户 | SELECT, INSERT, UPDATE, DELETE | — | 4 |
| `projects` | 0001 → 0001 改写 | 成员感知 | SELECT, INSERT, UPDATE, DELETE | — | 7 |
| `provider_attempts` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE | SELECT, INSERT, UPDATE | 13 |
| `source_candidates` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE | SELECT, INSERT, UPDATE | 10 |
| `source_chunks` | 0001 | 租户+项目 | SELECT, INSERT | SELECT, INSERT | 15 |
| `source_documents` | 0001 | 租户+项目 | SELECT, INSERT | SELECT | 18 |
| `source_fetch_artifacts` | 0001 | 租户 | SELECT, INSERT, UPDATE, DELETE | — | 8 |
| `sources` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE, DELETE | — | 8 |
| `task_assessments` | 0001 | 租户+项目 | SELECT, INSERT | — | 8 |
| `task_submissions` | 0001 | 租户+项目+主体 | SELECT, INSERT | — | 8 |
| `teaching_budgets` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE | SELECT, UPDATE | 11 |
| `teaching_events` | 0001 | 租户+项目 | SELECT, INSERT | SELECT, INSERT | 7 |
| `teaching_reservations` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE | SELECT, INSERT, UPDATE | 12 |
| `teaching_runs` | 0001 | 租户+项目 | SELECT, INSERT, UPDATE | SELECT, UPDATE | 24 |
| `teaching_tenant_budgets` | 0001 | 租户 | SELECT, INSERT, UPDATE | SELECT, UPDATE | 8 |
| `tenants` | 0001 | 租户 | SELECT, INSERT, UPDATE, DELETE | — | 3 |
| `user_sessions` | 0001 | 租户+主体 | SELECT, INSERT, UPDATE, DELETE | — | 6 |

### 列明细

| 表 | 列 | 类型 |
|---|---|---|
| `acquisition_jobs` | `acquisition_id` | `text` |
| `acquisition_jobs` | `tenant_id` | `text` |
| `acquisition_jobs` | `project_id` | `text` |
| `acquisition_jobs` | `source_id` | `text` |
| `acquisition_jobs` | `candidate_id` | `text` |
| `acquisition_jobs` | `requested_by` | `text` |
| `acquisition_jobs` | `url` | `text` |
| `acquisition_jobs` | `title` | `text` |
| `acquisition_jobs` | `media_type` | `text` |
| `acquisition_jobs` | `language` | `text` |
| `acquisition_jobs` | `idempotency_key` | `text` |
| `acquisition_jobs` | `status` | `text` |
| `acquisition_jobs` | `attempt_count` | `integer` |
| `acquisition_jobs` | `lease_owner` | `text` |
| `acquisition_jobs` | `lease_until` | `timestamptz` |
| `acquisition_jobs` | `claim_token` | `uuid` |
| `acquisition_jobs` | `error_code` | `text` |
| `acquisition_jobs` | `error_detail` | `text` |
| `acquisition_jobs` | `created_at` | `timestamptz` |
| `acquisition_jobs` | `updated_at` | `timestamptz` |
| `action_intents` | `logical_action_id` | `text` |
| `action_intents` | `tenant_id` | `text` |
| `action_intents` | `project_id` | `text` |
| `action_intents` | `run_id` | `text` |
| `action_intents` | `node_instance_id` | `text` |
| `action_intents` | `tool_id` | `text` |
| `action_intents` | `idempotency_key` | `text` |
| `action_intents` | `state` | `text` |
| `action_intents` | `attempts` | `jsonb` |
| `action_intents` | `created_at` | `timestamptz` |
| `action_intents` | `updated_at` | `timestamptz` |
| `auth_attempt_counters` | `bucket` | `text` |
| `auth_attempt_counters` | `window_start` | `timestamptz` |
| `auth_attempt_counters` | `attempts` | `integer` |
| `auth_attempt_counters` | `last_at` | `timestamptz` |
| `auth_audit_outbox` | `event_id` | `text` |
| `auth_audit_outbox` | `event_type` | `text` |
| `auth_audit_outbox` | `payload` | `jsonb` |
| `auth_audit_outbox` | `risk` | `text` |
| `auth_audit_outbox` | `tenant_id` | `text` |
| `auth_audit_outbox` | `project_id` | `text` |
| `auth_audit_outbox` | `request_id` | `text` |
| `auth_audit_outbox` | `created_at` | `timestamptz` |
| `auth_audit_outbox` | `projected_at` | `timestamptz` |
| `confirmations` | `confirmation_id` | `text` |
| `confirmations` | `tenant_id` | `text` |
| `confirmations` | `project_id` | `text` |
| `confirmations` | `principal_id` | `text` |
| `confirmations` | `tool_id` | `text` |
| `confirmations` | `params_hash` | `text` |
| `confirmations` | `ceiling_dimension` | `text` |
| `confirmations` | `ceiling_amount` | `bigint` |
| `confirmations` | `ceiling_note` | `text` |
| `confirmations` | `issued_at` | `timestamptz` |
| `confirmations` | `expires_at` | `timestamptz` |
| `confirmations` | `consumed_at` | `timestamptz` |
| `conversations` | `conversation_id` | `text` |
| `conversations` | `tenant_id` | `text` |
| `conversations` | `project_id` | `text` |
| `conversations` | `title` | `text` |
| `conversations` | `last_message_seq` | `bigint` |
| `conversations` | `created_at` | `timestamptz` |
| `diagnoses` | `diagnosis_id` | `text` |
| `diagnoses` | `tenant_id` | `text` |
| `diagnoses` | `project_id` | `text` |
| `diagnoses` | `principal_id` | `text` |
| `diagnoses` | `answers` | `jsonb` |
| `diagnoses` | `summary` | `text` |
| `diagnoses` | `created_at` | `timestamptz` |
| `evidence_events` | `seq` | `bigint` |
| `evidence_events` | `event_id` | `text` |
| `evidence_events` | `tenant_id` | `text` |
| `evidence_events` | `project_id` | `text` |
| `evidence_events` | `kind` | `text` |
| `evidence_events` | `task_id` | `text` |
| `evidence_events` | `contract_id` | `text` |
| `evidence_events` | `mapping_version` | `text` |
| `evidence_events` | `graph_version` | `text` |
| `evidence_events` | `occurred_at` | `timestamptz` |
| `evidence_events` | `recorded_at` | `timestamptz` |
| `evidence_events` | `payload` | `jsonb` |
| `http_idempotency` | `claim_id` | `text` |
| `http_idempotency` | `tenant_id` | `text` |
| `http_idempotency` | `principal_id` | `text` |
| `http_idempotency` | `command_scope` | `text` |
| `http_idempotency` | `client_key` | `text` |
| `http_idempotency` | `request_hash` | `text` |
| `http_idempotency` | `project_id` | `text` |
| `http_idempotency` | `state` | `text` |
| `http_idempotency` | `status_code` | `integer` |
| `http_idempotency` | `response_body` | `jsonb` |
| `http_idempotency` | `claimed_at` | `timestamptz` |
| `http_idempotency` | `completed_at` | `timestamptz` |
| `http_idempotency` | `owner_token` | `text` |
| `ingestion_jobs` | `job_id` | `text` |
| `ingestion_jobs` | `tenant_id` | `text` |
| `ingestion_jobs` | `project_id` | `text` |
| `ingestion_jobs` | `source_id` | `text` |
| `ingestion_jobs` | `document_id` | `text` |
| `ingestion_jobs` | `status` | `text` |
| `ingestion_jobs` | `attempt_count` | `integer` |
| `ingestion_jobs` | `lease_owner` | `text` |
| `ingestion_jobs` | `lease_until` | `timestamptz` |
| `ingestion_jobs` | `error_code` | `text` |
| `ingestion_jobs` | `error_detail` | `text` |
| `ingestion_jobs` | `created_at` | `timestamptz` |
| `ingestion_jobs` | `updated_at` | `timestamptz` |
| `ingestion_jobs` | `claim_token` | `uuid` |
| `learning_plans` | `plan_id` | `text` |
| `learning_plans` | `tenant_id` | `text` |
| `learning_plans` | `project_id` | `text` |
| `learning_plans` | `version` | `integer` |
| `learning_plans` | `goal` | `text` |
| `learning_plans` | `status` | `text` |
| `learning_plans` | `created_at` | `timestamptz` |
| `learning_tasks` | `task_id` | `text` |
| `learning_tasks` | `tenant_id` | `text` |
| `learning_tasks` | `project_id` | `text` |
| `learning_tasks` | `milestone_id` | `text` |
| `learning_tasks` | `order_index` | `integer` |
| `learning_tasks` | `title` | `text` |
| `learning_tasks` | `status` | `text` |
| `learning_tasks` | `objective` | `text` |
| `learning_tasks` | `instruction` | `text` |
| `learning_tasks` | `task_type` | `text` |
| `learning_tasks` | `estimated_minutes` | `integer` |
| `learning_tasks` | `deliverable` | `text` |
| `learning_tasks` | `acceptance_criteria` | `jsonb` |
| `learning_tasks` | `evidence_required` | `jsonb` |
| `learning_tasks` | `prerequisites` | `jsonb` |
| `learning_tasks` | `related_skill_id` | `text` |
| `library_documents` | `library_document_id` | `text` |
| `library_documents` | `tenant_id` | `text` |
| `library_documents` | `principal_id` | `text` |
| `library_documents` | `library_source_id` | `text` |
| `library_documents` | `version` | `integer` |
| `library_documents` | `content` | `text` |
| `library_documents` | `content_hash` | `text` |
| `library_documents` | `parser_version` | `text` |
| `library_documents` | `observed_at` | `timestamptz` |
| `library_sources` | `library_source_id` | `text` |
| `library_sources` | `tenant_id` | `text` |
| `library_sources` | `principal_id` | `text` |
| `library_sources` | `display_name` | `text` |
| `library_sources` | `media_type` | `text` |
| `library_sources` | `identity_hash` | `text` |
| `library_sources` | `acquisition` | `jsonb` |
| `library_sources` | `registered_at` | `timestamptz` |
| `messages` | `message_id` | `text` |
| `messages` | `tenant_id` | `text` |
| `messages` | `project_id` | `text` |
| `messages` | `conversation_id` | `text` |
| `messages` | `seq` | `bigint` |
| `messages` | `role` | `text` |
| `messages` | `content` | `text` |
| `messages` | `created_at` | `timestamptz` |
| `milestones` | `milestone_id` | `text` |
| `milestones` | `tenant_id` | `text` |
| `milestones` | `project_id` | `text` |
| `milestones` | `plan_id` | `text` |
| `milestones` | `order_index` | `integer` |
| `milestones` | `title` | `text` |
| `milestones` | `description` | `text` |
| `principals` | `principal_id` | `text` |
| `principals` | `tenant_id` | `text` |
| `principals` | `display_name` | `text` |
| `principals` | `created_at` | `timestamptz` |
| `project_grants` | `tenant_id` | `text` |
| `project_grants` | `principal_id` | `text` |
| `project_grants` | `project_id` | `text` |
| `project_grants` | `granted_at` | `timestamptz` |
| `projects` | `project_id` | `text` |
| `projects` | `tenant_id` | `text` |
| `projects` | `name` | `text` |
| `projects` | `created_at` | `timestamptz` |
| `projects` | `goal` | `text` |
| `projects` | `version` | `integer` |
| `projects` | `updated_at` | `timestamptz` |
| `provider_attempts` | `attempt_id` | `text` |
| `provider_attempts` | `tenant_id` | `text` |
| `provider_attempts` | `project_id` | `text` |
| `provider_attempts` | `run_id` | `text` |
| `provider_attempts` | `status` | `text` |
| `provider_attempts` | `provider_request_id` | `text` |
| `provider_attempts` | `result_payload` | `jsonb` |
| `provider_attempts` | `input_tokens` | `integer` |
| `provider_attempts` | `output_tokens` | `integer` |
| `provider_attempts` | `cost_micro` | `bigint` |
| `provider_attempts` | `created_at` | `timestamptz` |
| `provider_attempts` | `updated_at` | `timestamptz` |
| `provider_attempts` | `request_payload` | `jsonb` |
| `source_candidates` | `candidate_id` | `text` |
| `source_candidates` | `tenant_id` | `text` |
| `source_candidates` | `project_id` | `text` |
| `source_candidates` | `url` | `text` |
| `source_candidates` | `title` | `text` |
| `source_candidates` | `snippet` | `text` |
| `source_candidates` | `source_domain` | `text` |
| `source_candidates` | `status` | `text` |
| `source_candidates` | `discovered_at` | `timestamptz` |
| `source_candidates` | `expires_at` | `timestamptz` |
| `source_chunks` | `chunk_id` | `text` |
| `source_chunks` | `tenant_id` | `text` |
| `source_chunks` | `project_id` | `text` |
| `source_chunks` | `source_id` | `text` |
| `source_chunks` | `document_id` | `text` |
| `source_chunks` | `chunk_index` | `integer` |
| `source_chunks` | `heading_path` | `jsonb` |
| `source_chunks` | `heading_level` | `integer` |
| `source_chunks` | `span_start` | `integer` |
| `source_chunks` | `span_end` | `integer` |
| `source_chunks` | `content` | `text` |
| `source_chunks` | `content_hash` | `text` |
| `source_chunks` | `parser_version` | `text` |
| `source_chunks` | `display_policy` | `text` |
| `source_chunks` | `created_at` | `timestamptz` |
| `source_documents` | `document_id` | `text` |
| `source_documents` | `tenant_id` | `text` |
| `source_documents` | `project_id` | `text` |
| `source_documents` | `source_id` | `text` |
| `source_documents` | `version` | `integer` |
| `source_documents` | `document_title` | `text` |
| `source_documents` | `content` | `text` |
| `source_documents` | `content_hash` | `text` |
| `source_documents` | `media_type` | `text` |
| `source_documents` | `language` | `text` |
| `source_documents` | `parser_version` | `text` |
| `source_documents` | `acquisition_method` | `text` |
| `source_documents` | `taint_sources` | `jsonb` |
| `source_documents` | `derived_from` | `jsonb` |
| `source_documents` | `observed_at` | `timestamptz` |
| `source_documents` | `fetch_attempt_id` | `text` |
| `source_documents` | `source_content_type` | `text` |
| `source_documents` | `raw_content_hash` | `text` |
| `source_fetch_artifacts` | `acquisition_id` | `text` |
| `source_fetch_artifacts` | `tenant_id` | `text` |
| `source_fetch_artifacts` | `project_id` | `text` |
| `source_fetch_artifacts` | `content_type` | `text` |
| `source_fetch_artifacts` | `raw_content` | `bytea` |
| `source_fetch_artifacts` | `content_hash` | `text` |
| `source_fetch_artifacts` | `parser_version` | `text` |
| `source_fetch_artifacts` | `fetched_at` | `timestamptz` |
| `sources` | `source_id` | `text` |
| `sources` | `tenant_id` | `text` |
| `sources` | `project_id` | `text` |
| `sources` | `display_name` | `text` |
| `sources` | `media_type` | `text` |
| `sources` | `identity_hash` | `text` |
| `sources` | `acquisition` | `jsonb` |
| `sources` | `registered_at` | `timestamptz` |
| `task_assessments` | `assessment_id` | `text` |
| `task_assessments` | `tenant_id` | `text` |
| `task_assessments` | `project_id` | `text` |
| `task_assessments` | `task_id` | `text` |
| `task_assessments` | `component_id` | `text` |
| `task_assessments` | `contract_id` | `text` |
| `task_assessments` | `mapping_version` | `text` |
| `task_assessments` | `created_at` | `timestamptz` |
| `task_submissions` | `submission_id` | `text` |
| `task_submissions` | `tenant_id` | `text` |
| `task_submissions` | `project_id` | `text` |
| `task_submissions` | `task_id` | `text` |
| `task_submissions` | `principal_id` | `text` |
| `task_submissions` | `mode` | `text` |
| `task_submissions` | `content` | `text` |
| `task_submissions` | `created_at` | `timestamptz` |
| `teaching_budgets` | `tenant_id` | `text` |
| `teaching_budgets` | `project_id` | `text` |
| `teaching_budgets` | `total_micro` | `bigint` |
| `teaching_budgets` | `max_input_tokens` | `integer` |
| `teaching_budgets` | `max_output_tokens` | `integer` |
| `teaching_budgets` | `reserved_micro` | `bigint` |
| `teaching_budgets` | `in_flight_micro` | `bigint` |
| `teaching_budgets` | `spent_micro` | `bigint` |
| `teaching_budgets` | `price_version` | `text` |
| `teaching_budgets` | `created_at` | `timestamptz` |
| `teaching_budgets` | `updated_at` | `timestamptz` |
| `teaching_events` | `run_id` | `text` |
| `teaching_events` | `seq` | `integer` |
| `teaching_events` | `tenant_id` | `text` |
| `teaching_events` | `project_id` | `text` |
| `teaching_events` | `event_type` | `text` |
| `teaching_events` | `payload` | `jsonb` |
| `teaching_events` | `created_at` | `timestamptz` |
| `teaching_reservations` | `reservation_id` | `text` |
| `teaching_reservations` | `tenant_id` | `text` |
| `teaching_reservations` | `project_id` | `text` |
| `teaching_reservations` | `run_id` | `text` |
| `teaching_reservations` | `state` | `text` |
| `teaching_reservations` | `estimated_micro` | `bigint` |
| `teaching_reservations` | `estimated_input_tokens` | `integer` |
| `teaching_reservations` | `estimated_output_tokens` | `integer` |
| `teaching_reservations` | `actual_micro` | `bigint` |
| `teaching_reservations` | `price_version` | `text` |
| `teaching_reservations` | `created_at` | `timestamptz` |
| `teaching_reservations` | `updated_at` | `timestamptz` |
| `teaching_runs` | `run_id` | `text` |
| `teaching_runs` | `tenant_id` | `text` |
| `teaching_runs` | `project_id` | `text` |
| `teaching_runs` | `conversation_id` | `text` |
| `teaching_runs` | `user_message_id` | `text` |
| `teaching_runs` | `principal_id` | `text` |
| `teaching_runs` | `answer_message_id` | `text` |
| `teaching_runs` | `answer_seq` | `bigint` |
| `teaching_runs` | `question` | `text` |
| `teaching_runs` | `status` | `text` |
| `teaching_runs` | `attempt_count` | `integer` |
| `teaching_runs` | `claim_token` | `uuid` |
| `teaching_runs` | `lease_owner` | `text` |
| `teaching_runs` | `lease_until` | `timestamptz` |
| `teaching_runs` | `model_id` | `text` |
| `teaching_runs` | `prompt_version` | `text` |
| `teaching_runs` | `ranking_version` | `text` |
| `teaching_runs` | `grounding` | `text` |
| `teaching_runs` | `error_code` | `text` |
| `teaching_runs` | `error_detail` | `text` |
| `teaching_runs` | `created_at` | `timestamptz` |
| `teaching_runs` | `updated_at` | `timestamptz` |
| `teaching_runs` | `routing_decision` | `jsonb` |
| `teaching_runs` | `retrieval_decision` | `jsonb` |
| `teaching_tenant_budgets` | `tenant_id` | `text` |
| `teaching_tenant_budgets` | `total_micro` | `bigint` |
| `teaching_tenant_budgets` | `reserved_micro` | `bigint` |
| `teaching_tenant_budgets` | `in_flight_micro` | `bigint` |
| `teaching_tenant_budgets` | `spent_micro` | `bigint` |
| `teaching_tenant_budgets` | `price_version` | `text` |
| `teaching_tenant_budgets` | `created_at` | `timestamptz` |
| `teaching_tenant_budgets` | `updated_at` | `timestamptz` |
| `tenants` | `tenant_id` | `text` |
| `tenants` | `name` | `text` |
| `tenants` | `created_at` | `timestamptz` |
| `user_sessions` | `session_id` | `text` |
| `user_sessions` | `tenant_id` | `text` |
| `user_sessions` | `principal_id` | `text` |
| `user_sessions` | `issued_at` | `timestamptz` |
| `user_sessions` | `expires_at` | `timestamptz` |
| `user_sessions` | `revoked_at` | `timestamptz` |

### 本区不覆盖的内容

策略谓词、`GRANT` 语句、索引与 `CHECK` 约束的**文本**不在本表里 ——
它们由迁移文件承载，改迁移即可，不需要维护两份。
本区回答四个问题：有哪些表、每张表怎么隔离、**应用角色**能做什么、
**worker 角色**能做什么（两者是两条凭据边界，见 0008 迁移）。
<!-- END GENERATED -->

## 2. 手写区 · 不可协商的数据库约束

以下内容**必须在数据库层强制**，不得只靠应用代码自觉：

| 约束 | 具体要求 | 违反后果 |
|---|---|---|
| 应用角色 | 使用非表所有者、无 `BYPASSRLS` 的独立数据库角色；迁移使用另一个角色 | 应用可绕过 RLS |
| 租户上下文 | 只通过事务内 `SET LOCAL` 设置；连接池归还前结束事务；后台任务同样建立租户事务上下文 | 上下文泄漏到下一个请求 |
| 缺上下文必失败 | 查询缺少租户/项目上下文必须报错，**不得回落为无过滤查询** | 越权读取（不变量 #2） |
| 项目级实体 | 一律包含 `tenant_id` 与 `learning_project_id` | 无法隔离 |
| 约束列 | 外键、唯一约束、索引包含适当的租户/项目列，不能只依赖应用过滤 | ID 猜测即越权 |
| 证据不可变 | `EvidenceEvent`、`EvidenceCorrection` 仅允许 INSERT；应用角色无 UPDATE/DELETE 权限 | 事实可被篡改（不变量 #14 的基础） |
| 掌握只读 | `MasteryProjection` 只有 Projector Worker 有写权限 | API 直写掌握状态 |
| 事件顺序 | 排序以服务端 `event_seq` 为准；客户端时间不得决定事件顺序 | 投影顺序错乱 |
| 队列内容 | 队列表只存 task id，任务内容与状态在业务表 | 状态分裂 |
| 认领方式 | worker 使用 `FOR UPDATE SKIP LOCKED` 原子认领 | 重复执行 |

## 3. 手写区 · 主要实体归属

| 实体 | 归属 | 备注 |
|---|---|---|
| User、TenantMembership | 租户级 | — |
| LearningProject、Session、Message | 项目级 | 会话只是交互载体，长期状态归项目 |
| Source | **租户级** + ACL | 项目通过 `ProjectSourceGrant` 借用；删项目不删源 |
| ProjectKnowledgeIndex | 项目级 | 由 Source 派生 |
| PlatformContentRelease | 租户级（平台发布） | 不可变；项目显式 pin release |
| CompetencyComponent、Plan | 项目级 | 图谱带 `graph_version` |
| EvidenceEvent、EvidenceCorrection、MasteryProjection、MasteryPresentedEvent | 项目级 | 前三者写入权受限 |
| WorkflowRun、NodeRun、ActionIntent、ToolOutcome | 项目级 | 执行链路 |
| BudgetAccount、BudgetReservation | Tenant → User/Project → Run → Node | 树形 ownership |
| CapabilityGrant、PolicyDecision | 项目级 | 授权与决策记录 |
| AuditIndex | 项目级 | 只存索引与引用，正文在独立 sink |
| LearningProject 之外的跨项目能力引用 | **用户级** | 不得使项目查询自动扩大范围 |

## 4. 生成脚本约定（已实现）

| 项 | 约定 |
|---|---|
| 脚本 | `tools/skills/gen_contracts.py --target sql-schema` |
| 当前输入 | `backend/app/**` 的 dataclass 定义（AST 扫描，不 import，无副作用） |
| **目标输入** | `alembic/versions/**` —— 迁移落地后**必须替换**，届时导出真正的 DDL |
| 输出 | 本文件第 1 节生成区（实体、模块、字段数、是否含租户/项目列） |
| 排序 | 按模块名、类名排序，保证导出稳定可比对 |
| `source_hash` | 输入文件按路径排序后内容的 SHA-256 |
| CI | `--check` 重新渲染并与文件比对（不只看哈希，避免手改内容绕过） |
| 禁止 | 生成区出现人工编辑内容 |

## 5. 手写区 · 常见坑

- **外键只写 `id` 而不带租户列**：跨租户引用在数据库层就拦不住，必须复合外键。
- **迁移使用应用角色**：迁移角色必须独立，否则 RLS 会在迁移时被绕过，问题在测试环境看不出来。
- **把 `SET LOCAL` 写在连接级**：必须是事务级，连接复用时否则会串租户。
- **给 `EvidenceEvent` 加"便于修正"的 UPDATE 路径**：修正只能追加 `EvidenceCorrection`，永不改写事实。
- **在 vector 检索里做应用层过滤**：过滤必须在数据库内执行，禁止把跨租户候选拉到应用层再筛。
