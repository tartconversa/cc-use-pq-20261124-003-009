from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from sqlite3 import Connection

from silverwatch.contracts import ActivityProposal, ServiceAppointment
from silverwatch.errors import conflict, invalid
from silverwatch.ledger import append, current_cut, current_entities

APPOINTMENT_EVENT = "appointment"
PROPOSAL_EVENT = "proposal"
ACTIVE_PROPOSAL_STATES = ("proposed", "confirmed")
CANCELLED = "cancelled"


def month_end(service_month: str) -> datetime:
    from calendar import monthrange
    from datetime import timezone

    year, month = (int(part) for part in service_month.split("-"))
    last_day = monthrange(year, month)[1]
    return datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)


def register_appointment(db: Connection, item: ServiceAppointment) -> dict:
    provider = _provider(db, item.provider_id)
    _require_same_scope(provider, item)
    return append(db, APPOINTMENT_EVENT, item.appointment_id, item.source_revision,
                  item.source_ref, item.model_dump(mode="json"), item.observed_at)


def register_proposal(db: Connection, item: ActivityProposal) -> dict:
    provider = _provider(db, item.provider_id)
    _require_same_scope(provider, item)
    existing = _current_proposal(db, item.proposal_id)
    if existing is not None and existing == item.model_dump(mode="json"):
        return append(db, PROPOSAL_EVENT, item.proposal_id, item.source_revision,
                      item.source_ref, item.model_dump(mode="json"), item.observed_at)
    if existing is None:
        if item.status == CANCELLED:
            raise invalid("proposal_state", "尚未提交的活动提案不能直接取消")
    else:
        _validate_revision(existing, item)
        if item.status != CANCELLED:
            _ensure_capacity(db, provider, item.service_month, item.planned_people,
                             exclude_proposal=item.proposal_id)
    if existing is None and item.status != CANCELLED:
        _ensure_capacity(db, provider, item.service_month, item.planned_people)
    return append(db, PROPOSAL_EVENT, item.proposal_id, item.source_revision,
                  item.source_ref, item.model_dump(mode="json"), item.observed_at)


def _validate_revision(existing: dict, item: ActivityProposal) -> None:
    if item.source_revision <= existing["source_revision"]:
        raise conflict("revision_out_of_order", "活动提案修订必须使用更新的来源修订号",
                       {"current_revision": existing["source_revision"],
                        "submitted_revision": item.source_revision})
    if existing["status"] == CANCELLED:
        raise conflict("proposal_cancelled", "已取消的活动提案不能重新启用，需新建提案")
    for field in ("campaign_id", "provider_id", "region", "district",
                  "service_kind", "service_month"):
        if getattr(item, field) != existing[field]:
            raise invalid("proposal_identity", "活动提案修订不能改变机构、月份或活动归属")


def _ensure_capacity(db: Connection, provider: dict, service_month: str,
                     planned_people: int, exclude_proposal: str | None = None) -> None:
    view = provider_allocation(db, provider_id=provider["provider_id"],
                               service_month=service_month)
    already_held = sum(row["planned_people"] for row in view["active_proposals"]
                       if row["proposal_id"] != exclude_proposal)
    committed = view["committed_appointments"]
    available = view["reported_capacity"] - committed - already_held
    if planned_people > available:
        raise conflict("capacity_unavailable", "活动预留不能挤占既有预约或超过机构月度服务能力",
                       {"provider_id": provider["provider_id"], "service_month": service_month,
                        "reported_capacity": view["reported_capacity"],
                        "committed_appointments": committed,
                        "held_by_other_proposals": already_held,
                        "requested_people": planned_people,
                        "available_people": max(available, 0)})


def _provider(db: Connection, provider_id: str) -> dict:
    row = next((entry for entry in current_entities(db, "provider")
                if entry["entity_id"] == provider_id), None)
    if row is None:
        from silverwatch.errors import missing

        raise missing("provider", provider_id)
    return row["payload"]


def _require_same_scope(provider: dict, item) -> None:
    if (provider["region"] != item.region or provider["district"] != item.district
            or provider["service_kind"] != item.service_kind):
        raise invalid("provider_scope", "预约或活动的地区、街道、服务类型必须与机构一致",
                      {"provider_id": item.provider_id})


def _current_proposal(db: Connection, proposal_id: str) -> dict | None:
    row = next((entry for entry in current_entities(db, PROPOSAL_EVENT)
                if entry["entity_id"] == proposal_id), None)
    return row["payload"] if row else None


def current_appointments(db: Connection, *, cut: int | None = None,
                         as_of: datetime | None = None) -> list[dict]:
    return [row["payload"] | {"event_seq": row["seq"]}
            for row in current_entities(db, APPOINTMENT_EVENT, cut=cut, as_of=as_of)]


def current_proposals(db: Connection, *, cut: int | None = None,
                      as_of: datetime | None = None) -> list[dict]:
    return [row["payload"] | {"event_seq": row["seq"]}
            for row in current_entities(db, PROPOSAL_EVENT, cut=cut, as_of=as_of)]


