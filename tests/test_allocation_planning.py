from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from silverwatch import allocation, analysis, exports, planning, registry
from silverwatch.api import create_app
from silverwatch.contracts import (ActivityProposal, AnalysisPolicy, Provider,
                                   ServiceAppointment, TouchBatch)
from silverwatch.errors import Problem
from silverwatch.ledger import current_entities
from tests.support import (DatabaseCase, appointment_data, instant, proposal_data,
                          provider_data, touch_data)

POLICY = {"policy_id": "policy-one", "minimum_exposure": 100,
          "minimum_supply_coverage_bp": 8000, "max_substantiated_rate_bp": 50,
          "severe_open_cases_block": True, "require_verified_remediation": True,
          "minimum_touch_coverage_bp": 8000}


class AllocationContractTests(unittest.TestCase):
    def test_service_month_must_be_calendar_month(self):
        with self.assertRaises(Exception):
            ServiceAppointment.model_validate(appointment_data(service_month="2026-13"))
        with self.assertRaises(Exception):
            ActivityProposal.model_validate(proposal_data(service_month="202611"))

    def test_appointment_requires_at_least_one_committed_person(self):
        with self.assertRaises(Exception):
            ServiceAppointment.model_validate(appointment_data(committed_people=0))

    def test_proposal_rejects_unknown_status(self):
        with self.assertRaises(Exception):
            ActivityProposal.model_validate(proposal_data(status="held"))


