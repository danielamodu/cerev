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

