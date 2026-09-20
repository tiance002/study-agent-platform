"""审计 sink 的**持久化健壮性**：写失败、崩溃残留、缓冲回写失败。

这三个场景都不会出现在正常路径上，一旦出现却会造成**不可修复**的后果 ——
审计链是 append-only 的，断掉就再也补不回来。而且它们都很安静：
断链要等校验时才发现，丢记录甚至没有任何标记。

它们还共同暴露过一个设计错误：**先改内存状态、再落盘**。
顺序反了的时候，一次磁盘故障就让内存里的 `seq` / `last_hash` 领先于文件，
之后每条记录都找不到前序。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.audit.sink import AuditSink, ChainProblem, RiskLevel
from app.core.errors import ErrorCode, PlatformError


def _sink(directory: Path, **kwargs) -> AuditSink:
    return AuditSink(directory, **kwargs)


def _append_raw(path: Path, text: str) -> None:
    """直接往文件尾部塞一行**不完整**内容，模拟进程写入中途被杀。"""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _tamper_first_payload(path: Path, value: int) -> None:
    """改动某条记录的内容但**不重算哈希** —— 这就是"被篡改"的样子。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"] = {"n": value}
    lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------- 写失败


@pytest.mark.invariant
def test_write_failure_leaves_no_state_behind(tmp_path):
    """落盘失败时，内存状态不能领先于文件。

    改前 `_build_record` 会先推进 `_seq` / `_last_hash`，再交给 `_write`。
    写失败后内存已经"以为"那条记录进去了，于是**下一条**记录的
    `previous_hash` 指向一个文件里不存在的哈希 —— 之后的整条链全部断掉，
    而且 append-only 意味着无法回填修复。
    """
    sink = _sink(tmp_path / "audit")
    sink.append("a", {"n": 1}, risk=RiskLevel.HIGH)

    original = sink._write

    def failing(record):  # noqa: ANN001
        raise OSError("disk full")

    sink._write = failing
    with pytest.raises(OSError):
        sink.append("b", {"n": 2}, risk=RiskLevel.HIGH)
    sink._write = original

    assert sink._seq == 1, "写失败却推进了序号：内存状态与文件不再一致"

    sink.append("c", {"n": 3}, risk=RiskLevel.HIGH)
    report = sink.verify_chain_report()
    assert report.ok, f"写失败后链断了：{report.describe()}"
    assert len(sink.read_all()) == 2


# ------------------------------------------------------------- 缓冲回写


@pytest.mark.invariant
def test_flush_failure_keeps_pending_events_buffered(tmp_path):
    """缓冲回写失败时，剩余事件必须留在缓冲里。

    改前是「一次性清空缓冲，再逐个写」。中途失败会让剩下的记录
    既不在文件里、也不在缓冲里 —— **静默丢失审计记录**，
    与 sink 文档里"不做静默丢日志"的承诺直接冲突。
    """
    sink = _sink(tmp_path / "audit", available=False, buffer_capacity=8)
    for name in ("a", "b", "c"):
        sink.append(name, {"n": name}, risk=RiskLevel.LOW)
    assert sink.buffered_count == 3

    original = sink._write
    calls = {"n": 0}

    def flaky(record):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk full")
        return original(record)

    sink._write = flaky
    sink._available = True
    with pytest.raises(OSError):
        sink._flush_buffer()
    sink._write = original

    assert sink.buffered_count == 2, "回写失败后剩下的记录被静默丢弃了"

    # 恢复后必须能把剩下的写完，且链是连续的
    sink._flush_buffer()
    assert sink.buffered_count == 0
    report = sink.verify_chain_report()
    assert report.ok, f"回写重试后链不完整：{report.describe()}"
    assert len(sink.read_all()) == 3


# ------------------------------------------------------------- 崩溃残留


@pytest.mark.invariant
def test_crash_residue_does_not_block_construction(tmp_path):
    """进程写入中途被杀会留下半行 —— sink 必须还能构造出来。

    改前实测：`_read_last_hash()` 走 `read_all()`，半行让它抛 `JSONDecodeError`，
    于是 `AuditSink(...)` 直接失败。**一次崩溃残留就让整个平台起不来**，
    而重启恰恰是最需要它可用的时刻。

    半行没有 `entry_hash`，不在链上、也没被任何记录引用，
    所以新记录应当接在最后一个**完整**记录之后。
    """
    directory = tmp_path / "audit"
    sink = _sink(directory)
    sink.append("a", {"n": 1}, risk=RiskLevel.HIGH)
    _append_raw(sink.path, '{"event_id":"aud_x","seq":2,"event_ty')

    revived = AuditSink(directory)  # 不应抛异常
    assert revived._seq == 1, "链尾应当停在最后一个完整记录上"

    revived.append("b", {"n": 2}, risk=RiskLevel.HIGH)
    report = revived.verify_chain_report()
    # 链本身连续，但文件里有无法解析的内容 —— 两件事都要如实说出来。
    assert report.problem is ChainProblem.MALFORMED_LINE
    assert report.bad_index == 1


@pytest.mark.invariant
def test_new_record_does_not_fuse_with_crash_residue(tmp_path):
    """残行没有换行结尾时，新记录不能粘在它后面。

    否则垃圾从一行扩散成两行 —— 而其中一行是我们刚刚成功提交、
    已经记进内存状态的记录：它永远读不出来，
    校验时也只能报"这里解析不了"，分不清是残行还是被动了手脚。
    """
    sink = _sink(tmp_path / "audit")
    sink.append("a", {"n": 1}, risk=RiskLevel.HIGH)
    _append_raw(sink.path, '{"event_id":"aud_x"')  # 注意：没有换行结尾
    sink.append("b", {"n": 2}, risk=RiskLevel.HIGH)

    lines = [
        line
        for line in sink.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 3, f"新记录被粘在残行后面了：{lines}"
    assert json.loads(lines[-1])["event_type"] == "b", "最后一行应是刚写入的那条记录"
    assert sink.verify_chain_report().bad_index == 1, "残行应独立成行、位置不变"


@pytest.mark.invariant
def test_read_all_refuses_incomplete_data(tmp_path):
    """要全量数据的调用方必须拿到错误，而不是一个悄悄少了几条的列表。

    返回不完整的数据比返回错误更危险：前者看起来是完整的。
    """
    sink = _sink(tmp_path / "audit")
    sink.append("a", {"n": 1}, risk=RiskLevel.HIGH)
    _append_raw(sink.path, '{"event_id":"aud_x"')

    with pytest.raises(PlatformError) as exc:
        sink.read_all()
    assert exc.value.code is ErrorCode.AUDIT_LOG_CORRUPTED


@pytest.mark.invariant
def test_malformed_line_is_distinct_from_tampering(tmp_path):
    """「崩溃残留」与「被篡改」必须是两个可区分的原因。

    否则一次崩溃会让整条链报「无效」，真正的篡改淹没在噪音里 ——
    这与 `LEGACY_FORMAT` 当初要解决的是同一个问题：
    **一个布尔值不够用**。
    """
    crashed = _sink(tmp_path / "crashed")
    crashed.append("a", {"n": 1}, risk=RiskLevel.HIGH)
    _append_raw(crashed.path, '{"event_id":"aud_x"')
    assert crashed.verify_chain_report().problem is ChainProblem.MALFORMED_LINE

    tampered = _sink(tmp_path / "tampered")
    tampered.append("a", {"n": 1}, risk=RiskLevel.HIGH)
    _tamper_first_payload(tampered.path, 999)
    assert tampered.verify_chain_report().problem is ChainProblem.HASH_MISMATCH
