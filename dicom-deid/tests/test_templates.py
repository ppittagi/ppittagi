import copy

import pytest

from deid import templates
from deid.templates import Candidate, TemplateError, parse_spec

from conftest import make_dataset


def spec(**over):
    base = {
        "kind": "REDACT",
        "match": {"all": [
            {"tag": "Modality", "op": "eq", "value": "US"},
            {"tag": "Rows", "op": "eq", "value": 720},
            {"tag": "Columns", "op": "eq", "value": 960},
        ]},
        "regions": [{"x": 0, "y": 0, "w": 669, "h": 67}],
    }
    base.update(over)
    return base


def cand(raw, vid=1, code="T", study=None):
    return Candidate(version_id=vid, template_code=code, version=1, study_id=study, spec=parse_spec(raw))


@pytest.mark.parametrize("raw, code", [
    ({"match": {"all": [{"tag": "Modality", "op": "eq", "value": "US"}]}, "regions": [{"x": 0, "y": 0, "w": 1, "h": 1}]}, "TEMPLATE_NEEDS_DIMENSIONS"),
    (spec(regions=[{"x": 900, "y": 0, "w": 100, "h": 10}]), "TEMPLATE_REGION_OUT_OF_BOUNDS"),
    (spec(regions=[]), "TEMPLATE_NO_REGIONS"),
    (spec(match={"all": []}), "TEMPLATE_NO_CONDITIONS"),
    (spec(match={"all": [{"tag": "PatientName", "op": "eq", "value": "x"}]}), "TEMPLATE_PHI_CONDITION"),
    (spec(match={"all": [{"tag": "InstitutionName", "op": "contains", "value": "x"}]}), "TEMPLATE_PHI_CONDITION"),
    (spec(match={"all": [{"tag": "Modality", "op": "regex", "value": "("}]}), "TEMPLATE_INVALID_CONDITION"),
    (spec(match={"all": [{"tag": "NoSuchKeyword", "op": "eq", "value": 1}]}), "TEMPLATE_UNKNOWN_TAG"),
    (spec(match={"all": [{"tag": "0009,1010", "op": "eq", "value": 1}]}), "TEMPLATE_INVALID_CONDITION"),
    (spec(kind="PASS"), "TEMPLATE_INVALID_REGION"),
    (spec(kind="DELETE"), "TEMPLATE_INVALID_KIND"),
])
def test_invalid_templates_are_refused(raw, code):
    with pytest.raises(TemplateError) as exc:
        parse_spec(raw)
    assert exc.value.code == code


def test_tag_numbers_and_keywords_are_equivalent():
    ds = make_dataset(rows=16, cols=16)
    by_number = cand(spec(match={"all": [{"tag": "0008,0060", "op": "eq", "value": "US"},
                                         {"tag": "(0028,0010)", "op": "eq", "value": 16},
                                         {"tag": "Columns", "op": "eq", "value": 16}]},
                          regions=[{"x": 0, "y": 0, "w": 4, "h": 4}]))
    assert templates.match([by_number], ds).status == "MATCHED"


@pytest.mark.parametrize("cond, expected", [
    ({"tag": "Manufacturer", "op": "icontains", "value": "ge"}, True),
    ({"tag": "Manufacturer", "op": "contains", "value": "ge"}, False),
    ({"tag": "ManufacturerModelName", "op": "regex", "value": r"^LOGIQ\s?E9$"}, True),
    ({"tag": "ManufacturerModelName", "op": "in", "value": ["LOGIQE10", "LOGIQE9"]}, True),
    ({"tag": "SoftwareVersions", "op": "startsWith", "value": "LOGIQE9:R2"}, True),
    ({"tag": "ImageType", "op": "contains", "value": "VASCULAR"}, True),  # any value of a multi-valued tag
    ({"tag": "ImageType", "op": "eq", "value": "DERIVED"}, False),
    ({"tag": "Rows", "op": "gte", "value": 700}, True),
    ({"tag": "Rows", "op": "lte", "value": 700}, False),
    ({"tag": "NumberOfFrames", "op": "absent"}, True),
    ({"tag": "BodyPartExamined", "op": "eq", "value": "LEG"}, False),  # missing tag fails comparisons
    ({"tag": "BodyPartExamined", "op": "ne", "value": "LEG"}, True),
])
def test_operators(cond, expected):
    ds = make_dataset(rows=720, cols=960)
    c = parse_spec(spec())
    ok, _ = templates.evaluate(templates._parse_condition(cond, "t"), ds)
    assert ok is expected
    assert c.kind == "REDACT"


def test_study_specific_template_wins_over_shared():
    ds = make_dataset()
    shared = cand(spec(priority=500), vid=1, code="shared")
    study = cand(spec(regions=[{"x": 0, "y": 0, "w": 10, "h": 10}]), vid=2, code="study", study="S1")
    r = templates.match([shared, study], ds)
    assert r.status == "MATCHED" and r.selected.template_code == "study"
    assert r.score == templates.STUDY_SPECIFIC_BONUS + 30


def test_more_specific_template_wins():
    ds = make_dataset()
    generic = cand(spec(), vid=1, code="generic")
    specific_raw = spec()
    specific_raw["match"]["all"].append({"tag": "ManufacturerModelName", "op": "eq", "value": "LOGIQE9"})
    r = templates.match([generic, cand(specific_raw, vid=2, code="specific")], ds)
    assert r.selected.template_code == "specific"


def test_tie_with_different_regions_is_ambiguous_and_never_guessed():
    ds = make_dataset()
    a = cand(spec(), vid=1, code="a")
    b = cand(spec(regions=[{"x": 0, "y": 0, "w": 100, "h": 100}]), vid=2, code="b")
    r = templates.match([a, b], ds)
    assert r.status == "AMBIGUOUS" and r.selected is None
    assert sorted(r.trace["tied"]) == ["a@v1", "b@v1"]


def test_tie_with_identical_behaviour_is_not_ambiguous():
    ds = make_dataset()
    r = templates.match([cand(spec(), vid=1, code="a"), cand(copy.deepcopy(spec()), vid=2, code="b")], ds)
    assert r.status == "MATCHED"


def test_no_match_reports_failed_conditions_without_phi():
    ds = make_dataset(model="ACUSON")
    raw = spec()
    raw["match"]["all"].append({"tag": "ManufacturerModelName", "op": "eq", "value": "LOGIQE9"})
    r = templates.match([cand(raw)], ds)
    assert r.status == "NO_TEMPLATE"
    failed = r.trace["candidates"][0]["failed"]
    assert failed == [{"tag": "ManufacturerModelName", "op": "eq", "value": "LOGIQE9", "group": "all", "actual": "ACUSON"}]
    assert "CANARY" not in str(r.trace)


def test_none_group_excludes():
    ds = make_dataset()
    raw = spec(match={**spec()["match"], "none": [{"tag": "ImageType", "op": "contains", "value": "VASCULAR"}]})
    assert templates.match([cand(raw)], ds).status == "NO_TEMPLATE"


def test_content_hash_is_stable_and_sensitive():
    a, b = parse_spec(spec()), parse_spec(spec())
    assert a.content_sha256() == b.content_sha256()
    assert a.content_sha256() != parse_spec(spec(priority=1)).content_sha256()
