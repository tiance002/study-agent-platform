"""HTTP command idempotency contract and in-memory adapter."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from app.core.hashing import content_hash
from app.core.ids import new_id

#: client_key 的长度上限（0007 规格冻结）。超限直接拒绝：
#: 这个值会长期保留在幂等记录里，不设界就是客户端可控的存储放大入口。
IDEMPOTENCY_KEY_MAX_CHARS = 200

#: pending 占用的租约期（秒）。持有者超过该时长未 complete/release，
#: 状态进入 indeterminate 并要求对账。取值太小会把仍在运行的慢请求误标为
#: 不确定，太大则崩溃后暴露得太慢；命令端点的 P99 远小于 120 秒。
IDEMPOTENCY_LEASE_SECONDS = 120

#: 缓存响应体的字节上限。超过即落"过大不可重放"标记，缓存体积有界。
MAX_CACHED_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class ClaimOutcome:
    """一次占用尝试的结果。"""

    kind: Literal[
        "claimed", "replay", "in_progress", "violation", "reconciliation_required"
    ]
    #: replay 时为缓存的 (status_code, body)；claimed/in_progress/violation 为 None。
    cached_status_code: int | None = None
    cached_body: dict | None = None
    #: 本次占用的所有者令牌。complete/release 携带它：
    #: 状态进入 indeterminate 后，旧持有者的 complete 不再改写结果。
    owner_token: str | None = None


def _bounded_body(response_body: dict) -> dict:
    """缓存体有界化。超限落显式标记而不是原文 —— 无界 JSONB 不可接受。

    重放时返回标记体而非原文：客户端凭 `X-Idempotent-Replay` 与标记字段
    可明确知道"这次命令没有重复执行，但响应需通过查询接口重新获取"。
    这比两个更坏的替代方案好：缓存原文（无界放大）或 release
    （重试会重复执行已成功的副作用）。
    """
    encoded = json.dumps(response_body, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode("utf-8")) <= MAX_CACHED_RESPONSE_BYTES:
        return response_body
    return {
        "idempotency_cache": "response_too_large",
        "limit_bytes": MAX_CACHED_RESPONSE_BYTES,
        "actual_bytes": len(encoded.encode("utf-8")),
    }


def command_fingerprint(
    *,
    tenant_id: str,
    principal_id: str,
    command_scope: str,
    path_params: dict,
    body: dict,
) -> str:
    """指纹绑定请求语义的**每个字段**。漏字段 = 同键换语义静默串味（铁律 9）。"""
    return content_hash(
        {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "command_scope": command_scope,
            "path_params": path_params,
            "body": body,
        }
    )


def _claim_id(
    *, tenant_id: str, principal_id: str, command_scope: str, client_key: str
) -> str:
    """claim_id 从**完整逻辑键**派生。

    审查发现的缺陷：早先只哈希 client_key，而 claim_id 是全局主键 ——
    两个用户/两个端点用同一个客户端 key 时，本应互不相干的占用
    会在主键上相撞，返回 500。派生必须覆盖逻辑键的每个维度。
    """
    return "claim_" + content_hash(
        {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "command_scope": command_scope,
            "client_key": client_key,
        }
    )[:16]


class HttpIdempotencyStore(Protocol):
    """幂等存储协议：内存 / PostgreSQL 实现可互换（契约测试互验）。"""

    def claim(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        fingerprint: str,
    ) -> ClaimOutcome: ...

    def complete(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        status_code: int,
        response_body: dict,
        owner_token: str | None = None,
    ) -> None: ...

    def release(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        owner_token: str | None = None,
    ) -> None: ...


class InMemoryHttpIdempotencyStore:
    """内存实现（开发适配器）。结构镜像数据库行，语义与 PG 版一致。

    租约时钟用 `time.monotonic()`：单调时钟不受系统时间跳变影响，
    测试可通过 `lease_seconds=0` 注入"立即过期"。
    """

    def __init__(self, *, lease_seconds: float = IDEMPOTENCY_LEASE_SECONDS) -> None:
        self._lease_seconds = lease_seconds
        self._lock = threading.RLock()
        # key -> {claim_id, fingerprint, state, status_code, response_body,
        #         owner_token, claimed_at}
        self._rows: dict[tuple[str, str, str, str], dict] = {}

    @staticmethod
    def _key(
        tenant_id: str, principal_id: str, command_scope: str, client_key: str
    ) -> tuple[str, str, str, str]:
        return (tenant_id, principal_id, command_scope, client_key)

    def _expired(self, row: dict) -> bool:
        claimed_at = row.get("claimed_at")
        if claimed_at is None:
            return False
        # >= 而不是 >：lease_seconds=0（测试注入"立即过期"）时也成立。
        return (time.monotonic() - claimed_at) >= self._lease_seconds

    def claim(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        fingerprint: str,
    ) -> ClaimOutcome:
        with self._lock:
            return self._claim_unlocked(
                tenant_id=tenant_id,
                principal_id=principal_id,
                command_scope=command_scope,
                client_key=client_key,
                fingerprint=fingerprint,
            )

    def _claim_unlocked(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        fingerprint: str,
    ) -> ClaimOutcome:
        key = self._key(tenant_id, principal_id, command_scope, client_key)
        row = self._rows.get(key)
        owner_token = new_id("idm")
        if row is None:
            self._rows[key] = {
                "claim_id": _claim_id(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    command_scope=command_scope,
                    client_key=client_key,
                ),
                "fingerprint": fingerprint,
                "state": "pending",
                "status_code": None,
                "response_body": None,
                "owner_token": owner_token,
                "claimed_at": time.monotonic(),
            }
            return ClaimOutcome(kind="claimed", owner_token=owner_token)
        if row["fingerprint"] != fingerprint:
            return ClaimOutcome(kind="violation")
        if row["state"] == "completed":
            return ClaimOutcome(
                kind="replay",
                cached_status_code=row["status_code"],
                cached_body=row["response_body"],
            )
        if row["state"] == "indeterminate":
            return ClaimOutcome(kind="reconciliation_required")
        if row["state"] == "released":
            # 只有业务明确失败并 release 才允许重新执行。
            row["state"] = "pending"
            row["owner_token"] = owner_token
            row["claimed_at"] = time.monotonic()
            return ClaimOutcome(kind="claimed", owner_token=owner_token)
        if row["state"] == "pending" and self._expired(row):
            row["state"] = "indeterminate"
            row["owner_token"] = None
            return ClaimOutcome(kind="reconciliation_required")
        return ClaimOutcome(kind="in_progress")

    def complete(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        status_code: int,
        response_body: dict,
        owner_token: str | None = None,
    ) -> None:
        with self._lock:
            self._complete_unlocked(
                tenant_id=tenant_id,
                principal_id=principal_id,
                command_scope=command_scope,
                client_key=client_key,
                status_code=status_code,
                response_body=response_body,
                owner_token=owner_token,
            )

    def _complete_unlocked(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        status_code: int,
        response_body: dict,
        owner_token: str | None = None,
    ) -> None:
        key = self._key(tenant_id, principal_id, command_scope, client_key)
        row = self._rows.get(key)
        if (
            row is not None
            and row["state"] == "pending"
            # 严格相等（IS NOT DISTINCT FROM 语义，与 PG 版一致）：
            # owner_token=None 只匹配没有令牌的行，绝不充当通配符 ——
            # 否则旧持有者迟到完成会覆盖新持有者的占用。
            and row.get("owner_token") == owner_token
        ):
            row["state"] = "completed"
            row["status_code"] = status_code
            row["response_body"] = _bounded_body(response_body)

    def release(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        owner_token: str | None = None,
    ) -> None:
        with self._lock:
            self._release_unlocked(
                tenant_id=tenant_id,
                principal_id=principal_id,
                command_scope=command_scope,
                client_key=client_key,
                owner_token=owner_token,
            )

    def _release_unlocked(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        owner_token: str | None = None,
    ) -> None:
        key = self._key(tenant_id, principal_id, command_scope, client_key)
        row = self._rows.get(key)
        if (
            row is not None
            and row["state"] == "pending"
            and row.get("owner_token") == owner_token
        ):
            row["state"] = "released"


