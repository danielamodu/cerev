"""CEREV MCP Client and five-source Evidence Adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from cerev.evidence import SourceCategory
from cerev.receipt import canonical_json_bytes
from cerev.source_adapters import (
    SourceIdentity,
    SourceRecord,
    SourceRetrieval,
    SourceRetrievalError,
)
from mcp.server.mcpserver import MCPServer

from cerev_mcp.cerev.config import CerevMcpSourceConfig, LogicalSourceMapping
from cerev_mcp.errors import ZetarisMcpError
from cerev_mcp.models import ExecuteQueryResult
from cerev_mcp.server import create_server

TRANSPORT_COLUMNS = frozenset(
    {
        "transport_schema_version",
        "logical_source_id",
        "source_system",
        "source_record_id",
        "record_category",
        "dataset_kind",
        "source_native_version",
        "payload_sha256",
        "payload_json",
    }
)

CONTRACT_TRANSPORT_SCHEMA = "cerev.contract_record.transport.v1"


class McpQueryCaller(Protocol):
    """Protocol for executing MCP queries."""

    async def execute_query(self, sql: str, limit: int | None = None) -> ExecuteQueryResult: ...


class InProcessMcpClient:
    """High-performance in-process MCP client calling MCPServer tools directly."""

    def __init__(self, server: MCPServer) -> None:
        self.server = server

    async def execute_query(self, sql: str, limit: int | None = None) -> ExecuteQueryResult:
        # Call tool through MCPServer tool dispatcher
        result = await self.server.call_tool("execute_query", {"sql": sql, "limit": limit})
        if result.is_error:
            error_msg = result.content[0].text if result.content else "Tool call failed"
            raise ZetarisMcpError("mcp_tool_error", error_msg)

        # In mcp 2.x, structured_content or content carries output
        payload = result.structured_content
        if payload is None and result.content:
            try:
                payload = json.loads(result.content[0].text)
            except Exception:
                pass
        if not isinstance(payload, dict):
            raise ZetarisMcpError(
                "invalid_mcp_output", "MCP execute_query did not return structured dictionary"
            )

        return ExecuteQueryResult.model_validate(payload)

    def execute_query_sync(self, sql: str, limit: int | None = None) -> ExecuteQueryResult:
        """Synchronous wrapper for integration into synchronous CEREV pipelines."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # In an existing loop, create a new thread or task
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, self.execute_query(sql, limit)).result()
        else:
            return asyncio.run(self.execute_query(sql, limit))


