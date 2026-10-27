"""Validated configuration model for the Zetaris MCP server."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_ORIGIN_PATTERN = re.compile(r"^https://[^/?#]+$")


@dataclass(frozen=True, slots=True)
class ZetarisServerConfig:
    """Configuration for Zetaris Gateway communication and security boundaries."""

    base_url: str
    organization_id: str
    api_key: str = field(repr=False)
    engine_id: str | None = None
    timeout_seconds: float = 30.0
    allowed_datasets: frozenset[str] = field(default_factory=frozenset)
    allowed_tables: frozenset[str] = field(default_factory=frozenset)
    max_rows: int = 100
    max_response_bytes: int = 5 * 1024 * 1024
    cache_size: int = 100
    cache_ttl_seconds: int = 1800

    def __post_init__(self) -> None:
        if not _ORIGIN_PATTERN.fullmatch(self.base_url):
            raise ValueError(
                "ZETARIS_BASE_URL must be a valid HTTPS origin without path or trailing slash."
            )
        if not self.organization_id.isdecimal():
            raise ValueError("ZETARIS_ORG_ID must be a numeric string.")
        if not self.api_key.strip():
            raise ValueError("ZETARIS_API_KEY must not be blank.")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("ZETARIS_TIMEOUT_SECONDS must be between 0 and 300.")
        if not 1 <= self.max_rows <= 1000:
            raise ValueError("ZETARIS_MCP_MAX_ROWS must be between 1 and 1000.")
        if not 1024 <= self.max_response_bytes <= 50 * 1024 * 1024:
            raise ValueError("ZETARIS_MCP_MAX_RESPONSE_BYTES must be between 1 KB and 50 MB.")
        if not 1 <= self.cache_size <= 10000:
            raise ValueError("ZETARIS_MCP_CACHE_SIZE must be between 1 and 10000.")
        if not 1 <= self.cache_ttl_seconds <= 86400:
            raise ValueError("ZETARIS_MCP_CACHE_TTL_SECONDS must be between 1 and 86400.")

    def is_dataset_allowed(self, dataset: str) -> bool:
        """Check if a dataset/schema name is allowed."""
        if not self.allowed_datasets:
            return True  # If empty, all datasets are permitted
        return dataset.casefold() in {d.casefold() for d in self.allowed_datasets}

    def is_table_allowed(self, table: str) -> bool:
        """Check if a qualified table identifier is in the allowlist."""
        if not self.allowed_tables:
            return True  # If empty, allowlist check deferred or open
        return table.casefold() in {t.casefold() for t in self.allowed_tables}

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        env_file: Path | str | None = None,
    ) -> ZetarisServerConfig:
        """Construct from environment or .env file with strict validation."""
        if environ is None:
            if env_file:
                load_dotenv(dotenv_path=env_file, override=False)
            else:
                # Check current dir or parent dirs for .env
                local_env = Path.cwd() / ".env"
                if local_env.is_file():
                    load_dotenv(dotenv_path=local_env, override=False)
                else:
                    load_dotenv(override=False)
            values = os.environ
        else:
            values = environ

        base_url = values.get("ZETARIS_BASE_URL", "").rstrip("/")
        org_id = values.get("ZETARIS_ORG_ID", "").strip()
        api_key = values.get("ZETARIS_API_KEY", "").strip()

        missing: list[str] = []
        if not base_url:
            missing.append("ZETARIS_BASE_URL")
        if not org_id:
            missing.append("ZETARIS_ORG_ID")
        if not api_key:
            missing.append("ZETARIS_API_KEY")
        if missing:
            raise ValueError(f"Missing required Zetaris configuration: {', '.join(missing)}")

        engine_id = values.get("ZETARIS_ENGINE_ID", "").strip() or None

        raw_timeout = values.get("ZETARIS_TIMEOUT_SECONDS", "30.0")
        try:
            timeout = float(raw_timeout)
        except ValueError as e:
            raise ValueError(f"Invalid ZETARIS_TIMEOUT_SECONDS: {raw_timeout}") from e

        # Parse allowlists
        raw_datasets = values.get("ZETARIS_MCP_ALLOWED_DATASETS", "")
        allowed_datasets = frozenset(
            d.strip().casefold() for d in raw_datasets.split(",") if d.strip()
        )

        raw_tables = values.get("ZETARIS_MCP_ALLOWED_TABLES", "")
        allowed_tables = frozenset(t.strip().casefold() for t in raw_tables.split(",") if t.strip())

        # Default fallback: if allowed_tables not explicitly set, populate from CEREV tables
        if not allowed_tables:
            cerev_tables = [
                values.get("CEREV_ZETARIS_CONTRACT_RECORDS_TABLE", ""),
                values.get("CEREV_ZETARIS_CMS_POLICIES_TABLE", ""),
                values.get("CEREV_ZETARIS_CMS_CLASSIFICATIONS_TABLE", ""),
                values.get("CEREV_ZETARIS_MAINTENANCE_EVENTS_TABLE", ""),
                values.get("CEREV_ZETARIS_NOTICE_RECORDS_TABLE", ""),
                values.get("CEREV_ZETARIS_RELEASES_TABLE", ""),
                values.get("CEREV_ZETARIS_BILLING_CALCULATIONS_TABLE", ""),
                values.get("CEREV_ZETARIS_PROPOSED_ACTIONS_TABLE", ""),
            ]
            allowed_tables = frozenset(t.strip().casefold() for t in cerev_tables if t.strip())

        # If allowed_datasets not explicitly set, derive from allowed_tables
        if not allowed_datasets and allowed_tables:
            derived_datasets = set()
            for t in allowed_tables:
                parts = t.split(".")
                if len(parts) >= 2:
                    derived_datasets.add(parts[0].casefold())
            allowed_datasets = frozenset(derived_datasets)

        raw_max_rows = values.get("ZETARIS_MCP_MAX_ROWS", "100")
        try:
            max_rows = int(raw_max_rows)
        except ValueError as e:
            raise ValueError(f"Invalid ZETARIS_MCP_MAX_ROWS: {raw_max_rows}") from e

        raw_max_bytes = values.get("ZETARIS_MCP_MAX_RESPONSE_BYTES", str(5 * 1024 * 1024))
        try:
            max_bytes = int(raw_max_bytes)
        except ValueError as e:
            raise ValueError(f"Invalid ZETARIS_MCP_MAX_RESPONSE_BYTES: {raw_max_bytes}") from e

        raw_cache_size = values.get("ZETARIS_MCP_CACHE_SIZE", "100")
        try:
            cache_size = int(raw_cache_size)
        except ValueError as e:
            raise ValueError(f"Invalid ZETARIS_MCP_CACHE_SIZE: {raw_cache_size}") from e

        raw_cache_ttl = values.get("ZETARIS_MCP_CACHE_TTL_SECONDS", "1800")
        try:
            cache_ttl = int(raw_cache_ttl)
        except ValueError as e:
            raise ValueError(f"Invalid ZETARIS_MCP_CACHE_TTL_SECONDS: {raw_cache_ttl}") from e

        return cls(
            base_url=base_url,
            organization_id=org_id,
            api_key=api_key,
            engine_id=engine_id,
            timeout_seconds=timeout,
            allowed_datasets=allowed_datasets,
            allowed_tables=allowed_tables,
            max_rows=max_rows,
            max_response_bytes=max_bytes,
            cache_size=cache_size,
            cache_ttl_seconds=cache_ttl,
        )