class CapacityAllocationTests(DatabaseCase, unittest.TestCase):
    def seed_provider(self, db, **updates):
        registry.register_provider(db, Provider.model_validate(provider_data(**updates)),
                                   "provider-feed", 1)

    def test_provider_view_reports_four_capacity_layers_separately(self):
        with self.transaction() as db:
            self.seed_provider(db)
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=500)))
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=300)))
            view = allocation.provider_allocation(db, provider_id="provider-one",
                                                   service_month="2026-11")
            self.assertEqual(view["reported_capacity"], 1200)
            self.assertEqual(view["committed_appointments"], 500)
            self.assertEqual(view["activity_reserved"], 300)
            self.assertEqual(view["remaining_capacity"], 400)
            self.assertFalse(view["commitment_breach"])

    def test_proposal_cannot_take_capacity_already_promised_to_appointments(self):
        with self.transaction() as db:
            self.seed_provider(db)
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=1150)))
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(
                    db, ActivityProposal.model_validate(proposal_data(planned_people=100)))
            self.assertEqual(captured.exception.code, "capacity_unavailable")
            self.assertEqual(captured.exception.details["committed_appointments"], 1150)
            self.assertEqual(captured.exception.details["available_people"], 50)
            view = allocation.provider_allocation(db, provider_id="provider-one",
                                                   service_month="2026-11")
            self.assertEqual(view["activity_reserved"], 0)

    def test_two_proposals_in_same_month_cannot_double_book_remaining_capacity(self):
        with self.transaction() as db:
            self.seed_provider(db)
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=1100)))
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(db, ActivityProposal.model_validate(
                    proposal_data(proposal_id="proposal-two", campaign_id="campaign-two",
                                  planned_people=200)))
            self.assertEqual(captured.exception.code, "capacity_unavailable")
            self.assertEqual(captured.exception.details["held_by_other_proposals"], 1100)
            self.assertEqual(captured.exception.details["available_people"], 100)

    def test_proposals_fitting_exactly_the_remaining_capacity_are_accepted(self):
        with self.transaction() as db:
            self.seed_provider(db)
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=700)))
            allocation.register_proposal(db, ActivityProposal.model_validate(
                proposal_data(proposal_id="proposal-two", campaign_id="campaign-two",
                              planned_people=500)))
            view = allocation.provider_allocation(db, provider_id="provider-one",
                                                   service_month="2026-11")
            self.assertEqual(view["activity_reserved"], 1200)
            self.assertEqual(view["remaining_capacity"], 0)

    def test_cancelled_proposal_releases_capacity_without_touching_appointments(self):
        with self.transaction() as db:
            self.seed_provider(db)
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=500)))
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=600)))
            with self.assertRaises(Problem):
                allocation.register_proposal(db, ActivityProposal.model_validate(
                    proposal_data(proposal_id="proposal-two", campaign_id="campaign-two",
                                  planned_people=200)))
            allocation.register_proposal(db, ActivityProposal.model_validate(
                proposal_data(planned_people=600, status="cancelled",
                              source_revision=2, observed_at="2026-10-25T00:00:00Z")))
            view = allocation.provider_allocation(db, provider_id="provider-one",
                                                   service_month="2026-11")
            self.assertEqual(view["committed_appointments"], 500)
            self.assertEqual(view["activity_reserved"], 0)
            self.assertEqual(view["remaining_capacity"], 700)
            allocation.register_proposal(db, ActivityProposal.model_validate(
                proposal_data(proposal_id="proposal-two", campaign_id="campaign-two",
                              planned_people=200)))
            self.assertEqual(allocation.provider_allocation(
                db, provider_id="provider-one", service_month="2026-11")["activity_reserved"], 200)

    def test_revised_headcount_is_rechecked_against_current_remaining_capacity(self):
        with self.transaction() as db:
            self.seed_provider(db)
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=900)))
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=200)))
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(db, ActivityProposal.model_validate(
                    proposal_data(planned_people=400, source_revision=2,
                                  observed_at="2026-10-26T00:00:00Z")))
            self.assertEqual(captured.exception.code, "capacity_unavailable")
            current = allocation.provider_allocation(db, provider_id="provider-one",
                                                      service_month="2026-11")
            self.assertEqual(current["activity_reserved"], 200)
            allocation.register_proposal(db, ActivityProposal.model_validate(
                proposal_data(planned_people=100, source_revision=2,
                              observed_at="2026-10-26T00:00:00Z")))
            self.assertEqual(allocation.provider_allocation(
                db, provider_id="provider-one", service_month="2026-11")["activity_reserved"], 100)

    def test_revision_excludes_its_own_previous_hold(self):
        with self.transaction() as db:
            self.seed_provider(db)
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=400)))
            allocation.register_proposal(db, ActivityProposal.model_validate(
                proposal_data(planned_people=450, source_revision=2,
                              observed_at="2026-10-26T00:00:00Z")))
            self.assertEqual(allocation.provider_allocation(
                db, provider_id="provider-one", service_month="2026-11")["activity_reserved"], 450)

    def test_revision_cannot_change_identity_fields_or_reuse_revision_number(self):
        with self.transaction() as db:
            self.seed_provider(db)
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=100)))
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(db, ActivityProposal.model_validate(
                    proposal_data(planned_people=90, source_revision=1,
                                  observed_at="2026-10-26T00:00:00Z")))
            self.assertEqual(captured.exception.code, "revision_out_of_order")
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(db, ActivityProposal.model_validate(
                    proposal_data(planned_people=90, service_month="2026-12",
                                  source_revision=2, observed_at="2026-10-26T00:00:00Z")))
            self.assertEqual(captured.exception.code, "proposal_identity")

    def test_cancelled_proposal_cannot_be_reopened_and_cancel_needs_existing_proposal(self):
        with self.transaction() as db:
            self.seed_provider(db)
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(db, ActivityProposal.model_validate(
                    proposal_data(status="cancelled")))
            self.assertEqual(captured.exception.code, "proposal_state")
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=100)))
            allocation.register_proposal(db, ActivityProposal.model_validate(
                proposal_data(planned_people=100, status="cancelled", source_revision=2,
                              observed_at="2026-10-26T00:00:00Z")))
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(db, ActivityProposal.model_validate(
                    proposal_data(planned_people=100, status="proposed", source_revision=3,
                                  observed_at="2026-10-27T00:00:00Z")))
            self.assertEqual(captured.exception.code, "proposal_cancelled")

    def test_identical_proposal_replay_is_idempotent_without_new_event(self):
        with self.transaction() as db:
            self.seed_provider(db)
            first = allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=100)))
            second = allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=100)))
            self.assertTrue(second["replayed"])
            self.assertEqual(first["seq"], second["seq"])
            self.assertEqual(len(current_entities(db, "proposal")), 1)

    def test_appointment_over_capacity_is_recorded_but_flagged_as_breach(self):
        with self.transaction() as db:
            self.seed_provider(db, capacity_per_month=400)
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=500)))
            view = allocation.provider_allocation(db, provider_id="provider-one",
                                                   service_month="2026-11")
            self.assertTrue(view["commitment_breach"])
            self.assertTrue(view["overcommitted"])
            self.assertEqual(view["remaining_capacity"], 0)
            self.assertEqual(view["available_capacity"], -100)
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(
                    db, ActivityProposal.model_validate(proposal_data(planned_people=1)))
            self.assertEqual(captured.exception.code, "capacity_unavailable")

    def test_capacity_cut_recomputes_views_without_squeezing_existing_commitments(self):
        with self.transaction() as db:
            self.seed_provider(db)
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=700)))
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=200)))
            registry.register_provider(db, Provider.model_validate(
                provider_data(capacity_per_month=800,
                              updated_at="2026-10-25T00:00:00Z")), "provider-feed", 2)
            view = allocation.provider_allocation(db, provider_id="provider-one",
                                                   service_month="2026-11")
            self.assertEqual(view["committed_appointments"], 700)
            self.assertEqual(view["activity_reserved"], 200)
            self.assertEqual(view["available_capacity"], -100)
            self.assertTrue(view["overcommitted"])
            self.assertFalse(view["commitment_breach"])
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(db, ActivityProposal.model_validate(
                    proposal_data(proposal_id="proposal-two", campaign_id="campaign-two",
                                  planned_people=1)))
            self.assertEqual(captured.exception.code, "capacity_unavailable")

    def test_suspended_provider_keeps_commitments_but_reports_zero_available_capacity(self):
        with self.transaction() as db:
            self.seed_provider(db, status="suspended")
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=200)))
            view = allocation.provider_allocation(db, provider_id="provider-one",
                                                   service_month="2026-11")
            self.assertEqual(view["reported_capacity"], 0)
            self.assertEqual(view["committed_appointments"], 200)
            self.assertTrue(view["commitment_breach"])
            self.assertFalse(view["active_in_month"])

    def test_scope_mismatch_is_rejected_for_appointments_and_proposals(self):
        with self.transaction() as db:
            self.seed_provider(db)
            with self.assertRaises(Problem) as captured:
                allocation.register_appointment(db, ServiceAppointment.model_validate(
                    appointment_data(district="other-district")))
            self.assertEqual(captured.exception.code, "provider_scope")
            with self.assertRaises(Problem) as captured:
                allocation.register_proposal(db, ActivityProposal.model_validate(
                    proposal_data(region="rural")))
            self.assertEqual(captured.exception.code, "provider_scope")

    def test_unknown_provider_and_future_cut_are_rejected(self):
        with self.transaction() as db:
            self.seed_provider(db)
            with self.assertRaises(Problem):
                allocation.provider_allocation(db, provider_id="missing",
                                                service_month="2026-11")
            with self.assertRaises(Problem) as captured:
                allocation.provider_allocation(db, provider_id="provider-one",
                                                service_month="2026-11", cut=999)
            self.assertEqual(captured.exception.code, "allocation_cut")

    def test_historical_cut_keeps_the_earlier_allocation_state(self):
        with self.transaction() as db:
            self.seed_provider(db)
            first = allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=300)))
            allocation.register_proposal(db, ActivityProposal.model_validate(
                proposal_data(planned_people=300, status="cancelled", source_revision=2,
                              observed_at="2026-10-26T00:00:00Z")))
            before = allocation.provider_allocation(db, provider_id="provider-one",
                                                     service_month="2026-11", cut=first["seq"])
            self.assertEqual(before["activity_reserved"], 300)
            self.assertEqual(allocation.provider_allocation(
                db, provider_id="provider-one", service_month="2026-11")["activity_reserved"], 0)

    def test_region_view_aggregates_layers_per_district_and_service_kind(self):
        with self.transaction() as db:
            self.seed_provider(db, capacity_per_month=1000)
            registry.register_provider(db, Provider.model_validate(provider_data(
                provider_id="provider-two", district="district-b", capacity_per_month=500)),
                "provider-feed", 1)
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=400)))
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=200)))
            report = allocation.region_allocations(db, region="suburban",
                                                   service_month="2026-11")
            totals = report["totals"]
            self.assertEqual(totals["reported_capacity"], 1500)
            self.assertEqual(totals["committed_appointments"], 400)
            self.assertEqual(totals["activity_reserved"], 200)
            self.assertEqual(totals["remaining_capacity"], 900)
            first = next(row for row in report["areas"] if row["district"] == "district-a")
            self.assertTrue({"reported_capacity", "committed_appointments",
                             "activity_reserved", "remaining_capacity"} <= set(first))


