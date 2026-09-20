"""子任务运行时（ChildRun）。

设计依据：不变量 #15/#16、03 号规格 §10、05 号规格 §4、ADR-014。

三条硬规则：
1. 权限从父 token **派生且只能收缩**，默认只持 A0/A1；
2. 深度硬上限 1，不可递归 spawn；
3. 回传必须是**结构化信封**并携带谱系；散文摘要不得作为证据。

一句要记住的话：**上下文可以丢，谱系不能丢。**
主对话上下文隔离，但证据谱系、审计、预算三者全部旁路、都不隔离。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# 谱系引用契约（ArtifactRef / DisplayPolicy）定义在 `core/artifacts.py`，
# 在此再导出供外部导入。`noqa` 是必要的：ruff 会把它当作"未使用的导入"删掉，
# 而那会破坏对外的导入契约（这个文件被 import 时应当能拿到这两个类型）。
from app.core.artifacts import ArtifactRef, DisplayPolicy  # noqa: F401
from app.core.clock import Clock
from app.core.errors import ErrorCode, deny
from app.core.evidence_issues import EvidenceIssue
from app.policy.token import CapabilityToken, TokenIssuer
from app.registry.models import Authority
from app.registry.registry import Registry

MAX_DEPTH = 1


class ChildPurpose(StrEnum):
    """子任务类型。职责互斥：一个子任务只能对应一种。"""

    RESEARCH = "research"          # 上下文量大、只需结论
    EXTRACTION = "extraction"      # 输出可结构化校验
    VERIFICATION = "verification"  # 判定 + 理由 + 反例
    DRAFTING = "drafting"          # 产出待审草稿


PURPOSE_AUTHORITY_CEILING: dict[ChildPurpose, Authority] = {
    ChildPurpose.RESEARCH: Authority.A1C,
    ChildPurpose.EXTRACTION: Authority.A1A,
    ChildPurpose.VERIFICATION: Authority.A1A,
    ChildPurpose.DRAFTING: Authority.A0,
}


class EnvelopeStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ClaimKind(StrEnum):
    SOURCED = "sourced"
    INFERENCE = "inference"


# 谱系引用契约（ArtifactRef / DisplayPolicy）定义在 `core/artifacts.py`，
# 通过文件顶部 import 再导出，避免 L4 模块之间横向依赖。
@dataclass(frozen=True)
class Claim:
    """一条断言。自然语言只能出现在这里，且必须挂载来源。"""

    text: str
    evidence_refs: tuple[int, ...] = ()
    kind: ClaimKind = ClaimKind.SOURCED


@dataclass(frozen=True)
class ChildEnvelope:
    """子 agent 回传信封。结构固定，不接受自由格式。"""

    child_run_id: str
    status: EnvelopeStatus
    artifacts: tuple[ArtifactRef, ...] = ()
    claims: tuple[Claim, ...] = ()
    taint_sources: tuple[str, ...] = ()
    # 结构化证据问题。**不再是自然语言的 `unresolved[]`** ——
    # 自然语言无法机械比较，拿它做判断会让"证据状态"退化成措辞的产物；
    # 而且散文描述无法携带 claim/source 引用，也就断了谱系。
    # 闭集见 `core/evidence_issues.py`（02 号规格 §4 / ADR-014）。
    issues: tuple[EvidenceIssue, ...] = ()

    def to_dict(self) -> dict:
        return {
            "child_run_id": self.child_run_id,
            "status": str(self.status),
            "artifacts": [
                {
                    "source_id": a.source_id,
                    "span": list(a.span),
                    "content_hash": a.content_hash,
                    "parser_version": a.parser_version,
                    "display_policy": str(a.display_policy),
                }
                for a in self.artifacts
            ],
            "claims": [
                {
                    "text": c.text,
                    "evidence_refs": list(c.evidence_refs),
                    "kind": str(c.kind),
                }
                for c in self.claims
            ],
            "taint": {"sources": list(self.taint_sources)},
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class SpawnedChild:
    child_run_id: str
    purpose: ChildPurpose
    depth: int
    token: CapabilityToken


class ChildRunRuntime:
    """子任务的派生与回传校验。"""

    def __init__(
        self,
        *,
        tokens: TokenIssuer,
        clock: Clock,
        registry: Registry,
        max_depth: int = MAX_DEPTH,
    ) -> None:
        self._tokens = tokens
        self._clock = clock
        self._registry = registry
        self._max_depth = max_depth

    # ------------------------------------------------------------------ 派生

    def spawn(
        self,
        *,
        parent_token: CapabilityToken,
        child_run_id: str,
        purpose: ChildPurpose,
        allowed_tools: set[str],
        expires_at: datetime,
        parent_depth: int = 0,
    ) -> SpawnedChild:
        """派生一个子任务及其 token。

        三件事在这里一次性做掉：深度检查、用途权限上限检查、token 收缩派生。
        任一不满足即拒绝，且**不产生任何部分状态**。
        """
        depth = parent_depth + 1
        if depth > self._max_depth:
            raise deny(
                ErrorCode.CHILD_RUN_DEPTH_EXCEEDED,
                f"子任务深度 {depth} 超过上限 {self._max_depth}；"
                f"子 agent 不可递归 spawn（放宽需新 ADR）",
                depth=depth,
                max_depth=self._max_depth,
            )

        ceiling = PURPOSE_AUTHORITY_CEILING[purpose]
        if ceiling > Authority.A1C:
            raise deny(
                ErrorCode.CHILD_RUN_AUTHORITY_ESCALATION,
                f"子任务用途 {purpose} 的权限上限 {ceiling.label} 超出允许范围",
            )

        # 用途决定权限上限：research 可外网读取，drafting 连工具都不该用。
        for tool_id in allowed_tools:
            spec = self._registry.tool(tool_id)
            if spec.min_authority > ceiling:
                raise deny(
                    ErrorCode.CHILD_RUN_AUTHORITY_ESCALATION,
                    f"子任务用途 {purpose} 的权限上限为 {ceiling.label}，"
                    f"但工具 {tool_id} 需要 {spec.min_authority.label}",
                    tool_id=tool_id,
                    purpose=str(purpose),
                )

        # 派生只会收缩：derive() 内部会拒绝任何扩权。
        child_token = self._tokens.derive(
            parent_token,
            allowed_tools=allowed_tools,
            expires_at=min(expires_at, parent_token.expires_at),
            node_instance_id=child_run_id,
            audience=f"child:{purpose}",
        )
        return SpawnedChild(
            child_run_id=child_run_id,
            purpose=purpose,
            depth=depth,
            token=child_token,
        )

    # ------------------------------------------------------------------ 校验

    def validate_envelope(self, envelope: ChildEnvelope) -> None:
        """校验回传信封。四条硬规则在此强制。"""
        if envelope.status is EnvelopeStatus.UNKNOWN:
            raise deny(
                ErrorCode.CHILD_RUN_ENVELOPE_INVALID,
                "子任务状态未知时不得回传结果；必须先对账，"
                "主对话不得据未知状态推断结论",
                child_run_id=envelope.child_run_id,
            )

        for index, artifact in enumerate(envelope.artifacts):
            if not artifact.content_hash.startswith("sha256:"):
                raise deny(
                    ErrorCode.CHILD_RUN_ENVELOPE_INVALID,
                    f"artifact[{index}] 缺少可校验的 content_hash；"
                    f"谱系必须可追溯到具体内容版本",
                    child_run_id=envelope.child_run_id,
                )
            if artifact.span[0] < 0 or artifact.span[1] < artifact.span[0]:
                raise deny(
                    ErrorCode.CHILD_RUN_ENVELOPE_INVALID,
                    f"artifact[{index}] 的 span 非法：{artifact.span}",
                    child_run_id=envelope.child_run_id,
                )

        for index, claim in enumerate(envelope.claims):
            if not claim.text.strip():
                raise deny(
                    ErrorCode.CHILD_RUN_ENVELOPE_INVALID,
                    f"claim[{index}] 文本为空",
                    child_run_id=envelope.child_run_id,
                )
            if claim.kind is ClaimKind.SOURCED and not claim.evidence_refs:
                raise deny(
                    ErrorCode.CHILD_RUN_ENVELOPE_INVALID,
                    f"claim[{index}] 声明为有来源但未挂载 evidence_refs；"
                    f"散文摘要必须降级为 inference，不能冒充有来源的结论",
                    child_run_id=envelope.child_run_id,
                )
            for ref in claim.evidence_refs:
                if ref < 0 or ref >= len(envelope.artifacts):
                    raise deny(
                        ErrorCode.CHILD_RUN_ENVELOPE_INVALID,
                        f"claim[{index}] 引用了不存在的 artifact 下标 {ref}",
                        child_run_id=envelope.child_run_id,
                    )

    @staticmethod
    def evidence_eligible_claims(envelope: ChildEnvelope) -> tuple[Claim, ...]:
        """筛出可以作为学习证据的断言。

        `kind: inference` 一律排除 —— 这是「模型摘要不能自动去污点」在证据链上的落点。
        """
        return tuple(c for c in envelope.claims if c.kind is ClaimKind.SOURCED)
