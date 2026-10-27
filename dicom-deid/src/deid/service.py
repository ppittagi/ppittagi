"""Use cases shared by the REST API and the CLI: jobs, templates, quarantine.

Role checks live here so both entry points enforce the same rules.
"""

from __future__ import annotations

import json
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pydicom

from . import pixel, templates
from .pipeline import has_dicom_preamble, run_item, sha256_file
from .store import Store, now

GLOBAL_STUDY = "_global"
MIN_DRY_RUN_SAMPLES = 3
ZIP_MAX_ENTRIES = 5000
ZIP_MAX_UNCOMPRESSED = 10 * 1024**3
SKIP_NAMES = {".DS_Store", "Thumbs.db"}

ROLES = ("SITE_UPLOADER", "DM", "DEID_REVIEWER", "TEMPLATE_AUTHOR", "TEMPLATE_APPROVER", "QA")


class ServiceError(Exception):
    """An error with an API-facing ``code`` and an HTTP-like ``status``."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Actor:
    user: str
    roles: frozenset[str]

    def require(self, *roles: str) -> None:
        if not self.roles.intersection(roles):
            raise ServiceError("FORBIDDEN", f"requires one of: {', '.join(roles)}", 403)


def _study_key(study_id: str | None) -> str:
    return study_id or GLOBAL_STUDY


# --- jobs ------------------------------------------------------------------------


def _safe_extract(zip_path: Path, dest: Path) -> list[Path]:
    """Extract a ZIP with entry, size, path-traversal and symlink limits."""
    files = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > ZIP_MAX_ENTRIES:
            raise ServiceError("ZIP_TOO_MANY_FILES", f"more than {ZIP_MAX_ENTRIES} files")
        if sum(i.file_size for i in infos) > ZIP_MAX_UNCOMPRESSED:
            raise ServiceError("ZIP_TOO_LARGE", "uncompressed size exceeds the limit")
        for i, info in enumerate(infos):
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise ServiceError("ZIP_UNSAFE_PATH", "archive contains an unsafe path")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ServiceError("ZIP_UNSAFE_PATH", "archive contains a symbolic link")
            if name.name in SKIP_NAMES or "__MACOSX" in name.parts or name.name.startswith("._"):
                continue
            # Flatten to an index-prefixed name: originals can be long or clash.
            target = dest / f"{i:05d}_{name.name}"
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            files.append(target)
    return files


def create_job(store: Store, actor: Actor, *, study_id: str, site_id: str, subject_no: str,
               upload: Path, source_name: str) -> dict:
    """Register an upload (a .dcm file or a .zip of them) as a job."""
    actor.require("SITE_UPLOADER")
    for label, value in (("study_id", study_id), ("site_id", site_id), ("subject_no", subject_no)):
        if not value or not value.strip() or "/" in value or ".." in value:
            raise ServiceError("INVALID_INPUT", f"{label} is required and must not contain '/'")
    store.study_config(study_id)
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO processing_job (study_id, site_id, subject_no, uploaded_by, source_name, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'RECEIVED', ?)",
            (study_id, site_id, subject_no, actor.user, source_name, now()),
        )
        job_id = cur.lastrowid
    job_dir = store.raw_dir / "jobs" / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(upload):
        files = _safe_extract(upload, job_dir)
    else:
        target = job_dir / "00000_upload.dcm"
        shutil.copyfile(upload, target)
        files = [target]

    seen: dict[str, Path] = {}
    duplicates = 0
    with store.tx() as conn:
        for f in files:
            digest = sha256_file(f)
            if digest in seen:
                duplicates += 1
                f.unlink()
                continue
            seen[digest] = f
            conn.execute(
                "INSERT INTO job_item (job_id, source_path, original_sha256) VALUES (?, ?, ?)",
                (job_id, str(f.relative_to(store.data_dir)), digest),
            )
        conn.execute("UPDATE processing_job SET status = 'QUEUED' WHERE id = ?", (job_id,))
        store.audit(conn, actor.user, "JOB_CREATED", "processing_job", job_id, after={
            "studyId": study_id, "siteId": site_id, "subjectNo": subject_no, "files": len(seen), "duplicates": duplicates,
        })
    return get_job(store, job_id)


def approved_candidates(store: Store, study_id: str) -> list[templates.Candidate]:
    """The latest approved version of each active template visible to a study."""
    rows = store.query(
        """SELECT v.id, v.version, v.spec_json, t.code, t.study_key
             FROM template_version v JOIN redaction_template t ON t.id = v.template_id
            WHERE v.status = 'APPROVED' AND t.study_key IN (?, ?)""",
        (study_id, GLOBAL_STUDY),
    )
    return [_candidate(r) for r in rows]


def _candidate(row: dict) -> templates.Candidate:
    return templates.Candidate(
        version_id=row["id"], template_code=row["code"], version=row["version"],
        study_id=None if row["study_key"] == GLOBAL_STUDY else row["study_key"],
        spec=templates.parse_spec(json.loads(row["spec_json"])),
    )


def run_job(store: Store, job_id: int) -> dict:
    job = store.one("SELECT * FROM processing_job WHERE id = ?", (job_id,))
    if job is None:
        raise ServiceError("NOT_FOUND", "job not found", 404)
    with store.tx() as conn:
        conn.execute("UPDATE processing_job SET status = 'PROCESSING' WHERE id = ?", (job_id,))
    candidates = approved_candidates(store, job["study_id"])  # loaded once per job
    for item in store.query("SELECT * FROM job_item WHERE job_id = ? AND status = 'PENDING' ORDER BY id", (job_id,)):
        run_item(store, item, job, candidates)
    return _finish_job(store, job_id)


def _finish_job(store: Store, job_id: int) -> dict:
    counts = _counts(store, job_id)
    if counts["failed"]:
        status = "COMPLETED_WITH_ERRORS"
    elif counts["quarantined"]:
        status = "COMPLETED_WITH_QUARANTINE"
    else:
        status = "COMPLETED"
    with store.tx() as conn:
        conn.execute("UPDATE processing_job SET status = ?, finished_at = ? WHERE id = ?", (status, now(), job_id))
        store.audit(conn, "system", "JOB_FINISHED", "processing_job", job_id, after={"status": status, "counts": counts})
    return get_job(store, job_id)


def _counts(store: Store, job_id: int) -> dict:
    rows = store.query("SELECT status, COUNT(*) AS n FROM job_item WHERE job_id = ? GROUP BY status", (job_id,))
    by = {r["status"]: r["n"] for r in rows}
    return {
        "total": sum(by.values()),
        "done": by.get("DONE", 0),
        "quarantined": by.get("QUARANTINED", 0),
        "rejected": by.get("REJECTED", 0),
        "failed": by.get("FAILED", 0),
        "pending": by.get("PENDING", 0) + by.get("PROCESSING", 0),
    }


def get_job(store: Store, job_id: int) -> dict:
    job = store.one("SELECT * FROM processing_job WHERE id = ?", (job_id,))
    if job is None:
        raise ServiceError("NOT_FOUND", "job not found", 404)
    reasons = store.query(
        "SELECT reason_code, COUNT(*) AS n FROM job_item WHERE job_id = ? AND status IN ('QUARANTINED','REJECTED','FAILED') GROUP BY reason_code",
        (job_id,),
    )
    return {
        "jobId": job["id"], "studyId": job["study_id"], "siteId": job["site_id"], "subjectNo": job["subject_no"],
        "status": job["status"], "counts": _counts(store, job_id),
        "reasons": {r["reason_code"]: r["n"] for r in reasons},
        "createdAt": job["created_at"], "finishedAt": job["finished_at"],
    }


ITEM_FIELDS = ("id", "job_id", "status", "reason_code", "template_version_id", "sop_class_uid",
               "transfer_syntax", "original_sha256", "deid_sha256", "attempts", "processed_at")


def item_view(row: dict, with_trace: bool = False) -> dict:
    view = {k: row[k] for k in ITEM_FIELDS}
    view["hasDeidFile"] = bool(row.get("deid_path"))
    if with_trace and row.get("match_trace_json"):
        view["trace"] = json.loads(row["match_trace_json"])
    return view


def list_items(store: Store, job_id: int, status: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM job_item WHERE job_id = ?", [job_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    return [item_view(r) for r in store.query(sql + " ORDER BY id", tuple(params))]


def get_item(store: Store, item_id: int, study_id: str) -> dict:
    row = store.one(
        "SELECT i.* FROM job_item i JOIN processing_job j ON j.id = i.job_id WHERE i.id = ? AND j.study_id = ?",
        (item_id, study_id),
    )
    if row is None:
        raise ServiceError("NOT_FOUND", "item not found", 404)
    return row


def item_file(store: Store, actor: Actor, study_id: str, item_id: int, kind: str) -> Path:
    """Path of a de-identified file or preview, with the access rule for each."""
    row = get_item(store, item_id, study_id)
    if kind == "deid":
        actor.require("DM")
        rel = row["deid_path"]
    elif kind == "preview_after":
        actor.require("DM", "DEID_REVIEWER")
        rel = row["preview_after"]
    elif kind == "preview_before":  # contains PHI: reviewers only, and audited
        actor.require("DEID_REVIEWER")
        rel = row["preview_before"]
    else:
        raise ServiceError("INVALID_INPUT", kind)
    if not rel:
        raise ServiceError("NOT_FOUND", "file not available for this item", 404)
    with store.tx() as conn:
        store.audit(conn, actor.user, f"VIEW_{kind.upper()}", "job_item", item_id)
    return store.data_dir / rel


# --- quarantine ------------------------------------------------------------------


def list_quarantine(store: Store, actor: Actor, study_id: str, reason: str | None = None) -> list[dict]:
    actor.require("DEID_REVIEWER")
    sql = ("SELECT i.* FROM job_item i JOIN processing_job j ON j.id = i.job_id"
           " WHERE j.study_id = ? AND i.status = 'QUARANTINED'")
    params: list = [study_id]
    if reason:
        sql += " AND i.reason_code = ?"
        params.append(reason)
    return [item_view(r, with_trace=True) for r in store.query(sql + " ORDER BY i.id", tuple(params))]


def reprocess(store: Store, actor: Actor, study_id: str, item_id: int, *, comment: str,
              template_version_id: int | None = None) -> dict:
    """Re-run a quarantined item, optionally forcing one approved template version."""
    actor.require("DEID_REVIEWER")
    if not comment.strip():
        raise ServiceError("REASON_REQUIRED", "a comment is required")
    row = get_item(store, item_id, study_id)
    if row["status"] != "QUARANTINED":
        raise ServiceError("INVALID_STATE", f"item is {row['status']}, not QUARANTINED", 409)
    job = store.one("SELECT * FROM processing_job WHERE id = ?", (row["job_id"],))
    candidates = approved_candidates(store, study_id)
    if template_version_id is not None:
        candidates = [c for c in candidates if c.version_id == template_version_id]
        if not candidates:
            raise ServiceError("TEMPLATE_NOT_APPROVED", "the version must be APPROVED and visible to this study")
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO quarantine_review (item_id, action, template_version_id, reviewer, comment, reviewed_at) VALUES (?, 'REPROCESS', ?, ?, ?, ?)",
            (item_id, template_version_id, actor.user, comment, now()),
        )
        conn.execute("UPDATE job_item SET status = 'PENDING' WHERE id = ?", (item_id,))
        store.audit(conn, actor.user, "QUARANTINE_REPROCESS", "job_item", item_id,
                    before={"status": row["status"], "reason": row["reason_code"]},
                    after={"templateVersionId": template_version_id}, reason=comment)
    run_item(store, store.one("SELECT * FROM job_item WHERE id = ?", (item_id,)), job, candidates, actor=actor.user)
    _finish_job(store, job["id"])
    return item_view(get_item(store, item_id, study_id), with_trace=True)


def reject(store: Store, actor: Actor, study_id: str, item_id: int, *, comment: str) -> dict:
    actor.require("DEID_REVIEWER")
    if not comment.strip():
        raise ServiceError("REASON_REQUIRED", "a comment is required")
    row = get_item(store, item_id, study_id)
    if row["status"] != "QUARANTINED":
        raise ServiceError("INVALID_STATE", f"item is {row['status']}, not QUARANTINED", 409)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO quarantine_review (item_id, action, reviewer, comment, reviewed_at) VALUES (?, 'REJECT', ?, ?, ?)",
            (item_id, actor.user, comment, now()),
        )
        conn.execute("UPDATE job_item SET status = 'REJECTED', reason_code = 'REJECTED_BY_REVIEWER' WHERE id = ?", (item_id,))
        store.audit(conn, actor.user, "QUARANTINE_REJECT", "job_item", item_id,
                    before={"status": row["status"], "reason": row["reason_code"]}, reason=comment)
    _finish_job(store, row["job_id"])
    return item_view(get_item(store, item_id, study_id))


# --- templates -------------------------------------------------------------------


def _version_view(row: dict) -> dict:
    samples = row.get("samples")
    return {
        "versionId": row["id"], "templateId": row["template_id"], "version": row["version"], "status": row["status"],
        "spec": json.loads(row["spec_json"]), "contentSha256": row["content_sha256"],
        "createdBy": row["created_by"], "createdAt": row["created_at"],
        "approvedBy": row["approved_by"], "approvedAt": row["approved_at"],
        **({"matchedSamples": samples} if samples is not None else {}),
    }


def _template_view(store: Store, t: dict) -> dict:
    versions = store.query(
        """SELECT v.*, (SELECT COUNT(*) FROM template_sample s WHERE s.version_id = v.id AND s.matched = 1) AS samples
             FROM template_version v WHERE v.template_id = ? ORDER BY v.version""",
        (t["id"],),
    )
    return {
        "templateId": t["id"], "code": t["code"], "name": t["name"],
        "studyId": None if t["study_key"] == GLOBAL_STUDY else t["study_key"], "kind": t["kind"],
        "versions": [_version_view(v) for v in versions],
    }


def list_templates(store: Store, actor: Actor, study_id: str | None) -> list[dict]:
    actor.require("TEMPLATE_AUTHOR", "TEMPLATE_APPROVER", "DEID_REVIEWER")
    rows = store.query("SELECT * FROM redaction_template WHERE study_key IN (?, ?) ORDER BY id", (_study_key(study_id), GLOBAL_STUDY))
    return [_template_view(store, t) for t in rows]


def get_template(store: Store, template_id: int) -> dict:
    t = store.one("SELECT * FROM redaction_template WHERE id = ?", (template_id,))
    if t is None:
        raise ServiceError("NOT_FOUND", "template not found", 404)
    return _template_view(store, t)


def _parse(spec_raw: dict) -> templates.TemplateSpec:
    try:
        return templates.parse_spec(spec_raw)
    except templates.TemplateError as exc:
        raise ServiceError(exc.code, str(exc)) from exc


def create_template(store: Store, actor: Actor, *, code: str, name: str, study_id: str | None,
                    spec_raw: dict, reason: str) -> dict:
    actor.require("TEMPLATE_AUTHOR")
    spec = _parse(spec_raw)
    with store.tx() as conn:
        if conn.execute("SELECT 1 FROM redaction_template WHERE code = ? AND study_key = ?", (code, _study_key(study_id))).fetchone():
            raise ServiceError("TEMPLATE_CODE_EXISTS", f"template {code} already exists", 409)
        tid = conn.execute(
            "INSERT INTO redaction_template (code, name, study_key, kind, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, name, _study_key(study_id), spec.kind, actor.user, now()),
        ).lastrowid
        _insert_version(store, conn, actor, tid, 1, spec, reason)
    return get_template(store, tid)


def _insert_version(store, conn, actor: Actor, template_id: int, version: int, spec: templates.TemplateSpec, reason: str) -> int:
    vid = conn.execute(
        "INSERT INTO template_version (template_id, version, spec_json, content_sha256, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (template_id, version, json.dumps(spec.to_dict(), ensure_ascii=False), spec.content_sha256(), actor.user, now()),
    ).lastrowid
    store.audit(conn, actor.user, "TEMPLATE_VERSION_CREATED", "template_version", vid,
                after={"templateId": template_id, "version": version, "spec": spec.to_dict()}, reason=reason)
    return vid


def new_version(store: Store, actor: Actor, template_id: int, spec_raw: dict, reason: str) -> dict:
    actor.require("TEMPLATE_AUTHOR")
    spec = _parse(spec_raw)
    t = store.one("SELECT * FROM redaction_template WHERE id = ?", (template_id,))
    if t is None:
        raise ServiceError("NOT_FOUND", "template not found", 404)
    if spec.kind != t["kind"]:
        raise ServiceError("TEMPLATE_KIND_CHANGED", "a new version must keep the template kind")
    with store.tx() as conn:
        latest = conn.execute("SELECT MAX(version) FROM template_version WHERE template_id = ?", (template_id,)).fetchone()[0]
        _insert_version(store, conn, actor, template_id, latest + 1, spec, reason)
    return get_template(store, template_id)


def _version(store: Store, template_id: int, version: int) -> dict:
    row = store.one(
        """SELECT v.*, t.code, t.study_key FROM template_version v JOIN redaction_template t ON t.id = v.template_id
            WHERE v.template_id = ? AND v.version = ?""",
        (template_id, version),
    )
    if row is None:
        raise ServiceError("NOT_FOUND", "template version not found", 404)
    return row


def dry_run(store: Store, actor: Actor, template_id: int, version: int, sample: Path) -> dict:
    """Match one sample against one version and render before/after previews.

    Nothing is written to the de-identified store; previews live under raw/.
    """
    actor.require("TEMPLATE_AUTHOR", "TEMPLATE_APPROVER")
    row = _version(store, template_id, version)
    cand = _candidate(row)
    if not has_dicom_preamble(sample):
        raise ServiceError("INVALID_DICOM", "sample is not a DICOM file")
    ds = pydicom.dcmread(sample)
    score, detail = templates.score_candidate(cand, ds)
    digest = sha256_file(sample)
    result = {"matched": score is not None, "score": score, "trace": detail}
    before = after = None
    if score is not None and cand.spec.kind == "REDACT":
        check = pixel.check_supported(ds)
        if not check.ok:
            raise ServiceError(check.code, check.message)
        base = store.raw_dir / "template_samples" / f"v{row['id']}_{digest[:16]}"
        base.parent.mkdir(parents=True, exist_ok=True)
        before, after = f"{base}.before.png", f"{base}.after.png"
        pixel.preview_png(ds, before)
        result["pixel"] = pixel.redact(ds, cand.spec)
        pixel.preview_png(ds, after)
    with store.tx() as conn:
        conn.execute(
            """INSERT INTO template_sample (version_id, sample_sha256, matched, trace_json, preview_before, preview_after, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (version_id, sample_sha256) DO UPDATE SET matched = excluded.matched, trace_json = excluded.trace_json""",
            (row["id"], digest, int(score is not None), json.dumps(detail, ensure_ascii=False),
             before and str(Path(before).relative_to(store.data_dir)), after and str(Path(after).relative_to(store.data_dir)),
             actor.user, now()),
        )
        sample_id = conn.execute("SELECT id FROM template_sample WHERE version_id = ? AND sample_sha256 = ?", (row["id"], digest)).fetchone()[0]
        store.audit(conn, actor.user, "TEMPLATE_DRY_RUN", "template_version", row["id"],
                    after={"sampleSha256": digest, "matched": score is not None})
    result["sampleId"] = sample_id
    return result


def sample_preview(store: Store, actor: Actor, sample_id: int, stage: str) -> Path:
    actor.require("TEMPLATE_AUTHOR", "TEMPLATE_APPROVER")
    row = store.one("SELECT * FROM template_sample WHERE id = ?", (sample_id,))
    rel = row and row["preview_before" if stage == "before" else "preview_after"]
    if not rel:
        raise ServiceError("NOT_FOUND", "preview not available", 404)
    with store.tx() as conn:
        store.audit(conn, actor.user, f"VIEW_SAMPLE_{stage.upper()}", "template_sample", sample_id)
    return store.data_dir / rel


def _matched_samples(store: Store, version_id: int) -> int:
    return store.one("SELECT COUNT(*) AS n FROM template_sample WHERE version_id = ? AND matched = 1", (version_id,))["n"]


def _transition(store: Store, actor: Actor, row: dict, new_status: str, action: str, reason: str, **cols) -> None:
    sets = ", ".join(["status = ?"] + [f"{k} = ?" for k in cols])
    with store.tx() as conn:
        conn.execute(f"UPDATE template_version SET {sets} WHERE id = ?", (new_status, *cols.values(), row["id"]))
        store.audit(conn, actor.user, action, "template_version", row["id"],
                    before={"status": row["status"]}, after={"status": new_status, **cols}, reason=reason)


def submit(store: Store, actor: Actor, template_id: int, version: int, reason: str) -> dict:
    actor.require("TEMPLATE_AUTHOR")
    row = _version(store, template_id, version)
    if row["status"] != "DRAFT":
        raise ServiceError("INVALID_STATE", f"version is {row['status']}", 409)
    if _candidate(row).spec.kind == "REDACT" and _matched_samples(store, row["id"]) < MIN_DRY_RUN_SAMPLES:
        raise ServiceError("NOT_ENOUGH_SAMPLES", f"run at least {MIN_DRY_RUN_SAMPLES} matching samples first")
    _transition(store, actor, row, "IN_REVIEW", "TEMPLATE_SUBMITTED", reason, submitted_at=now())
    return get_template(store, template_id)


def approve(store: Store, actor: Actor, template_id: int, version: int, *, meaning: str, reason: str) -> dict:
    """Approve a version (electronic signature: user + meaning + time).

    The previously approved version of the same template is retired, so a
    template has at most one active version.
    """
    actor.require("TEMPLATE_APPROVER")
    row = _version(store, template_id, version)
    if row["status"] != "IN_REVIEW":
        raise ServiceError("INVALID_STATE", f"version is {row['status']}", 409)
    if row["created_by"] == actor.user:
        raise ServiceError("SELF_APPROVAL", "the author cannot approve their own version", 403)
    if not meaning.strip():
        raise ServiceError("SIGNATURE_MEANING_REQUIRED", "state the meaning of the signature")
    if _candidate(row).spec.kind == "REDACT" and _matched_samples(store, row["id"]) < MIN_DRY_RUN_SAMPLES:
        raise ServiceError("NOT_ENOUGH_SAMPLES", f"at least {MIN_DRY_RUN_SAMPLES} matching samples are required")
    for old in store.query("SELECT * FROM template_version WHERE template_id = ? AND status = 'APPROVED'", (template_id,)):
        _transition(store, actor, old, "RETIRED", "TEMPLATE_RETIRED", f"superseded by v{version}")
    _transition(store, actor, row, "APPROVED", "TEMPLATE_APPROVED", reason,
                approved_by=actor.user, approved_at=now(), approval_meaning=meaning)
    return get_template(store, template_id)


def retire(store: Store, actor: Actor, template_id: int, version: int, reason: str) -> dict:
    actor.require("TEMPLATE_APPROVER")
    row = _version(store, template_id, version)
    if row["status"] != "APPROVED":
        raise ServiceError("INVALID_STATE", f"version is {row['status']}", 409)
    _transition(store, actor, row, "RETIRED", "TEMPLATE_RETIRED", reason)
    return get_template(store, template_id)


def match_test(store: Store, actor: Actor, study_id: str, sample: Path) -> dict:
    """Which approved template would this file get? (Nothing is processed.)"""
    actor.require("TEMPLATE_AUTHOR", "TEMPLATE_APPROVER", "DEID_REVIEWER")
    if not has_dicom_preamble(sample):
        raise ServiceError("INVALID_DICOM", "sample is not a DICOM file")
    header = pydicom.dcmread(sample, stop_before_pixels=True)
    result = templates.match(approved_candidates(store, study_id), header)
    return {"status": result.status, "score": result.score, **result.trace}


def audit_log(store: Store, actor: Actor, entity_type: str | None, entity_id: str | None) -> dict:
    actor.require("QA")
    sql, params = "SELECT * FROM audit_log WHERE 1 = 1", []
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    if entity_id:
        sql += " AND entity_id = ?"
        params.append(entity_id)
    ok, broken = store.verify_audit_chain()
    rows = store.query(sql + " ORDER BY id", tuple(params))
    for r in rows:
        for k in ("before_json", "after_json"):
            r[k] = json.loads(r[k]) if r[k] else None
    return {"chainValid": ok, "firstBrokenId": broken, "entries": rows}


def save_upload(fileobj, suffix: str = "") -> Path:
    """Spool an uploaded stream to a temp file (caller deletes it)."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    with tmp:
        shutil.copyfileobj(fileobj, tmp)
    return Path(tmp.name)