class ZetarisMcpSourceAdapter:
    """CEREV logical evidence source adapter backed by Zetaris MCP server."""

    def __init__(
        self,
        mapping: LogicalSourceMapping,
        client: InProcessMcpClient,
        *,
        relation_category_specs: Mapping[str, Mapping[str, tuple[str, str, int]]] | None = None,
    ) -> None:
        self.mapping = mapping
        self.client = client
        self.source_id = mapping.source_id
        self.source_system = mapping.source_system
        self.category = SourceCategory(mapping.category_name)
        self.relation = mapping.relation
        self.category_specs = mapping.category_specs

        # Specs dictionary for decoding multi-category shared relations
        self.relation_category_specs = {
            rel: dict(cats) for rel, cats in (relation_category_specs or {}).items()
        }
        if self.relation not in self.relation_category_specs:
            self.relation_category_specs[self.relation] = {
                spec.record_category: (self.source_id, self.source_system, spec.expected_count)
                for spec in self.category_specs
            }

    @property
    def identity(self) -> SourceIdentity:
        return SourceIdentity(self.source_id, self.source_system, self.category)

    @property
    def connection_metadata(self) -> Mapping[str, Any]:
        return {
            "backend": "cerev_mcp",
            "transport": "mcp_stdio_or_inprocess",
            "mode": "live",
            "registered_relations": (self.relation,),
            "source_revision_kind": "cerev_sha256_content_digest",
            "source_native_revision_available": False,
            "source_native_observation_timestamp_available": False,
            "simulated": False,
        }

    def retrieve(self) -> SourceRetrieval:
        """Execute MCP query, validate transport envelopes, and construct SourceRetrieval."""
        dataset: dict[str, Any] = {"source_system": self.source_system}
        records: list[SourceRecord] = []
        query_diagnostics: list[dict[str, Any]] = []

        expected_categories = self.relation_category_specs[self.relation]
        expected_relation_count = sum(count for _sid, _sys, count in expected_categories.values())

        fetch_limit = expected_relation_count + 1
        sql = f"SELECT * FROM {self.relation} ORDER BY source_record_id LIMIT {fetch_limit}"

        try:
            query_result = self.client.execute_query_sync(sql, limit=fetch_limit)
            query_diagnostics.append(
                {
                    "relation": self.relation,
                    "query_id": query_result.query_id,
                    "request_id": query_result.request_id,
                    "retrieved_at": query_result.executed_at,
                    "returned_record_count": query_result.row_count,
                    "total": query_result.total,
                    "timeUsed": query_result.time_used_ms,
                }
            )

            # Map raw header and row arrays to dictionaries
            headers = query_result.headers
            rows: list[dict[str, Any]] = []
            for row in query_result.rows:
                if len(row) != len(headers):
                    raise ZetarisMcpError(
                        "source_schema_invalid",
                        f"Row width {len(row)} does not match header length {len(headers)}.",
                    )
                rows.append(dict(zip(headers, row, strict=True)))

            if len(rows) != expected_relation_count:
                msg = (
                    f"Relation {self.relation} returned {len(rows)} rows; "
                    f"expected {expected_relation_count}."
                )
                raise ZetarisMcpError("source_schema_invalid", msg)

            # Categorize and validate rows
            category_records: dict[str, list[dict[str, Any]]] = {
                cat: [] for cat in expected_categories
            }
            seen_ids: set[tuple[str, str]] = set()

            for raw_row in rows:
                cat = raw_row.get("record_category")
                if not isinstance(cat, str) or cat not in expected_categories:
                    raise ZetarisMcpError(
                        "source_schema_invalid",
                        f"Relation {self.relation} returned unexpected category: {cat!r}",
                    )
                src_id, src_sys, _count = expected_categories[cat]
                schema_ver = (
                    CONTRACT_TRANSPORT_SCHEMA
                    if cat == "service_agreement"
                    else f"cerev.{cat}.transport.v1"
                )

                decoded_row = _decode_transport_envelope(
                    raw_row,
                    logical_source_id=src_id,
                    source_system=src_sys,
                    record_category=cat,
                    schema_version=schema_ver,
                )

                rec_id = decoded_row["source_record_id"]
                key = (cat, rec_id)
                if key in seen_ids:
                    raise ZetarisMcpError(
                        "duplicate_record", f"Duplicate source record ID: {rec_id}"
                    )
                seen_ids.add(key)

                category_records[cat].append(decoded_row)

            # Assign rows to each category spec in mapping
            for spec in self.category_specs:
                group_rows = category_records[spec.record_category]
                if len(group_rows) != spec.expected_count:
                    msg = (
                        f"Group {spec.group_name} returned {len(group_rows)} rows; "
                        f"expected {spec.expected_count}."
                    )
                    raise ZetarisMcpError("source_schema_invalid", msg)
                dataset[spec.group_name] = group_rows
                for row in group_rows:
                    records.append(
                        SourceRecord(
                            source_record_id=row["source_record_id"],
                            record_group=spec.group_name,
                            source_version=_extract_source_version(row),
                            payload=row,
                        )
                    )

        except ZetarisMcpError as error:
            diag_str = f" Query ID: {query_result.query_id}" if "query_result" in locals() else ""
            raise SourceRetrievalError(
                self.identity, error.code, f"{error.message}{diag_str}"
            ) from error
        except Exception as error:
            raise SourceRetrievalError(
                self.identity, "source_schema_invalid", f"MCP evidence retrieval failed: {error}"
            ) from error

        dataset["dataset_id"] = f"zetaris:{self.relation}"
        revision = hashlib.sha256(canonical_json_bytes(dataset)).hexdigest()

        metadata = dict(self.connection_metadata)
        if query_diagnostics:
            metadata["query_diagnostics"] = tuple(query_diagnostics)

        return SourceRetrieval(
            identity=self.identity,
            dataset=dataset,
            records=tuple(records),
            retrieved_at=datetime.now(UTC),
            source_revision=revision,
            connection_metadata=metadata,
            simulated=False,
        )


