"""Redaction template model, validation and tag-condition matching.

A template answers two questions: which files it applies to (``match``) and
which pixel rectangles to paint over (``regions``). Matching only reads header
attributes, so it runs on a dataset read with ``stop_before_pixels=True``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydicom.datadict import dictionary_VR, keyword_for_tag, tag_for_keyword
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.tag import Tag

from .profile import PHI_KEYWORDS

KINDS = ("REDACT", "PASS", "REJECT")
OPS = ("eq", "ne", "in", "contains", "icontains", "startsWith", "regex", "gte", "lte", "exists", "absent")
VALUELESS_OPS = ("exists", "absent")
REGEX_MAX_LEN = 200

STUDY_SPECIFIC_BONUS = 1000
CONDITION_WEIGHT = 10


class TemplateError(ValueError):
    """Raised when a template definition is invalid; ``code`` is API-facing."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Condition:
    tag: str
    op: str
    value: Any = None

    def to_dict(self) -> dict:
        d = {"tag": self.tag, "op": self.op}
        if self.op not in VALUELESS_OPS:
            d["value"] = self.value
        return d


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    w: int
    h: int
    label: str = ""
    frames: Any = "ALL"


@dataclass
class TemplateSpec:
    """Content of one template version (what gets hashed and approved)."""

    kind: str
    match_all: list[Condition] = field(default_factory=list)
    match_any: list[Condition] = field(default_factory=list)
    match_none: list[Condition] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    frames: Any = "ALL"
    fill: dict = field(default_factory=lambda: {"mode": "CONSTANT"})
    priority: int = 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "priority": self.priority,
            "match": {
                "all": [c.to_dict() for c in self.match_all],
                "any": [c.to_dict() for c in self.match_any],
                "none": [c.to_dict() for c in self.match_none],
            },
            "regions": [
                {"x": r.x, "y": r.y, "w": r.w, "h": r.h, "label": r.label, "frames": r.frames}
                for r in self.regions
            ],
            "frames": self.frames,
            "fill": self.fill,
        }

    def content_sha256(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()

    def required_equal(self, keyword: str) -> Any:
        """Value of an ``all`` condition ``keyword eq value``, else None."""
        for c in self.match_all:
            if c.op == "eq" and resolve_tag(c.tag) == resolve_tag(keyword):
                return c.value
        return None


def resolve_tag(tag: str) -> int:
    """Accept a keyword (``Manufacturer``) or a tag number (``0008,0070``)."""
    t = tag.strip()
    m = re.fullmatch(r"\(?([0-9A-Fa-f]{4}),?([0-9A-Fa-f]{4})\)?", t)
    if m:
        return int(Tag(int(m.group(1), 16), int(m.group(2), 16)))
    resolved = tag_for_keyword(t)
    if resolved is None:
        raise TemplateError("TEMPLATE_UNKNOWN_TAG", f"unknown DICOM tag: {tag}")
    return int(resolved)


def _parse_condition(raw: dict, where: str) -> Condition:
    if not isinstance(raw, dict) or "tag" not in raw or "op" not in raw:
        raise TemplateError("TEMPLATE_INVALID_CONDITION", f"{where}: needs 'tag' and 'op'")
    op = raw["op"]
    if op not in OPS:
        raise TemplateError("TEMPLATE_INVALID_CONDITION", f"{where}: unsupported op {op!r}")
    tag_int = resolve_tag(str(raw["tag"]))
    if Tag(tag_int).is_private:
        raise TemplateError("TEMPLATE_INVALID_CONDITION", f"{where}: private tags are not supported")
    # Match traces record the values of every tag a condition reads, so a
    # condition on an identifying tag would copy PHI into the trace.
    keyword = keyword_for_tag(tag_int)
    try:
        vr = dictionary_VR(tag_int)
    except KeyError:
        raise TemplateError("TEMPLATE_UNKNOWN_TAG", f"{where}: {raw['tag']} is not in the DICOM dictionary") from None
    if keyword in PHI_KEYWORDS or vr == "PN":
        raise TemplateError("TEMPLATE_PHI_CONDITION", f"{where}: {keyword} is an identifying tag and cannot be matched on")
    value = raw.get("value")
    if op not in VALUELESS_OPS and value is None:
        raise TemplateError("TEMPLATE_INVALID_CONDITION", f"{where}: op {op!r} needs a value")
    if op == "in" and not isinstance(value, list):
        raise TemplateError("TEMPLATE_INVALID_CONDITION", f"{where}: 'in' needs a list value")
    if op in ("gte", "lte") and not isinstance(value, (int, float)):
        raise TemplateError("TEMPLATE_INVALID_CONDITION", f"{where}: {op!r} needs a number")
    if op == "regex":
        if len(str(value)) > REGEX_MAX_LEN:
            raise TemplateError("TEMPLATE_INVALID_CONDITION", f"{where}: regex longer than {REGEX_MAX_LEN}")
        try:
            re.compile(str(value))
        except re.error as exc:
            raise TemplateError("TEMPLATE_INVALID_CONDITION", f"{where}: bad regex: {exc}") from exc
    return Condition(tag=str(raw["tag"]), op=op, value=value)


def _parse_frames(raw: Any, where: str) -> Any:
    if raw is None or raw == "ALL":
        return "ALL"
    if isinstance(raw, list) and raw and all(isinstance(f, int) and f >= 1 for f in raw):
        return sorted(set(raw))
    raise TemplateError("TEMPLATE_INVALID_FRAMES", f"{where}: frames must be 'ALL' or a list of 1-based ints")


def parse_spec(raw: dict) -> TemplateSpec:
    """Validate a template version body (the JSON a user submits)."""
    kind = raw.get("kind", "REDACT")
    if kind not in KINDS:
        raise TemplateError("TEMPLATE_INVALID_KIND", f"kind must be one of {KINDS}")
    match = raw.get("match") or {}
    spec = TemplateSpec(
        kind=kind,
        match_all=[_parse_condition(c, f"match.all[{i}]") for i, c in enumerate(match.get("all", []))],
        match_any=[_parse_condition(c, f"match.any[{i}]") for i, c in enumerate(match.get("any", []))],
        match_none=[_parse_condition(c, f"match.none[{i}]") for i, c in enumerate(match.get("none", []))],
        frames=_parse_frames(raw.get("frames"), "frames"),
        fill=raw.get("fill") or {"mode": "CONSTANT"},
        priority=int(raw.get("priority", 0)),
    )
    if not spec.match_all:
        raise TemplateError("TEMPLATE_NO_CONDITIONS", "match.all needs at least one condition")
    if spec.fill.get("mode") != "CONSTANT":
        raise TemplateError("TEMPLATE_INVALID_FILL", "only fill.mode CONSTANT is supported")

    regions = raw.get("regions") or []
    if kind == "REDACT":
        rows, cols = spec.required_equal("Rows"), spec.required_equal("Columns")
        if rows is None or cols is None:
            raise TemplateError(
                "TEMPLATE_NEEDS_DIMENSIONS",
                "REDACT templates must pin Rows and Columns with 'eq' in match.all",
            )
        if not regions:
            raise TemplateError("TEMPLATE_NO_REGIONS", "REDACT templates need at least one region")
        for i, r in enumerate(regions):
            try:
                x, y, w, h = (int(r[k]) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError) as exc:
                raise TemplateError("TEMPLATE_INVALID_REGION", f"regions[{i}]: needs integer x, y, w, h") from exc
            if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > int(cols) or y + h > int(rows):
                raise TemplateError(
                    "TEMPLATE_REGION_OUT_OF_BOUNDS",
                    f"regions[{i}] ({x},{y},{w},{h}) is outside {cols}x{rows}",
                )
            spec.regions.append(
                Region(x, y, w, h, str(r.get("label", "")), _parse_frames(r.get("frames"), f"regions[{i}].frames"))
            )
    elif regions:
        raise TemplateError("TEMPLATE_INVALID_REGION", f"{kind} templates must not define regions")
    return spec


# --- matching ---------------------------------------------------------------


def _element_values(ds: Dataset, tag_int: int) -> list[Any] | None:
    """Values of a header element as a list (multi-valued aware); None if absent or empty."""
    elem = ds.get(tag_int)
    if elem is None or elem.value is None or elem.value == "":
        return None
    v = elem.value
    if isinstance(v, (MultiValue, list, tuple)):
        return list(v)
    return [v]


def _norm(v: Any) -> Any:
    if isinstance(v, (int, float)):
        return v
    return str(v).strip()


def _as_number(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _eq(actual: Any, expected: Any) -> bool:
    a, e = _norm(actual), _norm(expected)
    if isinstance(e, (int, float)):
        n = _as_number(a)
        return n is not None and n == e
    return str(a) == e


def evaluate(cond: Condition, ds: Dataset) -> tuple[bool, Any]:
    """Return (result, actual value) for one condition."""
    values = _element_values(ds, resolve_tag(cond.tag))
    actual = None if values is None else (values[0] if len(values) == 1 else [str(v) for v in values])
    if cond.op == "exists":
        return values is not None, actual
    if cond.op == "absent":
        return values is None, actual
    if values is None:
        # A missing tag fails every comparison except 'ne'.
        return cond.op == "ne", actual

    e = cond.value
    if cond.op == "eq":
        ok = any(_eq(v, e) for v in values)
    elif cond.op == "ne":
        ok = not any(_eq(v, e) for v in values)
    elif cond.op == "in":
        ok = any(_eq(v, x) for v in values for x in e)
    elif cond.op == "contains":
        ok = any(str(e) in str(v) for v in values)
    elif cond.op == "icontains":
        ok = any(str(e).lower() in str(v).lower() for v in values)
    elif cond.op == "startsWith":
        ok = any(str(v).strip().startswith(str(e)) for v in values)
    elif cond.op == "regex":
        pattern = re.compile(str(e))
        ok = any(pattern.search(str(v)) for v in values)
    elif cond.op in ("gte", "lte"):
        nums = [n for n in (_as_number(v) for v in values) if n is not None]
        ok = any(n >= e for n in nums) if cond.op == "gte" else any(n <= e for n in nums)
    else:  # pragma: no cover - parse_spec rejects unknown ops
        raise TemplateError("TEMPLATE_INVALID_CONDITION", cond.op)
    return bool(ok), actual


@dataclass
class Candidate:
    """A template version that can be matched (loaded from the store)."""

    version_id: int
    template_code: str
    version: int
    study_id: str | None
    spec: TemplateSpec


@dataclass
class MatchResult:
    status: str  # MATCHED | NO_TEMPLATE | AMBIGUOUS
    selected: Candidate | None
    score: int | None
    trace: dict


def score_candidate(cand: Candidate, ds: Dataset) -> tuple[int | None, dict]:
    """Score a candidate against ``ds``; None means it does not match."""
    detail = {"templateCode": cand.template_code, "version": cand.version, "failed": []}
    true_count = 0
    matched = True
    for c in cand.spec.match_all:
        ok, actual = evaluate(c, ds)
        if ok:
            true_count += 1
        else:
            matched = False
            detail["failed"].append({**c.to_dict(), "group": "all", "actual": _jsonable(actual)})
    if cand.spec.match_any:
        any_true = 0
        for c in cand.spec.match_any:
            ok, _ = evaluate(c, ds)
            any_true += ok
        if any_true == 0:
            matched = False
            detail["failed"].append({"group": "any", "reason": "no condition true"})
        true_count += any_true
    for c in cand.spec.match_none:
        ok, actual = evaluate(c, ds)
        if ok:
            matched = False
            detail["failed"].append({**c.to_dict(), "group": "none", "actual": _jsonable(actual)})
    if not matched:
        detail["matched"] = False
        return None, detail
    score = (STUDY_SPECIFIC_BONUS if cand.study_id else 0) + cand.spec.priority + CONDITION_WEIGHT * true_count
    detail.update(matched=True, score=score)
    del detail["failed"]
    return score, detail


def match(candidates: list[Candidate], ds: Dataset) -> MatchResult:
    """Pick the single best template for ``ds``; never guess on a tie."""
    scored: list[tuple[int, Candidate]] = []
    details = []
    for cand in candidates:
        score, detail = score_candidate(cand, ds)
        details.append(detail)
        if score is not None:
            scored.append((score, cand))

    used_tags = sorted({c.tag for cand in candidates for c in (*cand.spec.match_all, *cand.spec.match_any, *cand.spec.match_none)})
    trace = {"candidates": details, "tags": {t: _jsonable(evaluate(Condition(t, "exists"), ds)[1]) for t in used_tags}}
    if not scored:
        return MatchResult("NO_TEMPLATE", None, None, trace)

    scored.sort(key=lambda s: s[0], reverse=True)
    best_score, best = scored[0]
    tied = [c for s, c in scored if s == best_score]
    if len(tied) > 1 and len({_behaviour_key(c.spec) for c in tied}) > 1:
        trace["tied"] = [f"{c.template_code}@v{c.version}" for c in tied]
        return MatchResult("AMBIGUOUS", None, best_score, trace)
    trace["selected"] = {"templateCode": best.template_code, "version": best.version, "score": best_score}
    return MatchResult("MATCHED", best, best_score, trace)


def _behaviour_key(spec: TemplateSpec) -> str:
    """Two tied templates that would do the same thing are not ambiguous."""
    d = spec.to_dict()
    return json.dumps({"kind": d["kind"], "regions": d["regions"], "frames": d["frames"], "fill": d["fill"]}, sort_keys=True)


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)
