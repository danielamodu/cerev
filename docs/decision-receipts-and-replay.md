# Decision Receipts and Replay

Phase 5 adds immutable decision receipts, canonical serialization, a local filesystem store, and deterministic replay. The receipt captures the Phase 4 result and the full evidence package snapshot used for that evaluation.

## Receipt structure

`cerev.receipt.evaluate_and_create_receipt(package)` runs the existing reference calculation, conflict analysis, and policy gate and stores their results without recomputing historical values. The resulting version `1.0` receipt includes:

- Stable receipt and decision IDs, customer and billing-period identity, and a timezone-aware UTC creation timestamp.
- Proposed action identity, type, integer-cent amount, currency, target, and the original immutable payload.
- Every evidence ID and source record reference, including source system, dataset, version, content hash, timestamp semantics, and package completeness metadata.
- A full embedded evidence snapshot plus its SHA-256 digest. This keeps the receipt self-describing; the store also retains a separate snapshot file alongside it.
- The independent reference-engine output, complete conflict report, policy version, gate disposition, explicit reasons, checks, and evaluation parameters.

The canonical case receipt records a $4,800 (480,000 cent) proposal, a $19,200 (1,920,000 cent) independent reference, the $14,400 difference, `BLOCK`, Contract V4, CMS V2, CreditCalc 2.1, Events A–D, related notices, and all four canonical conflict findings. The missing written-delivery proof for Events A–C is retained as an explicit limitation.

## Canonical serialization and hash

`canonical_json_bytes` normalizes mapping keys in sorted order, uses compact UTF-8 JSON with unescaped Unicode, and preserves list order where it reflects package or finding order. Evidence references are sorted by stable evidence ID. Date-only values remain ISO dates; timezone-aware instants normalize to UTC with six fractional digits and `Z`. `Decimal` serializes as a fixed decimal string, monetary values remain integer cents, and `null` is preserved. Receipt serializers emit a fixed set of fields; absent required fields are rejected rather than filled in with implicit defaults.

Receipt schema version `1.0` governs these rules. SHA-256 covers the complete receipt contents, including the declared hash algorithm, while excluding only `integrity.receipt_hash`. `verify_receipt` independently recomputes the digest from the contents and compares it with the stored digest. Parsing rejects duplicate JSON keys, floating-point numbers, non-finite values, malformed fields, and otherwise valid JSON that is not in canonical form. Binary floats are not accepted by the serializer because their representation can be ambiguous; use `Decimal` or integer cents.

A valid hash shows that the checked contents match the digest. It does not prove who created the receipt, authenticate upstream sources, or prevent an actor from changing contents and recomputing the digest. Signing and key management are not part of this phase.

## Local store and retained snapshots

`ReceiptStore` writes each receipt under its stable ID in a dedicated directory containing `receipt.json` and `evidence_snapshot.json`. It creates files with temporary sibling files and atomic replacement, and creates the receipt directory exclusively so a duplicate ID cannot overwrite existing data. Loading checks canonical encodings, receipt hash, snapshot canonical encoding, snapshot digest, and equality with the embedded snapshot. Missing, partial, or corrupted files raise a clear `ReceiptStoreError`.

## Replay

`replay_decision(receipt, retained_evidence_snapshot)` first verifies the receipt hash and retained snapshot digest. It reconstructs temporary source datasets solely from the pinned snapshot, checks every rebuilt evidence record hash, reruns the independent reference calculation, conflict detectors, and policy gate, and compares their outputs with the recorded decision. It does not read or fetch current repository fixture state. The temporary reconstruction is discarded after replay.

Replay status is:

- `REPRODUCED`: receipt and evidence integrity pass, and reference results, conflict report, policy checks, disposition, and evaluated inputs match exactly.
- `DIFFERENT`: replay runs, but a result, input, or policy version differs.
- `UNVERIFIABLE`: receipt or required evidence snapshot is absent, malformed, altered, or cannot be reconstructed safely.

A modified snapshot fails its aggregate snapshot hash check and includes per-record hash diagnostics when those records can still be reconstructed. A missing snapshot never counts as a successful replay. Replay under a policy version different from the recorded version is reported as different, not exact; only the repository's implemented `cerev-policy-v1` gate behavior is available.

## Boundaries

A receipt preserves what CEREV evaluated and makes later comparison possible. It does not prove that source data was truthful, complete upstream, globally consistent at one point in time, or delivered in a particular way when the source left that fact unspecified. It is not a signature, permit, or execution authorization artifact. Digital signatures, key management, execution permits, external persistence, and action execution are deferred beyond Phase 5.