def _decode_transport_envelope(
    row: dict[str, Any],
    *,
    logical_source_id: str,
    source_system: str,
    record_category: str,
    schema_version: str,
) -> dict[str, Any]:
    """Strictly decode and validate one CEREV transport envelope."""
    if set(row) != TRANSPORT_COLUMNS:
        raise ZetarisMcpError("transport_schema_invalid", "Transport envelope column mismatch")

    for col in (
        "logical_source_id",
        "source_system",
        "source_record_id",
        "record_category",
        "dataset_kind",
    ):
        if not isinstance(row[col], str) or not row[col].strip():
            raise ZetarisMcpError(
                "transport_schema_invalid", f"Column '{col}' must be non-empty string"
            )

    if row["transport_schema_version"] != schema_version:
        raise ZetarisMcpError(
            "transport_schema_unsupported", f"Unsupported schema: {row['transport_schema_version']}"
        )

    if row["logical_source_id"] != logical_source_id:
        raise ZetarisMcpError("transport_metadata_mismatch", "logical_source_id mismatch")
    if row["source_system"] != source_system:
        raise ZetarisMcpError("transport_metadata_mismatch", "source_system mismatch")
    if row["record_category"] != record_category:
        raise ZetarisMcpError("transport_metadata_mismatch", "record_category mismatch")

    # Validate strict UTF-8 payload bytes & SHA256 digest
    payload_json = row.get("payload_json")
    if not isinstance(payload_json, str):
        raise ZetarisMcpError("transport_payload_invalid", "payload_json must be a string")

    payload_bytes = payload_json.encode("utf-8", errors="strict")
    actual_hash = hashlib.sha256(payload_bytes).hexdigest()
    if actual_hash != row["payload_sha256"]:
        raise ZetarisMcpError("transport_digest_mismatch", "payload_sha256 digest mismatch")

    # Strict JSON parse
    try:
        payload = json.loads(payload_bytes)
    except Exception as e:
        raise ZetarisMcpError("transport_payload_invalid", f"Invalid JSON payload: {e}") from e

    if not isinstance(payload, dict):
        raise ZetarisMcpError("transport_payload_invalid", "Payload root must be an object")

    # Check payload embedded identity
    if payload.get("source_record_id") != row["source_record_id"]:
        raise ZetarisMcpError("transport_metadata_mismatch", "Payload source_record_id mismatch")
    if payload.get("source_system") != row["source_system"]:
        raise ZetarisMcpError("transport_metadata_mismatch", "Payload source_system mismatch")

    return payload


def _extract_source_version(payload: Mapping[str, Any]) -> str | None:
    provenance = payload.get("provenance")
    if isinstance(provenance, Mapping):
        v = provenance.get("version_id")
        if isinstance(v, str) and v:
            return v
    for k in (
        "source_version",
        "version_id",
        "contract_version",
        "policy_version",
        "adapter_version",
    ):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def create_cerev_mcp_adapters(
    server: MCPServer | None = None,
    source_config: CerevMcpSourceConfig | None = None,
) -> tuple[ZetarisMcpSourceAdapter, ...]:
    """Construct all five CEREV source adapters backed by the Zetaris MCP server."""
    mcp_server = server or create_server()
    client = InProcessMcpClient(mcp_server)
    cfg = source_config or CerevMcpSourceConfig.from_environment()

    # Pre-calculate category relations for shared main table
    shared_categories = {
        "cms_policy": ("source-operational-cms", "operational_cms", 2),
        "cms_classification": ("source-operational-cms", "operational_cms", 4),
        "maintenance_event": (
            "source-maintenance-incident-registry",
            "maintenance_incident_registry",
            4,
        ),
        "notice_record": (
            "source-maintenance-incident-registry",
            "maintenance_incident_registry",
            3,
        ),
        "billing_calculation": (
            "source-billing-calculation-and-action",
            "billing_calculation_and_action",
            1,
        ),
        "proposed_action": (
            "source-billing-calculation-and-action",
            "billing_calculation_and_action",
            1,
        ),
        "service_agreement": ("source-contract-repository", "contract_repository", 1),
    }

    # For relations sharing cerev_main:
    main_relation = cfg.cms_mapping.relation
    relation_category_specs = {
        main_relation: shared_categories,
        cfg.release_mapping.relation: {
            "adapter_deployment": ("source-release-registry", "release_registry", 2),
        },
        cfg.contract_mapping.relation: {
            "service_agreement": ("source-contract-repository", "contract_repository", 1),
        },
    }

    adapters: list[ZetarisMcpSourceAdapter] = []
    for mapping in cfg.all_mappings:
        adapters.append(
            ZetarisMcpSourceAdapter(
                mapping=mapping,
                client=client,
                relation_category_specs=relation_category_specs,
            )
        )
    return tuple(adapters)
