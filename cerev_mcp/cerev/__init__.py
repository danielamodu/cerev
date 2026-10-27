"""CEREV integration layer for Zetaris MCP."""

from cerev_mcp.cerev.adapter import (
    InProcessMcpClient,
    ZetarisMcpSourceAdapter,
    create_cerev_mcp_adapters,
)
from cerev_mcp.cerev.config import (
    CerevMcpSourceConfig,
    LogicalSourceMapping,
    RecordCategorySpec,
)
from cerev_mcp.cerev.provider import (
    ZetarisMcpEvidenceProvider,
    mcp_evidence_provider_from_environment,
)

__all__ = [
    "CerevMcpSourceConfig",
    "LogicalSourceMapping",
    "RecordCategorySpec",
    "InProcessMcpClient",
    "ZetarisMcpSourceAdapter",
    "create_cerev_mcp_adapters",
    "ZetarisMcpEvidenceProvider",
    "mcp_evidence_provider_from_environment",
]