class PlanningIntegrationTests(DatabaseCase, unittest.TestCase):
    def test_service_plan_embeds_four_layer_capacity_per_area(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()),
                                       "provider-feed", 1)
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=500)))
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=300)))
            plan = planning.plan_region(db, region="suburban",
                                        period_end=instant("2026-11-15T00:00:00Z"))
            self.assertEqual(plan["service_month"], "2026-11")
            block = plan["areas"][0]["capacity_allocation"]
            self.assertEqual(block["reported_capacity"], 1200)
            self.assertEqual(block["committed_appointments"], 500)
            self.assertEqual(block["activity_reserved"], 300)
            self.assertEqual(block["remaining_capacity"], 400)
            self.assertEqual(plan["totals"]["committed_appointments"], 500)
            self.assertEqual(plan["totals"]["activity_reserved"], 300)

    def test_service_month_uses_utc_calendar_month(self):
        self.assertEqual(planning.service_month_of(instant("2026-12-31T23:30:00Z")), "2026-12")
        self.assertEqual(
            planning.service_month_of(instant("2027-01-01T00:30:00+02:00")), "2026-12")


class ServiceDeliverySeparationTests(DatabaseCase, unittest.TestCase):
    def test_analysis_separates_delivered_services_from_outreach_and_reservations(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()),
                                       "provider-feed", 1)
            registry.register_touch(db, TouchBatch.model_validate(touch_data(
                exposed_people=200, completed_services=150, nonresponse_people=10,
                report_count=0)))
            allocation.register_appointment(
                db, ServiceAppointment.model_validate(appointment_data(committed_people=500)))
            allocation.register_proposal(
                db, ActivityProposal.model_validate(proposal_data(planned_people=300)))
            document = analysis.run(
                db, cut=registry.source_cut(db)["cut"], as_of=instant("2026-11-02T00:00:00Z"),
                region="suburban", service_kind="meal", cohort="70_79",
                policy=AnalysisPolicy.model_validate(POLICY))
            delivery = document["service_delivery"]
            self.assertEqual(delivery["total_completed_services"], 150)
            self.assertEqual(delivery["total_exposed_people"], 200)
            area = delivery["areas"][0]
            self.assertEqual(area["completed_services"], 150)
            self.assertEqual(area["completion_rate"], "7500.00bp")
            self.assertEqual(document["areas"][0]["completed_services"], 150)
            # 预留与承诺绝不出现在服务交付区块
            self.assertNotIn("activity_reserved", delivery)
            self.assertNotIn("committed_appointments", delivery)

    def test_public_bulletin_suppresses_delivery_counts_for_small_groups(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()),
                                       "provider-feed", 1)
            registry.register_touch(db, TouchBatch.model_validate(touch_data(
                exposed_people=50, completed_services=40, nonresponse_people=0,
                report_count=0)))
            document = analysis.run(
                db, cut=registry.source_cut(db)["cut"], as_of=instant("2026-11-02T00:00:00Z"),
                region="suburban", service_kind="meal", cohort="70_79",
                policy=AnalysisPolicy.model_validate(POLICY))
            bulletin = {"edition": 1, "document": document}
            public = exports.public_view(bulletin)
            row = public["document"]["service_delivery"]["areas"][0]
            self.assertIsNone(row["completed_services"])
            self.assertEqual(row["disclosure_state"], "suppressed_small_group")
            self.assertIsNone(public["document"]["service_delivery"]["total_completed_services"])


