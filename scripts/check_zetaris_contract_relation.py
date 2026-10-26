"""Read and validate the single live CEREV contract relation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cerev.source_adapters import SourceRetrievalError
from cerev.zetaris import (
    RecordGroupBinding,
    ZetarisClientError,
    ZetarisConfig,
    ZetarisContractRepositoryAdapter,
    ZetarisGatewayProxyAdapter,
)

ROOT = Path(__file__).resolve().parents[1]
RELATION = "cerev_phase124.cerev_contract_records"
UPLOAD_FILE = ROOT / "provisioning/zetaris/zetaris-upload/contracts.json"
EXPECTED_ROW_ID = "contract-client-a-v4"


def _same_json_types(actual: Any, expected: Any, path: str = "row") -> None:
    """Require structural and scalar type equality, including int versus bool."""
    if type(actual) is not type(expected):
        raise ValueError(
            f"{path} has type {type(actual).__name__}; expected {type(expected).__name__}."
        )
    if isinstance(expected, dict):
        missing = sorted(set(expected) - set(actual))
        if missing:
            raise ValueError(f"{path} is missing required fields: {', '.join(missing)}.")
        for key, expected_value in expected.items():
            _same_json_types(actual[key], expected_value, f"{path}.{key}")
    elif isinstance(expected, list):
        if len(actual) != len(expected):
            raise ValueError(f"{path} has {len(actual)} items; expected {len(expected)}.")
        for index, (actual_value, expected_value) in enumerate(zip(actual, expected, strict=True)):
            _same_json_types(actual_value, expected_value, f"{path}[{index}]")
    elif actual != expected:
        raise ValueError(f"{path} does not match the canonical simulation fixture value.")


def _expected_record() -> dict[str, Any]:
    uploaded = json.loads(UPLOAD_FILE.read_text(encoding="utf-8"))
    if not isinstance(uploaded, list) or len(uploaded) != 1 or not isinstance(uploaded[0], dict):
        raise ValueError("contracts.json must contain exactly one JSON object.")
    return uploaded[0]


def main() -> int:
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
    if os.environ.get("CEREV_EVIDENCE_MODE") != "zetaris":
        print("FAILED: CEREV_EVIDENCE_MODE must explicitly be 'zetaris'.")
        return 2

    relation = os.environ.get("CEREV_ZETARIS_CONTRACT_RECORDS_TABLE", "").strip()
    if relation != RELATION:
        print(f"FAILED: contract relation must be configured as {RELATION}.")
        return 2

    try:
        config = ZetarisConfig.from_environment()
        adapter = ZetarisContractRepositoryAdapter(
            ZetarisGatewayProxyAdapter(config),
            config,
            (RecordGroupBinding("records", relation, expected_count=1),),
        )
        retrieval = adapter.retrieve()
        expected = _expected_record()
        if len(retrieval.records) != 1:
            raise ValueError(f"Adapter returned {len(retrieval.records)} records; expected one.")
        actual = retrieval.dataset["records"][0]
        _same_json_types(actual, expected)
        required = {
            "source_record_id": EXPECTED_ROW_ID,
            "source_system": "contract_repository",
            "source_native_version": "V4",
            "fixture_record_id": EXPECTED_ROW_ID,
            "dataset_kind": "canonical_simulation_fixture",
            "contract_version": "V4",
            "source_native_created_at": None,
            "source_native_observed_at": None,
        }
        for name, value in required.items():
            if name not in actual or type(actual[name]) is not type(value) or actual[name] != value:
                raise ValueError(f"Required field {name!r} is missing or incompatible.")

        diagnostics = retrieval.connection_metadata.get("query_diagnostics", ())
        if len(diagnostics) != 1:
            raise ValueError("Adapter did not retain exactly one query diagnostic.")
        diagnostic = diagnostics[0]
        report = {
            "status": "PASS",
            "mode": "zetaris",
            "relation": relation,
            "sql": f"SELECT * FROM {relation} ORDER BY source_record_id LIMIT 2",
            "http_status": diagnostic["http_status"],
            "request_id": diagnostic["request_id"],
            "retrieved_at": diagnostic["retrieved_at"],
            "row_count": len(retrieval.records),
            "source_id": retrieval.identity.source_id,
            "source_system": actual["source_system"],
            "source_record_id": actual["source_record_id"],
            "source_version": retrieval.records[0].source_version,
            "dataset_kind": actual["dataset_kind"],
            "nested_fields": {
                name: actual[name]
                for name in (
                    "effective_period",
                    "provenance",
                    "scheduled_maintenance_exclusion",
                    "sla",
                )
            },
            "source_native_created_at": actual["source_native_created_at"],
            "source_native_observed_at": actual["source_native_observed_at"],
            "simulation_fixture_match": True,
            "simulated_source_flag": retrieval.simulated,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ZetarisClientError,
        SourceRetrievalError,
    ) as error:
        details: dict[str, Any] = {"status": "FAIL", "error": str(error)}
        if isinstance(error, ZetarisClientError):
            details["error_code"] = error.code
            details["http_status"] = error.http_status
            details["request_id"] = error.request_id
        if isinstance(error, SourceRetrievalError):
            details["source_id"] = error.identity.source_id
            details["error_code"] = error.code
        print(json.dumps(details, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
