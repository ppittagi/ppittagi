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
    def __init__(self, spec: dict, client: Optional[SupersetClient], dry_run: bool):
        self.spec = spec
        self.c = client
        self.dry = dry_run
        self.dataset_ids: dict[str, int] = {}
        self.dataset_cols: dict[str, dict[str, dict]] = {}
        self.charts: dict[str, dict] = {}     # key -> {id, uuid, name, dataset_id, params}
        self.dashboard_id: Optional[int] = None
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
            if patch:
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
            found = self.c.find_chart(name, ds_id) or self.c.find_chart(name)
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

    def validate_charts(self) -> bool:
        ok = True
        if self.dry or not self.c:
            return ok
        for key, info in self.charts.items():
            qc = build_query_context(info["params"], info["dataset_id"], row_limit=5)
            try:
                res = self.c.chart_data(qc)
                r0 = res["result"][0]
                if r0.get("error"):
                    ok = False
                    log(f"[validate] {key}: ERROR {r0['error']}")
                else:
                    log(f"[validate] {key}: ok, {r0.get('rowcount')} rows, cols={r0.get('colnames')}")
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
        log(f"[dashboard] layout + {len(filters)} native filter(s) saved -> {self.c.url}/superset/dashboard/"
            f"{dash.get('slug') or self.dashboard_id}/")
        return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate", action="store_true", help="run every chart query after creation")
    ap.add_argument("--only", choices=["datasets", "charts", "dashboard"])
    ap.add_argument("--state", help="write resulting ids to this JSON file")
    ap.add_argument("--url"), ap.add_argument("--user"), ap.add_argument("--password")
    a = ap.parse_args()

    spec = load_spec(a.spec)
    conn = spec.get("superset", {})
    client = None
    if not a.dry_run:
        client = SupersetClient(url=a.url or conn.get("url"), username=a.user or conn.get("username"),
                                password=a.password or conn.get("password"))
        client.login()
        me = client.whoami()
        log(f"[auth] logged in as {me.get('username')} @ {client.url}")

    app = Applier(spec, client, a.dry_run)
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
