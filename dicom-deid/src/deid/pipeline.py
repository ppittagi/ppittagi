"""Per-file processing: the nine steps of design section 3.

A file only reaches the de-identified store when every step passes. Any
early stop leaves it in the raw store with a status and a reason code.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from pydicom.errors import InvalidDicomError

from . import pixel, profile, templates
from .store import Store, SubjectConflict, now

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 2 * 1024**3
MAX_ATTEMPTS = 3  # first try + 2 retries for unexpected errors

ALLOWED_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.6.1": "Ultrasound Image Storage",
    "1.2.840.10008.5.1.4.1.1.6": "Ultrasound Image Storage (Retired)",
    "1.2.840.10008.5.1.4.1.1.3.1": "Ultrasound Multi-frame Image Storage",
    "1.2.840.10008.5.1.4.1.1.3": "Ultrasound Multi-frame Image Storage (Retired)",
    "1.2.840.10008.5.1.4.1.1.7": "Secondary Capture Image Storage",
    "1.2.840.10008.5.1.4.1.1.7.1": "Multi-frame Single Bit Secondary Capture Image Storage",
    "1.2.840.10008.5.1.4.1.1.7.2": "Multi-frame Grayscale Byte Secondary Capture Image Storage",
    "1.2.840.10008.5.1.4.1.1.7.3": "Multi-frame Grayscale Word Secondary Capture Image Storage",
    "1.2.840.10008.5.1.4.1.1.7.4": "Multi-frame True Color Secondary Capture Image Storage",
    "1.2.840.10008.5.1.4.1.1.1": "Computed Radiography Image Storage",
    "1.2.840.10008.5.1.4.1.1.1.1": "Digital X-Ray Image Storage - For Presentation",
    "1.2.840.10008.5.1.4.1.1.1.2": "Digital Mammography X-Ray Image Storage - For Presentation",
    "1.2.840.10008.5.1.4.1.1.2": "CT Image Storage",
    "1.2.840.10008.5.1.4.1.1.4": "MR Image Storage",
    "1.2.840.10008.5.1.4.1.1.12.1": "X-Ray Angiographic Image Storage",
    "1.2.840.10008.5.1.4.1.1.12.2": "X-Ray Radiofluoroscopic Image Storage",
    "1.2.840.10008.5.1.4.1.1.20": "Nuclear Medicine Image Storage",
    "1.2.840.10008.5.1.4.1.1.128": "Positron Emission Tomography Image Storage",
    "1.2.840.10008.5.1.4.1.1.77.1.1": "VL Endoscopic Image Storage",
    "1.2.840.10008.5.1.4.1.1.77.1.4": "VL Photographic Image Storage",
}


@dataclass
class Outcome:
    status: str
    reason_code: str | None = None
    template_version_id: int | None = None
    trace: dict = field(default_factory=dict)
    sop_class_uid: str | None = None
    transfer_syntax: str | None = None
    deid_path: str | None = None
    deid_sha256: str | None = None
    preview_before: str | None = None
    preview_after: str | None = None


class Stop(Exception):
    """An expected early stop with a reason code (not a system error)."""

    def __init__(self, status: str, code: str, **extra):
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def has_dicom_preamble(path: Path) -> bool:
    with open(path, "rb") as f:
        head = f.read(132)
    return len(head) == 132 and head[128:132] == b"DICM"


def _relative(store: Store, path: Path) -> str:
    return str(path.relative_to(store.data_dir))


def _write_previews(ds, base: Path) -> str | None:
    """PNG previews of the first, middle and last frame; returns the first path."""
    first = None
    for i, f in enumerate(pixel.preview_frames(ds)):
        path = base.with_name(f"{base.name}.f{f + 1}.png")
        pixel.preview_png(ds, str(path), frame=f)
        if i == 0:
            first = path
    return first


def process_file(
    store: Store,
    *,
    source: Path,
    study_id: str,
    subject_no: str,
    item_id: int,
    candidates: list[templates.Candidate],
    date_mode: str,
) -> Outcome:
    """Run steps 1-9 for one file. Expected stops come back as an Outcome."""
    out = Outcome(status="PROCESSING")
    try:
        # 1. intake checks
        if source.stat().st_size > MAX_FILE_BYTES:
            raise Stop("REJECTED", "FILE_TOO_LARGE")
        if not has_dicom_preamble(source):
            raise Stop("REJECTED", "INVALID_DICOM")
        try:
            header = pydicom.dcmread(source, stop_before_pixels=True)
        except (InvalidDicomError, ValueError, EOFError) as exc:
            raise Stop("REJECTED", "INVALID_DICOM") from exc
        out.sop_class_uid = str(header.get("SOPClassUID", "")) or None
        out.transfer_syntax = str(header.file_meta.get("TransferSyntaxUID", "")) or None
        if out.sop_class_uid not in ALLOWED_SOP_CLASSES:
            raise Stop("REJECTED", "UNSUPPORTED_SOP")

        # 2. hashing happens at intake; 3. subject check
        patient_id = str(header.get("PatientID", "") or "").strip()
        try:
            offset = store.link_subject(study_id, patient_id or None, subject_no)
        except SubjectConflict:
            raise Stop("QUARANTINED", "SUBJECT_CONFLICT") from None

        # 4. template matching
        result = templates.match(candidates, header)
        out.trace["match"] = result.trace
        if result.status != "MATCHED":
            raise Stop("QUARANTINED", result.status)
        cand = result.selected
        out.template_version_id = cand.version_id
        if cand.spec.kind == "REJECT":
            raise Stop("REJECTED", "REJECTED_BY_TEMPLATE")

        # 5. transfer syntax / colour space
        ds = pydicom.dcmread(source)
        if "PixelData" not in ds:
            raise Stop("REJECTED", "NO_PIXEL_DATA")
        if cand.spec.kind == "REDACT":
            check = pixel.check_supported(ds)
            if not check.ok:
                raise Stop("QUARANTINED", check.code)

        tokens = profile.collect_phi_tokens(ds, date_mode)
        raw_base = store.raw_dir / "previews" / str(item_id)
        raw_base.parent.mkdir(parents=True, exist_ok=True)
        try:
            first = _write_previews(ds, raw_base)
            out.preview_before = _relative(store, first) if first else None
        except Exception:  # preview is best-effort for PASS files with exotic codecs
            if cand.spec.kind == "REDACT":
                raise
            log.warning("item %s: preview failed", item_id)

        # 6. pixel redaction (+ overlays/icons for every kind)
        out.trace["pixel"] = pixel.redact(ds, cand.spec)
        out.trace["removedImages"] = pixel.strip_non_pixel_images(ds)

        # 7. header de-identification
        ctx = profile.DeidContext(
            study_id=study_id,
            subject_no=subject_no,
            date_mode=date_mode,
            date_offset_days=offset,
            uid_mapper=lambda uid: store.map_uid(study_id, uid),
            template_ref=f"{cand.template_code}@v{cand.version}",
            phi_tokens=tokens,
        )
        out.trace["header"] = profile.deidentify(ds, ctx)

        # 8. output verification (values are never logged, only counted)
        residual = profile.find_residual_phi(ds, tokens)
        out.trace["residualPhiCount"] = len(residual)
        if residual:
            raise Stop("QUARANTINED", "RESIDUAL_PHI")

        # 9. store
        target = store.deid_dir / study_id / subject_no / ds.StudyInstanceUID / ds.SeriesInstanceUID / f"{ds.SOPInstanceUID}.dcm"
        target.parent.mkdir(parents=True, exist_ok=True)
        ds.save_as(target, enforce_file_format=True)
        out.deid_path = _relative(store, target)
        out.deid_sha256 = sha256_file(target)
        try:
            first = _write_previews(ds, target.with_suffix(""))
            out.preview_after = _relative(store, first) if first else None
        except Exception:
            if cand.spec.kind == "REDACT":
                raise
        out.status = "DONE"
        return out
    except Stop as stop:
        out.status, out.reason_code = stop.status, stop.code
        return out


def run_item(store: Store, item: dict, job: dict, candidates: list[templates.Candidate], actor: str = "system") -> Outcome:
    """Process one job_item with retries and persist the result + audit row."""
    cfg = store.study_config(job["study_id"])
    with store.tx() as conn:
        conn.execute("UPDATE job_item SET status = 'PROCESSING' WHERE id = ?", (item["id"],))
    source = store.data_dir / item["source_path"]
    outcome = None
    attempts = item.get("attempts", 0)
    for _ in range(MAX_ATTEMPTS):
        attempts += 1
        try:
            outcome = process_file(
                store, source=source, study_id=job["study_id"], subject_no=job["subject_no"],
                item_id=item["id"], candidates=candidates, date_mode=cfg["date_mode"],
            )
            break
        except Exception as exc:  # unexpected: retry, then FAILED
            log.exception("item %s attempt %s failed", item["id"], attempts)
            outcome = Outcome(status="FAILED", reason_code=f"ERROR:{type(exc).__name__}")
    with store.tx() as conn:
        conn.execute(
            """UPDATE job_item SET status = ?, reason_code = ?, template_version_id = ?, match_trace_json = ?,
                   sop_class_uid = COALESCE(?, sop_class_uid), transfer_syntax = COALESCE(?, transfer_syntax),
                   deid_path = ?, deid_sha256 = ?, preview_before = ?, preview_after = ?, attempts = ?, processed_at = ?
               WHERE id = ?""",
            (outcome.status, outcome.reason_code, outcome.template_version_id, json.dumps(outcome.trace, ensure_ascii=False),
             outcome.sop_class_uid, outcome.transfer_syntax, outcome.deid_path, outcome.deid_sha256,
             outcome.preview_before, outcome.preview_after, attempts, now(), item["id"]),
        )
        store.audit(conn, actor, "ITEM_PROCESSED", "job_item", item["id"], after={
            "status": outcome.status, "reason": outcome.reason_code, "templateVersionId": outcome.template_version_id,
            "originalSha256": item["original_sha256"], "deidSha256": outcome.deid_sha256, "engine": profile.ENGINE_VERSION,
        })
    return outcome
