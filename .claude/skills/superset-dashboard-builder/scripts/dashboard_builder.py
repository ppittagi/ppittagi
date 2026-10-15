#!/usr/bin/env python3
"""
Pure functions that turn a compact layout / filter spec into Superset's
`position_json` and `native_filter_configuration` structures.

Layout spec (YAML/JSON):

  layout:
    header: "Study KPI"                 # optional big header on top (HEADER component)
    tabs:                              # EITHER tabs ...
      - title: Overview
        rows:
          - [ {chart: kpi_enrolled, width: 3, height: 25}, {chart: kpi_sites, width: 3, height: 25} ]
          - [ {chart: enrollment_trend, width: 12, height: 60} ]
          - header: "Safety"                      # section header row
          - markdown: "## Notes\\nSome **markdown**"
          - [ {chart: ae_by_soc, width: 6}, {chart: ae_grade, width: 6} ]
      - title: Sites
        tabs:                                      # nested tabs are allowed
          - title: Asia
            rows: [ [ {chart: site_table} ] ]
    rows: [...]                         # ... OR a flat list of rows (no tabs)

  Widths are grid columns (1..12 per row, total <= 12). Height is in grid units (8px): 50 = 400px.
  `chart:` refers to a key in the `charts:` section; the caller supplies key -> {id, uuid, name}.

Filter spec:

  filters:
    - name: Site
      type: select                     # select | range | time | timegrain | timecolumn | divider
      column: site
      datasets: [subjects, ae]         # dataset keys; default = every dataset used by the dashboard
      default: [Seoul]                 # default value(s) (time: "Last month", range: [10, 60])
      multi: true  required: false  default_first: false  search_all: false  inverse: false  sort_asc: true
      depends_on: [Country]            # cascading parent filter names
      scope: {tabs: [Overview], exclude_charts: [site_table]}   # default: whole dashboard
      description: "..."
    - type: divider
      title: "Safety filters"
"""
from __future__ import annotations

import copy
import re
import uuid as _uuid
from typing import Any, Optional

GRID_COLUMNS = 12
DEFAULT_CHART_HEIGHT = 50
DEFAULT_MARKDOWN_HEIGHT = 30
DASHBOARD_VERSION = "v2"


def _short_id(prefix: str) -> str:
    return f"{prefix}-{_uuid.uuid4().hex[:10]}"


