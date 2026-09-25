"""规范化序列化与内容哈希。

设计依据：
- 03 号规格 §2 —— PolicyDecision 快照使用规范化序列化，记录 `snapshot_hash`，决策可重放。
- 04 号规格 §3 —— 索引复用键至少包含 content fingerprint。
- 05 号规格 §4.4 —— 子 agent 回传信封必须携带 `content_hash`。

关键性质：**相同语义的输入必得相同哈希**。这是「同一 policy_version + 快照
必产生同一 decision」这条不变量能被机械验证的前提。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """规范化 JSON：键排序、紧凑分隔符、保留非 ASCII 字符。

    禁止在此处引入随机数或当前时间，否则哈希不可复现，
    决策重放与投影重算都会失去意义。
    """
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_fallback,
    )


def _fallback(obj: Any) -> Any:
    """未知类型的稳定退化，保证哈希计算不因类型而抛错。"""
    if hasattr(obj, "value"):        # StrEnum / IntEnum
        return obj.value
    if hasattr(obj, "isoformat"):    # datetime / date
        return obj.isoformat()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return str(obj)


def content_hash(value: Any) -> str:
    """返回 `sha256:<hex>` 形式的内容指纹。"""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def acquisition_identity_hash(acquisition: dict) -> str:
    """从**获取方式**派生材料的稳定标识。

    这是**项目级** `sources` 去重（`UNIQUE (project_id, identity_hash)`）的
    唯一判定出口，也是知识库关联到项目时"复用项目里已有资料"所依据的身份。

    ⚠️ 知识库登记（`library_sources`）的身份**不完全是**本函数的结果：
    登记时若**携带原文**，`knowledge.library.library_source_identity_hash`
    会在此基础上并入内容指纹 —— 这样"同名不同内容"是两份材料，而不是
    被幂等命中后静默丢弃新原文。本函数只保留 acquisition 维度，
    项目级去重语义因此不受影响。

    因此它必须只有一份实现 —— 项目级各调用点各算一遍时，任何一次规范化差异
    都会让"同一份材料"得到不同哈希，于是关联会**静默地**
    在项目里创建第二份资料，而所有的去重声明看起来都还在。
    """
    return content_hash(acquisition)


def hash_chain(previous: str | None, payload_hash: str) -> str:
    """把上一条审计记录的哈希与当前载荷哈希串成哈希链。

    用于审计 sink 的篡改检测：任何一条记录被改动，其后所有摘要都不再匹配。
    """
    material = f"{previous or 'GENESIS'}|{payload_hash}"
    return content_hash(material)
