# Evidence Architecture

`cerev.evidence` converts the five canonical Phase 1 JSON datasets into an `EvidencePackage`. It records what each source supplied and preserves the original source record values. It does not decide whether independently supplied claims agree or which source is correct outside its own domain.

## Evidence model

- `EvidenceSource` identifies a source system, category, dataset, scoped authority, limitations, and versions observed in the dataset.
- `EvidenceRecord` wraps one original source record with a deterministic evidence ID, source and record identity, category, record type, available version, timestamp annotations, scope notes, original payload, and SHA-256 content digest.
- `EvidenceTimestamp` retains a source field path, timestamp semantic, parsed value, and precision. Values are a timezone-aware `datetime`, a date-only `date`, or `None` when the source field is explicitly unavailable.
- `EvidencePackage` includes customer and billing-period context, ordered source inventory and records, collection metadata, and the Phase 2 `ReferenceCalculation` result.

The loader includes 18 records from five datasets: one contract, six CMS policy/classification records, four events and three notices, two release records, and one billing calculation plus one proposed action. Stable evidence IDs use `evidence:{source_id}:{source_record_id}`. The package ID identifies the customer and billing month. `package_created_at` is the actual UTC time the package is constructed. Package and record ordering follows the canonical dataset and list order.

The payload is recursively made read-only in memory. `to_dict()` returns a JSON-friendly copy and retains original payload values, including source timestamp strings and explicit `null` values.

## Source inventory and authority

| Source | Authoritative for | Does not establish |
|---|---|---|
| Contract repository | Contractual terms stored in the record | Whether operational events occurred or notice evidence exists |
| Operational CMS | Its own policy versions and event classifications | Contract amendments or contractual notice compliance |
| Maintenance and incident registry | Its recorded events and notice entries | CMS classification or contractual exclusion eligibility |
| Release registry | Its recorded adapter deployments and behavior | Contract interpretation or calculation correctness |
| Billing calculation and action source | Its own calculation and proposed action | The amount actually owed or whether an action was authorized or executed |

Authority is domain-specific. A source record preserves what its source recorded; it does not make the source authoritative for facts outside that domain. The package does not adjudicate inconsistencies.

## Provenance and timestamps

Every record keeps its source system, dataset ID, source record ID, category, record type, version when supplied, and original payload. A mismatch between the dataset identity and a present record provenance identity is rejected.

Timestamp semantics are explicit: event occurrence, notice issuance, CMS classification record time, contract or policy effective time, adapter deployment, billing-period boundary, calculation, action proposal, source record creation, and evidence observation/retrieval. A timestamp with an instant must include a timezone offset. Date-only source values remain `date` values and are not assigned a fabricated time or offset. Null timestamps remain unavailable, and timestamp roles are not globally ordered.

The fixture provides no source retrieval/observation timestamps. `package_created_at` is the actual package construction time; it is not a source observation time. Collection metadata says upstream source completeness is unknown and does not claim a globally consistent point-in-time snapshot. Completeness only means all records in the five expected local fixture datasets were loaded.

## Content hash

Each evidence record has a `content_sha256` digest. The engine forms a JSON object containing the stable evidence ID; source ID, source system and dataset ID; source record ID; category, record type and version; record-scope annotations; parsed timestamp semantics and values; and the complete original payload. It serializes this object as UTF-8 JSON with recursively sorted object keys, no insignificant whitespace, unescaped Unicode, and non-finite JSON numbers rejected, then computes SHA-256. The record digest itself and volatile package-collection metadata are excluded.

The digest is deterministic for the same represented record and changes when covered identity or content changes. It is an integrity checksum only: it is not a signature, does not authenticate the source, and does not prevent an actor from changing content and recomputing the digest. No signing or key management is included.

## Reference result and boundaries

The package contains the existing Phase 2 reference calculation result. The evidence loader calls that engine in one direction; `cerev.reference` remains independently usable and does not import or depend on the evidence package.

The evidence layer deliberately does not perform cross-source conflict detection, legal interpretation beyond the reference engine's existing Contract V4 calculation, policy authorization, receipt generation, signing, permit issuance, or action execution. It also does not claim source completeness or point-in-time consistency beyond the local fixture's known contents.
