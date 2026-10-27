import sqlite3

import pydicom
import pytest

from deid import service
from deid.service import ServiceError

from conftest import BAR_H, BAR_W, CANARIES, ROLES, make_dataset, make_sr, write, zip_files


def upload(store, path, subject="S-01-0001", study="STUDY001"):
    job = service.create_job(store, ROLES["uploader"], study_id=study, site_id="S01", subject_no=subject,
                             upload=path, source_name=path.name)
    return service.run_job(store, job["jobId"])


def items_by_status(store, job_id):
    out = {}
    for item in service.list_items(store, job_id):
        out.setdefault(item["status"], []).append(item)
    return out


def test_zip_end_to_end(store, approved_template, tmp_path):
    files = [
        write(make_dataset(photometric="RGB", seed=1), tmp_path / "a.dcm"),
        write(make_dataset(photometric="MONOCHROME2", seed=2), tmp_path / "b.dcm"),
        write(make_dataset(model="ACUSON", seed=3), tmp_path / "unknown_device.dcm"),
        make_sr(tmp_path / "report.dcm"),
    ]
    (tmp_path / "notes.txt").write_text("not dicom")
    files.append(tmp_path / "notes.txt")
    files.append(files[0])  # duplicate in the archive
    job = upload(store, zip_files(files, tmp_path / "exam.zip"))

    assert job["counts"] == {"total": 5, "done": 2, "quarantined": 1, "rejected": 2, "failed": 0, "pending": 0}
    assert job["status"] == "COMPLETED_WITH_QUARANTINE"
    assert job["reasons"] == {"NO_TEMPLATE": 1, "UNSUPPORTED_SOP": 1, "INVALID_DICOM": 1}

    by = items_by_status(store, job["jobId"])
    for item in by["DONE"]:
        path = service.item_file(store, ROLES["dm"], "STUDY001", item["id"], "deid")
        raw = path.read_bytes()
        header = raw.split(b"\x7f\xe0\x10\x00")[0].upper()  # bytes before Pixel Data
        for canary in CANARIES:
            assert canary.encode() not in header, canary
        ds = pydicom.dcmread(path)
        assert (ds.pixel_array[:BAR_H, :BAR_W] == 0).all()
        assert ds.PatientID == "S-01-0001"
        assert not any(e.tag.group == 0x6000 for e in ds)
    # nothing for quarantined or rejected files reached the de-identified store
    assert len(list(store.deid_dir.rglob("*.dcm"))) == 2


def test_same_study_keeps_links_between_uploads(store, approved_template, tmp_path):
    first = upload(store, write(make_dataset(seed=1), tmp_path / "a.dcm"))
    second = upload(store, write(make_dataset(seed=2), tmp_path / "b.dcm"))
    a = pydicom.dcmread(service.item_file(store, ROLES["dm"], "STUDY001", service.list_items(store, first["jobId"])[0]["id"], "deid"))
    b = pydicom.dcmread(service.item_file(store, ROLES["dm"], "STUDY001", service.list_items(store, second["jobId"])[0]["id"], "deid"))
    assert a.StudyInstanceUID == b.StudyInstanceUID  # same original study -> same new UID
    assert a.SOPInstanceUID != b.SOPInstanceUID
    assert a.StudyDate == b.StudyDate  # same subject -> same date offset


def test_subject_conflict_is_quarantined(store, approved_template, tmp_path):
    upload(store, write(make_dataset(seed=1), tmp_path / "a.dcm"), subject="S-01-0001")
    job = upload(store, write(make_dataset(seed=2), tmp_path / "b.dcm"), subject="S-01-0002")
    assert job["reasons"] == {"SUBJECT_CONFLICT": 1}


def test_quarantine_reprocess_with_a_new_template(store, approved_template, tmp_path, sample_template):
    job = upload(store, write(make_dataset(model="ACUSON", seed=3), tmp_path / "acuson.dcm"))
    item = service.list_quarantine(store, ROLES["reviewer"], "STUDY001")[0]
    assert item["reason_code"] == "NO_TEMPLATE"

    spec = sample_template["spec"]
    spec["match"]["all"][2] = {"tag": "ManufacturerModelName", "op": "eq", "value": "ACUSON"}
    t = service.create_template(store, ROLES["author"], code="US-ACUSON", name="Acuson", study_id="STUDY001",
                                spec_raw=spec, reason="new device")
    for i in range(3):
        service.dry_run(store, ROLES["author"], t["templateId"], 1, write(make_dataset(model="ACUSON", seed=20 + i), tmp_path / f"s{i}.dcm"))
    service.submit(store, ROLES["author"], t["templateId"], 1, "ready")
    service.approve(store, ROLES["approver"], t["templateId"], 1, meaning="승인함", reason="ok")

    result = service.reprocess(store, ROLES["reviewer"], "STUDY001", item["id"], comment="template added")
    assert result["status"] == "DONE"
    assert service.get_job(store, job["jobId"])["status"] == "COMPLETED"


def test_quarantine_reject_requires_comment(store, approved_template, tmp_path):
    upload(store, write(make_dataset(model="ACUSON"), tmp_path / "a.dcm"))
    item = service.list_quarantine(store, ROLES["reviewer"], "STUDY001")[0]
    with pytest.raises(ServiceError):
        service.reject(store, ROLES["reviewer"], "STUDY001", item["id"], comment=" ")
    assert service.reject(store, ROLES["reviewer"], "STUDY001", item["id"], comment="unusable")["status"] == "REJECTED"


