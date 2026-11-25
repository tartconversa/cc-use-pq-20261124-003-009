from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from fastapi import Body, FastAPI, Query, Response
from pydantic import BaseModel, Field

from silverwatch import (allocation, analysis, cases, channels, comparison, csvio, exports,
                         intake, lineage, ndjson, planning, publication, queries,
                         reconciliation, registry, review, service, trends, workqueue)
from silverwatch.contracts import (ActivityProposal, AnalysisPolicy, AnalysisRequest, Approval,
                                   Case, CaseEvidence, NeedBatch, Provider, PublishRequest,
                                   Remediation, ReviewDecision, ServiceAppointment, TouchBatch)
from silverwatch.errors import Problem
from silverwatch.storage import Store


class CloseRequest(BaseModel):
    expected_finding: str
    revision: int = Field(ge=1)


class AnalyzeRequest(BaseModel):
    analysis_id: str
    region: str
    service_kind: str | None = None
    cohort: str | None = None
    as_of: datetime
    policy: AnalysisPolicy
    through_cut: int | None = None


class BundleRequest(BaseModel):
    batch_id: str
    source_ref: str
    revision: int = Field(ge=1)
    records: list[dict] = Field(min_length=1, max_length=500)
    created_at: datetime | None = None


class ComparisonRequest(BaseModel):
    regions: list[str] = Field(min_length=2, max_length=10)
    as_of: datetime
    policy: AnalysisPolicy
    through_cut: int | None = None
    service_kind: str | None = None
    cohort: str | None = None


class TrendRequest(BaseModel):
    region: str
    periods: list[dict] = Field(min_length=2, max_length=52)
    policy: AnalysisPolicy
    service_kind: str | None = None
    cohort: str | None = None


