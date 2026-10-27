"""CEREV EvidenceProvider implementation for Zetaris MCP."""

from __future__ import annotations

from collections.abc import Mapping

from cerev.evidence_provider import (
    CollectedEvidenceProvider,
)
from cerev.source_adapters import EvidenceCollector
from mcp.server.mcpserver import MCPServer

from cerev_mcp.cerev.adapter import create_cerev_mcp_adapters
from cerev_mcp.cerev.config import CerevMcpSourceConfig
from cerev_mcp.server import create_server


class ZetarisMcpEvidenceProvider(CollectedEvidenceProvider):
    """CEREV EvidenceProvider backed by Zetaris MCP server."""

    def __init__(self, collector: EvidenceCollector) -> None:
        super().__init__(
            collector,
            provider_id="cerev-mcp-live",
            backend="cerev_mcp",
        )


def mcp_evidence_provider_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    server: MCPServer | None = None,
) -> ZetarisMcpEvidenceProvider:
    """Build an EvidenceProvider backed by Zetaris MCP from environment settings."""
    mcp_server = server or create_server()
    source_cfg = CerevMcpSourceConfig.from_environment(environ)
    adapters = create_cerev_mcp_adapters(mcp_server, source_cfg)
    collector = EvidenceCollector(adapters)
    return ZetarisMcpEvidenceProvider(collector)