def test_unapproved_templates_are_never_used(store, sample_template, tmp_path):
    t = service.create_template(store, ROLES["author"], code="draft", name="draft", study_id=None,
                                spec_raw=sample_template["spec"], reason="x")
    job = upload(store, write(make_dataset(), tmp_path / "a.dcm"))
    assert job["reasons"] == {"NO_TEMPLATE": 1}
    assert t["versions"][0]["status"] == "DRAFT"


def test_approval_rules(store, sample_template, tmp_path):
    t = service.create_template(store, ROLES["author"], code="x", name="x", study_id=None,
                                spec_raw=sample_template["spec"], reason="x")
    tid = t["templateId"]
    with pytest.raises(ServiceError) as exc:
        service.submit(store, ROLES["author"], tid, 1, "too early")
    assert exc.value.code == "NOT_ENOUGH_SAMPLES"
    for i in range(3):
        service.dry_run(store, ROLES["author"], tid, 1, write(make_dataset(seed=i), tmp_path / f"{i}.dcm"))
    service.submit(store, ROLES["author"], tid, 1, "ready")

    both = service.Actor("author1", frozenset({"TEMPLATE_AUTHOR", "TEMPLATE_APPROVER"}))
    with pytest.raises(ServiceError) as exc:
        service.approve(store, both, tid, 1, meaning="승인함", reason="self")
    assert exc.value.code == "SELF_APPROVAL"
    with pytest.raises(ServiceError) as exc:
        service.approve(store, ROLES["author"], tid, 1, meaning="승인함", reason="no role")
    assert exc.value.code == "FORBIDDEN"
    service.approve(store, ROLES["approver"], tid, 1, meaning="승인함", reason="ok")

    # v2 approval retires v1: one active version per template
    service.new_version(store, ROLES["author"], tid, {**sample_template["spec"], "priority": 5}, "tweak")
    for i in range(3):
        service.dry_run(store, ROLES["author"], tid, 2, write(make_dataset(seed=10 + i), tmp_path / f"v2_{i}.dcm"))
    service.submit(store, ROLES["author"], tid, 2, "ready")
    t = service.approve(store, ROLES["approver"], tid, 2, meaning="승인함", reason="ok")
    assert [v["status"] for v in t["versions"]] == ["RETIRED", "APPROVED"]


def test_approved_versions_are_immutable_in_the_database(store, approved_template):
    vid = approved_template["versions"][0]["versionId"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.db.execute("UPDATE template_version SET spec_json = '{}' WHERE id = ?", (vid,))


def test_audit_trail_is_append_only_and_chained(store, approved_template, tmp_path):
    upload(store, write(make_dataset(), tmp_path / "a.dcm"))
    log = service.audit_log(store, ROLES["qa"], None, None)
    assert log["chainValid"]
    actions = {e["action"] for e in log["entries"]}
    assert {"TEMPLATE_VERSION_CREATED", "TEMPLATE_DRY_RUN", "TEMPLATE_SUBMITTED", "TEMPLATE_APPROVED",
            "JOB_CREATED", "ITEM_PROCESSED", "JOB_FINISHED"} <= actions
    assert "CANARY" not in str(log)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.db.execute("UPDATE audit_log SET actor = 'x' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.db.execute("DELETE FROM audit_log")


def test_previews_before_redaction_are_reviewer_only(store, approved_template, tmp_path):
    job = upload(store, write(make_dataset(), tmp_path / "a.dcm"))
    item_id = service.list_items(store, job["jobId"])[0]["id"]
    with pytest.raises(ServiceError) as exc:
        service.item_file(store, ROLES["dm"], "STUDY001", item_id, "preview_before")
    assert exc.value.status == 403
    assert service.item_file(store, ROLES["reviewer"], "STUDY001", item_id, "preview_before").exists()
    assert service.item_file(store, ROLES["dm"], "STUDY001", item_id, "preview_after").exists()
    with pytest.raises(ServiceError):
        service.item_file(store, ROLES["dm"], "OTHER", item_id, "deid")  # other study


def test_match_test_does_not_process(store, approved_template, tmp_path):
    r = service.match_test(store, ROLES["author"], "STUDY001", write(make_dataset(), tmp_path / "a.dcm"))
    assert r["status"] == "MATCHED" and r["selected"]["templateCode"] == "US-GE-LOGIQE9-960x720"
    assert "CANARY" not in str(r)
    assert not list(store.deid_dir.rglob("*.dcm"))


def test_unsafe_zip_is_refused(store, tmp_path):
    import zipfile
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../escape.dcm", b"x")
    with pytest.raises(ServiceError) as exc:
        service.create_job(store, ROLES["uploader"], study_id="S", site_id="S01", subject_no="1", upload=bad, source_name="bad.zip")
    assert exc.value.code == "ZIP_UNSAFE_PATH"


def test_uploader_cannot_be_path(store, tmp_path):
    with pytest.raises(ServiceError):
        service.create_job(store, ROLES["uploader"], study_id="../x", site_id="S01", subject_no="1",
                           upload=tmp_path, source_name="x")
