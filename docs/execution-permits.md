# Cryptographic execution permits

Phase 6 adds signed, short-lived authorizations for one exact CEREV action. It does not execute actions or change the reference engine, conflict detector, policy gate, or receipt format.

## Structure and signing payload

An `ExecutionPermit` binds:

- The permit ID, decision ID, receipt ID and recomputed receipt hash.
- The accepted policy version and original `PERMIT` disposition.
- The action ID and type, customer/account reference, integer-cent amount, ISO currency, destination, transaction reference, idempotency key, and complete original proposed-action source payload.
- The evidence package ID, SHA-256 fingerprint of canonical retained evidence, contract version, and source versions.
- UTC issued-at and expiry timestamps, a random nonce, key ID, algorithm identifier, and Ed25519 signature.

The signed payload has schema version `1.0` and canonical UTF-8 JSON encoding: sorted object keys, compact separators, no floating-point values, monetary values represented as integers, and timestamps rendered in UTC with microseconds and a `Z` suffix. The signature field is excluded; key ID and algorithm are included. Verification reconstructs this payload from the permit fields and does not accept a supplied payload digest.

## Issuance and verification

`issue_execution_permit` requires a hash-valid receipt, its retained snapshot, an explicit set of accepted policy versions, the in-memory Ed25519 signing key, and action claims. It verifies the receipt and asks Phase 5 replay to reproduce the recorded `PERMIT` from the retained snapshot. It rejects `BLOCK` and `ABSTAIN`, unresolved blocking conflicts, a stale or future-dated decision, modified snapshot data, unaccepted policy versions, and any action terms that differ from the receipt's original source payload. Permit validity is positive and capped at 15 minutes; the default is 5 minutes.

`verify_execution_permit` requires trusted public keys, the actual receipt, and retained evidence. It checks the signature, derives the trusted key identity from raw public-key bytes, validates algorithm/schema, timestamps and validity window, recomputes receipt and evidence bindings, checks policy and action matches, and reruns deterministic replay. Expiry is exclusive: a permit fails when verification time is at or after `expires_at`. Time-window decisions use `timedelta` comparisons.

## Key and lifecycle limits

The runtime dependency is `cryptography`, a maintained Python cryptographic library; Ed25519 is provided by its asymmetric-cryptography API. `generate_signing_key()` generates an in-memory key suitable for development and tests. Public keys can be stored as raw 32-byte keys encoded with canonical unpadded base64url. Private keys are not serialized by this module and must never be put in receipts, evidence snapshots, permits, fixtures, or source control. Production signing requires a separately designed key custody, rotation, access-control, and recovery process; this phase does not provide one.

Permits are immutable values. Editing any signed field invalidates signature verification. The random nonce only provides a unique authorization identifier. This phase has no nonce-consumption store, concurrency-safe single-use enforcement, protected execution boundary, or revocation registry. Those controls belong in a future protected-execution phase. Signature verification proves that the trusted key signed the canonical claims and that supplied receipt/evidence reproduce the decision; it does not prove external sources are truthful, guarantee action execution, prevent replay of a still-valid permit, or establish that a downstream executor enforces the claims.

## Current scope

The canonical $4,800 proposed credit remains `BLOCK` and cannot receive a permit. Tests use a separate hypothetical action with complete contractual evidence to exercise successful `PERMIT` issuance and verification. No Zetaris, Nemotron, PostgreSQL, LLM, UI, revocation, or execution integration is included.
