"""Header de-identification: DICOM PS3.15 Annex E Basic Application Level
Confidentiality Profile with the Retain Device Identity and Clean Descriptors
options, plus subject pseudonymisation, date shifting and UID remapping.

Action codes follow PS3.15: X remove, Z empty, D dummy, U new UID, K keep,
C clean, plus S (shift date) and M (map to the subject number).
"""

from __future__ import annotations

import datetime as dt
import io
import re
from dataclasses import dataclass, field
from typing import Callable

from pydicom.dataelem import DataElement
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.multival import MultiValue
from pydicom.sequence import Sequence

ENGINE_VERSION = "CUBE-DEID v1.0"
DICOM_ROOT = "1.2.840.10008."

# Elements the profile handles explicitly. Everything not listed keeps its
# value, except that private tags and any person name (PN) are removed, dates
# follow the study's date mode and instance UIDs are remapped.
ACTIONS: dict[str, str] = {
    # Patient
    "PatientName": "D",
    "PatientID": "M",
    "PatientBirthDate": "Z",
    "PatientSex": "K",
    "PatientAge": "K",  # capped at 090Y
    "IssuerOfPatientID": "X",
    "IssuerOfPatientIDQualifiersSequence": "X",
    "OtherPatientIDs": "X",
    "OtherPatientIDsSequence": "X",
    "OtherPatientNames": "X",
    "PatientBirthName": "X",
    "PatientMotherBirthName": "X",
    "PatientBirthTime": "X",
    "PatientAddress": "X",
    "PatientTelephoneNumbers": "X",
    "PatientTelecomInformation": "X",
    "PatientInsurancePlanCodeSequence": "X",
    "PatientPrimaryLanguageCodeSequence": "X",
    "PatientReligiousPreference": "X",
    "PatientComments": "X",
    "PatientState": "X",
    "PatientSize": "X",
    "PatientWeight": "X",
    "MedicalRecordLocator": "X",
    "MilitaryRank": "X",
    "BranchOfService": "X",
    "CountryOfResidence": "X",
    "RegionOfResidence": "X",
    "EthnicGroup": "X",
    "Occupation": "X",
    "AdditionalPatientHistory": "X",
    "ResponsiblePerson": "X",
    "ResponsibleOrganization": "X",
    "ReferencedPatientSequence": "X",
    "ReferencedPatientPhotoSequence": "X",
    # Visit / request / procedure step
    "AdmissionID": "X",
    "IssuerOfAdmissionIDSequence": "X",
    "AdmittingDiagnosesDescription": "X",
    "AdmittingDiagnosesCodeSequence": "X",
    "CurrentPatientLocation": "X",
    "AccessionNumber": "Z",
    "IssuerOfAccessionNumberSequence": "X",
    "StudyID": "Z",
    "RequestAttributesSequence": "X",
    "RequestedProcedureDescription": "X",
    "RequestedProcedureID": "X",
    "ScheduledProcedureStepID": "X",
    "PerformedProcedureStepID": "X",
    "PerformedProcedureStepDescription": "X",
    "PerformedProcedureStepStartDate": "X",
    "PerformedProcedureStepStartTime": "X",
    "PerformedProcedureStepEndDate": "X",
    "PerformedProcedureStepEndTime": "X",
    "ReferencedPerformedProcedureStepSequence": "X",
    "ReferencedStudySequence": "X",
    "StudyComments": "X",
    "InterpretationAuthor": "X",
    # People
    "ReferringPhysicianName": "Z",
    "ReferringPhysicianAddress": "X",
    "ReferringPhysicianTelephoneNumbers": "X",
    "ReferringPhysicianIdentificationSequence": "X",
    "ConsultingPhysicianName": "X",
    "PhysiciansOfRecord": "X",
    "PhysiciansOfRecordIdentificationSequence": "X",
    "PerformingPhysicianName": "X",
    "PerformingPhysicianIdentificationSequence": "X",
    "NameOfPhysiciansReadingStudy": "X",
    "PhysiciansReadingStudyIdentificationSequence": "X",
    "OperatorsName": "X",
    "OperatorIdentificationSequence": "X",
    "RequestingPhysician": "X",
    "ScheduledPerformingPhysicianName": "X",
    "ContentCreatorName": "Z",
    # Institution / device (Retain Device Identity keeps make, model, software)
    "InstitutionName": "X",
    "InstitutionAddress": "X",
    "InstitutionalDepartmentName": "X",
    "InstitutionalDepartmentTypeCodeSequence": "X",
    "InstitutionCodeSequence": "X",
    "StationName": "X",
    "DeviceSerialNumber": "X",
    "DetectorID": "X",
    "PlateID": "X",
    "GantryID": "X",
    "Manufacturer": "K",
    "ManufacturerModelName": "K",
    "SoftwareVersions": "K",
    "TimezoneOffsetFromUTC": "X",
    # Free text (Clean Descriptors keeps descriptions that look clean)
    "StudyDescription": "C",
    "SeriesDescription": "C",
    "ProtocolName": "C",
    "ImageComments": "X",
    "FrameComments": "X",
    "AcquisitionComments": "X",
    "DerivationDescription": "C",
    "OverlayComments": "X",
    # Audit and signature payloads may carry the original values
    "OriginalAttributesSequence": "X",
    "ModifiedAttributesSequence": "X",
    "DigitalSignaturesSequence": "X",
    "MACParametersSequence": "X",
    "DataSetTrailingPadding": "X",
}

