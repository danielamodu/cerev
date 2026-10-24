# CEREV

CEREV is intended to be a pre-execution evidence integrity layer for autonomous agents. Its purpose is to help an agent establish that the evidence supporting a proposed action is current, traceable, and acceptable under an explicit policy before execution is permitted.

## Architectural thesis

An agent should not move directly from retrieved information to an external action. CEREV is being designed as a boundary in that path: evidence is gathered and checked, policy is evaluated against that evidence, and an auditable decision can be produced before a downstream system acts. Evidence provenance and deterministic replay are intended to make that decision inspectable later.

This describes the intended architecture. No evidence validation, policy evaluation, permit issuance, replay, external service integration, or action execution is implemented yet.

## Current status

This repository is at **Phase 0: Repository Foundation**. It contains Python package and tooling scaffolding only. Business logic is intentionally deferred to later phases. There are no fixture records or mock service responses yet.

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

Run the foundation checks:

```sh
pytest
ruff check .
ruff format --check .
```

The package currently has no runtime dependencies and does not read environment variables. Add configuration fields and any `.env.example` placeholders when a later phase introduces configuration requirements. Never put credentials in the repository.

## Planned module map

The modules below are reserved for later implementation. At this phase they contain documentation only and expose no working behavior.

- `config`: explicit application configuration
- `reference` and `v21_simulation`: reference data and simulation
- `evidence` and `validation`: evidence records and validation
- `policy`: policy evaluation
- `receipt` and `replay`: decision records and deterministic replay
- `permit` and `nonce`: execution permits and nonce coordination
- `retrieval_direct` and `adapter`: retrieval and integration boundaries

Supporting directories (`fixture`, `tests`, `mock_services`, `demo`, and `docs`) are present for future phases. No database, LLM, Zetaris, or Nemotron integration is included.
