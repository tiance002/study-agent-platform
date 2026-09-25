"""Versioned, content-free routing facts for one teaching run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

QUERY_REWRITE_STATUSES = frozenset({"disabled", "applied", "fallback"})
ROUTE_REASONS = frozenset({"local_not_configured", "local_rewrite_accepted", "local_unavailable_or_invalid"})


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    query_rewrite_status: Literal["disabled", "applied", "fallback"]
    reason_code: str
    policy_version: str = "teaching-route/v1"
    answer_route: str = "cloud"

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str)
            for value in (
                self.query_rewrite_status,
                self.reason_code,
                self.policy_version,
                self.answer_route,
            )
        ):
            raise ValueError("教学路由快照字段必须是文本")
        if self.policy_version != "teaching-route/v1":
            raise ValueError("不支持的教学路由策略版本")
        if self.answer_route != "cloud":
            raise ValueError("首版最终教学答案必须走云端 provider")
        if self.query_rewrite_status not in QUERY_REWRITE_STATUSES:
            raise ValueError("query_rewrite_status 不在允许范围内")
        if self.reason_code not in ROUTE_REASONS:
            raise ValueError("reason_code 不在允许范围内")
        expected = {
            "disabled": "local_not_configured",
            "applied": "local_rewrite_accepted",
            "fallback": "local_unavailable_or_invalid",
        }[self.query_rewrite_status]
        if self.reason_code != expected:
            raise ValueError("路由状态与原因不一致")

    def to_dict(self) -> dict[str, str]:
        return {
            "policy_version": self.policy_version,
            "answer_route": self.answer_route,
            "query_rewrite_status": self.query_rewrite_status,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(cls, raw: object) -> RoutingDecision:
        if not isinstance(raw, dict) or set(raw) != {
            "policy_version",
            "answer_route",
            "query_rewrite_status",
            "reason_code",
        }:
            raise ValueError("教学路由快照字段无效")
        if not all(isinstance(value, str) for value in raw.values()):
            raise ValueError("教学路由快照值必须是文本")
        return cls(
            policy_version=raw["policy_version"],
            answer_route=raw["answer_route"],
            query_rewrite_status=raw["query_rewrite_status"],
            reason_code=raw["reason_code"],
        )
