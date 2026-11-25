from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from sqlite3 import Connection

from silverwatch import cases, evidence, registry, review
from silverwatch.errors import missing
from silverwatch.ledger import current_entities, history, revisions


def provider_profile(db: Connection, provider_id: str, *, cut: int | None = None) -> dict:
    provider = next((row for row in registry.catalog(db, cut=cut)
                     if row["provider_id"] == provider_id), None)
    if provider is None:
        raise missing("provider", provider_id)
    touch_batches = [row for row in registry.touch_facts(db, cut=cut)
                     if row["provider_id"] == provider_id]
    related = review.cases_for_provider(db, provider_id, cut=cut)
    by_finding = defaultdict(int)
    for item in related:
        by_finding[item["finding"]["finding"]] += 1
    return {"provider": provider,
            "activity": {"touch_batches": len(touch_batches),
                         "exposed_people": sum(item["exposed_people"] for item in touch_batches),
                         "completed_services": sum(item["completed_services"] for item in touch_batches),
                         "unanswered_people": sum(item["nonresponse_people"] for item in touch_batches),
                         "case_count": len(related), "findings": dict(sorted(by_finding.items()))},
            "cases": related, "cut": cut}


def district_overview(db: Connection, region: str, *, cut: int | None = None,
                      as_of: datetime | None = None) -> dict:
    providers = registry.catalog(db, region=region, cut=cut)
    batches = registry.touch_facts(db, region=region, cut=cut)
    if as_of is not None:
        batches = [row for row in batches if datetime.fromisoformat(row["observed_at"]) <= as_of]
    demand = registry.needs(db, region=region, cut=cut, as_of=as_of)
    cases_now = cases.current_cases(db, cut=cut, as_of=as_of)
    findings = cases.all_findings(db, cut=cut, as_of=as_of)
    areas = defaultdict(lambda: {"providers": 0, "capacity_per_month": 0,
                                 "exposed_people": 0, "nonresponse_people": 0,
                                 "completed_services": 0,
                                 "reported_issues": 0, "substantiated": 0,
                                 "pending": 0, "inconclusive": 0, "estimated_need": 0})
    for row in providers:
        area = areas[row["district"]]
        area["providers"] += 1
        if row["status"] == "active":
            area["capacity_per_month"] += row["capacity_per_month"]
    for row in batches:
        area = areas[row["district"]]
        area["exposed_people"] += row["exposed_people"]
        area["nonresponse_people"] += row["nonresponse_people"]
        area["completed_services"] += row["completed_services"]
        area["reported_issues"] += row["report_count"]
    for row in demand:
        areas[row["district"]]["estimated_need"] += row["estimated_people"]
    for row in cases_now:
        district = next((batch["district"] for batch in batches
                         if batch["batch_id"] == row["payload"]["batch_id"]), None)
        if district is None:
            continue
        finding = findings[row["entity_id"]]["finding"]
        if finding in {"substantiated", "pending", "inconclusive"}:
            areas[district][finding] += 1
    result = []
    for district, value in sorted(areas.items()):
        exposure = value["exposed_people"]
        total = exposure + value["nonresponse_people"]
        value["touch_coverage_bp"] = round(exposure * 10_000 / total) if total else None
        value["reported_issues_per_1000"] = round(value["reported_issues"] * 1000 / exposure, 2) if exposure else None
        value["supply_per_need_bp"] = round(value["capacity_per_month"] * 10_000 / value["estimated_need"]) if value["estimated_need"] else None
        result.append({"district": district, **value})
    return {"region": region, "as_of": as_of.isoformat() if as_of else None,
            "cut": cut, "areas": result, "provider_total": len(providers),
            "touch_batch_total": len(batches), "case_total": len(cases_now)}


def case_timeline(db: Connection, case_id: str, *, cut: int | None = None,
                  as_of: datetime | None = None) -> dict:
    case = next((row for row in cases.current_cases(db, cut=cut, as_of=as_of)
                 if row["entity_id"] == case_id), None)
    if case is None:
        raise missing("case", case_id)
    return {"case": case["payload"], "case_event": case["seq"],
            "evidence": evidence.for_case(db, case_id, cut=cut),
            "decisions": cases.decisions(db, case_id, cut=cut, as_of=as_of),
            "finding": cases.finding(db, case_id, cut=cut, as_of=as_of),
            "remediations": [row for row in cases.current_remediations(db, cut=cut, as_of=as_of)
                             if row["case_id"] == case_id],
            "revisions": revisions(db, "case", case_id, cut=cut)}


def source_history(db: Connection, event_type: str, entity_id: str,
                   source_ref: str | None = None) -> dict:
    rows = history(db, event_type, entity_id, source_ref)
    if not rows:
        raise missing(event_type, entity_id)
    return {"event_type": event_type, "entity_id": entity_id,
            "source_ref": source_ref, "revisions": rows,
            "current_by_source": [row for row in current_entities(db, event_type)
                                  if row["entity_id"] == entity_id]}


def validate_references(db: Connection, *, cut: int | None = None) -> dict:
    """检查分析所用的事实关联，并保留原始来源数据不作修复。"""
    problems = []
    providers = {row["entity_id"] for row in current_entities(db, "provider", cut=cut)}
    touches = {row["entity_id"]: row["payload"] for row in current_entities(db, "touch", cut=cut)}
    needs = current_entities(db, "need", cut=cut)
    for batch_id, row in touches.items():
        if row["provider_id"] not in providers:
            problems.append({"entity": batch_id, "issue": "provider_missing"})
    for row in current_entities(db, "case", cut=cut):
        item = row["payload"]
        batch = touches.get(item["batch_id"])
        if batch is None:
            problems.append({"entity": row["entity_id"], "issue": "touch_missing"})
        elif batch["provider_id"] != item["provider_id"]:
            problems.append({"entity": row["entity_id"], "issue": "provider_scope_mismatch"})
    for row in needs:
        item = row["payload"]
        if item["period_end"] <= item["period_start"]:
            problems.append({"entity": row["entity_id"], "issue": "invalid_need_period"})
    return {"valid": not problems, "checked": {"providers": len(providers),
            "touches": len(touches), "needs": len(needs)}, "issues": problems,
            "cut": cut}
