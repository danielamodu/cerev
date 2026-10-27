"""Meterless Engine integration for CEREV reasoning architecture.

Sponsor Technology: Meterless World Model
Open Agent Hackathon 2026 — Track 3: Reasoning Architecture

The Meterless World Model maintains the evolving state of the case:
- Active claims from investigation
- Contradictions across federated evidence sources
- Evidence gaps and missing corroborations
- Discrepancy tracking between proposed actions and authoritative reference calculations
- Proposal revision lineage across iterative cycles
- Advisory guidance for the next reasoning turn

CRITICAL ARCHITECTURAL INVARIANT:
The World Model is strictly ADVISORY. It provides contextual projections to guide
reasoning agents (Operator, Investigator, Challenger, Reasoner), but NEVER authorizes
actions, issues cryptographic permits, or overrides deterministic verification.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from cerev.conflicts import analyze_evidence
from cerev.coordinator import _calculate_package_reference
from cerev.evidence import EvidencePackage
from cerev.orchestration import (
    CaseContext,
    InvestigationTarget,
    VerificationFeedback,
)

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class MeterlessError(RuntimeError):
    """A Meterless request or processing step failed without exposing credentials."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class MeterlessConfig:
    """Configuration for Meterless Engine services. Credentials are excluded from repr."""

    api_base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    mode: str = "simulated"  # "simulated" or "live"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.mode == "live":
            if not self.api_base_url or (
                not self.api_base_url.startswith("http://")
                and not self.api_base_url.startswith("https://")
            ):
                raise ValueError("METERLESS_API_BASE_URL must be a full URL when mode is live.")
            if not self.api_key or not self.api_key.strip():
                raise ValueError("METERLESS_API_KEY must not be blank when mode is live.")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("METERLESS_TIMEOUT_SECONDS must be between 0 and 300.")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> MeterlessConfig:
        """Load configuration from environment variables."""
        if environ is None:
            load_dotenv(override=False)
            values = os.environ
        else:
            values = environ

        base_url = values.get("METERLESS_API_BASE_URL", "").strip() or None
        api_key = values.get("METERLESS_API_KEY", "").strip() or None
        mode = values.get("METERLESS_MODE", "").strip().lower()

        raw_timeout = values.get("METERLESS_TIMEOUT_SECONDS", "30")
        try:
            timeout = float(raw_timeout)
        except ValueError as err:
            raise ValueError("METERLESS_TIMEOUT_SECONDS must be numeric.") from err

        if not mode:
            mode = "live" if (base_url and api_key) else "simulated"

        return cls(
            api_base_url=base_url.rstrip("/") if base_url else None,
            api_key=api_key,
            mode=mode,
            timeout_seconds=timeout,
        )


@dataclass(frozen=True, slots=True)
class WorldModelProjection:
    """Synthesized projection of evolving case state maintained by the Meterless World Model.

    ADVISORY NOTICE:
    This projection is strictly advisory. It exists to assist agent reasoning and
    prioritization. It has NO authority to issue execution permits, override
    deterministic BLOCK dispositions, or bypass policy checks.
    """

    workflow_id: str
    case_id: str
    proposal_id: str
    revision_depth: int
    active_claims: tuple[str, ...]
    contradictions: tuple[str, ...]
    evidence_gaps: tuple[str, ...]
    discrepancy_cents: int | None
    advisory_guidance: str
    confidence_score: float  # Strictly advisory; never used to permit
    lineage: tuple[str, ...]
    source_attribution: str
    timestamp: datetime
    is_advisory: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "case_id": self.case_id,
            "proposal_id": self.proposal_id,
            "revision_depth": self.revision_depth,
            "active_claims": list(self.active_claims),
            "contradictions": list(self.contradictions),
            "evidence_gaps": list(self.evidence_gaps),
            "discrepancy_cents": self.discrepancy_cents,
            "advisory_guidance": self.advisory_guidance,
            "confidence_score": self.confidence_score,
            "lineage": list(self.lineage),
            "source_attribution": self.source_attribution,
            "timestamp": self.timestamp.isoformat(),
            "is_advisory": self.is_advisory,
        }


class MeterlessWorldModel(Protocol):
    """Protocol for Meterless World Model implementations."""

    def update(
        self,
        case: CaseContext,
        package: EvidencePackage | None = None,
        feedback: VerificationFeedback | None = None,
    ) -> WorldModelProjection:
        """Update world model state with new case developments and return current projection."""
        ...

    def project(self, case: CaseContext) -> WorldModelProjection:
        """Return the current advisory projection for the given case context."""
        ...


