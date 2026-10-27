import itertools

import pytest

from deid import profile
from deid.profile import DeidContext

from conftest import CANARIES, STUDY_DATE, make_dataset


def run(ds, date_mode="SHIFT", offset=10):
    counter = itertools.count(1)
    mapping = {}

    def uid_mapper(uid):
        return mapping.setdefault(uid, f"2.25.{next(counter)}")

    tokens = profile.collect_phi_tokens(ds, date_mode)
    ctx = DeidContext(study_id="STUDY001", subject_no="S-01-0003", date_mode=date_mode, date_offset_days=offset,
                      uid_mapper=uid_mapper, template_ref="T@v1", phi_tokens=tokens)
    log = profile.deidentify(ds, ctx)
    return ds, tokens, mapping, log


def test_every_canary_is_gone_from_the_header():
    ds, tokens, _, _ = run(make_dataset(rows=8, cols=8))
    header = profile.header_bytes(ds).upper()
    for canary in CANARIES:
        assert canary.encode() not in header, canary
    assert profile.find_residual_phi(ds, tokens) == []


def test_identity_is_replaced_with_the_subject():
    ds, *_ = run(make_dataset(rows=8, cols=8))
    assert str(ds.PatientName) == "STUDY001^S-01-0003"
    assert ds.PatientID == "S-01-0003"
    assert ds.PatientBirthDate == ""
    assert ds.ReferringPhysicianName == ""
    assert ds.AccessionNumber == "" and ds.StudyID == ""
    for removed in ("InstitutionName", "OperatorsName", "OtherPatientIDs", "StationName", "DeviceSerialNumber", "ImageComments"):
        assert removed not in ds


def test_device_identity_and_clinical_values_are_kept():
    ds, *_ = run(make_dataset(rows=8, cols=8))
    assert ds.Manufacturer == "GE Healthcare" and ds.ManufacturerModelName == "LOGIQE9"
    assert ds.SoftwareVersions == "LOGIQE9:R2.0.3"
    assert ds.PatientSex == "F" and ds.StudyTime == "102904"
    assert ds.PatientAge == "090Y"  # capped above 89


def test_private_tags_and_nested_person_names_are_removed():
    ds, *_ = run(make_dataset(rows=8, cols=8))
    assert not any(e.tag.is_private for e in ds.iterall())
    assert "PersonName" not in ds.ContributingEquipmentSequence[0]
    assert ds.ContributingEquipmentSequence[0].CodeMeaning == "observer"
    assert "SourceApplicationEntityTitle" not in ds.file_meta


def test_dates_are_shifted_consistently():
    ds, *_ = run(make_dataset(rows=8, cols=8), offset=10)
    assert ds.StudyDate == ds.SeriesDate == ds.ContentDate == "20250830"
    assert ds.AcquisitionDateTime == "20250830102904.000000"
    assert ds.LongitudinalTemporalInformationModified == "MODIFIED"


def test_keep_and_remove_date_modes():
    kept, *_ = run(make_dataset(rows=8, cols=8), date_mode="KEEP")
    assert kept.StudyDate == STUDY_DATE and kept.LongitudinalTemporalInformationModified == "UNMODIFIED"
    removed, *_ = run(make_dataset(rows=8, cols=8), date_mode="REMOVE")
    assert removed.StudyDate == "" and removed.LongitudinalTemporalInformationModified == "REMOVED"
    assert removed.PatientBirthDate == ""


def test_instance_uids_are_remapped_class_uids_kept():
    ds = make_dataset(rows=8, cols=8)
    original_study = ds.StudyInstanceUID
    ds, _, mapping, _ = run(ds)
    assert ds.StudyInstanceUID == mapping[original_study]
    assert ds.SOPClassUID == "1.2.840.10008.5.1.4.1.1.6.1"
    ref = ds.ReferencedImageSequence[0]
    assert ref.ReferencedSOPClassUID == "1.2.840.10008.5.1.4.1.1.6.1"
    assert ref.ReferencedSOPInstanceUID.startswith("2.25.")
    assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID


def test_unclean_descriptor_is_emptied_clean_one_kept():
    ds, *_ = run(make_dataset(rows=8, cols=8))
    assert ds.SeriesDescription == ""
    assert ds.StudyDescription == "VASCULAR LE ARTERIAL"


def test_method_is_recorded():
    ds, *_ = run(make_dataset(rows=8, cols=8))
    assert ds.PatientIdentityRemoved == "YES"
    assert all(len(v) <= 64 for v in ds.DeidentificationMethod)
    codes = {i.CodeValue for i in ds.DeidentificationMethodCodeSequence}
    assert codes == {"113100", "113105", "113107", "113109"}


def test_station_name_equal_to_model_is_not_a_false_positive():
    ds = make_dataset(rows=8, cols=8)
    ds.StationName = "LOGIQE9"
    ds, tokens, *_ = run(ds)
    assert "LOGIQE9" not in tokens
    assert profile.find_residual_phi(ds, tokens) == []


def test_residual_phi_is_detected():
    ds = make_dataset(rows=8, cols=8)
    tokens = profile.collect_phi_tokens(ds, "SHIFT")
    ds.StudyDescription = "for CANARYLAST"
    assert "CANARYLAST" in profile.find_residual_phi(ds, tokens)


def test_bad_date_mode():
    with pytest.raises(ValueError):
        run(make_dataset(rows=8, cols=8), date_mode="FUZZ")
