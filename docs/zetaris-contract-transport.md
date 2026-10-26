# Zetaris contract transport integration

## Status

CEREV's contract adapter supports the verified, read-only versioned transport
relation `cerev_contract_records_v1.cerev_contract_records_v1`. Its envelope
schema is `cerev.contract_record.transport.v1`. The transport wraps the original
canonical contract JSONL bytes in `payload_json` and carries a SHA-256 digest.
The standalone verifier remains a separate diagnostic; the production adapter
now independently validates and reconstructs the envelope before handing the
record to CEREV's evidence validation.

Only this one logical source has passed live transport verification. The other
four logical source adapters and their seven relations remain independently
configured and unverified. Full live `EvidencePackage` collection, the live
policy decision, and live receipt replay are not claimed.

## Configuration

Use the existing live mode and Gateway credentials in the ignored `.env`:

```dotenv
CEREV_EVIDENCE_MODE=zetaris
CEREV_ZETARIS_CONTRACT_RECORDS_TABLE=cerev_contract_records_v1.cerev_contract_records_v1
```

The adapter uses the existing `ZetarisConfig` and
`ZetarisGatewayProxyAdapter`. The Gateway origin, organization ID, API key, and
bounded timeout are handled by that established configuration. No credentials
belong in source control. The relation remains configurable under the same
environment-variable convention; a live-mode failure never falls back to the
older inferred relation or local sample data.

Run the opt-in, read-only, contract-only check with:

```powershell
python scripts/check_zetaris_contract_source.py
```

The script queries `SELECT * FROM cerev_contract_records_v1.cerev_contract_records_v1
ORDER BY source_record_id LIMIT 2`, reconstructs the row through the same
contract adapter used by the collector, and calls the existing per-source
canonical evidence validator. It prints safe request diagnostics and evidence
identity/hash metadata, not the contract payload or credentials. Its result
does not claim a complete package or decision.

## Envelope validation

The contract adapter accepts exactly these envelope columns:

`transport_schema_version`, `logical_source_id`, `source_system`,
`source_record_id`, `record_category`, `dataset_kind`,
`source_native_version`, `payload_sha256`, and `payload_json`.

It requires the supported schema version and the expected canonical contract
identity (`source-contract-repository`, `contract_repository`,
`contract-client-a-v4`, `service_agreement`,
`canonical_simulation_fixture`, `V4`). It requires string-valued envelope
fields, hashes the strict UTF-8 payload bytes, rejects an invalid digest,
strictly parses JSON, rejects duplicate keys, non-standard constants, and
floating-point JSON numbers, then checks the reconstructed identity/version
against the envelope. The transport row must match the one-record group count.
The decoder does not coerce values, drop nulls, or decode arbitrary strings.

The reconstructed object is passed unmodified into the existing contract
evidence rules. Those rules validate source identity, record identity,
provenance, record group, version, and timestamp semantics and then compute the
ordinary evidence content hash. The canonical evidence model and the reference
engine are unchanged.

## Failure and collection behavior

Transport failures are structured source retrieval failures. Missing relations,
HTTP/network errors, malformed or incomplete envelopes, digest mismatches,
metadata disagreement, strict-JSON failures, wrong row counts, and ordinary
evidence validation failures do not produce contract evidence. Zetaris mode
never switches to simulation data. The five-source collector remains
all-or-nothing: if any of its sources is unavailable or invalid, it returns no
`EvidencePackage`.

The independent contract diagnostic validates one source only. It deliberately
does not assemble the other four fixture datasets to make a partial live result
look complete. The new `evidence_records_for_source_dataset` helper is the same
per-source validation used by the full package loader, not a relaxed path.

## Provenance and limits

The reconstructed record preserves original source IDs, source system, V4
version, nested objects/arrays, scalar types, and distinctions between absent
and explicit-null fields. Zetaris transport metadata is not represented as a
fabricated source-native revision or observation timestamp. The adapter retains
Gateway retrieval diagnostics and its own retrieval timestamp according to the
existing `SourceRetrieval` convention. No global cross-source snapshot is
claimed.

The relation carries `canonical_simulation_fixture`: this record is a canonical
simulation artifact, not live production contract evidence. This phase uses a
read-only Gateway query and does not write to or alter any Zetaris object. Live
acceptance still requires registering and validating the other four logical
sources, then collecting and evaluating the complete package and replaying its
receipt.
