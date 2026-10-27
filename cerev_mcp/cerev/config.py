"""Typed configuration model for CEREV's five logical evidence sources."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecordCategorySpec:
    """Expected record category and count within a group."""

    group_name: str
    record_category: str
    expected_count: int


@dataclass(frozen=True, slots=True)
class LogicalSourceMapping:
    """Mapping from a CEREV logical evidence source to Zetaris relation and categories."""

    source_id: str
    source_system: str
    category_name: str
    relation: str
    category_specs: tuple[RecordCategorySpec, ...]

    @property
    def expected_total_records(self) -> int:
        return sum(spec.expected_count for spec in self.category_specs)


@dataclass(frozen=True, slots=True)
class CerevMcpSourceConfig:
    """Explicit, typed configuration for all five CEREV logical sources."""

    contract_mapping: LogicalSourceMapping
    cms_mapping: LogicalSourceMapping
    maintenance_mapping: LogicalSourceMapping
    release_mapping: LogicalSourceMapping
    billing_mapping: LogicalSourceMapping

    @property
    def all_mappings(self) -> tuple[LogicalSourceMapping, ...]:
        return (
            self.contract_mapping,
            self.cms_mapping,
            self.maintenance_mapping,
            self.release_mapping,
            self.billing_mapping,
        )

    @property
    def required_relations(self) -> frozenset[str]:
        return frozenset(m.relation for m in self.all_mappings)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> CerevMcpSourceConfig:
        values = os.environ if environ is None else environ

        contract_table = values.get(
            "CEREV_ZETARIS_CONTRACT_RECORDS_TABLE",
            "cerev_contract_records_v1.cerev_contract_records_v1",
        ).strip()
        cms_policies_table = values.get(
            "CEREV_ZETARIS_CMS_POLICIES_TABLE",
            "cerev_phase124.cerev_main",
        ).strip()
        cms_classifications_table = values.get(
            "CEREV_ZETARIS_CMS_CLASSIFICATIONS_TABLE",
            "cerev_phase124.cerev_main",
        ).strip()
        maint_events_table = values.get(
            "CEREV_ZETARIS_MAINTENANCE_EVENTS_TABLE",
            "cerev_phase124.cerev_main",
        ).strip()
        maint_notices_table = values.get(
            "CEREV_ZETARIS_NOTICE_RECORDS_TABLE",
            "cerev_phase124.cerev_main",
        ).strip()
        releases_table = values.get(
            "CEREV_ZETARIS_RELEASES_TABLE",
            "cerev_phase124.cerev_releases",
        ).strip()
        billing_calcs_table = values.get(
            "CEREV_ZETARIS_BILLING_CALCULATIONS_TABLE",
            "cerev_phase124.cerev_main",
        ).strip()
        proposed_actions_table = values.get(
            "CEREV_ZETARIS_PROPOSED_ACTIONS_TABLE",
            "cerev_phase124.cerev_main",
        ).strip()

        # Validate consistent tables for multi-group sources
        if cms_policies_table != cms_classifications_table:
            raise ValueError(
                "CEREV CMS policies and classifications must point to the same relation."
            )
        if maint_events_table != maint_notices_table:
            raise ValueError(
                "CEREV maintenance events and notice records must point to the same relation."
            )
        if billing_calcs_table != proposed_actions_table:
            raise ValueError(
                "CEREV billing calculations and proposed actions must point to the same relation."
            )

        contract_mapping = LogicalSourceMapping(
            source_id="source-contract-repository",
            source_system="contract_repository",
            category_name="contract",
            relation=contract_table,
            category_specs=(RecordCategorySpec("records", "service_agreement", 1),),
        )

        cms_mapping = LogicalSourceMapping(
            source_id="source-operational-cms",
            source_system="operational_cms",
            category_name="operational_cms",
            relation=cms_policies_table,
            category_specs=(
                RecordCategorySpec("policies", "cms_policy", 2),
                RecordCategorySpec("classifications", "cms_classification", 4),
            ),
        )

        maintenance_mapping = LogicalSourceMapping(
            source_id="source-maintenance-incident-registry",
            source_system="maintenance_incident_registry",
            category_name="maintenance_incidents",
            relation=maint_events_table,
            category_specs=(
                RecordCategorySpec("events", "maintenance_event", 4),
                RecordCategorySpec("notice_records", "notice_record", 3),
            ),
        )

        release_mapping = LogicalSourceMapping(
            source_id="source-release-registry",
            source_system="release_registry",
            category_name="release_registry",
            relation=releases_table,
            category_specs=(RecordCategorySpec("releases", "adapter_deployment", 2),),
        )

        billing_mapping = LogicalSourceMapping(
            source_id="source-billing-calculation-and-action",
            source_system="billing_calculation_and_action",
            category_name="billing_action",
            relation=billing_calcs_table,
            category_specs=(
                RecordCategorySpec("calculations", "billing_calculation", 1),
                RecordCategorySpec("proposed_actions", "proposed_action", 1),
            ),
        )

        return cls(
            contract_mapping=contract_mapping,
            cms_mapping=cms_mapping,
            maintenance_mapping=maintenance_mapping,
            release_mapping=release_mapping,
            billing_mapping=billing_mapping,
        )
