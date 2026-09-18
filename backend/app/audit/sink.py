"""独立审计 sink。

设计依据：03 号规格 §9、不变量 #6 与 00 号规格 §4 失败矩阵。

四条性质：
1. 审计正文写入**独立于主库**的存储，主库只保存引用；
2. 记录以哈希链串联，任何一条被改动都会使链校验失败；
3. **不提供删除接口** —— 应用层根本没有删除能力（不是"约定不删"）；
4. sink 不可用时：高影响动作 fail-closed；低风险事件只进有界缓冲，超限即拒绝。

本版用本地 JSONL 文件实现；生产应替换为对象锁 / WORM 存储，接口不变。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash, hash_chain
from app.core.ids import new_id

DEFAULT_BUFFER_CAPACITY = 256


class RiskLevel(StrEnum):
    """审计事件的处置分级。决定 sink 不可用时的失败方向。"""

    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True)
class AuditRecord:
    """审计记录。`entry_hash` 覆盖前序哈希与当前载荷，构成链。"""

    event_id: str
    seq: int
    event_type: str
    payload: dict
    risk: RiskLevel
    previous_hash: str | None
    entry_hash: str

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
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


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

    def append(self, event_type: str, payload: dict, *, risk: RiskLevel = RiskLevel.HIGH) -> str:
        """追加一条审计事件，返回 `event_id`。

        sink 不可用时的处置严格按风险分级，不做「静默丢日志」。
        """
        if event_type.strip() == "":
            raise ValueError("event_type 不能为空")

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
            record = self._build_record(event_type, payload, risk)
            self._buffer.append(record)
            return record.event_id

        record = self._build_record(event_type, payload, risk)
        self._write(record)
        return record.event_id

    def _build_record(self, event_type: str, payload: dict, risk: RiskLevel) -> AuditRecord:
        seq = self._seq + 1
        entry_hash = hash_chain(self._last_hash, content_hash({"seq": seq, "type": event_type, "payload": payload}))
        record = AuditRecord(
            event_id=new_id("aud"),
            seq=seq,
            event_type=event_type,
            payload=payload,
            risk=risk,
            previous_hash=self._last_hash,
            entry_hash=entry_hash,
        )
        self._seq = seq
        self._last_hash = entry_hash
        return record

    def _write(self, record: AuditRecord) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_line() + "\n")
            handle.flush()

    def _flush_buffer(self) -> None:
        pending, self._buffer = self._buffer, []
        for record in pending:
            self._write(record)

    # ------------------------------------------------------------------ 校验

    def verify_chain(self) -> bool:
        """重算整条哈希链。任一记录被篡改即返回 False。"""
        if not self._path.exists():
            return True
        previous: str | None = None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            expected = hash_chain(
                previous,
                content_hash(
                    {"seq": raw["seq"], "type": raw["event_type"], "payload": raw["payload"]}
                ),
            )
            if expected != raw["entry_hash"] or raw["previous_hash"] != previous:
                return False
            previous = raw["entry_hash"]
        return True

    def read_all(self) -> list[dict]:
        """只读遍历。sink 是审计事实的载体，只有读与追加两个出口。"""
        if not self._path.exists():
            return []
        return [
            json.loads(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # ------------------------------------------------------------------ 恢复

    def _read_last_hash(self) -> str | None:
        records = self.read_all()
        return records[-1]["entry_hash"] if records else None

    def _read_last_seq(self) -> int:
        records = self.read_all()
        return int(records[-1]["seq"]) if records else 0