class LayoutBuilder:
    """Build position_json from the spec. chart_lookup: key -> {"id": int, "uuid": str, "name": str}."""

    def __init__(self, chart_lookup: dict[str, dict]):
        self.charts = chart_lookup
        self.pos: dict[str, dict] = {}
        self.tab_ids: dict[str, str] = {}       # tab title -> TAB id
        self.chart_component: dict[int, str] = {}  # chart id -> CHART component id
        self.chart_tab: dict[int, list[str]] = {}  # chart id -> enclosing tab ids (root->leaf)

    # ---- components -------------------------------------------------------------
    def _add(self, comp_id: str, comp_type: str, parents: list[str], meta: Optional[dict] = None,
             children: Optional[list[str]] = None) -> dict:
        comp = {"type": comp_type, "id": comp_id, "children": children or [], "parents": list(parents)}
        if meta is not None:
            comp["meta"] = meta
        self.pos[comp_id] = comp
        return comp

    def _row(self, items: list[dict], parents: list[str], tab_path: list[str]) -> str:
        row_id = _short_id("ROW")
        row = self._add(row_id, "ROW", parents, {"background": "BACKGROUND_TRANSPARENT"})
        n = len(items)
        auto_width = max(1, GRID_COLUMNS // n) if n else GRID_COLUMNS
        used = 0
        for item in items:
            width = int(item.get("width") or auto_width)
            if used + width > GRID_COLUMNS:
                raise ValueError(f"row exceeds {GRID_COLUMNS} columns: {items}")
            used += width
            if "chart" in item:
                ref = item["chart"]
                info = self.charts.get(ref)
                if not info:
                    raise KeyError(f"layout references unknown chart key {ref!r}")
                cid = _short_id("CHART")
                self._add(cid, "CHART", parents + [row_id], {
                    "chartId": int(info["id"]), "uuid": info.get("uuid"), "width": width,
                    "height": int(item.get("height") or DEFAULT_CHART_HEIGHT),
                    "sliceName": info.get("name"), **({"sliceNameOverride": item["title"]} if item.get("title") else {}),
                })
                row["children"].append(cid)
                self.chart_component[int(info["id"])] = cid
                self.chart_tab[int(info["id"])] = list(tab_path)
            elif "markdown" in item:
                mid = _short_id("MARKDOWN")
                self._add(mid, "MARKDOWN", parents + [row_id], {
                    "width": width, "height": int(item.get("height") or DEFAULT_MARKDOWN_HEIGHT),
                    "code": item["markdown"]})
                row["children"].append(mid)
            else:
                raise ValueError(f"row item needs `chart` or `markdown`: {item}")
        return row_id

    def _rows(self, rows: list, parents: list[str], tab_path: list[str]) -> list[str]:
        ids: list[str] = []
        for row in rows:
            if isinstance(row, dict) and "header" in row:
                hid = _short_id("HEADER")
                self._add(hid, "HEADER", parents, {"text": row["header"],
                                                   "headerSize": row.get("size", "MEDIUM_HEADER"),
                                                   "background": "BACKGROUND_TRANSPARENT"})
                ids.append(hid)
            elif isinstance(row, dict) and "divider" in row:
                did = _short_id("DIVIDER")
                self._add(did, "DIVIDER", parents)
                ids.append(did)
            elif isinstance(row, dict) and "markdown" in row:
                ids.append(self._row([row], parents, tab_path))
            elif isinstance(row, dict) and "tabs" in row:
                ids.append(self._tabs(row["tabs"], parents, tab_path))
            elif isinstance(row, list):
                ids.append(self._row(row, parents, tab_path))
            else:
                raise ValueError(f"unknown row: {row!r}")
        return ids

    def _tabs(self, tabs: list[dict], parents: list[str], tab_path: list[str]) -> str:
        tabs_id = _short_id("TABS")
        comp = self._add(tabs_id, "TABS", parents, {})
        for tab in tabs:
            tab_id = _short_id("TAB")
            title = tab.get("title") or f"Tab {len(comp['children']) + 1}"
            self.tab_ids[title] = tab_id
            tab_comp = self._add(tab_id, "TAB", parents + [tabs_id],
                                 {"text": title, "defaultText": "Tab title", "placeholder": "Tab title"})
            comp["children"].append(tab_id)
            path = tab_path + [tab_id]
            if tab.get("tabs"):
                tab_comp["children"].append(self._tabs(tab["tabs"], parents + [tabs_id, tab_id], path))
            tab_comp["children"] += self._rows(tab.get("rows", []), parents + [tabs_id, tab_id], path)
        return tabs_id

    def build(self, layout: dict, dashboard_title: str) -> dict:
        self.pos = {"DASHBOARD_VERSION_KEY": DASHBOARD_VERSION}
        self._add("ROOT_ID", "ROOT", [], children=["GRID_ID"])
        self.pos["ROOT_ID"].pop("parents")
        grid = self._add("GRID_ID", "GRID", ["ROOT_ID"])
        self._add("HEADER_ID", "HEADER", [], {"text": dashboard_title})
        self.pos["HEADER_ID"].pop("parents"); self.pos["HEADER_ID"].pop("children")
        parents = ["ROOT_ID", "GRID_ID"]
        if layout.get("header"):
            hid = _short_id("HEADER")
            self._add(hid, "HEADER", parents, {"text": layout["header"], "headerSize": "LARGE_HEADER",
                                               "background": "BACKGROUND_TRANSPARENT"})
            grid["children"].append(hid)
        if layout.get("tabs"):
            grid["children"].append(self._tabs(layout["tabs"], parents, []))
        grid["children"] += self._rows(layout.get("rows", []), parents, [])
        return self.pos


def simple_grid_layout(chart_keys: list[str], per_row: int = 2, height: int = DEFAULT_CHART_HEIGHT) -> dict:
    """Fallback layout: charts in rows of `per_row`."""
    width = GRID_COLUMNS // per_row
    rows = []
    for i in range(0, len(chart_keys), per_row):
        rows.append([{"chart": k, "width": width, "height": height} for k in chart_keys[i:i + per_row]])
    return {"rows": rows}


# --------------------------------------------------------------------------- #
# native filters
# --------------------------------------------------------------------------- #
FILTER_TYPES = {"select": "filter_select", "range": "filter_range", "time": "filter_time",
                "timegrain": "filter_timegrain", "timecolumn": "filter_timecolumn"}


def _filter_id(name: str) -> str:
    """Deterministic per name, so re-applying a spec keeps filter ids (and users' permalinks) stable."""
    import hashlib
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return "NATIVE_FILTER-" + re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") + "-" + digest


def build_native_filters(filters: list[dict], dataset_lookup: dict[str, int], all_dataset_keys: list[str],
                         chart_ids: list[int], tab_ids: dict[str, str], chart_lookup: dict[str, dict],
                         chart_tab: Optional[dict[int, list[str]]] = None) -> list[dict]:
    """
    dataset_lookup: dataset key -> dataset id
    chart_lookup:   chart key -> {"id": ...}
    tab_ids:        tab title -> TAB component id
    chart_tab:      chart id -> enclosing TAB ids (used to compute chartsInScope for tab-scoped filters)
    """
    out: list[dict] = []
    name_to_id: dict[str, str] = {}
    for f in filters:  # ids first so `depends_on` can reference filters defined later
        if f.get("type") != "divider":
            name_to_id[f["name"]] = f.get("id") or _filter_id(f["name"])

    for f in filters:  # keep spec order (dividers are positional section titles in the filter bar)
        if f.get("type") == "divider":
            out.append({"id": f.get("id") or _filter_id(f.get("title", "divider")), "type": "DIVIDER",
                        "title": f.get("title", ""), "description": f.get("description", "")})
            continue
        name = f["name"]
        fid = name_to_id[name]
        ftype = FILTER_TYPES.get(f.get("type", "select"), f.get("type"))
        ds_keys = f.get("datasets") or all_dataset_keys
        column = f.get("column")

        # ---- targets ------------------------------------------------------------
        if ftype == "filter_time":
            targets: list[dict] = [{}]
        else:
            targets = []
            for k in ds_keys:
                if k not in dataset_lookup:
                    raise KeyError(f"filter {name!r}: unknown dataset key {k!r}")
                t: dict[str, Any] = {"datasetId": int(dataset_lookup[k])}
                if column and ftype in ("filter_select", "filter_range"):
                    col = column[k] if isinstance(column, dict) else column
                    t["column"] = {"name": col}
                targets.append(t)

        # ---- control values ------------------------------------------------------
        cv: dict[str, Any] = {}
        if ftype == "filter_select":
            cv = {"enableEmptyFilter": bool(f.get("required", False)),
                  "defaultToFirstItem": bool(f.get("default_first", False)),
                  "multiSelect": bool(f.get("multi", True)),
                  "searchAllOptions": bool(f.get("search_all", False)),
                  "inverseSelection": bool(f.get("inverse", False)),
                  "sortAscending": bool(f.get("sort_asc", True)),
                  "creatable": bool(f.get("creatable", False))}
        elif ftype == "filter_range":
            cv = {"enableEmptyFilter": bool(f.get("required", False)),
                  "enableSingleValue": f.get("single_value")}  # None | "minimum" | "exact" | "maximum"
            if cv["enableSingleValue"] is None:
                cv.pop("enableSingleValue")
        elif ftype in ("filter_time", "filter_timegrain", "filter_timecolumn"):
            cv = {"enableEmptyFilter": bool(f.get("required", False))}

        # ---- default value ---------------------------------------------------------
        dm: dict[str, Any] = {"extraFormData": {}, "filterState": {}, "ownState": {}}
        default = f.get("default")
        if default is not None:
            if ftype == "filter_select":
                vals = default if isinstance(default, list) else [default]
                col = column if isinstance(column, str) else next(iter(column.values()))
                dm = {"extraFormData": {"filters": [{"col": col, "op": "NOT IN" if f.get("inverse") else "IN",
                                                     "val": vals}]},
                      "filterState": {"value": vals}, "ownState": {}}
            elif ftype == "filter_range":
                lo, hi = (default + [None, None])[:2] if isinstance(default, list) else (default, None)
                col = column if isinstance(column, str) else next(iter(column.values()))
                flt = []
                if lo is not None:
                    flt.append({"col": col, "op": ">=", "val": lo})
                if hi is not None:
                    flt.append({"col": col, "op": "<=", "val": hi})
                dm = {"extraFormData": {"filters": flt}, "filterState": {"value": [lo, hi]}, "ownState": {}}
            elif ftype == "filter_time":
                dm = {"extraFormData": {"time_range": default}, "filterState": {"value": default}, "ownState": {}}
            elif ftype == "filter_timegrain":
                dm = {"extraFormData": {"time_grain_sqla": default}, "filterState": {"value": [default]}, "ownState": {}}
            elif ftype == "filter_timecolumn":
                dm = {"extraFormData": {"granularity_sqla": default}, "filterState": {"value": [default]}, "ownState": {}}

        # ---- scope ---------------------------------------------------------------------
        scope_spec = f.get("scope") or {}
        root_path = ["ROOT_ID"]
        if scope_spec.get("tabs"):
            root_path = []
            for title in scope_spec["tabs"]:
                if title not in tab_ids:
                    raise KeyError(f"filter {name!r}: unknown tab {title!r} (known: {list(tab_ids)})")
                root_path.append(tab_ids[title])
        excluded: list[int] = []
        for key in scope_spec.get("exclude_charts", []):
            if key not in chart_lookup:
                raise KeyError(f"filter {name!r}: unknown chart key {key!r}")
            excluded.append(int(chart_lookup[key]["id"]))
        # charts whose dataset is not targeted cannot be in scope
        target_ds = {t["datasetId"] for t in targets if "datasetId" in t}
        for key, info in chart_lookup.items():
            if ftype != "filter_time" and info.get("dataset_id") not in target_ds and int(info["id"]) not in excluded:
                excluded.append(int(info["id"]))
        in_scope = []
        for cid in chart_ids:
            if cid in excluded:
                continue
            if root_path != ["ROOT_ID"] and chart_tab is not None:
                if not any(t in (chart_tab.get(cid) or []) for t in root_path):
                    continue
            in_scope.append(cid)

        entry: dict[str, Any] = {
            "id": fid, "name": name, "filterType": ftype, "type": "NATIVE_FILTER",
            "description": f.get("description", ""),
            "targets": targets, "controlValues": cv, "defaultDataMask": dm,
            "cascadeParentIds": [name_to_id[p] for p in f.get("depends_on", [])],
            "scope": {"rootPath": root_path, "excluded": excluded},
            "chartsInScope": in_scope,
            "tabsInScope": sorted(set(t for cid in in_scope for t in (chart_tab or {}).get(cid, []))) if chart_tab else [],
        }
        if f.get("sort_metric"):
            entry["sortMetric"] = f["sort_metric"]
        if f.get("required_first"):
            entry["requiredFirst"] = True
        if f.get("filters"):  # pre-filter the option list, e.g. [{col: active, op: "==", val: 1}]
            from chart_builder import filter_obj
            entry["adhoc_filters"] = [filter_obj(x) for x in f["filters"]]
        if f.get("time_range"):
            entry["time_range"] = f["time_range"]
        out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# json_metadata
# --------------------------------------------------------------------------- #
def build_json_metadata(dash: dict, native_filters: list[dict], chart_ids: list[int],
                        existing: Optional[dict] = None) -> dict:
    meta: dict[str, Any] = copy.deepcopy(existing or {})
    meta["native_filter_configuration"] = native_filters
    meta.setdefault("expanded_slices", {})
    meta.setdefault("timed_refresh_immune_slices", [])
    meta["refresh_frequency"] = int(dash.get("refresh_frequency", meta.get("refresh_frequency", 0)) or 0)
    meta["color_scheme"] = dash.get("color_scheme") or meta.get("color_scheme") or ""
    if dash.get("label_colors"):
        meta["label_colors"] = dict(dash["label_colors"])
        meta["shared_label_colors"] = sorted(meta["label_colors"].keys())
    meta["cross_filters_enabled"] = bool(dash.get("cross_filters", True))
    if dash.get("filter_bar_orientation"):
        meta["filter_bar_orientation"] = dash["filter_bar_orientation"].upper()  # VERTICAL | HORIZONTAL
    if meta["cross_filters_enabled"]:
        meta["global_chart_configuration"] = {"scope": {"rootPath": ["ROOT_ID"], "excluded": []},
                                              "chartsInScope": list(chart_ids)}
        meta["chart_configuration"] = {
            str(cid): {"id": cid, "crossFilters": {"scope": "global", "chartsInScope": [c for c in chart_ids if c != cid]}}
            for cid in chart_ids}
    else:
        meta.pop("chart_configuration", None)
        meta.pop("global_chart_configuration", None)
    for k in ("default_filters", "filter_scopes"):  # legacy keys confuse native filters
        meta.pop(k, None)
    return meta
