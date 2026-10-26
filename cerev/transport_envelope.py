"""Byte-preserving primitives for versioned canonical-record transport envelopes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

TRANSPORT_METADATA_FIELDS = (
    "logical_source_id",
    "source_system",
    "source_record_id",
    "record_category",
    "dataset_kind",
    "source_native_version",
)


def build_transport_envelope(
    payload_bytes: bytes,
    *,
    schema_version: str,
    metadata: Mapping[str, str | None],
) -> dict[str, str | None]:
    """Create the shared envelope shape without parsing or reserializing its payload."""
    if not isinstance(payload_bytes, bytes) or not payload_bytes:
        raise ValueError("Transport payload must be non-empty bytes.")
    if b"\n" in payload_bytes or b"\r" in payload_bytes:
        raise ValueError("Transport payload must contain exactly one JSONL record line.")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise ValueError("Transport schema version must be non-empty text.")
    if set(metadata) != set(TRANSPORT_METADATA_FIELDS):
        raise ValueError("Transport metadata fields do not match the shared envelope contract.")
    for name in TRANSPORT_METADATA_FIELDS[:-1]:
        value = metadata[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Transport metadata {name!r} must be non-empty text.")
    source_version = metadata["source_native_version"]
    if source_version is not None and not isinstance(source_version, str):
        raise ValueError("Transport source_native_version must be text or null.")

    payload_json = payload_bytes.decode("utf-8", errors="strict")
    return {
        "transport_schema_version": schema_version,
        **metadata,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "payload_json": payload_json,
    }


def serialize_transport_envelopes(envelopes: Sequence[Mapping[str, Any]]) -> bytes:
    """Serialize a deterministic UTF-8 JSON array of envelopes with a final LF."""
    if not envelopes:
        raise ValueError("At least one transport envelope is required.")
    return (
        json.dumps(list(envelopes), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8", errors="strict")