def provider_allocation(db: Connection, *, provider_id: str, service_month: str,
                        cut: int | None = None) -> dict:
    """单个机构在单个服务月份的四层容量视图。"""
    chosen_cut = current_cut(db) if cut is None else cut
    if chosen_cut > current_cut(db):
        raise invalid("allocation_cut", "容量视图截点尚未存在")
    provider_row = next((entry for entry in current_entities(db, "provider", cut=chosen_cut)
                         if entry["entity_id"] == provider_id), None)
    if provider_row is None:
        from silverwatch.errors import missing

        raise missing("provider", provider_id)
    provider = provider_row["payload"]
    active_window = _active_in_month(provider, service_month)
    reported = provider["capacity_per_month"] if active_window else 0
    appointments = [row for row in current_appointments(db, cut=chosen_cut)
                    if row["provider_id"] == provider_id and row["service_month"] == service_month]
    proposals = [row for row in current_proposals(db, cut=chosen_cut)
                 if row["provider_id"] == provider_id and row["service_month"] == service_month]
    active_proposals = [row for row in proposals if row["status"] in ACTIVE_PROPOSAL_STATES]
    committed = sum(row["committed_people"] for row in appointments)
    held = sum(row["planned_people"] for row in active_proposals)
    remaining = reported - committed - held
    return {
        "provider_id": provider_id, "service_month": service_month, "cut": chosen_cut,
        "provider_status": provider["status"], "active_in_month": active_window,
        "reported_capacity": reported,
        "committed_appointments": committed,
        "activity_reserved": held,
        "remaining_capacity": max(remaining, 0),
        "available_capacity": remaining,
        "commitment_breach": committed > reported,
        "overcommitted": remaining < 0,
        "appointments": sorted(appointments, key=lambda row: row["appointment_id"]),
        "active_proposals": sorted(active_proposals, key=lambda row: row["proposal_id"]),
        "cancelled_proposals": sorted(
            (row for row in proposals if row["status"] == CANCELLED),
            key=lambda row: row["proposal_id"]),
    }


def region_allocations(db: Connection, *, region: str, service_month: str,
                       cut: int | None = None) -> dict:
    chosen_cut = current_cut(db) if cut is None else cut
    if chosen_cut > current_cut(db):
        raise invalid("allocation_cut", "容量视图截点尚未存在")
    providers = [row["payload"] for row in current_entities(db, "provider", cut=chosen_cut)
                 if row["payload"]["region"] == region]
    provider_ids = {row["provider_id"] for row in providers}
    appointments = [row for row in current_appointments(db, cut=chosen_cut)
                    if row["region"] == region and row["service_month"] == service_month]
    proposals = [row for row in current_proposals(db, cut=chosen_cut)
                 if row["region"] == region and row["service_month"] == service_month]
    areas = defaultdict(lambda: {"reported_capacity": 0, "committed_appointments": 0,
                                 "activity_reserved": 0, "remaining_capacity": 0,
                                 "available_capacity": 0, "provider_count": 0,
                                 "commitment_breach": False, "overcommitted": False,
                                 "provider_ids": []})
    for provider in providers:
        view = provider_allocation(db, provider_id=provider["provider_id"],
                                   service_month=service_month, cut=chosen_cut)
        key = (provider["district"], provider["service_kind"])
        area = areas[key]
        area["reported_capacity"] += view["reported_capacity"]
        area["committed_appointments"] += view["committed_appointments"]
        area["activity_reserved"] += view["activity_reserved"]
        area["remaining_capacity"] += view["remaining_capacity"]
        area["available_capacity"] += view["available_capacity"]
        area["commitment_breach"] = area["commitment_breach"] or view["commitment_breach"]
        area["overcommitted"] = area["overcommitted"] or view["overcommitted"]
        area["provider_count"] += 1
        area["provider_ids"].append(provider["provider_id"])
    rows = []
    for (district, kind), area in sorted(areas.items()):
        rows.append({"district": district, "service_kind": kind,
                     "provider_ids": sorted(area["provider_ids"]), **{
                         key: value for key, value in area.items() if key != "provider_ids"}})
    totals = {"reported_capacity": sum(row["reported_capacity"] for row in rows),
              "committed_appointments": sum(row["committed_appointments"] for row in rows),
              "activity_reserved": sum(row["activity_reserved"] for row in rows),
              "remaining_capacity": sum(row["remaining_capacity"] for row in rows),
              "available_capacity": sum(row["available_capacity"] for row in rows)}
    return {"region": region, "service_month": service_month, "cut": chosen_cut,
            "provider_count": len(provider_ids), "areas": rows, "totals": totals,
            "appointment_count": len(appointments),
            "active_proposal_count": sum(1 for row in proposals
                                         if row["status"] in ACTIVE_PROPOSAL_STATES)}


def _active_in_month(provider: dict, service_month: str) -> bool:
    if provider["status"] != "active":
        return False
    end = month_end(service_month)
    start = datetime.fromisoformat(provider["active_from"])
    stop = datetime.fromisoformat(provider["active_until"]) if provider["active_until"] else None
    return start <= end and (stop is None or end < stop)
