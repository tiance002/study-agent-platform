"""独立审计 sink。

设计依据：03 号规格 §9、不变量 #6 与 00 号规格 §4 失败矩阵。

四条性质：
1. 审计正文写入**独立于主库**的存储，主库只保存引用；
2. 记录以哈希链串联，任何一条被改动都会使链校验失败；
3. **不提供删除接口** —— 应用层根本没有删除能力（不是"约定不删"）；
4. sink 不可用时：高影响动作 fail-closed；低风险事件只进有界缓冲，超限即拒绝。

外加两条在后续审查中补上的：

5. 记录带 `request_id`，与响应头 `X-Request-Id` / 响应体 `request_id` **同值**。
   此前审计只带 `run_id`（由 request_id 与 node_id 哈希而来，**不可逆**），
   也就是"错误响应"和"审计事件"之间没有可用的关联键 ——
   而全链路追踪正是追踪 id 存在的唯一理由。
6. 记录带 `schema_version` 并纳入哈希。原因见 `AUDIT_SCHEMA_VERSION` 的说明：
   哈希公式一变更，旧记录会整体校验失败，而布尔校验分不清"旧格式"和"被篡改"。

本版用本地 JSONL 文件实现；生产应替换为对象锁 / WORM 存储，接口不变。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash, hash_chain
from app.core.ids import new_id
from app.core.request_context import current_request_id

DEFAULT_BUFFER_CAPACITY = 256

# 审计记录格式版本。
#
# 存在的理由是一次真实的教训：`entry_hash` 覆盖记录结构本身，所以**公式一变，
# 此前写入的记录会全部校验失败**，而 `verify_chain()` 只返回布尔值 ——
# 分不清「这是旧格式记录」和「记录被篡改」。生产里一次代码升级就能让整条链报
# 「无效」，真正的篡改会淹没在噪音里。
#
# v2 起记录自带版本号并纳入哈希，校验据此**区分**两种情况并指出第一处坏点。
AUDIT_SCHEMA_VERSION = 2


class RiskLevel(StrEnum):
    """审计事件的处置分级。决定 sink 不可用时的失败方向。"""

    LOW = "low"
    HIGH = "high"


class ChainProblem(StrEnum):
    """链校验失败的**原因**。布尔值不够用，见 `AUDIT_SCHEMA_VERSION` 的说明。"""

    LEGACY_FORMAT = "legacy_format"      # 旧格式记录：不可用当前公式校验，且**不等于被篡改**
    BROKEN_LINK = "broken_link"          # `previous_hash` 与上一条对不上
    HASH_MISMATCH = "hash_mismatch"      # 内容与自身 `entry_hash` 不符（被改过）


# 给人看的原因说明。运维看到 `False` 时第一个要问的就是"是格式问题还是被改了"。
_CHAIN_PROBLEM_LABELS: dict[ChainProblem, str] = {
    ChainProblem.LEGACY_FORMAT: "旧格式记录（不是篡改，需按旧公式或迁移后重验）",
    ChainProblem.BROKEN_LINK: "前序哈希断链（记录被删/插/换过）",
    ChainProblem.HASH_MISMATCH: "内容与哈希不符（疑似被改动）",
}


@dataclass(frozen=True)
class ChainVerification:
    """链校验结果。失败时给出**位置与原因**，而不是一个孤零零的 `False`。"""

    ok: bool
    checked: int
    bad_index: int | None = None
    problem: ChainProblem | None = None
    legacy_count: int = 0

    def describe(self) -> str:
        if self.ok:
            return f"链完整（{self.checked} 条）"
        if self.problem is None:
            return f"第 {self.bad_index} 条失败：原因未知"
        return f"第 {self.bad_index} 条失败：{_CHAIN_PROBLEM_LABELS[self.problem]}"


@dataclass(frozen=True)
class AuditRecord:
    """审计记录。`entry_hash` 覆盖前序哈希与当前载荷，构成链。

    `tenant_id` / `project_id` 是**结构化顶层字段**，不是塞在 `payload` 里的：
    审计读取必须能可靠地按租户与项目隔离，而 `payload` 的结构由调用方决定，
    靠解析它来过滤迟早会漏。它们同时参与哈希，因此不能事后补写。
    """

    event_id: str
    seq: int
    event_type: str
    payload: dict
    risk: RiskLevel
    previous_hash: str | None
    entry_hash: str
    tenant_id: str | None = None
    project_id: str | None = None
    # 追踪 id。与响应头 `X-Request-Id`、响应体 `request_id`、错误体 `request_id`
    # **必须同值** —— 否则「按 id 检索服务端日志」这句话在审计上是空的。
    # 早先审计事件只带 `run_id`（由 request_id 与 node_id 哈希而来，不可逆），
    # 也就是说错误响应与审计事件之间**没有可用的关联键**。
    request_id: str | None = None
    schema_version: int = AUDIT_SCHEMA_VERSION

    def to_line(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "seq": self.seq,
                "event_type": self.event_type,
                "payload": self.payload,
                "risk": str(self.risk),
                "previous_hash": self.previous_hash,
                "entry_hash": self.entry_hash,
                "tenant_id": self.tenant_id,
                "project_id": self.project_id,
                "request_id": self.request_id,
                "schema_version": self.schema_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def hash_material(self) -> dict:
        """参与链哈希的字段。任何一项被改动都会使链校验失败。"""
        return {
            "seq": self.seq,
            "type": self.event_type,
            "payload": self.payload,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "request_id": self.request_id,
            "schema_version": self.schema_version,
        }


class AuditSink:
    """追加式审计 sink。故意不实现删除与更新。"""

    def __init__(
        self,
        directory: Path | str,
        *,
        filename: str = "audit.jsonl",
        available: bool = True,
        buffer_capacity: int = DEFAULT_BUFFER_CAPACITY,
    ) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / filename
        self._available = available
        self._buffer_capacity = buffer_capacity
        # 有界缓冲：只允许低风险事件堆积，且必须有上限。
        self._buffer: list[AuditRecord] = []
        # ⚠️ 「分配序号 → 读取前序哈希 → 计算本记录哈希 → 更新状态 → 写文件」
        # 必须是一个**整体临界区**。少了这把锁，两个并发追加会拿到相同的
        # `seq` 与 `previous_hash`，第二条记录当场断链。
        # FastAPI 的同步路由跑在线程池里，所以这不是理论问题：
        # 实测（压小 GIL 切换间隔）30 轮里 24 轮断链，seqs = [1, 1]。
        #
        # 这把锁只管**单进程**。多 worker / 多进程必须换成单一写入者进程、
        # 系统级文件锁或落库 —— 进程内的锁对它们等于不存在。
        #
        # 必须在读取现有链之前创建：下面的 `read_all()` 也会取这把锁。
        self._lock = threading.RLock()
        self._last_hash: str | None = self._read_last_hash()
        self._seq: int = self._read_last_seq()

    # ------------------------------------------------------------------ 状态

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, available: bool) -> None:
        """故障注入用。恢复时会把缓冲回放进 sink。"""
        self._available = available
        if available:
            self._flush_buffer()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def buffered_count(self) -> int:
        return len(self._buffer)

    # ------------------------------------------------------------------ 追加

    def append(
        self,
        event_type: str,
        payload: dict,
        *,
        risk: RiskLevel = RiskLevel.HIGH,
        tenant_id: str | None = None,
        project_id: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """追加一条审计事件，返回 `event_id`。

        `tenant_id` / `project_id` 用于**隔离读取**：只有带租户标记的事件才能被
        按租户查询到；不带标记的视为系统级事件，不对项目接口暴露。

        `request_id` 用于**全链路关联**：显式传入优先；不传时取当前请求上下文里
        中间件绑定的那个（HTTP 请求内自动成立）。两者都没有则记为 `None` ——
        不编造 id：假的可检索 id 比没有更糟。

        sink 不可用时的处置严格按风险分级，不做「静默丢日志」。
        """
        if event_type.strip() == "":
            raise ValueError("event_type 不能为空")

        effective_request_id = request_id or current_request_id()

        # 整个「分配序号 → 算哈希 → 写文件/入缓冲」在同一临界区内完成。
        with self._lock:
            if not self._available:
                if risk is RiskLevel.HIGH:
                    raise deny(
                        ErrorCode.AUDIT_SINK_UNAVAILABLE,
                        "审计 sink 不可用，高影响动作拒绝执行（fail-closed）",
                        event_type=event_type,
                    )
                if len(self._buffer) >= self._buffer_capacity:
                    raise deny(
                        ErrorCode.AUDIT_SINK_UNAVAILABLE,
                        f"审计缓冲已达上限 {self._buffer_capacity}，低风险事件也不再接收",
                        event_type=event_type,
                    )
                record = self._build_record(
                    event_type, payload, risk, tenant_id, project_id, effective_request_id
                )
                self._buffer.append(record)
                return record.event_id

            record = self._build_record(
                event_type, payload, risk, tenant_id, project_id, effective_request_id
            )
            self._write(record)
            return record.event_id

    def _build_record(
        self,
        event_type: str,
        payload: dict,
        risk: RiskLevel,
        tenant_id: str | None,
        project_id: str | None,
        request_id: str | None,
    ) -> AuditRecord:
        """构造记录并计算链哈希。

        哈希覆盖租户、项目、追踪 id 与格式版本，因此**不能事后补写**这些字段 ——
        补写会直接破坏链校验。
        """
        seq = self._seq + 1
        draft = AuditRecord(
            event_id=new_id("aud"),
            seq=seq,
            event_type=event_type,
            payload=payload,
            risk=risk,
            previous_hash=self._last_hash,
            entry_hash="",
            tenant_id=tenant_id,
            project_id=project_id,
            request_id=request_id,
        )
        entry_hash = hash_chain(self._last_hash, content_hash(draft.hash_material()))
        record = replace(draft, entry_hash=entry_hash)
        self._seq = seq
        self._last_hash = entry_hash
        return record

    def _write(self, record: AuditRecord) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_line() + "\n")
            handle.flush()

    def _flush_buffer(self) -> None:
        with self._lock:
            pending, self._buffer = self._buffer, []
            for record in pending:
                self._write(record)

    # ------------------------------------------------------------------ 校验

    def verify_chain_report(self) -> ChainVerification:
        """重算整条哈希链，失败时给出**位置与原因**。

        `verify_chain()` 的布尔值不够用：它分不清「旧格式记录」与「被篡改」，
        于是**一次格式升级就会让整条链报无效**，真正的篡改淹没在噪音里。
        这也是 `schema_version` 存在的理由。
        """
        records = self.read_all()
        legacy = sum(
            1
            for raw in records
            if int(raw.get("schema_version", 1)) != AUDIT_SCHEMA_VERSION
        )

        previous: str | None = None
        for index, raw in enumerate(records):
            if int(raw.get("schema_version", 1)) != AUDIT_SCHEMA_VERSION:
                return ChainVerification(
                    ok=False,
                    checked=index,
                    bad_index=index,
                    problem=ChainProblem.LEGACY_FORMAT,
                    legacy_count=legacy,
                )
            if raw.get("previous_hash") != previous:
                return ChainVerification(
                    ok=False,
                    checked=index,
                    bad_index=index,
                    problem=ChainProblem.BROKEN_LINK,
                    legacy_count=legacy,
                )
            expected = hash_chain(
                previous,
                content_hash(
                    {
                        "seq": raw["seq"],
                        "type": raw["event_type"],
                        "payload": raw["payload"],
                        "tenant_id": raw.get("tenant_id"),
                        "project_id": raw.get("project_id"),
                        "request_id": raw.get("request_id"),
                        "schema_version": raw.get("schema_version"),
                    }
                ),
            )
            if expected != raw["entry_hash"]:
                return ChainVerification(
                    ok=False,
                    checked=index,
                    bad_index=index,
                    problem=ChainProblem.HASH_MISMATCH,
                    legacy_count=legacy,
                )
            previous = raw["entry_hash"]
        return ChainVerification(ok=True, checked=len(records), legacy_count=legacy)

    def verify_chain(self) -> bool:
        """链是否完整。等价于 `verify_chain_report().ok`。

        保留布尔入口给只需要结论的调用方；**排障请用 `verify_chain_report()`**，
        否则看到 `False` 时无法判断是格式演进还是真的被改过。
        """
        return self.verify_chain_report().ok

    def read_all(self) -> list[dict]:
        """只读遍历。sink 是审计事实的载体，只有读与追加两个出口。

        读取也取锁：否则可能读到"写了一半"的行，把并发写变成读侧的解析错误 ——
        那会被误判成记录损坏。
        """
        with self._lock:
            if not self._path.exists():
                return []
            return [
                json.loads(line)
                for line in self._path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def read_scoped(self, *, tenant_id: str, project_id: str | None = None) -> list[dict]:
        """按租户（可选项目）过滤读取。

        不带租户标记的系统级事件**不会**被返回：项目接口不应看到它们。
        这是「所有项目级实体隔离」在审计读取路径上的落点。
        """
        records = [
            record for record in self.read_all() if record.get("tenant_id") == tenant_id
        ]
        if project_id is not None:
            records = [
                record for record in records if record.get("project_id") == project_id
            ]
        return records

    # ------------------------------------------------------------------ 恢复

    def _read_last_hash(self) -> str | None:
        records = self.read_all()
        return records[-1]["entry_hash"] if records else None

    def _read_last_seq(self) -> int:
        records = self.read_all()
        return int(records[-1]["seq"]) if records else 0
