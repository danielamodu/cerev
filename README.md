# CEREV

CEREV is a pre-execution evidence integrity layer for autonomous agents. It helps establish whether evidence supporting a proposed action is traceable and acceptable under an explicit policy before execution is permitted.

## Architectural thesis

An agent should not move directly from retrieved information to an external action. CEREV is designed as a boundary in that path: evidence is gathered and checked, policy is evaluated against that evidence, and an auditable disposition is produced before a downstream system acts. Evidence provenance and deterministic replay are intended to make decisions inspectable later.

## Current status

The repository has completed **Phase 0: Repository Foundation**, **Phase 1: Canonical Evidence Fixture**, **Phase 2: Deterministic Reference Engine**, **Phase 3: Evidence Architecture**, **Phase 4: Conflict Detection and Policy Gate**, **Phase 5: Decision Receipts and Replay**, and **Phase 6: Cryptographic Execution Permits**. It includes an independent Client A SLA reference calculation, a source-preserving evidence package for the five canonical datasets, deterministic cross-source conflict findings, and a separate fail-closed policy gate. The canonical proposed $4,800 credit remains blocked against the independent $19,200 reference and cannot receive a permit. A separate hypothetical fully evidenced `PERMIT` scenario exercises the permit infrastructure.

Phase 6 permits use Ed25519 signatures over canonical, action-specific claims and require successful receipt/evidence replay. The implementation does not execute actions, provide production key custody, maintain a revocation registry, or enforce nonce consumption/single-use. Receipts and signatures do not authenticate the truthfulness of upstream sources. No database, LLM, Zetaris, Nemotron, or PostgreSQL integration is included.

## Development

Requirements: Python 3.12 or newer.

Create and activate a virtual environment, then install the package with its development tools:

```sh
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the project checks:

```sh
pytest
ruff check .
ruff format --check .
```

The runtime dependency is `cryptography` for Ed25519; the package does not read environment variables. Never put credentials or private keys in the repository.

## Module map

- `cerev/reference.py`: deterministic Contract V4 SLA reference calculation.
- `cerev/evidence.py`: source records, timestamp semantics, provenance, and evidence packages.
- `cerev/conflicts.py`: deterministic cross-source findings linked to evidence.
- `cerev/policy.py`: independent `PERMIT`/`BLOCK`/`ABSTAIN` policy evaluation.
- `cerev/receipt.py`: canonical immutable decision receipts and local JSON storage.
- `cerev/replay.py`: replay from retained evidence snapshots and result comparison.
- `cerev/permit.py`: short-lived Ed25519 permits bound to a verified decision and exact action.
- `cerev/config.py`, `cerev/v21_simulation.py`, `cerev/validation.py`, `cerev/nonce.py`, `cerev/retrieval_direct.py`, and `cerev/adapter.py`: reserved modules for future phases.

The `fixture/` directory contains the Phase 1 datasets and guide. `mock_services/` and `demo/` remain available for later phases. See [docs/conflict-detection-and-policy.md](docs/conflict-detection-and-policy.md) for Phase 4 behavior, [docs/decision-receipts-and-replay.md](docs/decision-receipts-and-replay.md) for Phase 5, and [docs/execution-permits.md](docs/execution-permits.md) for Phase 6 guarantees and limitations.
