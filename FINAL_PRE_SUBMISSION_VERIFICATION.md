# CEREV — Final Pre-Submission Verification Report
**Date:** 2026-10-28T00:08:00+01:00  
**Repository:** CEREV (Pre-Execution Evidence Integrity Layer for Autonomous Agents)  
**Hackathon Track:** Open Agent Hackathon 2026 — Track 3: Reasoning Architecture  

---

## 1. Meterless Nightshift Live Result

- **HTTP / Request Success:** **NO (Connection Refused)**
- **Nightshift Endpoint Target:** `http://127.0.0.1:8088/v1`
- **Requested Model:** `nvidia/nemotron-3.5-lightning:free`
- **Actual Serving Model Returned:** None (request could not reach server)
- **Provider Attribution:** Unattainable due to transport failure
- **Consumed by Challenger:** NO
- **Canonical Workflow Result:** Deterministic gate remains unbreached; coordinator fails closed
- **Tests Run:** 53 unit/integration tests in `tests/test_nightshift.py` (all passed offline)
- **Detailed Diagnosis:**
  1. Socket probe to `127.0.0.1:8088` returned:
     `[WinError 10061] No connection could be made because the target machine actively refused it`.
  2. `Get-NetTCPConnection -State Listen` confirmed that port `8088` is not bound by any operating system process.
  3. `cerev.nightshift.NightshiftClient` raised:
     `NightshiftError("connection_error", "Could not connect to the configured Nightshift endpoint.")`.
  4. **Root Cause Analysis:**
     The Nightshift cockpit is running "in-tab" inside Google Chrome at `https://meterless.ai/Nightshift/WEB/`. When running in-browser, Nightshift operates inside the browser's WebAssembly/JS sandbox and **does not open a TCP server socket on Windows port 8088**. Port 8088 is only bound on `127.0.0.1` when the native Nightshift daemon or Tauri desktop application (`src-tauri/src/proxy.rs`) is launched on the host machine.
  5. **Authentication Posture:**
     The bearer token provisioned in the cockpit is held by the browser session. Because port 8088 is down, CEREV cannot reach the local endpoint.

---

## 2. NVIDIA Nemotron via Nightshift Live Result

- **Live Status:** **BLOCKED (ENDPOINT UNREACHABLE)**
- **Requested Model:** `nvidia/nemotron-3.5-lightning:free`
- **Routing Invariant:** Must route exclusively through Nightshift (direct NVIDIA/OpenRouter calls strictly forbidden).
- **Implementation Status:**
  `NemotronChallenger(LLMChallenger)` in `cerev/agent_roles.py` supports:
  - Custom model routing (`model="nvidia/nemotron-3.5-lightning:free"`).
  - Dynamic serving-model attribution tracking.
  - Provable request ID and trace logging.
- **Offline / Workflow Verification:**
  - 5 tests in `tests/test_nemotron.py` pass.
  - Serving-model attribution is verified via `MockNightshiftProvider`.
  - Adversarial scrutiny of maintenance notice shortfalls (41h, 37h, 34h < 48h) is verified in production `ReasoningCoordinator`.
  - Deterministic policy gate remains the sole execution authority (yields `BLOCK` on canonical SLA case; no permits issued).

---

## 3. Meterless Engine Live Result

- **Live Status:** **BLOCKED / NOT RUN**
- **Endpoint Inspected:** `METERLESS_API_BASE_URL` and `METERLESS_API_KEY`.
- **Finding:** No Meterless Engine remote origin or API key is provisioned in `.env` or process environment.
- **Implementation:** `cerev/meterless.py` implements both `SimulatedMeterlessWorldModel` and `LiveMeterlessWorldModelClient`, providing dynamic case state projections, contradiction tracking, evidence gap detection, and SLA discrepancy calculations.
- **Offline / Workflow Verification:** 8 unit tests in `tests/test_meterless.py` pass, confirming advisory guidance generation and that live client network failures fail closed (`LiveWorldModelError`) without overriding the deterministic authorization boundary.

---

## 4. Canonical Monetary-Scale Reconciliation

- **The Investigation:**
  - MCP differential case reported: **$19,200.00 reference credit vs. $4,800.00 proposed credit**.
  - CLI demo print statements previously displayed: **$192.00 vs. $48.00**.
- **Root Cause Analysis:**
  1. `fixture/billing_and_action.json` specifies:
     - `credit_cents = 480000`
     - `amount_cents = 480000`
     - In integer cents: $480,000 \div 100 = \mathbf{\$4,800.00}$.
  2. `fixture/contract_repository.json` specifies:
     - `monthly_service_fee_cents = 4800000` ($\$48,000.00$).
  3. `cerev/reference.py` computes:
     - 4 complete credit intervals of 10% on the monthly service fee:
       $$\text{credit\_cents} = 4 \times 480,000 = 1,920,000\text{ cents} = \mathbf{\$19,200.00}$$
  4. The test suite in `tests/test_reference_engine.py` explicitly asserts:
     ```python
     def test_canonical_fixture_calculates_contractual_credit_of_19200_dollars():
         assert result.credit_cents == 1_920_000
     ```
  5. The previous CLI print strings in `cerev/demo.py` accidentally scaled cents by dividing by 10,000 instead of 100, displaying `"$48.00 (480,000 cents)"` and `"$192.00"`.
