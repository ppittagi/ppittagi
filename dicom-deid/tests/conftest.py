"""Synthetic DICOM factory. Every identifying value is a canary string that
the tests then search for in the output; no real patient data is used."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from deid import service
from deid.service import Actor
from deid.store import Store

CANARY_NAME = "CANARYLAST^CANARYFIRST"
CANARY_ID = "CANARYPID9911"
CANARY_INSTITUTION = "CANARY GENERAL HOSPITAL"
CANARY_OPERATOR = "OPERCANARY^ANNA"
CANARY_DOB = "19470312"
STUDY_DATE = "20250909"
CANARIES = ("CANARYLAST", "CANARYFIRST", CANARY_ID, CANARY_INSTITUTION, "OPERCANARY", CANARY_DOB, STUDY_DATE,
            "CANARYACC77", "CANARYSTATION", "CANARYSERIAL42", "CANARYPRIVATE", "CANARYREFDOC", "CANARYCOMMENT")

US_IMAGE = "1.2.840.10008.5.1.4.1.1.6.1"
BAR_H, BAR_W = 67, 669

ROLES = {
    "uploader": Actor("site1", frozenset({"SITE_UPLOADER"})),
    "author": Actor("author1", frozenset({"TEMPLATE_AUTHOR"})),
    "approver": Actor("approver1", frozenset({"TEMPLATE_APPROVER"})),
    "reviewer": Actor("reviewer1", frozenset({"DEID_REVIEWER"})),
    "dm": Actor("dm1", frozenset({"DM"})),
    "qa": Actor("qa1", frozenset({"QA"})),
}


def make_dataset(*, rows=720, cols=960, photometric="RGB", frames=1, model="LOGIQE9", modality="US",
                 sop_class=US_IMAGE, patient_id=CANARY_ID, bits=8, signed=False, seed=0) -> Dataset:
    rng = np.random.default_rng(seed)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.SourceApplicationEntityTitle = "CANARYSTATION"

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = "1.2.826.0.1.3680043.99.1"
    ds.SeriesInstanceUID = "1.2.826.0.1.3680043.99.1.2"
    ds.FrameOfReferenceUID = "1.2.826.0.1.3680043.99.1.3"
    ds.Modality = modality
    ds.Manufacturer = "GE Healthcare"
    ds.ManufacturerModelName = model
    ds.SoftwareVersions = f"{model}:R2.0.3"
    ds.StationName = "CANARYSTATION"
    ds.DeviceSerialNumber = "CANARYSERIAL42"
    ds.InstitutionName = CANARY_INSTITUTION
    ds.PatientName = CANARY_NAME
    ds.PatientID = patient_id
    ds.PatientBirthDate = CANARY_DOB
    ds.PatientSex = "F"
    ds.PatientAge = "093Y"
    ds.OtherPatientIDs = "CANARYPID9911-B"
    ds.ReferringPhysicianName = "CANARYREFDOC^JOHN"
    ds.OperatorsName = CANARY_OPERATOR
    ds.AccessionNumber = "CANARYACC77"
    ds.StudyID = "CANARYACC77"
    ds.StudyDate = ds.SeriesDate = ds.ContentDate = STUDY_DATE
    ds.AcquisitionDateTime = STUDY_DATE + "102904.000000"
    ds.StudyTime = "102904"
    ds.StudyDescription = "VASCULAR LE ARTERIAL"
    ds.SeriesDescription = f"CANARYLAST {STUDY_DATE}"  # unclean descriptor
    ds.ImageComments = "CANARYCOMMENT"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "VASCULAR"]
    item = Dataset()
    item.ReferencedSOPClassUID = US_IMAGE
    item.ReferencedSOPInstanceUID = "1.2.826.0.1.3680043.99.1.2.77"
    ds.ReferencedImageSequence = Sequence([item])
    nested = Dataset()
    nested.PersonName = "CANARYLAST^NESTED"  # PN inside a sequence must go too
    nested.CodeMeaning = "observer"
    ds.ContributingEquipmentSequence = Sequence([nested])
    ds.add_new(0x00091010, "LO", "CANARYPRIVATE")
    ds.add_new(0x00090010, "LO", "GEMS_PRIVATE")

    samples = 1 if photometric.startswith("MONOCHROME") else 3
    shape = (rows, cols) if samples == 1 else (rows, cols, 3)
    if frames > 1:
        shape = (frames, *shape)
    lead = (slice(None),) if frames > 1 else ()
    text_blocks = [lead + (slice(10, 40), slice(40, 300)), lead + (slice(10, 40), slice(380, 620))]
    if bits == 16:
        # 12 bits stored: unsigned 0..4095, signed -2048..2047
        arr = rng.integers(-1000 if signed else 100, 1500, size=shape).astype(np.int16 if signed else np.uint16)
        bright = 2000 if signed else 4000
    else:
        arr = rng.integers(20, 200, size=shape, dtype=np.uint8)
        bright = 255
    for block in text_blocks:  # "burned-in text" in the top bar
        arr[block] = bright
    ds.set_pixel_data(arr, photometric if not photometric.startswith("YBR") else "RGB", 12 if bits == 16 else 8,
                      generate_instance_uid=False)
    if photometric.startswith("YBR"):
        ds.PhotometricInterpretation = photometric  # data stays RGB; only used for decode-path tests
    # an overlay plane with burned-in text
    ds.add_new(0x60000010, "US", rows)
    ds.add_new(0x60000011, "US", cols)
    ds.add_new(0x60003000, "OW", b"\x00" * (rows * cols // 8))
    return ds


def write(ds: Dataset, path: Path) -> Path:
    ds.save_as(path, enforce_file_format=True)
    return path


def make_sr(path: Path) -> Path:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.11"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.Modality = "SR"
    ds.PatientName = CANARY_NAME
    return write(ds, path)


def zip_files(paths: list[Path], dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w") as zf:
        for p in paths:
            zf.write(p, f"exam/{p.name}")
    return dest


TEMPLATE_FILE = Path(__file__).resolve().parents[1] / "templates" / "US-GE-LOGIQE9-960x720.json"


@pytest.fixture
def store(tmp_path, monkeypatch) -> Store:
    monkeypatch.setenv("DEID_HMAC_KEY", "test-key")
    return Store(tmp_path / "data")


@pytest.fixture
def sample_template() -> dict:
    return json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))


@pytest.fixture
def approved_template(store, sample_template, tmp_path) -> dict:
    """The shipped GE LOGIQ E9 template, dry-run on 3 samples and approved."""
    t = service.create_template(store, ROLES["author"], code=sample_template["code"], name=sample_template["name"],
                                study_id=None, spec_raw=sample_template["spec"], reason="initial")
    for i, pi in enumerate(("RGB", "MONOCHROME2", "RGB")):
        sample = write(make_dataset(photometric=pi, seed=i + 10), tmp_path / f"sample{i}.dcm")
        assert service.dry_run(store, ROLES["author"], t["templateId"], 1, sample)["matched"]
    service.submit(store, ROLES["author"], t["templateId"], 1, "ready")
    return service.approve(store, ROLES["approver"], t["templateId"], 1, meaning="승인함", reason="reviewed")
