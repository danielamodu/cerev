# CEREV — Hackathon Video Demonstration Script (1–4 Minutes)

**Open Agent Hackathon 2026 — Track 3: Reasoning Architecture**  
**Project:** CEREV (Pre-Execution Evidence Integrity Layer for Autonomous Agents)  
**Presenter:** Engineering Lead / Hackathon Veteran  
**Video Target Duration:** 2 minutes 30 seconds to 3 minutes 30 seconds  

---

## Technical Setup Before Recording
1. Open terminal in repository root (`c:\Users\USER\Desktop\cerev`).
2. Ensure virtualenv is active: `.venv\Scripts\Activate.ps1`.
3. Set terminal font to a clean monospace font (Consolas / Cascadia Code) with a clear 80+ column width.
4. Prepare two terminal tabs or windows:
   - **Tab 1:** Demo Execution (`cerev-demo` or `python -m cerev.demo`).
   - **Tab 2:** Test Suite Verification (`pytest -q`).

---

## Video Script Walkthrough

### 0:00 – 0:35 | Introduction & Core Product Thesis
**Visual:** Show slide or terminal header with CEREV title.  
**Speaker Narration:**
> "Welcome to CEREV — the pre-execution evidence integrity layer for autonomous agents, submitted for Open Agent Hackathon 2026, Track 3: Reasoning Architecture.
>
> Here is our core product thesis:  
> **'Agents are great at deciding what to do, and terrible at proving their evidence hasn't expired or been contradicted. CEREV is the pre-execution integrity gate that forces an agent to prove its action across federated silos before money or state moves.'**
>
> In autonomous systems, agent consensus and model confidence are routinely mistaken for authorization. If two LLMs agree that a credit should be paid, or if an agent hallucinates that maintenance notice was served, traditional agent runtimes simply execute the tool call. CEREV flips this paradigm: multi-agent reasoning investigates and challenges claims, but deterministic verification alone holds the cryptographic signing keys."

---

### 0:35 – 1:15 | Sponsor Integration Architecture
**Visual:** Show architecture diagram or point to the sponsor status badges in the terminal demo output.  
**Speaker Narration:**
> "To deliver true enterprise evidence integrity, CEREV integrates three key sponsor platforms:
>
> 1. **Zetaris:** Provides federated evidence retrieval across five independent data silos — customer contract repositories, incident stores, notice records, operational CMS, and billing records — via a live, verified SQL gateway proxy.
> 2. **Meterless Engine:** Operates as our World Model, projecting evolving case state, tracking open claims and hypotheses, identifying contradictions across federated silos, and computing SLA discrepancies before policy evaluation.
> 3. **NVIDIA Nemotron (via Nightshift):** Powers our adversarial Challenger role using `nvidia/llama-3.1-nemotron-70b-instruct`, specifically interrogating timestamps, downtime windows, and unproven assumptions.
>
> These sponsor capabilities collaborate with our four-role architecture: Operator, Investigator, Challenger, and Reasoner."

---

### 1:15 – 2:15 | Part 1: Canonical SLA Incident Scenario (Deterministic BLOCK)
**Visual:** Run `cerev-demo` in terminal. Watch Part 1 execute.  
**Speaker Narration:**
> "Let's watch CEREV run our canonical enterprise benchmark.
>
> A customer proposes a $48.00 SLA credit for March 2024 outages.
>
> **Step 1:** The Investigator retrieves 18 evidence records across the five federated silos via Zetaris.
>
> **Step 2:** The Meterless World Model projects the case state: it flags that the calculated SLA credit is $192.00, detecting a -$144.00 net discrepancy against the proposed $48.00 credit.
>
> **Step 3:** Our adversarial NVIDIA Nemotron Challenger interrogates the maintenance records. It uncovers that maintenance notices were delivered only 41, 37, and 34 hours in advance — violating the strict 48-hour prior written notice clause in Contract V4.
>
> **Step 4:** The Reasoner synthesizes findings and passes them to the Authoritative Deterministic Reference Engine.
>
> Look at the result: The reference engine independently computes 99.0524% availability, finding 4 complete shortfall intervals. Because the proposed credit ($48.00) does not match the authoritative calculation ($192.00), the policy gate produces a deterministic **BLOCK**.
>
> An immutable cryptographic receipt (`sha256:d8a0c24f...`) is generated. Because the disposition is BLOCK, **no Ed25519 execution permit is issued**. Money does NOT move on contradictory evidence."

---

### 2:15 – 3:00 | Part 2: Qualifying Scenario & Protected Execution Boundary
**Visual:** Terminal scrolls to Part 2 of `cerev-demo`.  
**Speaker Narration:**
> "Now, what happens when an action is legitimate and properly evidenced?
>
> In Part 2, a synthetic qualifying scenario is evaluated:
> - Maintenance notice was delivered with full 48-hour qualifying advance notice.
> - The proposed credit amount ($192.00) matches the authoritative SLA reference calculation down to the cent.
>
> The collaborative reasoning loop completes: Investigator cites the qualifying notice, Nemotron verifies zero unresolved contradictions, and the Deterministic Policy Gate evaluates all checks.
>
> The disposition is **PERMIT**.
>
> Now watch the cryptographic boundary:
> 1. An ephemeral Ed25519 signing authority issues a tamper-evident Execution Permit bound to the receipt hash, action fingerprint, and fresh 32-byte nonce.
> 2. The Protected Execution Boundary verifies the signature against trusted keys.
> 3. Execution attempt 1 **succeeds** ($192.00 credit issued).
> 4. Execution attempt 2 — an adversarial replay attack using the same permit — is **instantly rejected** by the SQLite idempotent store. The state never corrupts."

---

### 3:00 – 3:30 | Technical Defensibility & Conclusion
**Visual:** Switch to Tab 2 and run `pytest -q`. Show 429+ passing tests in seconds.  
**Speaker Narration:**
> "Everything you've seen is backed by rigorous engineering:
> - Over 420 unit and workflow tests covering four-role orchestration, durable SQLite state recovery, Meterless World Model projections, Nemotron model routing, and adversarial boundary defenses.
> - Strict fail-closed posture: tampered payloads, forged signatures, expired permits, and LLM hallucinations are deterministically blocked.
>
> In an era where autonomous agents are given real financial and operational power, CEREV guarantees that **evidence integrity and cryptographic proof precede execution**.
>
> Thank you, judges and sponsors of the Open Agent Hackathon 2026!"
