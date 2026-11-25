from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from silverwatch import cases, evidence, registry, review
from silverwatch.contracts import (Case, CaseEvidence, NeedBatch, Provider,
                                   ReviewDecision, TouchBatch)
from silverwatch.storage import Store


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class DatabaseCase:
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="silverwatch-test-")
        self.store = Store(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def transaction(self, write=True):
        return self.store.transaction(write=write)


def provider_data(**updates):
    value = {"provider_id": "provider-one", "display_name": "Community Meals",
             "region": "suburban", "district": "district-a", "service_kind": "meal",
             "capacity_per_month": 1200, "active_from": "2026-01-01T00:00:00Z",
             "active_until": None, "status": "active", "updated_at": "2026-10-01T00:00:00Z"}
    return value | updates


def touch_data(**updates):
    value = {"batch_id": "touch-one", "campaign_id": "campaign-one",
             "provider_id": "provider-one", "region": "suburban", "district": "district-a",
             "service_kind": "meal", "channel": "community", "cohort": "70_79",
             "period_start": "2026-10-01T00:00:00Z", "period_end": "2026-10-31T00:00:00Z",
             "exposed_people": 200, "completed_services": 150, "report_count": 1,
             "nonresponse_people": 10, "coverage_complete": True, "source_revision": 1,
             "source_ref": "district-feed", "observed_at": "2026-11-01T00:00:00Z"}
    return value | updates


def need_data(**updates):
    value = {"estimate_id": "need-one", "region": "suburban", "district": "district-a",
             "cohort": "70_79", "service_kind": "meal", "estimated_people": 1000,
             "period_start": "2026-10-01T00:00:00Z", "period_end": "2026-10-31T00:00:00Z",
             "source_revision": 1, "source_ref": "survey"}
    return value | updates


def appointment_data(**updates):
    value = {"appointment_id": "appointment-one", "provider_id": "provider-one",
             "region": "suburban", "district": "district-a", "service_kind": "meal",
             "service_month": "2026-11", "committed_people": 200,
             "source_revision": 1, "source_ref": "booking-desk",
             "observed_at": "2026-10-20T00:00:00Z"}
    return value | updates


def proposal_data(**updates):
    value = {"proposal_id": "proposal-one", "campaign_id": "campaign-one",
             "provider_id": "provider-one", "region": "suburban", "district": "district-a",
             "service_kind": "meal", "service_month": "2026-11", "planned_people": 100,
             "status": "proposed", "source_revision": 1, "source_ref": "campaign-desk",
             "observed_at": "2026-10-21T00:00:00Z"}
    return value | updates


def case_data(**updates):
    value = {"case_id": "case-one", "provider_id": "provider-one", "batch_id": "touch-one",
             "kind": "misleading_claim", "severity": "medium",
             "reported_at": "2026-10-10T09:00:00Z", "allegation": "Renewal price was omitted.",
             "source_ref": "hotline", "affected_people": 1, "status": "investigating"}
    return value | updates


def evidence_data(**updates):
    value = {"evidence_id": "evidence-one", "case_id": "case-one", "evidence_type": "recording",
             "source_ref": "archive://recording-1", "content_digest": "a" * 64,
             "observed_at": "2026-10-11T10:00:00Z", "verified": True}
    return value | updates


def decision_data(role="investigator", reviewer="reviewer-one", finding="substantiated", **updates):
    value = {"decision_id": f"decision-{role}", "case_id": "case-one", "reviewer": reviewer,
             "role": role, "finding": finding, "decided_at": "2026-10-12T10:00:00Z",
             "rationale": "The preserved notice supports this finding.",
             "evidence_refs": ["evidence-one"]}
    return value | updates


def seed_case(db, *, severity="medium", report_count=1, exposed_people=200,
              nonresponse_people=10, coverage_complete=True,
              touch_observed_at="2026-11-01T00:00:00Z"):
    registry.register_provider(db, Provider.model_validate(provider_data()), "provider-feed", 1)
    registry.register_touch(db, TouchBatch.model_validate(touch_data(report_count=report_count,
        exposed_people=exposed_people, nonresponse_people=nonresponse_people,
        coverage_complete=coverage_complete, observed_at=touch_observed_at)))
    registry.register_need(db, NeedBatch.model_validate(need_data()))
    cases.create(db, Case.model_validate(case_data(severity=severity)), "case-feed", 1)
    evidence.add(db, CaseEvidence.model_validate(evidence_data()))


def resolve_case(db, *, first="substantiated", second="substantiated"):
    review.decide(db, ReviewDecision.model_validate(decision_data("investigator", "reviewer-one", first)))
    review.decide(db, ReviewDecision.model_validate(decision_data("compliance", "reviewer-two", second)))
