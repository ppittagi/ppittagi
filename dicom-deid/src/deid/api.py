"""REST API (design section 8). Run with ``uvicorn deid.api:app``.

Authentication is a placeholder: the caller passes ``X-User`` and
``X-Roles`` headers. Replace ``current_actor`` with the SSO integration
before any deployment outside a development machine.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import service
from .service import Actor, ServiceError
from .store import Store

app = FastAPI(title="DICOM de-identification", version="1.0.0")
PREFIX = "/api/v1/studies/{study_id}"


@lru_cache(maxsize=1)
def get_store() -> Store:
    return Store(os.environ.get("DEID_DATA_DIR", "./data"))


def current_actor(x_user: Annotated[str, Header()], x_roles: Annotated[str, Header()] = "") -> Actor:
    roles = frozenset(r.strip() for r in x_roles.split(",") if r.strip())
    unknown = roles - set(service.ROLES)
    if unknown:
        raise ServiceError("UNKNOWN_ROLE", f"unknown roles: {', '.join(sorted(unknown))}", 403)
    return Actor(user=x_user, roles=roles)


StoreDep = Annotated[Store, Depends(get_store)]
ActorDep = Annotated[Actor, Depends(current_actor)]


@app.exception_handler(ServiceError)
async def service_error(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": str(exc)})


class _Upload:
    """Spool an UploadFile to disk for the duration of a request."""

    def __init__(self, upload: UploadFile):
        self.upload = upload

    def __enter__(self) -> Path:
        self.path = service.save_upload(self.upload.file, suffix=Path(self.upload.filename or "").suffix)
        return self.path

    def __exit__(self, *_):
        self.path.unlink(missing_ok=True)


# --- jobs ------------------------------------------------------------------------


@app.post(PREFIX + "/jobs", status_code=202)
def create_job(study_id: str, store: StoreDep, actor: ActorDep, background: BackgroundTasks,
               file: UploadFile = File(...), site_id: str = Form(...), subject_no: str = Form(...)):
    with _Upload(file) as path:
        job = service.create_job(store, actor, study_id=study_id, site_id=site_id, subject_no=subject_no,
                                 upload=path, source_name=file.filename or "upload")
    background.add_task(service.run_job, store, job["jobId"])
    return job


def _job_in_study(store: Store, study_id: str, job_id: int) -> dict:
    job = service.get_job(store, job_id)
    if job["studyId"] != study_id:
        raise ServiceError("NOT_FOUND", "job not found", 404)
    return job


@app.get(PREFIX + "/jobs/{job_id}")
def get_job(study_id: str, job_id: int, store: StoreDep, actor: ActorDep):
    actor.require("SITE_UPLOADER", "DM")
    return _job_in_study(store, study_id, job_id)


@app.get(PREFIX + "/jobs/{job_id}/items")
def list_items(study_id: str, job_id: int, store: StoreDep, actor: ActorDep, status: str | None = None):
    actor.require("DM", "DEID_REVIEWER")
    _job_in_study(store, study_id, job_id)
    return service.list_items(store, job_id, status)


@app.get(PREFIX + "/items/{item_id}")
def get_item(study_id: str, item_id: int, store: StoreDep, actor: ActorDep):
    actor.require("DM", "DEID_REVIEWER")
    return service.item_view(service.get_item(store, item_id, study_id), with_trace=True)


@app.get(PREFIX + "/items/{item_id}/preview")
def item_preview(study_id: str, item_id: int, store: StoreDep, actor: ActorDep, stage: str = "after"):
    kind = "preview_before" if stage == "before" else "preview_after"
    return FileResponse(service.item_file(store, actor, study_id, item_id, kind), media_type="image/png")


@app.get(PREFIX + "/items/{item_id}/download")
def item_download(study_id: str, item_id: int, store: StoreDep, actor: ActorDep):
    path = service.item_file(store, actor, study_id, item_id, "deid")
    return FileResponse(path, media_type="application/dicom", filename=path.name)


# --- quarantine ------------------------------------------------------------------


class ReviewBody(BaseModel):
    comment: str
    templateVersionId: int | None = None


@app.get(PREFIX + "/quarantine")
def list_quarantine(study_id: str, store: StoreDep, actor: ActorDep, reason: str | None = None):
    return service.list_quarantine(store, actor, study_id, reason)


@app.post(PREFIX + "/quarantine/{item_id}/reprocess")
def reprocess(study_id: str, item_id: int, body: ReviewBody, store: StoreDep, actor: ActorDep):
    return service.reprocess(store, actor, study_id, item_id, comment=body.comment,
                             template_version_id=body.templateVersionId)


@app.post(PREFIX + "/quarantine/{item_id}/reject")
def reject(study_id: str, item_id: int, body: ReviewBody, store: StoreDep, actor: ActorDep):
    return service.reject(store, actor, study_id, item_id, comment=body.comment)


# --- templates -------------------------------------------------------------------


class TemplateBody(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    shared: bool = False  # True = visible to every study
    spec: dict
    reason: str = Field(min_length=1)


class VersionBody(BaseModel):
    spec: dict
    reason: str = Field(min_length=1)


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1)


class ApproveBody(BaseModel):
    meaning: str = Field(min_length=1, description="Meaning of the signature, e.g. '승인함'")
    reason: str = Field(min_length=1)


@app.get(PREFIX + "/templates")
def list_templates(study_id: str, store: StoreDep, actor: ActorDep):
    return service.list_templates(store, actor, study_id)


@app.post(PREFIX + "/templates", status_code=201)
def create_template(study_id: str, body: TemplateBody, store: StoreDep, actor: ActorDep):
    return service.create_template(store, actor, code=body.code, name=body.name,
                                   study_id=None if body.shared else study_id, spec_raw=body.spec, reason=body.reason)


def _visible_template(store: Store, study_id: str, template_id: int) -> dict:
    t = service.get_template(store, template_id)
    if t["studyId"] not in (None, study_id):
        raise ServiceError("NOT_FOUND", "template not found", 404)
    return t


@app.get(PREFIX + "/templates/{template_id}")
def get_template(study_id: str, template_id: int, store: StoreDep, actor: ActorDep):
    actor.require("TEMPLATE_AUTHOR", "TEMPLATE_APPROVER", "DEID_REVIEWER")
    return _visible_template(store, study_id, template_id)


@app.post(PREFIX + "/templates/{template_id}/versions", status_code=201)
def new_version(study_id: str, template_id: int, body: VersionBody, store: StoreDep, actor: ActorDep):
    _visible_template(store, study_id, template_id)
    return service.new_version(store, actor, template_id, body.spec, body.reason)


@app.post(PREFIX + "/templates/{template_id}/versions/{version}/dry-run")
def dry_run(study_id: str, template_id: int, version: int, store: StoreDep, actor: ActorDep,
            file: UploadFile = File(...)):
    _visible_template(store, study_id, template_id)
    with _Upload(file) as path:
        return service.dry_run(store, actor, template_id, version, path)


@app.get(PREFIX + "/template-samples/{sample_id}/preview")
def sample_preview(study_id: str, sample_id: int, store: StoreDep, actor: ActorDep, stage: str = "after"):
    return FileResponse(service.sample_preview(store, actor, sample_id, stage), media_type="image/png")


@app.post(PREFIX + "/templates/{template_id}/versions/{version}/submit")
def submit(study_id: str, template_id: int, version: int, body: ReasonBody, store: StoreDep, actor: ActorDep):
    _visible_template(store, study_id, template_id)
    return service.submit(store, actor, template_id, version, body.reason)


@app.post(PREFIX + "/templates/{template_id}/versions/{version}/approve")
def approve(study_id: str, template_id: int, version: int, body: ApproveBody, store: StoreDep, actor: ActorDep):
    _visible_template(store, study_id, template_id)
    return service.approve(store, actor, template_id, version, meaning=body.meaning, reason=body.reason)


@app.post(PREFIX + "/templates/{template_id}/versions/{version}/retire")
def retire(study_id: str, template_id: int, version: int, body: ReasonBody, store: StoreDep, actor: ActorDep):
    _visible_template(store, study_id, template_id)
    return service.retire(store, actor, template_id, version, body.reason)


@app.post(PREFIX + "/templates/match-test")
def match_test(study_id: str, store: StoreDep, actor: ActorDep, file: UploadFile = File(...)):
    with _Upload(file) as path:
        return service.match_test(store, actor, study_id, path)


@app.get("/api/v1/audit")
def audit(store: StoreDep, actor: ActorDep, entityType: str | None = None, entityId: str | None = None):
    return service.audit_log(store, actor, entityType, entityId)