class AllocationApiTests(DatabaseCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.store.location))

    def seed(self):
        self.client.post("/providers?source_ref=feed&revision=1", json=provider_data())

    def test_appointment_and_proposal_routes_enforce_capacity_and_expose_views(self):
        self.seed()
        appointment = self.client.post("/appointments",
                                       json=appointment_data(committed_people=500))
        self.assertEqual(appointment.status_code, 200)
        accepted = self.client.post("/proposals", json=proposal_data(planned_people=300))
        self.assertEqual(accepted.status_code, 200)
        blocked = self.client.post("/proposals", json=proposal_data(
            proposal_id="proposal-two", campaign_id="campaign-two", planned_people=500))
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["code"], "capacity_unavailable")
        view = self.client.get("/providers/provider-one/allocations/2026-11")
        self.assertEqual(view.status_code, 200)
        body = view.json()
        self.assertEqual(body["committed_appointments"], 500)
        self.assertEqual(body["activity_reserved"], 300)
        self.assertEqual(body["remaining_capacity"], 400)
        region_view = self.client.get("/regions/suburban/allocations/2026-11")
        self.assertEqual(region_view.status_code, 200)
        self.assertEqual(region_view.json()["totals"]["committed_appointments"], 500)

    def test_put_revision_route_cancels_proposal_and_releases_capacity(self):
        self.seed()
        self.client.post("/proposals", json=proposal_data(planned_people=400))
        revised = proposal_data(planned_people=400, status="cancelled", source_revision=2,
                                observed_at="2026-10-26T00:00:00Z")
        response = self.client.put("/proposals/proposal-one?revision=2", json=revised)
        self.assertEqual(response.status_code, 200)
        mismatched = self.client.put(
            "/proposals/proposal-one?revision=3",
            json=proposal_data(planned_people=400, status="cancelled", source_revision=2,
                               observed_at="2026-10-26T00:00:00Z"))
        self.assertEqual(mismatched.status_code, 422)
        view = self.client.get("/providers/provider-one/allocations/2026-11").json()
        self.assertEqual(view["activity_reserved"], 0)
        self.assertEqual(view["remaining_capacity"], 1200)

    def test_appointment_route_rejects_scope_mismatch_without_writing(self):
        self.seed()
        response = self.client.post("/appointments",
                                    json=appointment_data(district="other-district"))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "provider_scope")