class SimulatedMeterlessWorldModel:
    """Local deterministic simulation of the Meterless World Model.

    Maintains evolving case state, detects evidence gaps and contradictions,
    and computes discrepancy between proposed actions and reference calculation.
    """

    SOURCE_ATTRIBUTION = "meterless_world_model_v1_simulated"

    def __init__(self, config: MeterlessConfig | None = None) -> None:
        self.config = config or MeterlessConfig(mode="simulated")

    def update(
        self,
        case: CaseContext,
        package: EvidencePackage | None = None,
        feedback: VerificationFeedback | None = None,
    ) -> WorldModelProjection:
        return self._compute_projection(case, package, feedback)

    def project(self, case: CaseContext) -> WorldModelProjection:
        return self._compute_projection(case, None, None)

    def _compute_projection(
        self,
        case: CaseContext,
        package: EvidencePackage | None,
        feedback: VerificationFeedback | None,
    ) -> WorldModelProjection:
        current_proposal = case.proposals[-1]
        workflow_id = case.workflow_id
        proposal_id = current_proposal.proposal_id
        case_id = case.workflow_id
        revision_depth = len(case.proposals) - 1
        lineage = tuple(p.proposal_id for p in case.proposals)

        # 1. Active claims
        active_claims = tuple(claim.statement for claim in case.claims)

        # 2. Contradictions & Conflicts
        contradictions: list[str] = []
        if package is not None:
            try:
                analysis = analyze_evidence(package.records)
                for conflict in analysis.conflicts:
                    if not conflict.resolved:
                        contradictions.append(
                            f"Evidence conflict {conflict.conflict_id}: {conflict.explanation}"
                        )
            except Exception:
                pass

        for objection in case.objections:
            if "contradiction" in objection.explanation.lower() or getattr(
                objection, "severity", None
            ) in ("FATAL", "MAJOR"):
                contradictions.append(
                    f"Challenger objection {objection.objection_id}: {objection.explanation}"
                )

        # 3. Evidence Gaps
        evidence_gaps: list[str] = []
        for req in case.evidence_requests:
            if req.target == InvestigationTarget.EVIDENCE_GAP:
                evidence_gaps.append(f"Gap requested by {req.requested_by.value}: {req.question}")
        for objection in case.objections:
            if "missing" in objection.explanation.lower() or "gap" in objection.explanation.lower():
                evidence_gaps.append(
                    f"Objection gap {objection.objection_id}: {objection.explanation}"
                )

        # 4. Discrepancy Tracking
        discrepancy_cents: int | None = None
        calculated_credit_cents: int | None = None

        if package is not None:
            try:
                reference_calc = _calculate_package_reference(package)
                calculated_credit_cents = reference_calc.credit_cents
                discrepancy_cents = current_proposal.action.amount_cents - calculated_credit_cents
            except Exception:
                pass

        if discrepancy_cents is None and feedback is not None:
            for finding in feedback.findings:
                if "discrepancy" in finding.summary.lower() or "credit" in finding.summary.lower():
                    # Attempt to extract discrepancy or note discrepancy presence
                    pass

        # 5. Advisory Guidance Synthesis
        guidance_parts: list[str] = []
        if discrepancy_cents is not None and discrepancy_cents != 0:
            prop_cents = current_proposal.action.amount_cents
            prop_curr = current_proposal.action.currency
            guidance_parts.append(
                f"Discrepancy detected: Proposed credit is {prop_cents} cents "
                f"({prop_cents / 100:.2f} {prop_curr}) "
                f"vs authoritative calculated credit of {calculated_credit_cents} cents. "
                f"Net difference is {abs(discrepancy_cents)} cents. "
                "Deterministic policy gate will BLOCK unless proposal is revised."
            )
        elif discrepancy_cents == 0:
            guidance_parts.append(
                "Proposed credit matches authoritative reference calculation exactly."
            )

        if contradictions:
            guidance_parts.append(
                f"{len(contradictions)} active contradiction(s) identified across federated "
                "sources. Investigator must reconcile before final verification."
            )

        if evidence_gaps:
            guidance_parts.append(
                f"{len(evidence_gaps)} evidence gap(s) flagged. Request targeted records."
            )

        if not guidance_parts:
            guidance_parts.append("Evidence state is consistent. Ready for policy evaluation.")

        advisory_guidance = " | ".join(guidance_parts)

        # 6. Advisory Confidence Score (Strictly informational)
        confidence = 1.0
        if discrepancy_cents is not None and discrepancy_cents != 0:
            confidence -= 0.4
        if contradictions:
            confidence -= 0.3
        if evidence_gaps:
            confidence -= 0.2
        confidence = max(0.0, min(1.0, confidence))

        return WorldModelProjection(
            workflow_id=workflow_id,
            case_id=case_id,
            proposal_id=proposal_id,
            revision_depth=revision_depth,
            active_claims=active_claims,
            contradictions=tuple(contradictions),
            evidence_gaps=tuple(evidence_gaps),
            discrepancy_cents=discrepancy_cents,
            advisory_guidance=advisory_guidance,
            confidence_score=confidence,
            lineage=lineage,
            source_attribution=self.SOURCE_ATTRIBUTION,
            timestamp=datetime.now(UTC),
            is_advisory=True,
        )


