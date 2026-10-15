#!/usr/bin/env python3
"""
Apply a dashboard spec (YAML or JSON) to a Superset instance: datasets -> charts -> dashboard
(layout with tabs/rows) -> native filters. Idempotent: re-running updates the same objects
(matched by dataset table/sql name, chart name, dashboard slug/title).

  python build_dashboard.py spec.yaml                 # create / update everything
  python build_dashboard.py spec.yaml --dry-run       # print payloads, no API calls
  python build_dashboard.py spec.yaml --validate      # also run each chart's query (chart/data)
  python build_dashboard.py spec.yaml --only charts   # datasets | charts | dashboard
  python build_dashboard.py spec.yaml --state out.json

Spec: see assets/spec.example.yaml and the docstrings of chart_builder.py / dashboard_builder.py.
Connection: SUPERSET_URL / SUPERSET_USERNAME / SUPERSET_PASSWORD env vars (or `superset:` block in spec).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chart_builder import build_params, build_query_context  # noqa: E402
from dashboard_builder import (LayoutBuilder, build_json_metadata, build_native_filters,  # noqa: E402
                               simple_grid_layout)
from superset_client import SupersetClient, SupersetError  # noqa: E402


def load_spec(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            sys.exit("pip install pyyaml (or use a .json spec)")
        return yaml.safe_load(text)
    return json.loads(text)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


class Applier:
    def __init__(self, spec: dict, client: Optional[SupersetClient], dry_run: bool, keep_unplaced: bool = False,
                 no_dataset_patch: bool = False):
        self.spec = spec
        self.c = client
        self.dry = dry_run
        self.keep_unplaced = keep_unplaced
        self.no_dataset_patch = no_dataset_patch
        self.dataset_ids: dict[str, int] = {}
        self.dataset_cols: dict[str, dict[str, dict]] = {}
        self.charts: dict[str, dict] = {}     # key -> {id, uuid, name, dataset_id, params}
        self.dashboard_id: Optional[int] = None
        self.prev_state: dict = {}             # contents of an earlier --state file (ids by key)
        self._fake = 1000

    def _fake_id(self) -> int:
        self._fake += 1
        return self._fake

    # ---- datasets ---------------------------------------------------------------- #
    def apply_datasets(self) -> None:
        for ds in self.spec.get("datasets", []):
            key = ds["key"]
            if ds.get("id"):
                self.dataset_ids[key] = int(ds["id"])
                log(f"[dataset] {key}: using existing id {ds['id']}")
                continue
            if self.dry:
                self.dataset_ids[key] = self._fake_id()
                log(f"[dataset] {key}: (dry-run) would create/lookup {ds.get('table') or ds.get('name')}")
                continue
            assert self.c
            db_id = ds.get("database")
            if isinstance(db_id, str) and not db_id.isdigit():
                db = self.c.database_by_name(db_id)
                if not db:
                    raise SupersetError(f"database {db_id!r} not found; known: "
                                        f"{[d['database_name'] for d in self.c.databases()]}")
                db_id = db["id"]
            db_id = int(db_id)
            table = ds.get("table") or ds.get("name")
            schema = ds.get("schema")
            found = self.c.find_dataset(table, db_id, schema)
            if found:
                ds_id = found["id"]
                log(f"[dataset] {key}: found #{ds_id} ({table})")
                if ds.get("sql"):
                    current = self.c.dataset(ds_id)
                    if (current.get("sql") or "").strip() != ds["sql"].strip():
                        self.c.update_dataset(ds_id, {"sql": ds["sql"]})
                        self.c.refresh_dataset(ds_id)
                        log(f"[dataset] {key}: sql updated + columns refreshed")
            else:
                created = self.c.create_dataset(db_id, table, schema, ds.get("sql"), ds.get("catalog"))
                ds_id = created["id"]
                log(f"[dataset] {key}: created #{ds_id} ({table})")
            patch: dict[str, Any] = {}
            if ds.get("metrics"):
                existing = {m["metric_name"]: m for m in self.c.dataset(ds_id).get("metrics", [])}
                merged = []
                for m in ds["metrics"]:
                    entry = {"metric_name": m["metric_name"], "expression": m["expression"]}
                    for k in ("verbose_name", "d3format", "description", "metric_type"):
                        if m.get(k):
                            entry[k] = m[k]
                    if m["metric_name"] in existing:
                        entry["id"] = existing[m["metric_name"]]["id"]
                    merged.append(entry)
                for name, m in existing.items():  # keep the ones we do not manage (e.g. default count)
                    if name not in {x["metric_name"] for x in merged}:
                        merged.append({"id": m["id"], "metric_name": name, "expression": m["expression"]})
                patch["metrics"] = merged
            if ds.get("columns"):
                existing_cols = {c["column_name"]: c for c in self.c.dataset(ds_id).get("columns", [])}
                cols = []
                for col in ds["columns"]:
                    entry = {"column_name": col["column_name"]}
                    cur = existing_cols.get(col["column_name"])
                    if cur:
                        entry["id"] = cur["id"]
                    for k in ("verbose_name", "is_dttm", "expression", "description", "python_date_format",
                              "filterable", "groupby", "type"):
                        if k in col:
                            entry[k] = col[k]
                    cols.append(entry)
                for name, cur in existing_cols.items():
                    if name not in {x["column_name"] for x in cols}:
                        cols.append({"id": cur["id"], "column_name": name})
                patch["columns"] = cols
            for k in ("main_dttm_col", "description", "cache_timeout", "fetch_values_predicate"):
                if k in ds:
                    patch[k] = ds[k]
            if patch and found and self.no_dataset_patch:
                log(f"[dataset] {key}: existing dataset #{ds_id} left untouched (--no-dataset-patch); "
                    f"spec metadata ({', '.join(patch)}) not applied")
            elif patch:
                if found:
                    log(f"[dataset] {key}: NOTE patching metadata of an existing dataset #{ds_id} that other "
                        f"dashboards may share ({', '.join(patch)})")
                self.c.update_dataset(ds_id, patch)
                log(f"[dataset] {key}: metadata updated ({', '.join(patch)})")
            self.dataset_ids[key] = ds_id
            self.dataset_cols[key] = {c["column_name"]: c for c in self.c.dataset(ds_id).get("columns", [])}

    # ---- charts -------------------------------------------------------------------- #
    def _check_columns(self, key: str, chart: dict, params: dict) -> None:
        cols = self.dataset_cols.get(chart["dataset"])
        if not cols:
            return
        used: set[str] = set()
        for k in ("x_axis", "groupby", "columns", "all_columns", "groupbyRows", "groupbyColumns", "entity",
                  "series", "column", "source", "target"):
            v = params.get(k)
            if isinstance(v, str):
                used.add(v)
            elif isinstance(v, list):
                used.update(x for x in v if isinstance(x, str))
        for m in params.get("metrics", []) + ([params["metric"]] if isinstance(params.get("metric"), dict) else []):
            if isinstance(m, dict) and m.get("expressionType") == "SIMPLE":
                used.add(m["column"]["column_name"])
        for f in params.get("adhoc_filters", []):
            if f.get("expressionType") == "SIMPLE":
                used.add(f["subject"])
        missing = sorted(u for u in used if u not in cols)
        if missing:
            log(f"[chart] {key}: WARNING columns not in dataset {chart['dataset']!r}: {missing} "
                f"(available: {sorted(cols)[:40]}{'...' if len(cols) > 40 else ''})")

    def apply_charts(self) -> None:
        dash_default_colors = self.spec.get("dashboard", {}).get("label_colors") or {}
        dash_scheme = self.spec.get("dashboard", {}).get("color_scheme")
        for chart in self.spec.get("charts", []):
            key, name = chart["key"], chart["name"]
            ds_key = chart["dataset"]
            if ds_key not in self.dataset_ids:
                raise KeyError(f"chart {key!r}: unknown dataset key {ds_key!r}")
            ds_id = self.dataset_ids[ds_key]
            cspec = dict(chart)
            if cspec.get("annotations"):
                cspec["annotations"] = self._resolve_annotations(key, cspec["annotations"])
            if dash_scheme and not cspec.get("color_scheme"):
                cspec["color_scheme"] = dash_scheme
            if dash_default_colors and not cspec.get("label_colors"):
                cspec["label_colors"] = dash_default_colors
            params = build_params(cspec, ds_id)
            qc = build_query_context(params, ds_id)
            self._check_columns(key, chart, params)
            if self.dry:
                cid = self._fake_id()
                self.charts[key] = {"id": cid, "uuid": None, "name": name, "dataset_id": ds_id, "params": params}
                log(f"[chart] {key}: (dry-run) {chart['viz_type']} -> {json.dumps(params, ensure_ascii=False)[:160]}...")
                continue
            assert self.c
            found = self._match_chart(key, name, ds_id)
            dashboards = [self.dashboard_id] if self.dashboard_id else []
            if found:
                cur_dash = [d["id"] for d in self.c.chart(found["id"]).get("dashboards", [])]
                self.c.update_chart(found["id"], slice_name=name, viz_type=chart["viz_type"], params=params,
                                    query_context=qc, datasource_id=ds_id, datasource_type="table",
                                    dashboards=sorted(set(cur_dash) | set(dashboards)),
                                    description=chart.get("description"))
                cid, cuuid = found["id"], self.c.chart(found["id"]).get("uuid")
                log(f"[chart] {key}: updated #{cid} ({chart['viz_type']})")
            else:
                created = self.c.create_chart(name, chart["viz_type"], ds_id, params, dashboards,
                                              description=chart.get("description"), query_context=qc)
                cid = created["id"]
                cuuid = self.c.chart(cid).get("uuid")
                log(f"[chart] {key}: created #{cid} ({chart['viz_type']})")
            self.charts[key] = {"id": cid, "uuid": cuuid, "name": name, "dataset_id": ds_id, "params": params}

    def _resolve_annotations(self, key: str, annotations: list[dict]) -> list[dict]:
        """Create native annotation layers (+ their interval/event annotations) referenced by a chart."""
        out = []
        for a in annotations:
            a = dict(a)
            if str(a.get("type", "formula")).lower() in ("interval", "event"):
                layer_name = a.get("layer") or a.get("name") or f"{key} annotations"
                if self.dry or not self.c:
                    a["layer_id"] = self._fake_id()
                else:
                    layer = self.c.annotation_layer_by_name(layer_name)
                    if not layer:
                        layer = self.c.create_annotation_layer(layer_name, a.get("description", ""))
                        log(f"[annotation] created layer '{layer_name}' #{layer['id']}")
                    existing = {(x.get("short_descr"), str(x.get("start_dttm"))[:10]) for x in self.c.annotations(layer["id"])}
                    for iv in a.get("intervals", []):
                        label = iv.get("label", a.get("name", layer_name))
                        if (label, str(iv["start"])[:10]) in existing:
                            continue
                        self.c.create_annotation(layer["id"], label, f"{iv['start']}T00:00:00",
                                                 f"{iv.get('end', iv['start'])}T00:00:00", iv.get("description", ""))
                        log(f"[annotation] '{layer_name}': {label} {iv['start']} .. {iv.get('end', iv['start'])}")
                    a["layer_id"] = layer["id"]
            out.append(a)
        return out

    def _match_chart(self, key: str, name: str, ds_id: int) -> Optional[dict]:
        """
        Find the chart this spec entry refers to without hijacking charts of other dashboards:
        1. the id recorded for this key in a previous `--state` file (if it still exists),
        2. a chart with this name that is linked to *this* dashboard,
        3. otherwise none (a same-named chart on another dashboard is left alone, with a warning).
        """
        assert self.c
        prev = (self.prev_state.get("charts") or {}).get(key) or {}
        if prev.get("id"):
            try:
                c = self.c.chart(int(prev["id"]))
                return {"id": int(prev["id"]), **c}
            except SupersetError:
                log(f"[chart] {key}: previous id #{prev['id']} no longer exists; matching by name")
        candidates = self.c.list_all("/api/v1/chart/", [("slice_name", "eq", name)])
        linked = [c for c in candidates if self.dashboard_id in {d.get("id") for d in c.get("dashboards", [])}]
        if linked:
            return linked[0]
        if candidates:
            log(f"[chart] {key}: a chart named {name!r} exists on another dashboard (#{candidates[0]['id']}); "
                f"creating a separate chart for this dashboard instead of overwriting it")
        return None

    def unlink_unplaced_charts(self, placed_ids: set[int]) -> list[int]:
        """Charts linked to the dashboard but absent from position_json are auto-rendered under the layout
        by Superset. Detach them so the canvas shows exactly what the spec says."""
        if self.dry or not self.c or not self.dashboard_id:
            return []
        removed = []
        for c in self.c.dashboard_charts(self.dashboard_id):
            cid = int(c["id"])
            if cid in placed_ids:
                continue
            full = self.c.chart(cid)
            others = [d["id"] for d in full.get("dashboards", []) if d["id"] != self.dashboard_id]
            self.c.update_chart(cid, dashboards=others)
            removed.append(cid)
            log(f"[dashboard] unlinked chart #{cid} ({full.get('slice_name')}) - not in the spec layout"
                f"{' (still on other dashboards)' if others else ''}")
        return removed

    def validate_charts(self) -> bool:
        ok = True
        if self.dry or not self.c:
            return ok
        for key, info in self.charts.items():
            qc = build_query_context(info["params"], info["dataset_id"], row_limit=5)
            try:
                res = self.c.chart_data(qc)
                for i, r0 in enumerate(res["result"]):
                    tag = f"{key}" if len(res["result"]) == 1 else f"{key} (query {'AB'[i] if i < 2 else i})"
                    if r0.get("error"):
                        ok = False
                        log(f"[validate] {tag}: ERROR {r0['error']}")
                    else:
                        log(f"[validate] {tag}: ok, {r0.get('rowcount')} rows, cols={r0.get('colnames')}")
            except SupersetError as e:
                ok = False
                log(f"[validate] {key}: ERROR {e}")
        return ok

    # ---- dashboard ------------------------------------------------------------------ #
    def apply_dashboard_shell(self) -> None:
        dash = self.spec.get("dashboard", {})
        title = dash.get("title", "Untitled dashboard")
        if self.dry:
            self.dashboard_id = self._fake_id()
            log(f"[dashboard] (dry-run) {title}")
            return
        assert self.c
        found = self.c.find_dashboard(title=title, slug=dash.get("slug"))
        if found:
            self.dashboard_id = found["id"]
            log(f"[dashboard] found #{found['id']} ({title})")
        else:
            created = self.c.create_dashboard(title, dash.get("slug"), bool(dash.get("published", True)),
                                              json_metadata={}, css=dash.get("css"), theme_id=dash.get("theme_id"),
                                              description=dash.get("description"))
            self.dashboard_id = created["id"]
            log(f"[dashboard] created #{self.dashboard_id} ({title})")

    def apply_dashboard_layout(self) -> dict:
        dash = self.spec.get("dashboard", {})
        title = dash.get("title", "Untitled dashboard")
        layout = self.spec.get("layout") or simple_grid_layout(list(self.charts))
        lb = LayoutBuilder(self.charts)
        position = lb.build(layout, title)
        chart_ids = sorted(self.charts[k]["id"] for k in self.charts)
        all_ds_keys = [d["key"] for d in self.spec.get("datasets", [])]
        filters = build_native_filters(self.spec.get("filters", []), self.dataset_ids, all_ds_keys, chart_ids,
                                       lb.tab_ids, self.charts, lb.chart_tab)
        existing_meta: dict = {}
        if not self.dry and self.c and self.dashboard_id:
            try:
                existing_meta = json.loads(self.c.dashboard(self.dashboard_id).get("json_metadata") or "{}")
            except (SupersetError, ValueError):
                existing_meta = {}
        # Inside a dashboard, series colours come from json_metadata.label_colors (chart-level
        # params.label_colors only apply in Explore), so merge every chart's pins into the dashboard.
        merged_colors: dict[str, str] = {}
        for info in self.charts.values():
            merged_colors.update(info["params"].get("label_colors") or {})
        merged_colors.update(dash.get("label_colors") or {})
        dash = {**dash, "label_colors": merged_colors} if merged_colors else dash
        meta = build_json_metadata(dash, filters, chart_ids, existing_meta)
        result = {"position_json": position, "json_metadata": meta}
        if self.dry:
            log("[dashboard] (dry-run) position_json / json_metadata built")
            return result
        assert self.c and self.dashboard_id
        fields: dict[str, Any] = {"position_json": position, "json_metadata": meta,
                                  "dashboard_title": title, "published": bool(dash.get("published", True))}
        if dash.get("slug"):
            fields["slug"] = dash["slug"]
        if dash.get("css") is not None:
            fields["css"] = dash["css"]
        if dash.get("theme_id") is not None:
            fields["theme_id"] = dash["theme_id"]
        self.c.update_dashboard(self.dashboard_id, **fields)
        if not self.keep_unplaced:
            self.unlink_unplaced_charts({int(c["id"]) for c in self.charts.values()})
        log(f"[dashboard] layout + {len(filters)} native filter(s) saved -> {self.c.url}/superset/dashboard/"
            f"{dash.get('slug') or self.dashboard_id}/")
        return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate", action="store_true", help="run every chart query after creation")
    ap.add_argument("--only", choices=["datasets", "charts", "dashboard"])
    ap.add_argument("--state", help="JSON file with the resulting ids; if it already exists its ids are used "
                                    "to find the same charts again (safer than matching by name)")
    ap.add_argument("--keep-unplaced", action="store_true",
                    help="do not unlink charts that are linked to the dashboard but missing from the layout")
    ap.add_argument("--no-dataset-patch", action="store_true",
                    help="never modify columns/metrics of datasets that already existed (shared instances)")
    ap.add_argument("--url"), ap.add_argument("--user"), ap.add_argument("--password")
    a = ap.parse_args()

    spec = load_spec(a.spec)
    conn = spec.get("superset", {})
    client = None
    if not a.dry_run:
        # precedence: CLI flag > SUPERSET_* env var > `superset:` block in the spec (so the same spec
        # can be applied to dev and prod by switching the environment)
        client = SupersetClient(url=a.url or os.environ.get("SUPERSET_URL") or conn.get("url"),
                                username=a.user or os.environ.get("SUPERSET_USERNAME") or conn.get("username"),
                                password=a.password or os.environ.get("SUPERSET_PASSWORD") or conn.get("password"))
        try:
            client.login()
        except Exception as e:  # noqa: BLE001 - connection refused / DNS / TLS
            sys.exit(f"cannot reach Superset at {client.url}: {e}")
        me = client.whoami()
        log(f"[auth] logged in as {me.get('username')} @ {client.url}")

    app = Applier(spec, client, a.dry_run, a.keep_unplaced, a.no_dataset_patch)
    if a.state and os.path.exists(a.state) and not a.dry_run:
        try:
            with open(a.state, encoding="utf-8") as fh:
                app.prev_state = json.load(fh).get("state", {})
            log(f"[state] using ids from {a.state}")
        except (OSError, ValueError):
            app.prev_state = {}
    app.apply_datasets()
    if a.only == "datasets":
        return 0
    app.apply_dashboard_shell()           # charts are linked to the dashboard at creation time
    app.apply_charts()
    ok = app.validate_charts() if a.validate else True
    if a.only == "charts":
        return 0 if ok else 2
    out = app.apply_dashboard_layout()

    state = {"dashboard_id": app.dashboard_id, "datasets": app.dataset_ids,
             "charts": {k: {"id": v["id"], "uuid": v["uuid"]} for k, v in app.charts.items()}}
    if a.dry_run:
        print(json.dumps({"state": state, **out}, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(state, indent=2, ensure_ascii=False))
    if a.state:
        with open(a.state, "w", encoding="utf-8") as fh:
            json.dump({"state": state, **out}, fh, indent=2, ensure_ascii=False)
    return 0 if ok else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SupersetError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
