"""Pytest fixtures and deterministic test data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cerev_mcp.cache import QueryResultCache
from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.validator import SqlValidator

UPLOAD_DIR = Path(r"C:\Users\USER\Desktop\cerev\provisioning\zetaris\zetaris-upload")


@pytest.fixture
def mock_config() -> ZetarisServerConfig:
    return ZetarisServerConfig(
        base_url="https://zetaris.example.com",
        organization_id="1",
        api_key="mock_secret_key_12345",
        allowed_datasets=frozenset({"cerev_contract_records_v1", "cerev_phase124"}),
        allowed_tables=frozenset(
            {
                "cerev_contract_records_v1.cerev_contract_records_v1",
                "cerev_phase124.cerev_main",
                "cerev_phase124.cerev_releases",
                "cerev_phase124.cerev_contract_records",
            }
        ),
        max_rows=100,
        max_response_bytes=1024 * 1024,
        cache_size=50,
        cache_ttl_seconds=300,
    )


@pytest.fixture
def result_cache() -> QueryResultCache:
    return QueryResultCache(max_size=10, ttl_seconds=60)


@pytest.fixture
def sql_validator(mock_config: ZetarisServerConfig) -> SqlValidator:
    return SqlValidator(
        allowed_tables=mock_config.allowed_tables,
        max_rows=mock_config.max_rows,
    )


def load_canonical_transport_fixtures() -> dict[str, list[dict]]:
    """Load exact canonical CEREV transport envelopes uploaded to Zetaris."""
    contract_rows = json.loads(
        (UPLOAD_DIR / "contracts.transport-v1.json").read_text(encoding="utf-8")
    )
    cms_pol_rows = json.loads(
        (UPLOAD_DIR / "cms-policies.transport-v1.json").read_text(encoding="utf-8")
    )
    cms_cls_rows = json.loads(
        (UPLOAD_DIR / "cms-classifications.transport-v1.json").read_text(encoding="utf-8")
    )
    maint_rows = json.loads(
        (UPLOAD_DIR / "maintenance-events.transport-v1.json").read_text(encoding="utf-8")
    )
    notice_rows = json.loads(
        (UPLOAD_DIR / "notice-records.transport-v1.json").read_text(encoding="utf-8")
    )
    calc_rows = json.loads(
        (UPLOAD_DIR / "billing-calculations.transport-v1.json").read_text(encoding="utf-8")
    )
    action_rows = json.loads(
        (UPLOAD_DIR / "proposed-actions.transport-v1.json").read_text(encoding="utf-8")
    )
    release_rows = json.loads(
        (UPLOAD_DIR / "releases.transport-v1.json").read_text(encoding="utf-8")
    )

    main_rows = [
        *cms_cls_rows,
        *cms_pol_rows,
        *contract_rows,
        *calc_rows,
        *maint_rows,
        *notice_rows,
        *action_rows,
    ]

    return {
        "cerev_contract_records_v1.cerev_contract_records_v1": contract_rows,
        "cerev_phase124.cerev_main": main_rows,
        "cerev_phase124.cerev_releases": release_rows,
    }
