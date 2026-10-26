"""Opt-in read-only live validation of the versioned Zetaris contract source."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cerev.evidence import EvidencePackageError, evidence_records_for_source_dataset
from cerev.source_adapters import SourceRetrievalError
from cerev.zetaris import (
    RecordGroupBinding,
    ZetarisClientError,
    ZetarisConfig,
    ZetarisContractRepositoryAdapter,
    ZetarisGatewayProxyAdapter,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RELATION = "cerev_contract_records_v1.cerev_contract_records_v1"


def _safe_error(error: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {"error_type": type(error).__name__}
    if isinstance(error, (ZetarisClientError, SourceRetrievalError)):
        result["error_code"] = error.code
        if isinstance(error, ZetarisClientError):
            result["http_status"] = error.http_status
            result["request_id"] = error.request_id
        else:
            result["source_id"] = error.identity.source_id
    else:
        result["error"] = str(error)
    return result


def main() -> int:
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
    if os.environ.get("CEREV_EVIDENCE_MODE") != "zetaris":
        print(json.dumps({"status": "FAIL", "error": "Explicit Zetaris mode is required."}))
        return 2
    relation = os.environ.get("CEREV_ZETARIS_CONTRACT_RECORDS_TABLE", "").strip()
    if relation != EXPECTED_RELATION:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error": f"Configure the contract relation as {EXPECTED_RELATION}.",
                }
            )
        )
        return 2

    try:
        config = ZetarisConfig.from_environment()
        adapter = ZetarisContractRepositoryAdapter(
            ZetarisGatewayProxyAdapter(config),
            config,
            (RecordGroupBinding("records", relation, expected_count=1),),
        )
        retrieval = adapter.retrieve()
        records = evidence_records_for_source_dataset(
            retrieval.identity.source_id,
            {
                "source_system": retrieval.dataset["source_system"],
                "dataset_id": retrieval.dataset["dataset_id"],
                "records": retrieval.dataset["records"],
            },
        )
        if len(records) != 1:
            raise EvidencePackageError("Expected exactly one validated contract evidence record.")
        diagnostics = retrieval.connection_metadata.get("query_diagnostics", ())
        diagnostic = diagnostics[0] if diagnostics else {}
        evidence = records[0]
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "mode": "zetaris",
                    "relation": relation,
                    "sql": f"SELECT * FROM {relation} ORDER BY source_record_id LIMIT 2",
                    "http_status": diagnostic.get("http_status"),
                    "request_id": diagnostic.get("request_id"),
                    "retrieved_at": diagnostic.get("retrieved_at"),
                    "source_id": retrieval.identity.source_id,
                    "source_system": evidence.source_system,
                    "source_record_id": evidence.source_record_id,
                    "source_version": evidence.version_id,
                    "record_category": evidence.record_type,
                    "dataset_kind": evidence.payload.get("dataset_kind"),
                    "payload_content_hash": evidence.content_sha256,
                    "validated_evidence_records": len(records),
                    "evidence_package_complete": False,
                    "simulated_source": retrieval.simulated,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ZetarisClientError,
        SourceRetrievalError,
        EvidencePackageError,
    ) as error:
        print(json.dumps({"status": "FAIL", **_safe_error(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