def create_app(database: str | Path = "silverwatch.sqlite3") -> FastAPI:
    store = Store(database)
    app = FastAPI(title="Silverwatch", version="1.0.0")
    app.state.store = store

    @app.exception_handler(Problem)
    async def problem_handler(_request, exc: Problem):
        return Response(content=__import__("json").dumps(exc.payload(), ensure_ascii=False),
                        status_code=exc.status, media_type="application/json")

    @contextmanager
    def read() -> Iterator:
        with store.transaction() as db:
            yield db

    @contextmanager
    def write() -> Iterator:
        with store.transaction(write=True) as db:
            yield db

    @app.get("/health")
    def health():
        return {"status": "ok", "database": "ready"}

    @app.post("/providers")
    def add_provider(body: Provider, source_ref: str = Query(min_length=1), revision: int = Query(ge=1)):
        with write() as db:
            return registry.register_provider(db, body, source_ref, revision)

    @app.post("/needs")
    def add_need(body: NeedBatch):
        with write() as db:
            return registry.register_need(db, body)

    @app.post("/touches")
    def add_touch(body: TouchBatch):
        with write() as db:
            return registry.register_touch(db, body)

    @app.post("/cases")
    def add_case(body: Case, source_ref: str = Query(min_length=1), revision: int = Query(ge=1)):
        with write() as db:
            return cases.create(db, body, source_ref, revision)

    @app.post("/cases/{case_id}/evidence")
    def add_evidence(case_id: str, body: CaseEvidence):
        if body.case_id != case_id:
            raise Problem(422, "case_scope", "URL 与证据案件编号不一致")
        with write() as db:
            return review.add_evidence(db, body)

    @app.post("/cases/{case_id}/decisions")
    def decide_case(case_id: str, body: ReviewDecision):
        if body.case_id != case_id:
            raise Problem(422, "case_scope", "URL 与复核案件编号不一致")
        with write() as db:
            return review.decide(db, body)

    @app.post("/cases/{case_id}/remediations")
    def add_remediation(case_id: str, body: Remediation):
        if body.case_id != case_id:
            raise Problem(422, "case_scope", "URL 与整改案件编号不一致")
        with write() as db:
            return cases.add_remediation(db, body)

    @app.put("/cases/{case_id}/remediations/{action_id}")
    def update_remediation(case_id: str, action_id: str, body: Remediation,
                           revision: int = Query(ge=1)):
        if body.case_id != case_id or body.action_id != action_id:
            raise Problem(422, "remediation_scope", "URL 与整改对象编号不一致")
        with write() as db:
            return cases.update_remediation(db, action_id, body, revision)

    @app.post("/cases/{case_id}/close")
    def close_case(case_id: str, body: CloseRequest):
        with write() as db:
            return review.close(db, case_id, body.expected_finding, body.revision)

    @app.get("/cases/{case_id}")
    def get_case(case_id: str, cut: int | None = None):
        with read() as db:
            return queries.case_timeline(db, case_id, cut=cut)

    @app.get("/work/cases")
    def case_work(region: str | None = None, as_of: datetime | None = None,
                  cut: int | None = None):
        with read() as db:
            return workqueue.investigation_queue(db, region=region, as_of=as_of, cut=cut)

    @app.get("/work/remediations")
    def remediation_work(as_of: datetime | None = None, cut: int | None = None):
        with read() as db:
            return workqueue.remediation_queue(db, as_of=as_of, cut=cut)

    @app.get("/regions/{region}/attention")
    def region_attention(region: str, as_of: datetime):
        with read() as db:
            return workqueue.area_attention(db, region=region, as_of=as_of)

    @app.get("/providers/{provider_id}")
    def get_provider(provider_id: str, cut: int | None = None):
        with read() as db:
            return queries.provider_profile(db, provider_id, cut=cut)

    @app.get("/regions/{region}/overview")
    def overview(region: str, cut: int | None = None, as_of: datetime | None = None):
        with read() as db:
            return queries.district_overview(db, region, cut=cut, as_of=as_of)

    @app.get("/regions/{region}/service-plan")
    def service_plan(region: str, period_end: datetime, cut: int | None = None):
        with read() as db:
            return planning.plan_region(db, region=region, period_end=period_end, cut=cut)

    @app.post("/appointments")
    def add_appointment(body: ServiceAppointment):
        with write() as db:
            return allocation.register_appointment(db, body)

    @app.post("/proposals")
    def add_proposal(body: ActivityProposal):
        with write() as db:
            return allocation.register_proposal(db, body)

    @app.put("/proposals/{proposal_id}")
    def revise_proposal(proposal_id: str, body: ActivityProposal, revision: int = Query(ge=1)):
        if body.proposal_id != proposal_id:
            raise Problem(422, "proposal_scope", "URL 与提案编号不一致")
        if body.source_revision != revision:
            raise Problem(422, "proposal_revision", "查询参数修订号必须与提案来源修订号一致")
        with write() as db:
            return allocation.register_proposal(db, body)

    @app.get("/providers/{provider_id}/allocations/{service_month}")
    def provider_month_allocation(provider_id: str, service_month: str, cut: int | None = None):
        with read() as db:
            return allocation.provider_allocation(db, provider_id=provider_id,
                                                  service_month=service_month, cut=cut)

    @app.get("/regions/{region}/allocations/{service_month}")
    def region_month_allocation(region: str, service_month: str, cut: int | None = None):
        with read() as db:
            return allocation.region_allocations(db, region=region,
                                                 service_month=service_month, cut=cut)

    @app.get("/regions/{region}/channels")
    def channel_report(region: str, as_of: datetime, cut: int | None = None,
                       service_kind: str | None = None, cohort: str | None = None,
                       minimum_exposure: int = Query(default=100, ge=1)):
        with read() as db:
            report = channels.compare_channels(db, region=region, as_of=as_of,
                cut=registry.source_cut(db)["cut"] if cut is None else cut,
                service_kind=service_kind, cohort=cohort, minimum_exposure=minimum_exposure)
            return {**report, "rate_differences": channels.rate_differences(report)}

    @app.post("/imports/validate")
    def validate_import(body: BundleRequest):
        return intake.validate_bundle(body.model_dump(mode="json"))

    @app.post("/imports/preview")
    def preview_import(body: BundleRequest):
        with write() as db:
            return intake.preview_import(db, body.model_dump(mode="json"))

    @app.post("/imports")
    def import_records(body: BundleRequest):
        with write() as db:
            return intake.import_bundle(db, body.model_dump(mode="json"))

    @app.post("/imports/ndjson/validate")
    def validate_ndjson(text: str = Body(media_type="text/plain"), batch_id: str = Query(min_length=1),
                        source_ref: str = Query(min_length=1), revision: int = Query(ge=1)):
        decoded = ndjson.decode(text, batch_id=batch_id, source_ref=source_ref, revision=revision)
        return {key: decoded[key] for key in ("batch_id", "source_ref", "revision", "record_count",
                "source_digest", "byte_count", "line_count", "validation")}

    @app.post("/imports/ndjson")
    def import_ndjson(text: str = Body(media_type="text/plain"), batch_id: str = Query(min_length=1),
                      source_ref: str = Query(min_length=1), revision: int = Query(ge=1)):
        decoded = ndjson.decode(text, batch_id=batch_id, source_ref=source_ref, revision=revision)
        bundle = {key: decoded[key] for key in ("batch_id", "source_ref", "revision", "records", "created_at")}
        with write() as db:
            return {**intake.import_bundle(db, bundle), "source_digest": decoded["source_digest"]}

    @app.post("/imports/csv/{kind}/validate")
    def validate_csv(kind: str, text: str = Body(media_type="text/plain"),
                     batch_id: str = Query(min_length=1), source_ref: str = Query(min_length=1),
                     revision: int = Query(ge=1)):
        decoded = csvio.decode(text, kind=kind, batch_id=batch_id,
                               source_ref=source_ref, revision=revision)
        return {key: decoded[key] for key in ("batch_id", "source_ref", "revision", "record_count",
                "source_digest", "byte_count", "header", "validation")}

    @app.post("/imports/csv/{kind}")
    def import_csv(kind: str, text: str = Body(media_type="text/plain"),
                   batch_id: str = Query(min_length=1), source_ref: str = Query(min_length=1),
                   revision: int = Query(ge=1)):
        decoded = csvio.decode(text, kind=kind, batch_id=batch_id,
                               source_ref=source_ref, revision=revision)
        bundle = {key: decoded[key] for key in ("batch_id", "source_ref", "revision", "records", "created_at")}
        with write() as db:
            return {**intake.import_bundle(db, bundle), "source_digest": decoded["source_digest"]}

    @app.post("/analyses")
    def create_analysis(body: AnalyzeRequest):
        with write() as db:
            return service.analyze(db, {**body.model_dump(mode="json"),
                                        "through_cut": body.through_cut})

    @app.post("/analysis-comparisons")
    def compare_regions(body: ComparisonRequest):
        with read() as db:
            return comparison.compare_regions(db, regions=body.regions, as_of=body.as_of,
                policy=body.policy, cut=body.through_cut,
                service_kind=body.service_kind, cohort=body.cohort)

    @app.post("/analysis-trends")
    def analyze_trends(body: TrendRequest):
        with read() as db:
            return trends.build(db, region=body.region, periods=body.periods,
                policy=body.policy, service_kind=body.service_kind, cohort=body.cohort)

    @app.post("/approvals")
    def approve(body: Approval):
        with write() as db:
            return service.approve(db, body.model_dump(mode="json"))

    @app.get("/analyses/{analysis_id}/review")
    def analysis_review(analysis_id: str, revision: int | None = None):
        with read() as db:
            return service.review_status(db, analysis_id, revision)

    @app.post("/bulletins")
    def publish_bulletin(body: PublishRequest):
        with write() as db:
            return service.publish(db, body.analysis_id, body.expected_revision)

    @app.get("/bulletins/{analysis_id}")
    def get_bulletin(analysis_id: str, edition: int | None = None):
        with read() as db:
            return exports.public_view(publication.bulletin(db, analysis_id, edition))

    @app.get("/bulletins/{analysis_id}/manifest")
    def bulletin_manifest(analysis_id: str, edition: int | None = None):
        with read() as db:
            return exports.publication_manifest(db, analysis_id, edition)

    @app.get("/bulletins/{analysis_id}/export")
    def export_bulletin(analysis_id: str, format: str = "json", edition: int | None = None):
        if format not in {"json", "csv"}:
            raise Problem(422, "export_format", "format 只支持 json 或 csv")
        with read() as db:
            result = (exports.bulletin_json(db, analysis_id, edition) if format == "json"
                      else exports.bulletin_csv(db, analysis_id, edition))
        return Response(content=result["content"], media_type=result["media_type"],
                        headers={"Content-Disposition": f'attachment; filename="{result["filename"]}"',
                                 "ETag": result["content_digest"]})

    @app.get("/bulletins/{analysis_id}/compare")
    def compare_bulletins(analysis_id: str, left: int, right: int):
        with read() as db:
            return service.compare_publications(db, analysis_id, left, right)

    @app.get("/audit/references")
    def audit_references(cut: int | None = None):
        with read() as db:
            return queries.validate_references(db, cut=cut)

    @app.get("/audit/events")
    def audit_events(cut: int | None = None):
        with read() as db:
            return __import__("silverwatch.ledger", fromlist=["audit_integrity"]).audit_integrity(
                db, through_cut=cut)

    @app.get("/audit/reconciliation")
    def audit_reconciliation(cut: int | None = None, as_of: datetime | None = None):
        with read() as db:
            return reconciliation.reconcile(db, cut=cut, as_of=as_of)

    @app.get("/audit/analysis/{analysis_id}/inputs")
    def analysis_lineage(analysis_id: str, revision: int | None = None):
        with read() as db:
            return lineage.analysis_inputs(db, analysis_id, revision)

    @app.get("/audit/analyses/{analysis_id}")
    def verify_analysis(analysis_id: str, revision: int | None = None):
        with read() as db:
            return lineage.verify_analysis(db, analysis_id, revision)

    @app.get("/audit/bulletins/{analysis_id}/{edition}")
    def verify_bulletin(analysis_id: str, edition: int):
        with read() as db:
            return lineage.verify_bulletin(db, analysis_id, edition)

    return app


