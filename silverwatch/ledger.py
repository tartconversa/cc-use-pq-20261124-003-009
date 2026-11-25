from __future__ import annotations

from datetime import datetime, timezone
import json
from sqlite3 import Connection

from silverwatch.digest import digest
from silverwatch.errors import conflict, invalid, missing
from silverwatch.storage import encode

EVENT_TYPES = {"provider", "touch", "need", "case", "case_evidence", "decision",
               "remediation", "appointment", "proposal"}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def append(db: Connection, event_type: str, entity_id: str, revision: int,
           source_ref: str, payload: dict, observed_at: datetime) -> dict:
    if revision < 1:
        raise invalid("revision", "来源修订必须为正")
    body = {"event_type": event_type, "entity_id": entity_id, "revision": revision,
            "source_ref": source_ref, "payload": payload}
    fingerprint = digest(body)
    existing = db.execute(
        "SELECT * FROM event WHERE event_type=? AND entity_id=? AND revision=? AND source_ref=?",
        (event_type, entity_id, revision, source_ref),
    ).fetchone()
    if existing is not None:
        if existing["digest"] != fingerprint:
            raise conflict("source_revision_changed", "同一来源修订对应了不同事实",
                           {"entity_id": entity_id, "source_ref": source_ref, "revision": revision})
        return {"seq": existing["seq"], "digest": fingerprint, "replayed": True}
    newest = db.execute(
        "SELECT MAX(revision) FROM event WHERE event_type=? AND entity_id=? AND source_ref=?",
        (event_type, entity_id, source_ref),
    ).fetchone()[0]
    if newest is not None and revision < newest:
        raise conflict("revision_out_of_order", "较旧修订不能改写已有事实",
                       {"current_revision": newest, "submitted_revision": revision})
    cursor = db.execute(
        "INSERT INTO event(event_type,entity_id,revision,source_ref,digest,payload,observed_at) VALUES(?,?,?,?,?,?,?)",
        (event_type, entity_id, revision, source_ref, fingerprint, encode(payload), observed_at.isoformat()),
    )
    return {"seq": cursor.lastrowid, "digest": fingerprint, "replayed": False}


def latest(db: Connection, event_type: str, *, cut: int | None = None,
           entity_id: str | None = None, as_of: datetime | None = None) -> list[dict]:
    conditions = ["event_type=?"]
    params: list = [event_type]
    if cut is not None:
        conditions.append("seq<=?")
        params.append(cut)
    if entity_id is not None:
        conditions.append("entity_id=?")
        params.append(entity_id)
    if as_of is not None:
        conditions.append("observed_at<=?")
        params.append(as_of.isoformat())
    clause = " AND ".join(conditions)
    rows = db.execute(
        f"""WITH ranked AS (
            SELECT *,ROW_NUMBER() OVER(PARTITION BY event_type,entity_id,source_ref
                    ORDER BY revision DESC,seq DESC) AS choice
            FROM event WHERE {clause}
        ) SELECT * FROM ranked WHERE choice=1 ORDER BY entity_id,source_ref""", params,
    )
    return [row_value(row) for row in rows]


def current_entities(db: Connection, event_type: str, *, cut: int | None = None,
                     as_of: datetime | None = None) -> list[dict]:
    """将各个来源修订流解析为单一实体当前视图。"""
    rows = latest(db, event_type, cut=cut, as_of=as_of)
    selected = {}
    for row in rows:
        previous = selected.get(row["entity_id"])
        if previous is None or row["seq"] > previous["seq"]:
            selected[row["entity_id"]] = row
    return [selected[key] for key in sorted(selected)]


def revisions(db: Connection, event_type: str, entity_id: str, *,
              source_ref: str | None = None, cut: int | None = None) -> list[dict]:
    conditions = ["event_type=?", "entity_id=?"]
    params: list = [event_type, entity_id]
    if source_ref is not None:
        conditions.append("source_ref=?")
        params.append(source_ref)
    if cut is not None:
        conditions.append("seq<=?")
        params.append(cut)
    rows = db.execute("SELECT * FROM event WHERE " + " AND ".join(conditions)
                      + " ORDER BY seq", params)
    return [row_value(row) for row in rows]


def history(db: Connection, event_type: str, entity_id: str, source_ref: str | None = None) -> list[dict]:
    sql = "SELECT * FROM event WHERE event_type=? AND entity_id=?"
    params = [event_type, entity_id]
    if source_ref:
        sql += " AND source_ref=?"
        params.append(source_ref)
    rows = db.execute(sql + " ORDER BY source_ref,revision,seq", params)
    return [row_value(row) for row in rows]


def current_cut(db: Connection) -> int:
    return db.execute("SELECT COALESCE(MAX(seq),0) FROM event").fetchone()[0]


def audit_integrity(db: Connection, *, through_cut: int | None = None) -> dict:
    maximum = current_cut(db)
    selected_cut = maximum if through_cut is None else through_cut
    if selected_cut < 0 or selected_cut > maximum:
        raise invalid("audit_cut", "完整性检查截点无效", {"maximum": maximum})
    rows = db.execute("SELECT * FROM event WHERE seq<=? ORDER BY seq", (selected_cut,)).fetchall()
    issues = []
    stream_revisions = {}
    event_counts = {}
    for row in rows:
        if row["event_type"] not in EVENT_TYPES:
            issues.append({"seq": row["seq"], "code": "unknown_event_type",
                           "event_type": row["event_type"]})
        stream = (row["event_type"], row["entity_id"], row["source_ref"])
        prior = stream_revisions.get(stream, 0)
        if row["revision"] <= prior:
            issues.append({"seq": row["seq"], "code": "revision_not_increasing",
                           "previous": prior, "revision": row["revision"]})
        stream_revisions[stream] = max(prior, row["revision"])
        event_counts[row["event_type"]] = event_counts.get(row["event_type"], 0) + 1
        try:
            payload = json.loads(row["payload"])
            occurred = datetime.fromisoformat(row["observed_at"])
            if occurred.tzinfo is None:
                raise ValueError("missing timezone")
            body = {"event_type": row["event_type"], "entity_id": row["entity_id"],
                    "revision": row["revision"], "source_ref": row["source_ref"],
                    "payload": payload}
            calculated = digest(body)
            if calculated != row["digest"]:
                issues.append({"seq": row["seq"], "code": "digest_mismatch",
                               "expected": row["digest"], "calculated": calculated})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            issues.append({"seq": row["seq"], "code": "event_unreadable", "detail": str(exc)})
    return {"valid": not issues, "cut": selected_cut, "current_cut": maximum,
            "event_count": len(rows), "entity_count": len({(row["event_type"], row["entity_id"])
                                                                 for row in rows}),
            "source_stream_count": len(stream_revisions),
            "event_counts": dict(sorted(event_counts.items())), "issues": issues}


def row_value(row) -> dict:
    return {"seq": row["seq"], "event_type": row["event_type"], "entity_id": row["entity_id"],
            "revision": row["revision"], "source_ref": row["source_ref"], "digest": row["digest"],
            "payload": json.loads(row["payload"]), "observed_at": row["observed_at"]}