class LiveMeterlessWorldModelClient:
    """Client for a live Meterless World Model API endpoint.

    Fails closed if the endpoint is unreachable or returns invalid responses.
    Does NOT fabricate simulated responses if live mode was explicitly configured.
    """

    def __init__(self, config: MeterlessConfig) -> None:
        if config.mode != "live":
            raise ValueError("LiveMeterlessWorldModelClient requires mode='live'.")
        self.config = config

    def update(
        self,
        case: CaseContext,
        package: EvidencePackage | None = None,
        feedback: VerificationFeedback | None = None,
    ) -> WorldModelProjection:
        return self._send_request("update", case, package, feedback)

    def project(self, case: CaseContext) -> WorldModelProjection:
        return self._send_request("project", case, None, None)

    def _send_request(
        self,
        endpoint_action: str,
        case: CaseContext,
        package: EvidencePackage | None,
        feedback: VerificationFeedback | None,
    ) -> WorldModelProjection:
        req_id = str(uuid.uuid4())
        url = urljoin(f"{self.config.api_base_url}/", f"world-model/{endpoint_action}")
        payload = {
            "workflow_id": case.workflow_id,
            "proposal_id": case.current_proposal_id,
            "proposals_count": len(case.proposals),
            "claims_count": len(case.claims),
            "objections_count": len(case.objections),
            "package_id": package.package_id if package else None,
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "X-Request-ID": req_id,
        }
        req = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise MeterlessError(
                        "response_too_large",
                        "Meterless response exceeded size bound.",
                        http_status=resp.status,
                        request_id=req_id,
                    )
                resp_data = json.loads(raw.decode("utf-8"))
        except HTTPError as err:
            raise MeterlessError(
                "http_error",
                f"Meterless endpoint returned HTTP {err.code}.",
                http_status=err.code,
                request_id=req_id,
            ) from err
        except (URLError, TimeoutError, OSError) as err:
            raise MeterlessError(
                "connection_failed",
                "Could not connect to configured Meterless World Model endpoint.",
                request_id=req_id,
            ) from err
        except json.JSONDecodeError as err:
            raise MeterlessError(
                "invalid_json",
                "Meterless endpoint returned non-JSON payload.",
                request_id=req_id,
            ) from err

        return WorldModelProjection(
            workflow_id=case.workflow_id,
            case_id=case.workflow_id,
            proposal_id=case.current_proposal_id,
            revision_depth=len(case.proposals) - 1,
            active_claims=tuple(resp_data.get("active_claims", ())),
            contradictions=tuple(resp_data.get("contradictions", ())),
            evidence_gaps=tuple(resp_data.get("evidence_gaps", ())),
            discrepancy_cents=resp_data.get("discrepancy_cents"),
            advisory_guidance=resp_data.get("advisory_guidance", ""),
            confidence_score=float(resp_data.get("confidence_score", 0.0)),
            lineage=tuple(resp_data.get("lineage", (case.current_proposal_id,))),
            source_attribution=f"meterless_world_model_v1_live ({self.config.api_base_url})",
            timestamp=datetime.now(UTC),
            is_advisory=True,
        )


def create_meterless_world_model(
    config: MeterlessConfig | None = None,
) -> MeterlessWorldModel:
    """Factory creating the appropriate Meterless World Model based on configuration."""
    cfg = config or MeterlessConfig.from_environment()
    if cfg.mode == "live" and cfg.api_base_url and cfg.api_key:
        return LiveMeterlessWorldModelClient(cfg)
    return SimulatedMeterlessWorldModel(cfg)