# File meta information elements that identify the sender.
FILE_META_REMOVE = ("SourceApplicationEntityTitle", "SendingApplicationEntityTitle", "ReceivingApplicationEntityTitle", "PrivateInformation", "PrivateInformationCreatorUID")

DATE_MODES = ("SHIFT", "KEEP", "REMOVE")
AGE_CAP = "090Y"

CODE_BASIC = ("113100", "Basic Application Confidentiality Profile")
CODE_CLEAN_DESCRIPTORS = ("113105", "Clean Descriptors Option")
CODE_FULL_DATES = ("113106", "Retain Longitudinal Temporal Information Full Dates Option")
CODE_MODIFIED_DATES = ("113107", "Retain Longitudinal Temporal Information Modified Dates Option")
CODE_DEVICE_IDENTITY = ("113109", "Retain Device Identity Option")

_DIGIT_RUN = re.compile(r"\d{6,}")


@dataclass
class DeidContext:
    """Everything the profile needs to know about one file's subject."""

    study_id: str
    subject_no: str
    date_mode: str = "SHIFT"
    date_offset_days: int = 0
    uid_mapper: Callable[[str], str] = lambda uid: uid
    template_ref: str = "-"
    # Strings from the original header that must not survive (filled by collect_phi_tokens).
    phi_tokens: set[str] = field(default_factory=set)


def is_class_uid(value: str) -> bool:
    """Well-known DICOM UIDs (SOP classes, transfer syntaxes, coding schemes) are not identifying."""
    return str(value).startswith(DICOM_ROOT)


def collect_phi_tokens(ds: Dataset, date_mode: str) -> set[str]:
    """Strings from the original header that must not appear in the output.

    Name parts of 3+ characters and other values of 4+ characters are kept; short
    numbers are ignored because they collide with ordinary values.
    """
    tokens: set[str] = set()

    def add(text: str, min_len: int) -> None:
        t = text.strip()
        if len(t) < min_len or (t.isdigit() and len(t) < 6):
            return
        tokens.add(t)

    def visit(d: Dataset) -> None:
        for elem in d:
            if elem.VR == "SQ":
                for item in elem.value:
                    visit(item)
                continue
            keyword = elem.keyword
            action = ACTIONS.get(keyword)
            if elem.VR == "PN" and elem.value:
                for v in _values(elem):
                    for part in re.split(r"[\^=\s,]+", str(v)):
                        add(part, 3)
                continue
            if action in ("X", "Z", "D", "M") and elem.VR not in ("SQ", "UN", "OB", "OW") and elem.value:
                for v in _values(elem):
                    add(str(v), 4)
            elif elem.VR in ("DA", "DT") and date_mode != "KEEP" and elem.value:
                for v in _values(elem):
                    add(str(v)[:8], 8)

    visit(ds)
    # A removed value can legitimately repeat a kept one (StationName is often
    # the model name); those would always be "found" and are not identifying.
    kept = " ".join(str(ds[k].value) for k, a in ACTIONS.items() if a == "K" and k in ds and ds[k].value).lower()
    return {t for t in tokens if t.lower() not in kept}


def _values(elem: DataElement) -> list:
    v = elem.value
    if isinstance(v, (MultiValue, list, tuple)):
        return list(v)
    return [v]


def _shift_da(value: str, days: int) -> str:
    d = dt.datetime.strptime(value.strip()[:8], "%Y%m%d").date()
    return (d - dt.timedelta(days=days)).strftime("%Y%m%d")


def _shift_dt(value: str, days: int) -> str:
    v = value.strip()
    return _shift_da(v[:8], days) + v[8:]


def _cap_age(value: str) -> str:
    m = re.fullmatch(r"(\d{3})Y", str(value).strip())
    return AGE_CAP if m and int(m.group(1)) > 89 else str(value)


def _is_clean(value: str, tokens: set[str]) -> bool:
    text = str(value)
    if _DIGIT_RUN.search(text):
        return False
    low = text.lower()
    return not any(t.lower() in low for t in tokens if len(t) >= 3)


