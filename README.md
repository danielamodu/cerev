# CEREV

CEREV is intended to be a pre-execution evidence integrity layer for autonomous agents. Its purpose is to help an agent establish that the evidence supporting a proposed action is current, traceable, and acceptable under an explicit policy before execution is permitted.

## Architectural thesis

An agent should not move directly from retrieved information to an external action. CEREV is being designed as a boundary in that path: evidence is gathered and checked, policy is evaluated against that evidence, and an auditable decision can be produced before a downstream system acts. Evidence provenance and deterministic replay are intended to make that decision inspectable later.

This describes the intended architecture. The only calculation logic currently implemented is the independent, deterministic Client A SLA reference engine. General-purpose evidence validation, policy evaluation, permit issuance, replay, external service integration, and action execution are not implemented.

## Current status

This repository has completed **Phase 0: Repository Foundation**, **Phase 1: Canonical Evidence Fixture**, and **Phase 2: Deterministic Reference Engine**. It contains a source-separated JSON fixture and an independent calculation of the March 2024 Client A SLA credit. Tests cover fixture integrity, contractual exclusion rules, calculation boundaries, and fail-safe handling of missing evidence. Broader evidence integrity and execution controls remain deferred to later phases.

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

The reference engine is implemented in `cerev/reference.py`. The other modules below are reserved for later implementation and currently contain documentation only.

- `config`: explicit application configuration
- `reference`: deterministic Contract V4 SLA reference calculation
- `v21_simulation`: future simulated adapter behavior
- `evidence` and `validation`: evidence records and validation
- `policy`: policy evaluation
- `receipt` and `replay`: decision records and deterministic replay
- `permit` and `nonce`: execution permits and nonce coordination
- `retrieval_direct` and `adapter`: retrieval and integration boundaries

The `fixture/` directory contains the Phase 1 datasets and fixture guide. `mock_services/`, `demo/`, and `docs/` remain available for later phases. No database, LLM, Zetaris, or Nemotron integration is included.
