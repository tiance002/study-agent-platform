"""注册表：注册即校验，冻结即版本化。

三道机械门（对应 05 号规格 §5.2、§6.3）：
1. 必填字段门 —— 用于边界判定的字段缺失即拒绝注册。
2. 重叠门 —— 同一 `(intent_tag, sink_class, min_authority)` 下多个 exclusive 即拒绝。
3. 写入安全门 —— A2/A3 必须有可对账或可补偿的幂等语义。

另有按需加载门 —— 单 node 常驻工具数超过 8 即拒绝（05 号规格 §3.2）。
"""

from __future__ import annotations

from app.core.errors import ErrorCode, PlatformError, deny
from app.core.hashing import content_hash
from app.registry.models import (
    IDEMPOTENT_ENOUGH_FOR_WRITE,
    Authority,
    Exclusivity,
    IdempotencyLevel,
    NodeSpec,
    ToolSpec,
)

MAX_TOOLS_PER_NODE = 8


class Registry:
    """node 与 tool 的唯一注册处。

    冻结后不可再注册，并产生 `registry_version`；运行必须固定该版本
    （不变量 #13：变更必须进入决策记录）。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._nodes: dict[str, NodeSpec] = {}
        self._frozen = False
        self._version: str | None = None

    # ------------------------------------------------------------------ 注册

    def register_tool(self, spec: ToolSpec) -> None:
        """注册工具。任何一门不通过即抛错，不会留下部分状态。"""
        self._ensure_mutable()
        self._validate_tool_declaration(spec)
        self._assert_no_intent_conflict(spec)
        if spec.tool_id in self._tools:
            raise deny(
                ErrorCode.TOOL_DECLARATION_INVALID,
                f"tool_id 重复：{spec.tool_id}",
                tool_id=spec.tool_id,
            )
        self._tools[spec.tool_id] = spec

    def register_node(self, spec: NodeSpec) -> None:
        """注册 node。允许工具必须已注册，且权限不得越出 node 上限。"""
        self._ensure_mutable()
        if not spec.node_id:
            raise deny(ErrorCode.TOOL_DECLARATION_INVALID, "node_id 不能为空")
        unknown = [t for t in spec.allowed_tools if t not in self._tools]
        if unknown:
            raise deny(
                ErrorCode.TOOL_NOT_REGISTERED,
                f"node {spec.node_id} 引用了未注册工具：{unknown}",
                node_id=spec.node_id,
                tools=unknown,
            )
        for tool_id in spec.allowed_tools:
            tool = self._tools[tool_id]
            if tool.min_authority > spec.authority_ceiling:
                raise deny(
                    ErrorCode.TOOL_NOT_ALLOWED_FOR_NODE,
                    f"node {spec.node_id} 的权限上限 {spec.authority_ceiling.label} "
                    f"低于工具 {tool_id} 所需的 {tool.min_authority.label}",
                    node_id=spec.node_id,
                    tool_id=tool_id,
                )
        if len(spec.allowed_tools) > MAX_TOOLS_PER_NODE:
            raise deny(
                ErrorCode.TOOL_DECLARATION_INVALID,
                f"node {spec.node_id} 声明了 {len(spec.allowed_tools)} 个工具，"
                f"超过上限 {MAX_TOOLS_PER_NODE}；说明该 node 职责过宽",
                node_id=spec.node_id,
            )
        # 注意：max_tool_calls 允许为 0 —— 表示该 node 不调用任何工具（纯解释 node）。
        # 其余上限必须为正，否则循环无法保证退出（退出条件 1）。
        for name, value in (
            ("max_loops", spec.max_loops),
            ("max_recursion", spec.max_recursion),
            ("max_concurrency", spec.max_concurrency),
            ("max_steps", spec.max_steps),
            ("max_tokens", spec.max_tokens),
            ("max_tool_calls", spec.max_tool_calls),
        ):
            floor = 0 if name == "max_tool_calls" else 1
            if value < floor:
                raise deny(
                    ErrorCode.TOOL_DECLARATION_INVALID,
                    f"node {spec.node_id} 的 {name} 必须不小于 {floor}，"
                    f"否则循环无法保证退出",
                    node_id=spec.node_id,
                )
        self._nodes[spec.node_id] = spec

    # ------------------------------------------------------------------ 校验门

    def _validate_tool_declaration(self, spec: ToolSpec) -> None:
        """门 1：用于边界判定的字段必须显式声明。"""
        missing = [
            name
            for name, value in (
                ("tool_id", spec.tool_id),
                ("intent_tag", spec.intent_tag),
                ("owner_module", spec.owner_module),
            )
            if not value
        ]
        if missing:
            raise deny(
                ErrorCode.TOOL_DECLARATION_INVALID,
                f"工具声明的必填字段缺失：{missing}",
                missing=missing,
            )

        # 门 3：A2/A3 必须能对账或补偿，否则重试会产生重复副作用。
        if spec.min_authority >= Authority.A2 and spec.idempotency not in IDEMPOTENT_ENOUGH_FOR_WRITE:
            raise deny(
                ErrorCode.TOOL_DECLARATION_INVALID,
                f"工具 {spec.tool_id} 申请 {spec.min_authority.label}，"
                f"但幂等语义为 {spec.idempotency}；无幂等声明者最高只给 A1",
                tool_id=spec.tool_id,
            )

        # 访问外部网络至少需要 A1c。
        if spec.network_domains and spec.min_authority < Authority.A1C:
            raise deny(
                ErrorCode.TOOL_DECLARATION_INVALID,
                f"工具 {spec.tool_id} 声明了网络边界，但权限为 {spec.min_authority.label}；"
                f"外部网络读取至少需要 A1c",
                tool_id=spec.tool_id,
            )

    def _assert_no_intent_conflict(self, spec: ToolSpec) -> None:
        """门 2：同一意图组合下不允许出现第二个独占工具。

        有意只做机械判定：它拦得住「同一 intent 的重复实现」，
        **拦不住「语义相近但标签不同」的歧义**。后者靠 when-to-use 表与人工复查。
        """
        for existing in self._tools.values():
            if existing.intent_key != spec.intent_key:
                continue
            if (
                existing.exclusivity is Exclusivity.EXCLUSIVE
                or spec.exclusivity is Exclusivity.EXCLUSIVE
            ):
                raise deny(
                    ErrorCode.TOOL_INTENT_CONFLICT,
                    f"意图冲突：{existing.tool_id} 与 {spec.tool_id} 同为 "
                    f"({spec.intent_tag}, {spec.sink_class}, {spec.min_authority.label})；"
                    f"第一动作应是合并两个工具，而不是补充描述文字",
                    existing=existing.tool_id,
                    incoming=spec.tool_id,
                    intent_key=list(spec.intent_key),
                )

    # ------------------------------------------------------------------ 冻结

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise deny(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                "注册表已冻结；运行期禁止改变 node/tool 定义",
            )

    def freeze(self) -> str:
        """冻结注册表并返回版本号。版本由内容哈希决定，可复现。"""
        if self._version is None:
            snapshot = {
                "tools": [
                    {
                        "tool_id": t.tool_id,
                        "intent_tag": t.intent_tag,
                        "sink_class": str(t.sink_class),
                        "owner_module": t.owner_module,
                        "min_authority": int(t.min_authority),
                        "exclusivity": str(t.exclusivity),
                        "idempotency": str(t.idempotency),
                    }
                    for t in sorted(self._tools.values(), key=lambda x: x.tool_id)
                ],
                "nodes": [
                    {
                        "node_id": n.node_id,
                        "allowed_tools": sorted(n.allowed_tools),
                        "min_tier": str(n.min_tier),
                        "authority_ceiling": int(n.authority_ceiling),
                        "limits": [
                            n.max_tool_calls,
                            n.max_loops,
                            n.max_recursion,
                            n.max_concurrency,
                            n.max_steps,
                        ],
                    }
                    for n in sorted(self._nodes.values(), key=lambda x: x.node_id)
                ],
            }
            self._version = content_hash(snapshot)
            self._frozen = True
        return self._version

    # ------------------------------------------------------------------ 查询

    @property
    def version(self) -> str:
        if self._version is None:
            raise PlatformError(
                code=ErrorCode.ILLEGAL_STATE_TRANSITION,
                message="注册表尚未冻结；运行前必须先 freeze() 以固定版本",
            )
        return self._version

    def tool(self, tool_id: str) -> ToolSpec:
        try:
            return self._tools[tool_id]
        except KeyError as exc:
            raise deny(ErrorCode.TOOL_NOT_REGISTERED, f"工具未注册：{tool_id}") from exc

    def node(self, node_id: str) -> NodeSpec:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise deny(
                ErrorCode.NODE_NOT_REGISTERED,
                f"node 未注册：{node_id}；未知 node 只允许解释、澄清或重新规划",
            ) from exc

    def tool_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def node_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._nodes))

    def tools_for_node(self, node_id: str) -> tuple[ToolSpec, ...]:
        """按需加载：只返回该 node 被授权且可能用到的工具声明。

        低权限 node 不接收无关工具描述（03 号规格），避免 schema 挤占上下文。
        """
        spec = self.node(node_id)
        return tuple(self._tools[t] for t in spec.allowed_tools)