def _date_element(elem: DataElement, ctx: DeidContext) -> None:
    if not elem.value:
        return
    if ctx.date_mode == "KEEP":
        return
    if ctx.date_mode == "REMOVE":
        elem.value = ""
        return
    shift = _shift_da if elem.VR == "DA" else _shift_dt
    vals = [shift(str(v), ctx.date_offset_days) for v in _values(elem)]
    elem.value = vals if len(vals) > 1 else vals[0]


def _apply(d: Dataset, ctx: DeidContext, log: dict) -> None:
    for elem in list(d):
        tag, keyword, vr = elem.tag, elem.keyword, elem.VR
        if tag.is_private:
            del d[tag]
            log["removed"] += 1
            continue
        action = ACTIONS.get(keyword)
        if action == "X":
            del d[tag]
            log["removed"] += 1
        elif action == "Z":
            elem.value = None if vr == "SQ" else ""
            log["emptied"] += 1
        elif action == "D":
            elem.value = f"{ctx.study_id}^{ctx.subject_no}"
            log["replaced"] += 1
        elif action == "M":
            elem.value = ctx.subject_no
            log["replaced"] += 1
        elif action == "C":
            if elem.value and not _is_clean(elem.value, ctx.phi_tokens):
                elem.value = ""
                log["emptied"] += 1
        elif action == "K":
            if keyword == "PatientAge" and elem.value:
                elem.value = _cap_age(elem.value)
        elif vr == "SQ":
            for item in elem.value:
                _apply(item, ctx, log)
        elif vr == "PN":
            del d[tag]
            log["removed"] += 1
        elif vr == "UI":
            if elem.value and not is_class_uid(elem.value):
                vals = [ctx.uid_mapper(str(v)) for v in _values(elem)]
                elem.value = vals if len(vals) > 1 else vals[0]
                log["uids"] += 1
        elif vr in ("DA", "DT"):
            _date_element(elem, ctx)
            log["dates"] += 1 if ctx.date_mode != "KEEP" else 0


def _code_item(code: tuple[str, str]) -> Dataset:
    item = Dataset()
    item.CodeValue, item.CodeMeaning = code
    item.CodingSchemeDesignator = "DCM"
    return item


def deidentify(ds: Dataset, ctx: DeidContext) -> dict:
    """De-identify ``ds`` in place and record what was done in the header."""
    if ctx.date_mode not in DATE_MODES:
        raise ValueError(f"date_mode must be one of {DATE_MODES}")
    log = {"removed": 0, "emptied": 0, "replaced": 0, "uids": 0, "dates": 0}
    _apply(ds, ctx, log)

    meta: FileMetaDataset = ds.file_meta
    for keyword in FILE_META_REMOVE:
        if keyword in meta:
            delattr(meta, keyword)
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID

    codes = [CODE_BASIC, CODE_DEVICE_IDENTITY, CODE_CLEAN_DESCRIPTORS]
    if ctx.date_mode == "SHIFT":
        codes.append(CODE_MODIFIED_DATES)
        ds.LongitudinalTemporalInformationModified = "MODIFIED"
    elif ctx.date_mode == "KEEP":
        codes.append(CODE_FULL_DATES)
        ds.LongitudinalTemporalInformationModified = "UNMODIFIED"
    else:
        ds.LongitudinalTemporalInformationModified = "REMOVED"
    ds.PatientIdentityRemoved = "YES"
    # LO allows 64 characters per value, so the method is recorded as several values.
    ds.DeidentificationMethod = [ENGINE_VERSION, "profile=BASIC+RDI+CD", f"dates={ctx.date_mode}", f"template={ctx.template_ref}"[:64]]
    ds.DeidentificationMethodCodeSequence = Sequence([_code_item(c) for c in codes])
    return log


def header_bytes(ds: Dataset) -> bytes:
    """Serialised header without pixel data, for residual PHI scanning."""
    clone = Dataset()
    clone.file_meta = ds.file_meta
    for elem in ds:
        if elem.keyword not in ("PixelData", "FloatPixelData", "DoubleFloatPixelData"):
            clone.add(elem)
    buf = io.BytesIO()
    clone.save_as(buf, enforce_file_format=False)
    return buf.getvalue()


def find_residual_phi(ds: Dataset, tokens: set[str]) -> list[str]:
    """Return the tokens still present in the header (case-insensitive).

    The result is only used for counting; callers must not log the values.
    """
    data = header_bytes(ds).lower()
    found = []
    for t in tokens:
        for enc in ("utf-8", "latin-1"):
            try:
                needle = t.lower().encode(enc)
            except UnicodeEncodeError:
                continue
            if needle in data:
                found.append(t)
                break
    return found


PHI_KEYWORDS = frozenset(k for k, a in ACTIONS.items() if a in ("X", "Z", "D", "M", "C"))
