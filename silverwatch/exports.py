from __future__ import annotations

import csv
import copy
import io
import json
from datetime import datetime, timezone
from sqlite3 import Connection

from silverwatch import publication
from silverwatch.digest import digest
from silverwatch.errors import invalid


def bulletin_json(db: Connection, analysis_id: str, edition: int | None = None) -> dict:
    value = publication.bulletin(db, analysis_id, edition)
    public = public_view(value)
    canonical = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"media_type": "application/json", "filename": f"{analysis_id}-{value['edition']}.json",
            "content": canonical, "content_digest": digest(public),
            "generated_at": datetime.now(timezone.utc).isoformat()}


def bulletin_csv(db: Connection, analysis_id: str, edition: int | None = None) -> dict:
    value = publication.bulletin(db, analysis_id, edition)
    output = io.StringIO(newline="")
    columns = ["district", "service_kind", "cohort", "exposed", "nonresponse",
               "coverage_unknown_batches", "reports", "substantiated", "pending",
               "inconclusive", "substantiated_rate", "eligible", "gaps"]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    public = public_view(value)
    for row in public["document"]["areas"]:
        writer.writerow({**row, "eligible": str(row["eligible"]).lower(),
                         "gaps": ";".join(row["gaps"])})
    text = output.getvalue()
    return {"media_type": "text/csv; charset=utf-8",
            "filename": f"{analysis_id}-{value['edition']}.csv",
            "content": "\ufeff" + text, "content_digest": digest(text),
            "edition": value["edition"]}


def publication_manifest(db: Connection, analysis_id: str, edition: int | None = None) -> dict:
    bulletin = publication.bulletin(db, analysis_id, edition)
    body = bulletin["document"]
    return {"analysis_id": analysis_id, "edition": bulletin["edition"],
            "analysis_digest": bulletin["analysis_digest"],
            "bulletin_digest": bulletin["bulletin_digest"],
            "region": body["region"], "service_kind": body["service_kind"],
            "cohort": body["cohort"], "cut": body["cut"], "as_of": body["as_of"],
            "area_count": len(body["areas"]),
            "risk_area_count": len(body["risk_flags"]),
            "case_counts": body["case_counts"], "published_at": bulletin["published_at"]}


def parse_edition(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise invalid("edition", "公告版本必须是整数") from exc
    if parsed < 1:
        raise invalid("edition", "公告版本必须大于零")
    return parsed


def public_view(bulletin: dict) -> dict:
    result = copy.deepcopy(bulletin)
    document = result["document"]
    threshold = document["policy"]["minimum_exposure"]
    masks = {"exposed", "nonresponse", "uncertainty_people", "reports", "substantiated",
             "pending", "inconclusive", "verified_remediations", "substantiated_rate",
             "potential_rate_bounds", "reconciliation_incomplete_batches", "batches",
             "completed_services", "completion_rate"}
    for row in document["areas"]:
        row.pop("batches", None)
        row.pop("case_ids", None)
        row.pop("capacity_providers", None)
        if row["exposed"] < threshold:
            for field in masks:
                if field in row:
                    row[field] = None
            row["disclosure_state"] = "suppressed_small_group"
            row["suppressed_fields"] = sorted(masks)
    for row in document["risk_flags"]:
        identity = (row["district"], row["service_kind"], row["cohort"])
        area = next((item for item in document["areas"]
                     if (item["district"], item["service_kind"], item["cohort"]) == identity), None)
        if area is not None and area.get("disclosure_state"):
            row.update({key: area[key] for key in area if key in masks or key.startswith("disclosure_")})
            row.pop("case_ids", None)
            row.pop("batches", None)
            row.pop("capacity_providers", None)
    for value in document["supply"].values():
        demand = value["estimated_people"]
        value.pop("capacity_providers", None)
        if demand < threshold:
            value["estimated_people"] = None
            value["monthly_capacity"] = None
            value["capacity_ratio"] = None
            value["disclosure_state"] = "suppressed_small_group"
    delivery = document.get("service_delivery")
    if delivery:
        for row in delivery["areas"]:
            row.pop("batches", None)
            if row["exposed_people"] < threshold:
                row["completed_services"] = None
                row["exposed_people"] = None
                row["nonresponse_people"] = None
                row["completion_rate"] = None
                row["disclosure_state"] = "suppressed_small_group"
        delivery["total_completed_services"] = None
        delivery["total_exposed_people"] = None
    if sum(document["case_counts"].values()) < threshold:
        document["case_counts"] = {key: None for key in document["case_counts"]}
        document["case_counts_disclosure"] = "suppressed_small_group"
    result["public_projection"] = 1
    return result
