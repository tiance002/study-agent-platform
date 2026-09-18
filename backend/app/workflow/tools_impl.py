"""内置工具的占位实现。

⚠️ **降级声明**：`fetch_external_url` 不真的联网，`run_in_sandbox` 不真的执行代码。
   它们存在的目的是让「策略 → 预算 → intent → 派发 → 审计」这条链路能端到端跑通。
   真实实现需要独立 Fetcher（SSRF 防护、私网拦截）与隔离沙箱，本版未实现。

约定：每个实现返回 dict，且必须包含 `cost_units` 供预算结算使用。
"""

from __future__ import annotations

from app.workflow.context import NodeContext

# 极简能力图谱：只保留演示所需的组件与先修关系。
COMPETENCY_GRAPH: dict[str, dict] = {
    "llm.tool_calling": {
        "title": "工具调用",
        "prerequisites": ["llm.basics"],
        "version": "graph/v1",
    },
    "llm.basics": {
        "title": "LLM 基础",
        "prerequisites": [],
        "version": "graph/v1",
    },
    "agent.harness": {
        "title": "Agent harness 编排",
        "prerequisites": ["llm.tool_calling", "runtime.async"],
        "version": "graph/v1",
    },
    "runtime.async": {
        "title": "异步与并发",
        "prerequisites": [],
        "version": "graph/v1",
    },
}


def query_competency_graph(ctx: NodeContext, params: dict) -> dict:
    """读图谱。支持查询单个组件及其直接先修。"""
    component_id = params.get("component_id")
    if component_id:
        entry = COMPETENCY_GRAPH.get(component_id)
        if entry is None:
            return {"cost_units": 1, "found": False, "component_id": component_id}
        return {
            "cost_units": 1,
            "found": True,
            "component_id": component_id,
            "title": entry["title"],
            "prerequisites": entry["prerequisites"],
            "graph_version": entry["version"],
        }

    targets = params.get("targets") or []
    closure: list[str] = []
    seen: set[str] = set()

    def walk(node: str) -> None:
        if node in seen:
            return
        seen.add(node)
        entry = COMPETENCY_GRAPH.get(node)
        if entry is None:
            return
        for prerequisite in entry["prerequisites"]:
            walk(prerequisite)
        closure.append(node)

    for target in targets:
        walk(target)
    return {
        "cost_units": max(1, len(closure)),
        "closure": closure,
        "graph_version": "graph/v1",
    }


def retrieve_project_chunks(ctx: NodeContext, params: dict) -> dict:
    """召回项目资料片段。结果带来源引用与 taint 标记。"""
    query = str(params.get("query", ""))
    limit = int(params.get("limit", 3))
    hits = ctx.chunk_index.search(query, limit=limit)
    return {
        "cost_units": max(1, len(hits)),
        "hits": [
            {
                "chunk_id": hit.chunk.chunk_id,
                "source_id": hit.chunk.source_id,
                "span": list(hit.chunk.span),
                "score": hit.score,
                "excerpt": hit.chunk.text[:120],
                "artifact": hit.chunk.artifact_ref().to_dict(),
                "tainted": True,
            }
            for hit in hits
        ],
    }


def read_source_span(ctx: NodeContext, params: dict) -> dict:
    """按 source_id + span 精确回读。要求已知位置，不做召回。

    读取走 `ChunkIndex.read_span()`，**租户与项目过滤在那里强制**。
    绝不在此处直接遍历内部列表 —— 那样会在某次后续改动里悄悄丢掉作用域判断，
    而它已经注册在工具目录中，一旦启用就是跨租户读取的入口。
    """
    source_id = params.get("source_id")
    span = params.get("span") or []
    if not source_id or len(span) != 2:
        raise ValueError("read_source_span 需要 source_id 与 span=[start, end]")

    chunk = ctx.chunk_index.read_span(source_id, (int(span[0]), int(span[1])))
    if chunk is None:
        return {"cost_units": 1, "found": False, "source_id": source_id, "span": span}
    return {
        "cost_units": 1,
        "found": True,
        "text": chunk.text,
        "artifact": chunk.artifact_ref().to_dict(),
        "tainted": True,
    }


def fetch_external_url(ctx: NodeContext, params: dict) -> dict:
    """外部抓取占位实现。

    真实实现必须经独立 Fetcher：固定 DNS 解析、阻断 loopback/RFC1918/云元数据地址、
    限制重定向与响应大小、归档在沙箱内解压。此处只做域名白名单校验以示边界位置。
    """
    url = str(params.get("url", ""))
    allowed = ("docs.python.org", "fastapi.tiangolo.com")
    if not any(domain in url for domain in allowed):
        raise ValueError(f"目标域名不在白名单内：{url}")
    return {
        "cost_units": 2,
        "url": url,
        "excerpt": f"[开发适配器] 未真实抓取 {url}；真实实现由独立 Fetcher 完成",
        "tainted": True,
    }


def run_validator(ctx: NodeContext, params: dict) -> dict:
    """确定性验证器。判定与理由分开返回，不用一个总分代替。"""
    artifact = params.get("artifact") or {}
    required_keys = params.get("required_keys") or []
    missing = [key for key in required_keys if key not in artifact]
    return {
        "cost_units": 1,
        "passed": not missing,
        "missing_keys": missing,
        "validator_version": "validator/rubric/v1",
    }


def run_in_sandbox(ctx: NodeContext, params: dict) -> dict:
    """沙箱执行占位实现。本版**不执行任何代码**。"""
    return {
        "cost_units": 1,
        "executed": False,
        "reason": "本版未接入隔离沙箱；真实实现需一次性文件系统、默认断网与资源上限",
    }


def append_project_evidence(ctx: NodeContext, params: dict) -> dict:
    """追加证据事实。追加即幂等，因此可以安全重试。"""
    from app.learning.evidence import (
        ComponentVerdict,
        Direction,
        EvidenceKind,
        IndependenceLevel,
        ObservationStrength,
        Validity,
    )

    verdicts = tuple(
        ComponentVerdict(
            component_id=item["component_id"],
            assessment_validity=Validity(item.get("validity", "valid")),
            observation_strength=ObservationStrength(int(item.get("observation_strength", 3))),
            source_reliability_ok=bool(item.get("source_reliability_ok", True)),
            independence_level=IndependenceLevel(item.get("independence_level", "introduced")),
            direction=Direction(item.get("direction", "positive")),
            independence_group=str(item.get("independence_group", "g1")),
        )
        for item in params.get("verdicts", [])
    )
    if not verdicts:
        raise ValueError("append_project_evidence 需要至少一个组件级裁决")

    event = ctx.evidence_log.append(
        tenant_id=params["tenant_id"],
        project_id=params["project_id"],
        kind=EvidenceKind(params.get("kind", "learning")),
        task_id=str(params.get("task_id", "task")),
        contract_id=params.get("contract_id"),
        mapping_version=params.get("mapping_version"),
        graph_version=str(params.get("graph_version", "graph/v1")),
        occurred_at=params["occurred_at"],
        verdicts=verdicts,
    )
    return {"cost_units": 1, "event_id": event.event_id, "seq": event.seq}
