"""Immutable original response data for a worker-owned acquisition attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from app.core.contracts import require_aware, require_id, require_text
from app.knowledge.models import (
    MAX_DOCUMENT_BYTES,
    WEB_PARSER_VERSIONS,
    WEB_SOURCE_CONTENT_TYPES,
)

ACQUISITION_CONTENT_TYPES = WEB_SOURCE_CONTENT_TYPES
ACQUISITION_PARSER_VERSIONS = WEB_PARSER_VERSIONS
WEB_TEXT_PARSER_VERSION = "web-text/v1"


@dataclass(frozen=True, slots=True)
class AcquisitionArtifact:
    acquisition_id: str
    tenant_id: str
    project_id: str
    content_type: str
    raw_content: bytes
    parser_version: str
    fetched_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("acquisition_id", "tenant_id", "project_id"):
            require_id(getattr(self, field_name), field_name)
        if self.content_type not in ACQUISITION_CONTENT_TYPES:
            raise ValueError("获取响应 content type 不在允许范围内")
        if self.parser_version not in ACQUISITION_PARSER_VERSIONS:
            raise ValueError("获取响应 parser version 不在允许范围内")
        if not isinstance(self.raw_content, bytes):
            raise TypeError("raw_content 必须是 bytes")
        if not self.raw_content or len(self.raw_content) > MAX_DOCUMENT_BYTES:
            raise ValueError("获取响应为空或超过大小上限")
        require_text(self.parser_version, "parser_version")
        require_aware(self.fetched_at, "fetched_at")

    @property
    def content_hash(self) -> str:
        return "sha256:" + sha256(self.raw_content).hexdigest()