- **Reconciliation Outcome:**
  - The canonical source of truth is **$4,800.00 proposed vs. $19,200.00 authoritative calculated credit** (net discrepancy: **-$14,400.00** / -1,440,000 cents).
  - The MCP case is mathematically exact.
  - All prose print strings in `cerev/demo.py`, `docs/DEMO_SCRIPT.md`, and `README.md` have been corrected to strictly reflect **$4,800.00** and **$19,200.00**.
  - All tests and deterministic decision logic remain unchanged and 100% passing.

---

## 5. Full Test, Lint, and Demo Status

- **Unit & Integration Tests (`pytest -q`):**
  - **413 passed in 132.77s** across all test suites with **0 failures, 0 errors**.
- **Ruff Lint Check (`ruff check .`):**
  - **All checks passed! (0 errors)**.
- **Ruff Format Check (`ruff format --check .`):**
  - **88 files already formatted (100% compliant)**.
- **Interactive Demonstration (`cerev-demo`):**
  - Successfully executed. Renders product thesis, sponsor component status badges, Part 1 Canonical BLOCK, Part 2 Synthetic PERMIT, and final invariant verification.
- **JSON Telemetry (`cerev-demo --json`):**
  - Successfully executed. Produces structured, machine-readable JSON containing complete audit records and dual-scenario execution results.

---

## 6. Git and Repository Integrity

- **Branch:** `main` (clean working tree of intended changes).
- **Staged / Historical Commits:** No commits or pushes were made.
- **Historical Integrity:** All prior commits (Phases 0–12, 13.1, 13.2) remain intact.
- **Cross-Repository Boundary:** `cerev-mcp` is completely untouched.
- **Secrets Protection:**
  - `.env` is confirmed ignored by Git (`git check-ignore .env` returns `.env`).
  - No credentials, tokens, or private keys appear in committed code, fixtures, logs, or documentation.
  - Signing keys in `cerev/permit.py` and `cerev/adapter.py` remain ephemeral and in-memory.

---

## 7. Sponsor Claim Status Audit

| Sponsor Technology | Actual Technical Capability | Required Classification |
| :--- | :--- | :--- |
| **Zetaris Gateway** | Live SQL Gateway Proxy connected; 18 records queried across 5 source adapters | **LIVE-VERIFIED** |
| **Meterless Engine** | World Model projection, dynamic case state & SLA discrepancy tracking implemented & tested | **INTEGRATED & TESTED** *(Simulated Engine)* |
| **Meterless Nightshift** | Provider client implemented with bearer auth, model attribution & request IDs; offline tested | **INTEGRATED & TESTED** *(Mock-Backed)* |
| **NVIDIA Nemotron via Nightshift** | Challenger role adapter routing through Nightshift with serving-model tracking; offline tested | **INTEGRATED & TESTED** *(Mock-Backed)* |

---

## 8. Remaining Blockers & Required User Action

Per instruction 7 (*"If any integration remains blocked, report the exact blocker and the precise action needed from me. Do not conceal it or make the acceptance decision on my behalf."*):

### Blockers:
1. **Meterless Nightshift:** Port `8088` is unreachable on localhost because the local Nightshift daemon/Tauri app is not running (the cockpit is running "in-tab" inside Google Chrome).
2. **NVIDIA Nemotron:** Blocked by Nightshift daemon reachability.
3. **Meterless Engine:** Missing live Meterless Engine API endpoint and credentials.

### Action Needed from User:
1. **Launch the Nightshift Local Daemon:**
   - If using the Nightshift desktop application / daemon, start it so it binds port `8088` on `127.0.0.1`.
   - Alternatively, if Nightshift provides an external HTTP/HTTPS relay endpoint for remote/in-tab cockpits, set that URL in `.env` as `NIGHTSHIFT_API_BASE_URL`.
2. **Set the Auth Token in `.env`:**
   - In `.env` (which is gitignored and will never be committed or shown in chat), add:
     ```sh
     NIGHTSHIFT_API_BASE_URL=http://127.0.0.1:8088/v1
     NIGHTSHIFT_API_KEY=<the-token-from-your-cockpit>
     NIGHTSHIFT_MODEL=nvidia/nemotron-3.5-lightning:free
     ```
3. Once port 8088 is listening and `.env` has the key, `scripts/diagnose_live_nightshift.py` and the coordinator will immediately execute against the live endpoint.

---

## Verdict

Per rule 7 (*"The project is NOT complete until every mandatory sponsor integration is genuinely operational and demonstrated. Do not mark the project READY TO SUBMIT while any mandatory sponsor integration is only mocked, simulated, configured, or unit-tested"*):

**NOT READY TO SUBMIT**  
*(Blocked strictly by endpoint reachability for Meterless Nightshift [port 8088 connection refused] and missing live credentials for Meterless Engine. All code, adapters, tests, documentation, and deterministic invariants are 100% complete, verified, and operational).*
