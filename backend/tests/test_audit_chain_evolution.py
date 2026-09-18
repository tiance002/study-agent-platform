"""审计链的**可诊断性**。

这条 README 早就记为待办：`entry_hash` 覆盖记录结构本身，所以哈希公式一变更，
此前写入的记录会全部校验失败，而 `verify_chain()` 只返回布尔值 ——
**分不清「这是旧格式记录」和「记录被篡改」**。生产里一次代码升级就能让整条链报
「无效」，真正的篡改会淹没在噪音里。

`schema_version` + `ChainVerification` 就是为此存在的。这一组同时钉住三件事：
三种失败原因**必须互相区分**，且**不许退化成"反正都是 False"**。
"""

from __future__ import annotations

import json

import pytest
from app.audit.sink import (
    AUDIT_SCHEMA_VERSION,
    AuditSink,
    ChainProblem,
    RiskLevel,
)


def _dump(records: list[dict]) -> str:
    return "\n".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for r in records
    ) + "\n"


@pytest.mark.invariant
def test_healthy_chain_reports_ok_with_reason(tmp_path):
    sink = AuditSink(tmp_path)
    sink.append("a", {"i": 1}, risk=RiskLevel.LOW)
    sink.append("b", {"i": 2}, risk=RiskLevel.LOW)

    report = sink.verify_chain_report()
    assert report.ok is True
    assert report.checked == 2
    assert report.problem is None
    assert "完整" in report.describe()


@pytest.mark.invariant
def test_records_declare_schema_version(tmp_path):
    """每条记录都带版本号 —— 没有它，校验就无法区分格式演进与篡改。"""
    sink = AuditSink(tmp_path)
    sink.append("a", {"i": 1}, risk=RiskLevel.LOW)
    assert sink.read_all()[0]["schema_version"] == AUDIT_SCHEMA_VERSION


@pytest.mark.invariant
def test_legacy_record_is_reported_as_format_not_tampering(tmp_path):
    """旧格式记录必须被识别为**格式问题**，而不是被当成篡改。

    这是本轮要解决的核心痛点：升级一次就整条链报"无效"，
    真正的篡改夹在里面没人看得见。
    """
    sink = AuditSink(tmp_path)
    sink.append("a", {"i": 1}, risk=RiskLevel.LOW)

    record = sink.read_all()[0]
    record.pop("schema_version")  # 模拟历史版本写下的记录
    sink.path.write_text(_dump([record]), encoding="utf-8")

    report = sink.verify_chain_report()
    assert report.ok is False
    assert report.problem is ChainProblem.LEGACY_FORMAT
    assert report.bad_index == 0
    assert report.legacy_count == 1
    assert "不是篡改" in report.describe()


@pytest.mark.invariant
def test_tampered_payload_is_reported_as_hash_mismatch(tmp_path):
    """改内容必须报"内容与哈希不符"，而不是笼统的 False。"""
    sink = AuditSink(tmp_path)
    sink.append("a", {"amount": 1}, risk=RiskLevel.LOW)

    text = sink.path.read_text(encoding="utf-8")
    sink.path.write_text(text.replace('"amount":1', '"amount":999'), encoding="utf-8")

    report = sink.verify_chain_report()
    assert report.problem is ChainProblem.HASH_MISMATCH
    assert "被改动" in report.describe()


@pytest.mark.invariant
def test_dropped_record_is_reported_as_broken_link(tmp_path):
    """删掉中间一条 → 断链。这与"改内容"是不同的证据轨迹，必须分开报。"""
    sink = AuditSink(tmp_path)
    for i in range(3):
        sink.append("e", {"i": i}, risk=RiskLevel.LOW)

    records = sink.read_all()
    sink.path.write_text(_dump([records[0], records[2]]), encoding="utf-8")

    report = sink.verify_chain_report()
    assert report.problem is ChainProblem.BROKEN_LINK
    assert report.bad_index == 1
    assert "断链" in report.describe()


@pytest.mark.invariant
def test_request_id_is_part_of_the_chain(tmp_path):
    """追踪 id 参与哈希 —— 因此不能事后补写。

    如果它不在哈希里，就可以给旧记录"补上"一个追踪 id 来制造虚假关联，
    而链校验不会发现。
    """
    sink = AuditSink(tmp_path, filename="with_request.jsonl")
    sink.append("a", {"i": 1}, risk=RiskLevel.LOW, request_id="req_abc")

    text = sink.path.read_text(encoding="utf-8")
    sink.path.write_text(text.replace("req_abc", "req_zzz"), encoding="utf-8")

    assert sink.verify_chain_report().problem is ChainProblem.HASH_MISMATCH


@pytest.mark.invariant
def test_boolean_entry_point_still_available(tmp_path):
    """只关心结论的调用方仍可用 `verify_chain()`；它是 report 的投影，不是第二套逻辑。"""
    sink = AuditSink(tmp_path)
    sink.append("a", {"i": 1}, risk=RiskLevel.LOW)
    assert sink.verify_chain() is sink.verify_chain_report().ok

    sink.path.write_text("", encoding="utf-8")
    assert sink.verify_chain() is True  # 空文件视作空链
