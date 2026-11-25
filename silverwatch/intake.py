from __future__ import annotations

from datetime import datetime
from sqlite3 import Connection
from typing import Callable

from pydantic import ValidationError

from silverwatch import evidence, registry, review
from silverwatch.contracts import (ActivityProposal, Case, CaseEvidence, NeedBatch,
                                   Provider, ReviewDecision, ServiceAppointment, TouchBatch)
from silverwatch.errors import Problem, invalid
from silverwatch.ledger import current_cut

HANDLERS: dict[str, tuple[type, Callable]] = {
    "providers": (Provider, registry.register_provider),
    "needs": (NeedBatch, registry.register_need),
    "touches": (TouchBatch, registry.register_touch),
    "appointments": (ServiceAppointment, None),
    "proposals": (ActivityProposal, None),
    "cases": (Case, None),
    "evidence": (CaseEvidence, evidence.add),
    "decisions": (ReviewDecision, review.decide),
}


def _validate(kind: str, value: dict):
    model, _ = HANDLERS[kind]
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        details = [{"path": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"], "type": item["type"]}
                   for item in exc.errors(include_url=False)]
        raise invalid("record_invalid", f"{kind} 记录不符合数据契约", details) from exc


def _dispatch(db: Connection, kind: str, model, source: str, revision: int) -> dict:
    if kind == "providers":
        return registry.register_provider(db, model, source, revision)
    if kind == "needs":
        return registry.register_need(db, model)
    if kind == "touches":
        return registry.register_touch(db, model)
    if kind == "appointments":
        from silverwatch import allocation

        return allocation.register_appointment(db, model)
    if kind == "proposals":
        from silverwatch import allocation

        return allocation.register_proposal(db, model)
    if kind == "cases":
        return registry.register_case(db, model.model_dump(mode="json"), source, revision)
    if kind == "evidence":
        return evidence.add(db, model)
    if kind == "decisions":
        return review.decide(db, model)
    raise invalid("record_kind", "不支持的导入记录类型", {"kind": kind})


def import_bundle(db: Connection, bundle: dict) -> dict:
    """按来源批次校验并写入事实，调用方负责包围同一个写事务。"""
    if set(bundle) - {"batch_id", "source_ref", "revision", "records", "created_at"}:
        raise invalid("bundle_shape", "导入批次包含不支持的顶层字段")
    batch_id = bundle.get("batch_id")
    source = bundle.get("source_ref")
    revision = bundle.get("revision")
    records = bundle.get("records")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise invalid("bundle_id", "导入批次必须有稳定编号")
    if not isinstance(source, str) or not source.strip():
        raise invalid("bundle_source", "导入批次必须标明来源")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise invalid("bundle_revision", "导入批次修订号必须为正整数")
    if not isinstance(records, list) or not records:
        raise invalid("bundle_records", "导入批次至少包含一条记录")
    if len(records) > 500:
        raise invalid("bundle_limit", "单次导入最多接受 500 条记录")
    start_cut = current_cut(db)
    accepted = []
    failures = []
    seen = set()
    for position, envelope in enumerate(records):
        try:
            if not isinstance(envelope, dict) or set(envelope) != {"kind", "data"}:
                raise invalid("record_envelope", "记录必须包含 kind 与 data")
            kind, data = envelope["kind"], envelope["data"]
            if kind not in HANDLERS or not isinstance(data, dict):
                raise invalid("record_kind", "记录类型或数据对象无效")
            model = _validate(kind, data)
            identity = (kind, getattr(model, "provider_id", None) or
                        getattr(model, "estimate_id", None) or
                        getattr(model, "batch_id", None) or
                        getattr(model, "appointment_id", None) or
                        getattr(model, "proposal_id", None) or
                        getattr(model, "case_id", None) or
                        getattr(model, "evidence_id", None) or
                        getattr(model, "decision_id", None))
            if identity in seen:
                raise invalid("duplicate_in_bundle", "同一导入批次不能重复包含同类对象")
            seen.add(identity)
            result = _dispatch(db, kind, model, source, revision)
            accepted.append({"position": position, "kind": kind, "identity": identity[1],
                             "seq": result.get("seq"), "replayed": result.get("replayed", False)})
        except Problem as exc:
            failures.append({"position": position, **exc.payload()})
    if failures:
        raise invalid("bundle_rejected", "导入批次未完整应用；事务应整体回滚",
                      {"batch_id": batch_id, "accepted": accepted, "failures": failures,
                       "start_cut": start_cut})
    return {"batch_id": batch_id, "source_ref": source, "revision": revision,
            "accepted_count": len(accepted), "replayed_count": sum(row["replayed"] for row in accepted),
            "records": accepted, "start_cut": start_cut, "end_cut": current_cut(db),
            "created_at": bundle.get("created_at") or datetime.now().astimezone().isoformat()}


def validate_bundle(bundle: dict) -> dict:
    """在开启写事务前检查批次记录的字段结构。"""
    if not isinstance(bundle, dict):
        raise invalid("bundle_shape", "导入内容必须是对象")
    counts = {kind: 0 for kind in HANDLERS}
    errors = []
    records = bundle.get("records")
    if not isinstance(records, list):
        raise invalid("bundle_records", "records 必须是数组")
    for position, envelope in enumerate(records):
        if not isinstance(envelope, dict) or envelope.get("kind") not in HANDLERS:
            errors.append({"position": position, "code": "record_kind"})
            continue
        kind = envelope["kind"]
        try:
            _validate(kind, envelope.get("data"))
            counts[kind] += 1
        except (Problem, TypeError) as exc:
            errors.append({"position": position, "kind": kind,
                           "code": exc.code if isinstance(exc, Problem) else "record_shape"})
    return {"valid": not errors, "counts": {key: value for key, value in counts.items() if value},
            "errors": errors, "record_count": len(records)}


def preview_import(db: Connection, bundle: dict) -> dict:
    """在回滚的预演事务中检查记录关系及当前保存状态。"""
    start_cut = current_cut(db)
    db.execute("SAVEPOINT silverwatch_import_preview")
    try:
        receipt = import_bundle(db, bundle)
    except Problem as exc:
        db.execute("ROLLBACK TO silverwatch_import_preview")
        db.execute("RELEASE silverwatch_import_preview")
        return {"ready": False, "batch_id": bundle.get("batch_id"), "start_cut": start_cut,
                "end_cut": current_cut(db), "error": exc.payload()}
    except BaseException:
        db.execute("ROLLBACK TO silverwatch_import_preview")
        db.execute("RELEASE silverwatch_import_preview")
        raise
    db.execute("ROLLBACK TO silverwatch_import_preview")
    db.execute("RELEASE silverwatch_import_preview")
    return {"ready": True, "batch_id": receipt["batch_id"],
            "source_ref": receipt["source_ref"], "record_count": receipt["accepted_count"],
            "replay_count": receipt["replayed_count"], "would_change_cut": receipt["end_cut"] - start_cut,
            "start_cut": start_cut, "resulting_cut": receipt["end_cut"],
            "would_write": receipt["replayed_count"] != receipt["accepted_count"]}
