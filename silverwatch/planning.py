from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from sqlite3 import Connection

from silverwatch import allocation
from silverwatch.analysis import format_rate
from silverwatch.errors import invalid
from silverwatch.ledger import current_cut
from silverwatch.registry import catalog, needs


def service_month_of(period_end: datetime) -> str:
    return period_end.astimezone(timezone.utc).strftime("%Y-%m")


def plan_region(db: Connection, *, region: str, period_end: datetime,
                cut: int | None = None) -> dict:
    if period_end.tzinfo is None:
        raise invalid("planning_time", "规划时点必须包含时区")
    chosen_cut = current_cut(db) if cut is None else cut
    if chosen_cut > current_cut(db):
        raise invalid("planning_cut", "规划截点尚未存在")
    provider_rows = catalog(db, region=region, cut=chosen_cut)
    demand_rows = needs(db, region=region, cut=chosen_cut, as_of=period_end)
    supply_by_area = defaultdict(int)
    providers_by_area = defaultdict(list)
    for row in provider_rows:
        start = datetime.fromisoformat(row["active_from"])
        stop = datetime.fromisoformat(row["active_until"]) if row["active_until"] else None
        if row["status"] != "active" or start > period_end or stop and stop <= period_end:
            continue
        key = (row["district"], row["service_kind"])
        supply_by_area[key] += row["capacity_per_month"]
        providers_by_area[key].append(row["provider_id"])
    demand_by_area = defaultdict(int)
    cohorts_by_area = defaultdict(lambda: defaultdict(int))
    for row in demand_rows:
        key = (row["district"], row["service_kind"])
        demand_by_area[key] += row["estimated_people"]
        cohorts_by_area[key][row["cohort"]] += row["estimated_people"]
    service_month = service_month_of(period_end)
    capacity_view = allocation.region_allocations(db, region=region,
                                                   service_month=service_month, cut=chosen_cut)
    capacity_by_area = {(row["district"], row["service_kind"]): row
                        for row in capacity_view["areas"]}
    rows = []
    for district, kind in sorted(set(supply_by_area) | set(demand_by_area)):
        supply = supply_by_area[(district, kind)]
        demand = demand_by_area[(district, kind)]
        gap = max(demand - supply, 0)
        capacity = capacity_by_area.get((district, kind))
        rows.append({"district": district, "service_kind": kind,
                     "monthly_capacity": supply, "estimated_people": demand,
                     "capacity_ratio": format_rate(Fraction(supply, demand) if demand else None),
                     "unserved_estimate": gap,
                     "provider_ids": sorted(providers_by_area[(district, kind)]),
                     "cohort_need": dict(sorted(cohorts_by_area[(district, kind)].items())),
                     "demand_observed": demand > 0,
                     "capacity_observed": bool(providers_by_area[(district, kind)]),
                     "capacity_allocation": _allocation_block(capacity)})
    return {"region": region, "period_end": period_end.isoformat(), "cut": chosen_cut,
            "service_month": service_month,
            "areas": rows,
            "totals": {"estimated_people": sum(row["estimated_people"] for row in rows),
                       "monthly_capacity": sum(row["monthly_capacity"] for row in rows),
                       "unserved_estimate": sum(row["unserved_estimate"] for row in rows),
                       "committed_appointments": capacity_view["totals"]["committed_appointments"],
                       "activity_reserved": capacity_view["totals"]["activity_reserved"],
                       "remaining_capacity": capacity_view["totals"]["remaining_capacity"],
                       "available_capacity": capacity_view["totals"]["available_capacity"]},
            "capacity_note": "报告容量是机构名录月度能力；已承诺预约不可被活动预留挤占；活动预留来自未取消提案；实际完成服务只统计触达批次，不在容量视图内。"}


def _allocation_block(area: dict | None) -> dict:
    if area is None:
        return {"reported_capacity": 0, "committed_appointments": 0,
                "activity_reserved": 0, "remaining_capacity": 0,
                "available_capacity": 0, "commitment_breach": False,
                "overcommitted": False}
    return {key: area[key] for key in ("reported_capacity", "committed_appointments",
                                       "activity_reserved", "remaining_capacity",
                                       "available_capacity", "commitment_breach",
                                       "overcommitted")}


def compare_plans(first: dict, second: dict) -> dict:
    if first["region"] != second["region"]:
        raise invalid("planning_region", "供需版本比较必须属于同一区域")
    before = {(row["district"], row["service_kind"]): row for row in first["areas"]}
    after = {(row["district"], row["service_kind"]): row for row in second["areas"]}
    changes = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        old_capacity = old["monthly_capacity"] if old else 0
        new_capacity = new["monthly_capacity"] if new else 0
        old_need = old["estimated_people"] if old else 0
        new_need = new["estimated_people"] if new else 0
        changes.append({"district": key[0], "service_kind": key[1],
                        "capacity_delta": new_capacity - old_capacity,
                        "need_delta": new_need - old_need,
                        "unserved_delta": (new["unserved_estimate"] if new else 0)
                            - (old["unserved_estimate"] if old else 0),
                        "appeared": old is None, "removed": new is None})
    return {"before_cut": first["cut"], "after_cut": second["cut"],
            "region": first["region"], "changes": changes}
