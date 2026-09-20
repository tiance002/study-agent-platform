"""资料摄取的**端口**（ports）。

与 `product/ports.py` / `identity/ports.py` 同一哲学：`Protocol` 结构化类型，
内存实现与 PostgreSQL 实现跑同一套契约测试，靠「结构相同」互换。

## 为什么 `claim_next` 是唯一没有 `Principal` 的方法

其余方法都接收 `Principal`：端口层就是「身份从哪来」的边界，一旦允许调用方
传裸的 `tenant_id`，租户隔离就退化成"调用方记得传对"。

`claim_next` 是刻意的例外，理由与 `InvitationRepository.exchange` 同类：
**worker 必须先发现"哪个租户有活干"，才能建立任何租户上下文**。
要求它带身份，等于要求它在知道自己要处理谁之前就声称自己是谁 ——
那只能靠伪造一个身份来满足签名。

因此这条路径有三重约束，缺一不可：

1. **不可从 HTTP 到达**：没有任何端点调用它（`test_ingestion_api.py` 守着）；
2. **只读队列元数据**：认领后的原文读取走 `load_document(job)` ——
   它用任务自带的租户/项目建立上下文，与普通读取同一套隔离；
3. **认领返回的是完整任务契约**，状态推进由 `complete` / `fail` 用
   任务自带的租户/项目上下文完成，worker 无法借认领结果触碰别的项目。

## 为什么没有 `update` / `delete`

原文与片段在协议层面就没有修改与删除方法。库层再叠一层：
`study_app` 对这两张表只有 `SELECT, INSERT`（0007 迁移的 GRANT）。
「不可变」如果只写在注释里，第一个需要"临时改一下"的人就会改掉它。
"""

from __future__ import annotations

from typing import Protocol

from app.identity.models import Principal
from app.knowledge.models import IngestionJob, SourceDocument, StoredChunk


class IngestionRepository(Protocol):
    """原文、摄取任务与片段的读写端口。"""

    def enqueue(
        self,
        actor: Principal,
        project_id: str,
        source_id: str,
        *,
        document_id: str,
        job_id: str,
        title: str,
        content: str,
        media_type: str,
        language: str,
    ) -> tuple[SourceDocument, IngestionJob]:
        """登记一版新原文并入队一个摄取任务 —— **一次原子写入**。

        拆成"写原文"和"入队"两次调用会留下半完成状态：原文在库里但没人处理它，
        而它**看起来完全合法**（有内容、有指纹、有来源）。
        用户看到的是"上传成功但永远搜不到"。

        版本号由实现分配（同一 `source_id` 的最大版本 + 1）。并发上传同一份资料时
        `UNIQUE (source_id, version)` 兜底，冲突翻译成可重试的 `VERSION_CONFLICT`。
        """
        ...

    def get_job(self, actor: Principal, project_id: str, job_id: str) -> IngestionJob:
        """按 id 取任务状态。不可见（未授予/跨租户）与不存在**同码同话术**。"""
        ...

    def stored_chunks(
        self, actor: Principal, project_id: str, *, latest_only: bool = True
    ) -> tuple[StoredChunk, ...]:
        """该项目下的片段（按 `document_id`、`chunk_index` 排序）。

        `latest_only=True`（**默认**）：每个来源只取**最新的成功版本**。
        同一份资料可以上传多版，历史版本的片段必须留在库里（老引用要能
        回读原版），但检索不该同时看到两版 —— 否则一次查询返回两份近似结果，
        而"哪份是当前的"客户端无从判断（R4-03 的修改要求）。

        `latest_only=False`：全部版本，供历史检视。**没有任何默认调用方** ——
        它是一个显式选择，不是"以防万一多拿一点"。

        **作用域过滤在实现内部完成**，调用方不需要、也不应该自己判断 ——
        把隔离留给调用方，等于把安全交给"记得写"。

        这是检索适配器的读取入口：分数计算可以在应用层做，
        但**候选集必须先由数据库按 tenant/project 收窄**
        （02 号规格 §7 明确禁止把跨项目候选拉回应用层再筛）。
        """
        ...

    def chunk_at(
        self,
        actor: Principal,
        project_id: str,
        *,
        document_id: str,
        span: tuple[int, int],
    ) -> StoredChunk | None:
        """按**不可变标识**精确回读一个片段。

        为什么不拿 `source_id + span` 去 `stored_chunks` 里挑第一条：
        同一来源的两版片段**可能落在同一个跨度上**（改写过的段落常常长度相近），
        此时"先到先得"取决于存储顺序 —— 实测命中 `doc_v2/'delta!'`，
        回读拿到 `doc_v1/'bravo!'`，而引用看起来完全正常。

        返回 `None` 只表示**粒度落空**（该文档里没有这个跨度）。
        授权拒绝仍由成员关系那一层抛码（与 `get_job` 同源），
        不在本方法里翻译成一个空值。
        """
        ...

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> IngestionJob | None:
        """认领下一个可处理的任务（系统级操作，见模块 docstring）。

        可认领 = `queued`，或 `processing` 且租约已过期（崩溃残留）。
        返回 `None` 表示当前没有可做的活 —— 这是正常结果，不是错误。

        实现必须用 `FOR UPDATE SKIP LOCKED` 单条 SQL 完成"选 + 占"：
        「先查后占」是 check-then-act，两个 worker 会认领同一个任务。

        返回的 `claim_token` 是这一代的**围栏**：每次认领换一个新的、
        不可复用的值，`complete` / `fail` 必须带上它才能落定。
        """
        ...

    def load_document(self, job: IngestionJob) -> SourceDocument:
        """读取任务对应的**不可变原文**。

        用任务自带的租户/项目建立上下文，因此 worker 拿不到认领范围之外的原文。
        """
        ...

    def complete(self, job: IngestionJob, chunks: tuple[StoredChunk, ...]) -> None:
        """把片段集合与任务终态**在同一事务**写入。

        ⚠️ **必须持有仍然有效的认领**：`job.claim_token` 要等于库里的那一代，
        状态要是 `processing`，租约也还没过期。三者是**一条条件更新**里的
        判定，不是"先查再写"。

        为什么：租约只规定"谁能认领"，不规定"谁能落定"。A 超时、B 接管之后，
        A 迟到的 `complete` 会抢先落结果，B 写的东西没人知道是谁写的
        （R4-02 实测复现）。`lease_owner` 不能当代次 —— 它是运维给的
        `--worker-id`，同一个进程重启后是同一个名字。

        幂等：任务已经是 `succeeded` 时直接返回既有结果，不重复插入片段 ——
        重复投递（worker 完成写入后进程被杀、消息重新投递）必须是幂等成功。
        **但这与"认领失效"是两件相反的事**：后者必须抛
        `ILLEGAL_STATE_TRANSITION`，绝不能改写新持有者的任务。
        """
        ...

    def fail(self, job: IngestionJob, *, error_code: str, safe_detail: str) -> None:
        """把任务标为 `failed` 并记下稳定错误码与**安全**描述。

        与 `complete` 同一套认领围栏。这条尤其重要：`fail` 写的是**终态**，
        旧持有者迟到的一次上报会把"可恢复"变成"不可恢复" —— 比不处理更糟。

        `safe_detail` 会被状态接口原样返回给用户，因此不得包含原始异常文本、
        文件路径或 SQL 片段。已经是终态的任务重复调用是幂等成功。
        """
        ...