class AllocationBundleTests(DatabaseCase, unittest.TestCase):
    def test_capacity_conflict_rolls_back_the_whole_bundle(self):
        from silverwatch import intake
        from silverwatch.ledger import current_cut

        with self.transaction() as db:
            seeded = {"batch_id": "seed-bundle", "source_ref": "feed", "revision": 1,
                      "records": [
                          {"kind": "providers", "data": provider_data()},
                          {"kind": "appointments",
                           "data": appointment_data(committed_people=1000)},
                          {"kind": "proposals", "data": proposal_data(planned_people=200)}]}
            intake.import_bundle(db, seeded)
            cut = current_cut(db)
            overbooked = {"batch_id": "overbooked-bundle", "source_ref": "feed", "revision": 1,
                          "records": [{"kind": "proposals", "data": proposal_data(
                              proposal_id="proposal-two", campaign_id="campaign-two",
                              planned_people=10)}]}
            with self.assertRaises(Problem) as captured:
                intake.import_bundle(db, overbooked)
            self.assertEqual(captured.exception.code, "bundle_rejected")
            self.assertEqual(captured.exception.details["failures"][0]["code"],
                             "capacity_unavailable")
            self.assertEqual(current_cut(db), cut)

    def test_cancel_proposal_bundle_releases_reserved_capacity(self):
        from silverwatch import intake

        with self.transaction() as db:
            seeded = {"batch_id": "seed-bundle", "source_ref": "feed", "revision": 1,
                      "records": [
                          {"kind": "providers", "data": provider_data()},
                          {"kind": "proposals", "data": proposal_data(planned_people=400)}]}
            intake.import_bundle(db, seeded)
            cancelled = {"batch_id": "cancel-bundle", "source_ref": "feed", "revision": 1,
                         "records": [{"kind": "proposals", "data": proposal_data(
                             planned_people=400, status="cancelled", source_revision=2,
                             observed_at="2026-10-25T00:00:00Z")}]}
            intake.import_bundle(db, cancelled)
            view = allocation.provider_allocation(db, provider_id="provider-one",
                                                   service_month="2026-11")
            self.assertEqual(view["activity_reserved"], 0)
            self.assertEqual(view["remaining_capacity"], 1200)


if __name__ == "__main__":
    unittest.main()
