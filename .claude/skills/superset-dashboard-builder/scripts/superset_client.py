#!/usr/bin/env python3
"""
Thin Apache Superset REST API client + CLI (tested against 6.1, written for 7.0).

Usage (CLI):
  export SUPERSET_URL=http://localhost:8088 SUPERSET_USERNAME=admin SUPERSET_PASSWORD=admin
  python superset_client.py whoami
  python superset_client.py databases
  python superset_client.py tables --database 1 [--schema public]
  python superset_client.py columns --database 1 --table subjects [--schema public]
  python superset_client.py datasets [--search subj]
  python superset_client.py dataset get 12
  python superset_client.py dataset create --database 1 --table subjects [--schema public]
  python superset_client.py dataset create --database 1 --name v_enrollment --sql "SELECT ..."
  python superset_client.py dataset update 12 --json dataset_patch.json
  python superset_client.py charts [--search enroll]
  python superset_client.py chart get 34
  python superset_client.py chart create --name "Enrollment" --viz-type echarts_timeseries_bar \
        --dataset 12 --params params.json [--dashboard 7]
  python superset_client.py chart update 34 --params params.json
  python superset_client.py chart data --query-context qc.json      # run a query (validation)
  python superset_client.py dashboards [--search kpi]
  python superset_client.py dashboard get 7
  python superset_client.py dashboard create --title "Study KPI" [--slug study-kpi] [--published]
  python superset_client.py dashboard update 7 [--position position.json] [--metadata metadata.json]
  python superset_client.py dashboard charts 7
  python superset_client.py dashboard export 7 --out study-kpi.zip
  python superset_client.py dashboard import study-kpi.zip [--overwrite]
  python superset_client.py dashboard screenshot 7 --out dash.png
  python superset_client.py raw GET /api/v1/chart/_info
  python superset_client.py raw POST /api/v1/dashboard/ --json body.json      # or --json '{"dashboard_title": "x"}' / --json -

Environment:
  SUPERSET_URL        base URL, e.g. http://localhost:8088 (no trailing slash needed)
  SUPERSET_USERNAME / SUPERSET_PASSWORD
  SUPERSET_PROVIDER   "db" (default) or "ldap"
  SUPERSET_VERIFY_SSL "false" to skip TLS verification (self-signed certs)
  SUPERSET_TOKEN      optional: pre-issued JWT access token (skips login)

Only dependency: `requests` (pip install requests).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Iterable, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("missing dependency: pip install requests")


# --------------------------------------------------------------------------- #
# rison helpers (Superset list endpoints take `q=` in rison format)
# --------------------------------------------------------------------------- #
def rison_str(value: str) -> str:
    """Quote a string for rison: single quotes, with ' and ! escaped by '!'."""
    return "'" + value.replace("!", "!!").replace("'", "!'") + "'"


def rison_filters(filters: Iterable[tuple[str, str, Any]], page_size: int = 100,
                  order_column: Optional[str] = None, order_direction: str = "asc") -> str:
    parts = []
    for col, opr, val in filters:
        if isinstance(val, bool):
            v = "!t" if val else "!f"
        elif isinstance(val, (int, float)):
            v = str(val)
        else:
            v = rison_str(str(val))
        parts.append(f"(col:{col},opr:{opr},value:{v})")
    q = f"(filters:!({','.join(parts)}),page_size:{page_size}"
    if order_column:
        q += f",order_column:{order_column},order_direction:{order_direction}"
    q += ")"
    return q


class SupersetError(RuntimeError):
    pass


class SupersetClient:
    def __init__(self, url: Optional[str] = None, username: Optional[str] = None,
                 password: Optional[str] = None, provider: Optional[str] = None,
                 verify_ssl: Optional[bool] = None, token: Optional[str] = None):
        self.url = (url or os.environ.get("SUPERSET_URL", "http://localhost:8088")).rstrip("/")
        self.username = username or os.environ.get("SUPERSET_USERNAME", "admin")
        self.password = password or os.environ.get("SUPERSET_PASSWORD", "admin")
        self.provider = provider or os.environ.get("SUPERSET_PROVIDER", "db")
        if verify_ssl is None:
            verify_ssl = os.environ.get("SUPERSET_VERIFY_SSL", "true").lower() not in ("0", "false", "no")
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.token = token or os.environ.get("SUPERSET_TOKEN")
        self._csrf: Optional[str] = None
        self._logged_in = False

    # ---- auth ------------------------------------------------------------- #
    def login(self) -> None:
        if not self.token:
            r = self.session.post(
                f"{self.url}/api/v1/security/login",
                json={"username": self.username, "password": self.password,
                      "provider": self.provider, "refresh": True},
                timeout=60,
            )
            if r.status_code != 200:
                raise SupersetError(f"login failed ({r.status_code}): {r.text[:300]}")
            self.token = r.json()["access_token"]
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        # CSRF token is required for POST/PUT/DELETE when the session cookie is present.
        r = self.session.get(f"{self.url}/api/v1/security/csrf_token/", timeout=60)
        if r.status_code == 200:
            self._csrf = r.json()["result"]
            self.session.headers.update({"X-CSRFToken": self._csrf, "Referer": self.url + "/"})
        self._logged_in = True

    def _ensure(self) -> None:
        if not self._logged_in:
            self.login()

    # ---- generic ---------------------------------------------------------- #
    def request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None,
                files: dict | None = None, data: dict | None = None, raw: bool = False,
                retries: int = 2) -> Any:
        self._ensure()
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.url}{path}"
        for attempt in range(retries + 1):
            r = self.session.request(method, url, json=json_body, params=params, files=files,
                                     data=data, timeout=300)
            if r.status_code == 401 and attempt < retries:
                # token expired -> re-login once
                self.token = None
                self._logged_in = False
                self.login()
                continue
            if r.status_code in (502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            break
        if r.status_code >= 400:
            raise SupersetError(f"{method} {path} -> {r.status_code}: {r.text[:2000]}")
        if raw:
            return r
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, json_body=json_body, **kw)

    def put(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return self.request("PUT", path, json_body=json_body, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    def list_all(self, path: str, filters: list[tuple[str, str, Any]] | None = None,
                 page_size: int = 100, order_column: Optional[str] = None) -> list[dict]:
        """Iterate over every page of a list endpoint."""
        results: list[dict] = []
        page = 0
        while True:
            parts = []
            for col, opr, val in (filters or []):
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    v = str(val)
                else:
                    v = rison_str(str(val))
                parts.append(f"(col:{col},opr:{opr},value:{v})")
            q = f"(filters:!({','.join(parts)}),page:{page},page_size:{page_size}"
            if order_column:
                q += f",order_column:{order_column},order_direction:asc"
            q += ")"
            data = self.get(path, params={"q": q})
            chunk = data.get("result", [])
            results.extend(chunk)
            if len(chunk) < page_size:
                return results
            page += 1

    # ---- version / identity ---------------------------------------------- #
    def whoami(self) -> dict:
        return self.get("/api/v1/me/")["result"]

    def version(self) -> Optional[str]:
        """Superset version string if the instance exposes it (best effort)."""
        for path in ("/api/v1/version/", "/health"):
            try:
                d = self.get(path)
                if isinstance(d, dict) and "version" in d:
                    return str(d["version"])
            except SupersetError:
                pass
        return None

    # ---- databases -------------------------------------------------------- #
    def databases(self) -> list[dict]:
        return self.list_all("/api/v1/database/")

    def database_by_name(self, name: str) -> Optional[dict]:
        for db in self.databases():
            if db.get("database_name") == name:
                return db
        return None

    def schemas(self, database_id: int) -> list[str]:
        return self.get(f"/api/v1/database/{database_id}/schemas/")["result"]

    def tables(self, database_id: int, schema: Optional[str] = None) -> list[dict]:
        """List tables; `schema_name` is mandatory on the API, so without `schema` every schema is scanned."""
        schemas = [schema] if schema else self.schemas(database_id)
        out: list[dict] = []
        for s in schemas:
            q = f"(force:!f,schema_name:{rison_str(s)})"
            for t in self.get(f"/api/v1/database/{database_id}/tables/", params={"q": q})["result"]:
                out.append({"schema": s, **t})
        return out

    def table_metadata(self, database_id: int, table: str, schema: Optional[str] = None) -> dict:
        params = {"name": table}
        if schema:
            params["schema"] = schema
        return self.get(f"/api/v1/database/{database_id}/table_metadata/", params=params)

    # ---- datasets --------------------------------------------------------- #
    def datasets(self, search: Optional[str] = None) -> list[dict]:
        filters = [("table_name", "ct", search)] if search else None
        return self.list_all("/api/v1/dataset/", filters)

    def dataset(self, dataset_id: int) -> dict:
        return self.get(f"/api/v1/dataset/{dataset_id}")["result"]

    def find_dataset(self, table_name: str, database_id: Optional[int] = None,
                     schema: Optional[str] = None) -> Optional[dict]:
        for ds in self.list_all("/api/v1/dataset/", [("table_name", "eq", table_name)]):
            if database_id is not None and ds.get("database", {}).get("id") != database_id:
                continue
            if schema is not None and (ds.get("schema") or None) != (schema or None):
                continue
            return ds
        return None

    def create_dataset(self, database_id: int, table_name: str, schema: Optional[str] = None,
                       sql: Optional[str] = None, catalog: Optional[str] = None,
                       normalize_columns: bool = False, always_filter_main_dttm: bool = False) -> dict:
        body: dict[str, Any] = {"database": database_id, "table_name": table_name,
                                "normalize_columns": normalize_columns,
                                "always_filter_main_dttm": always_filter_main_dttm}
        if schema:
            body["schema"] = schema
        if catalog:
            body["catalog"] = catalog
        if sql:
            body["sql"] = sql
        res = self.post("/api/v1/dataset/", body)
        return {"id": res["id"], **res.get("result", {})}

    def update_dataset(self, dataset_id: int, patch: dict, override_columns: bool = False) -> dict:
        params = {"override_columns": "true"} if override_columns else None
        return self.put(f"/api/v1/dataset/{dataset_id}", patch, params=params)

    def refresh_dataset(self, dataset_id: int) -> Any:
        return self.put(f"/api/v1/dataset/{dataset_id}/refresh")

    # ---- charts ----------------------------------------------------------- #
    def charts(self, search: Optional[str] = None) -> list[dict]:
        filters = [("slice_name", "ct", search)] if search else None
        return self.list_all("/api/v1/chart/", filters)

    def chart(self, chart_id: int) -> dict:
        return self.get(f"/api/v1/chart/{chart_id}")["result"]

    def find_chart(self, slice_name: str, datasource_id: Optional[int] = None) -> Optional[dict]:
        for c in self.list_all("/api/v1/chart/", [("slice_name", "eq", slice_name)]):
            if datasource_id is not None and c.get("datasource_id") != datasource_id:
                continue
            return c
        return None

    def create_chart(self, slice_name: str, viz_type: str, datasource_id: int, params: dict,
                     dashboards: Optional[list[int]] = None, description: Optional[str] = None,
                     query_context: Optional[dict] = None, cache_timeout: Optional[int] = None) -> dict:
        params = dict(params)
        params.setdefault("datasource", f"{datasource_id}__table")
        params.setdefault("viz_type", viz_type)
        body: dict[str, Any] = {
            "slice_name": slice_name,
            "viz_type": viz_type,
            "datasource_id": datasource_id,
            "datasource_type": "table",
            "params": json.dumps(params),
            "dashboards": dashboards or [],
        }
        if description:
            body["description"] = description
        if query_context is not None:
            body["query_context"] = json.dumps(query_context)
            body["query_context_generation"] = True
        if cache_timeout is not None:
            body["cache_timeout"] = cache_timeout
        res = self.post("/api/v1/chart/", body)
        return {"id": res["id"], **res.get("result", {})}

    def update_chart(self, chart_id: int, **fields: Any) -> dict:
        body = dict(fields)
        for key in ("params", "query_context"):
            if key in body and not isinstance(body[key], str) and body[key] is not None:
                body[key] = json.dumps(body[key])
        return self.put(f"/api/v1/chart/{chart_id}", body)

    def chart_data(self, query_context: dict) -> dict:
        return self.post("/api/v1/chart/data", query_context)

    # ---- dashboards ------------------------------------------------------- #
    def dashboards(self, search: Optional[str] = None) -> list[dict]:
        filters = [("dashboard_title", "ct", search)] if search else None
        return self.list_all("/api/v1/dashboard/", filters)

    def dashboard(self, id_or_slug: int | str) -> dict:
        return self.get(f"/api/v1/dashboard/{id_or_slug}")["result"]

    def find_dashboard(self, title: Optional[str] = None, slug: Optional[str] = None) -> Optional[dict]:
        if slug:
            try:
                return self.dashboard(slug)
            except SupersetError:
                return None
        if title:
            for d in self.list_all("/api/v1/dashboard/", [("dashboard_title", "eq", title)]):
                return d
        return None

    def create_dashboard(self, title: str, slug: Optional[str] = None, published: bool = False,
                         json_metadata: Optional[dict] = None, position_json: Optional[dict] = None,
                         css: Optional[str] = None, theme_id: Optional[int] = None,
                         description: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"dashboard_title": title, "published": published}
        if slug:
            body["slug"] = slug
        if json_metadata is not None:
            body["json_metadata"] = json.dumps(json_metadata)
        if position_json is not None:
            body["position_json"] = json.dumps(position_json)
        if css:
            body["css"] = css
        if theme_id is not None:
            body["theme_id"] = theme_id
        if description:
            body["description"] = description
        res = self.post("/api/v1/dashboard/", body)
        return {"id": res["id"], **res.get("result", {})}

    def update_dashboard(self, dashboard_id: int, **fields: Any) -> dict:
        body = dict(fields)
        for key in ("json_metadata", "position_json"):
            if key in body and not isinstance(body[key], str) and body[key] is not None:
                body[key] = json.dumps(body[key])
        return self.put(f"/api/v1/dashboard/{dashboard_id}", body)

    def dashboard_charts(self, id_or_slug: int | str) -> list[dict]:
        return self.get(f"/api/v1/dashboard/{id_or_slug}/charts")["result"]

    def dashboard_datasets(self, id_or_slug: int | str) -> list[dict]:
        return self.get(f"/api/v1/dashboard/{id_or_slug}/datasets")["result"]

    def set_native_filters(self, dashboard_id: int, filter_configuration: list[dict]) -> list[dict]:
        """
        Replace the native filter configuration.
        Keeps existing json_metadata keys (colors, cross filters, ...) intact.
        """
        current = self.dashboard(dashboard_id)
        meta = json.loads(current.get("json_metadata") or "{}")
        meta["native_filter_configuration"] = filter_configuration
        self.update_dashboard(dashboard_id, json_metadata=meta)
        return filter_configuration

    def export_dashboard(self, dashboard_id: int, out_path: str) -> str:
        r = self.get("/api/v1/dashboard/export/", params={"q": f"!({dashboard_id})"}, raw=True)
        with open(out_path, "wb") as fh:
            fh.write(r.content)
        return out_path

    def import_dashboard(self, zip_path: str, overwrite: bool = False,
                         passwords: Optional[dict] = None) -> Any:
        with open(zip_path, "rb") as fh:
            files = {"formData": (os.path.basename(zip_path), fh, "application/zip")}
            data: dict[str, Any] = {"overwrite": "true" if overwrite else "false"}
            if passwords:
                data["passwords"] = json.dumps(passwords)
            return self.post("/api/v1/dashboard/import/", files=files, data=data)

    def dashboard_screenshot(self, dashboard_id: int, out_path: str, timeout_s: int = 120) -> Optional[str]:
        """
        Best effort: needs a configured screenshot backend (Playwright in 7.0).
        Flow: POST cache_dashboard_screenshot -> poll GET screenshot/<digest>/
        """
        res = self.post(f"/api/v1/dashboard/{dashboard_id}/cache_dashboard_screenshot/",
                        {"dataMask": {}, "activeTabs": [], "anchor": "", "urlParams": []})
        digest = res.get("cache_key") or res.get("image_url", "").rstrip("/").split("/")[-1]
        if not digest:
            return None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = self.get(f"/api/v1/dashboard/{dashboard_id}/screenshot/{digest}/", raw=True)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                with open(out_path, "wb") as fh:
                    fh.write(r.content)
                return out_path
            time.sleep(3)
        return None

    # ---- themes (6.0+) ---------------------------------------------------- #
    def themes(self) -> list[dict]:
        return self.list_all("/api/v1/theme/")

    def create_theme(self, name: str, json_data: dict) -> dict:
        res = self.post("/api/v1/theme/", {"theme_name": name, "json_data": json.dumps(json_data)})
        return {"id": res["id"], **res.get("result", {})}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_json(path: Optional[str]) -> Any:
    """Accepts a file path, '-' for stdin, or an inline JSON literal ('{...}' / '[...]')."""
    if not path:
        return None
    if path == "-":
        return json.load(sys.stdin)
    if path.lstrip().startswith(("{", "[")):
        return json.loads(path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _slim(items: list[dict], keys: list[str]) -> list[dict]:
    return [{k: it.get(k) for k in keys if k in it} for it in items]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Superset REST API helper")
    p.add_argument("--url"), p.add_argument("--user"), p.add_argument("--password")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami")
    sub.add_parser("version")
    sub.add_parser("databases")
    s = sub.add_parser("tables"); s.add_argument("--database", type=int, required=True); s.add_argument("--schema")
    s = sub.add_parser("columns"); s.add_argument("--database", type=int, required=True)
    s.add_argument("--table", required=True); s.add_argument("--schema")

    s = sub.add_parser("datasets"); s.add_argument("--search")
    s = sub.add_parser("dataset"); ss = s.add_subparsers(dest="sub", required=True)
    x = ss.add_parser("get"); x.add_argument("id", type=int)
    x = ss.add_parser("create"); x.add_argument("--database", type=int, required=True)
    x.add_argument("--table"); x.add_argument("--name"); x.add_argument("--schema"); x.add_argument("--sql")
    x = ss.add_parser("update"); x.add_argument("id", type=int); x.add_argument("--json", required=True)
    x.add_argument("--override-columns", action="store_true")
    x = ss.add_parser("refresh"); x.add_argument("id", type=int)

    s = sub.add_parser("charts"); s.add_argument("--search")
    s = sub.add_parser("chart"); ss = s.add_subparsers(dest="sub", required=True)
    x = ss.add_parser("get"); x.add_argument("id", type=int)
    x = ss.add_parser("create"); x.add_argument("--name", required=True); x.add_argument("--viz-type", required=True)
    x.add_argument("--dataset", type=int, required=True); x.add_argument("--params", required=True)
    x.add_argument("--dashboard", type=int, action="append"); x.add_argument("--query-context")
    x = ss.add_parser("update"); x.add_argument("id", type=int); x.add_argument("--params")
    x.add_argument("--name"); x.add_argument("--dashboard", type=int, action="append")
    x = ss.add_parser("data"); x.add_argument("--query-context", required=True)
    x = ss.add_parser("delete"); x.add_argument("id", type=int)

    s = sub.add_parser("dashboards"); s.add_argument("--search")
    s = sub.add_parser("dashboard"); ss = s.add_subparsers(dest="sub", required=True)
    x = ss.add_parser("get"); x.add_argument("id")
    x = ss.add_parser("charts"); x.add_argument("id")
    x = ss.add_parser("create"); x.add_argument("--title", required=True); x.add_argument("--slug")
    x.add_argument("--published", action="store_true")
    x = ss.add_parser("update"); x.add_argument("id", type=int); x.add_argument("--position")
    x.add_argument("--metadata"); x.add_argument("--title"); x.add_argument("--published", choices=["true", "false"])
    x = ss.add_parser("filters"); x.add_argument("id", type=int); x.add_argument("--json", required=True)
    x = ss.add_parser("export"); x.add_argument("id", type=int); x.add_argument("--out", required=True)
    x = ss.add_parser("import"); x.add_argument("zip"); x.add_argument("--overwrite", action="store_true")
    x = ss.add_parser("screenshot"); x.add_argument("id", type=int); x.add_argument("--out", required=True)
    x = ss.add_parser("delete"); x.add_argument("id", type=int)

    s = sub.add_parser("raw"); s.add_argument("method"); s.add_argument("path"); s.add_argument("--json")

    a = p.parse_args(argv)
    c = SupersetClient(url=a.url, username=a.user, password=a.password)

    if a.cmd == "whoami":
        _print(c.whoami())
    elif a.cmd == "version":
        print(c.version() or "unknown")
    elif a.cmd == "databases":
        _print(_slim(c.databases(), ["id", "database_name", "backend", "expose_in_sqllab"]))
    elif a.cmd == "tables":
        _print(c.tables(a.database, a.schema))
    elif a.cmd == "columns":
        _print(c.table_metadata(a.database, a.table, a.schema))
    elif a.cmd == "datasets":
        _print(_slim(c.datasets(a.search), ["id", "table_name", "schema", "database", "kind", "sql"]))
    elif a.cmd == "dataset":
        if a.sub == "get":
            d = c.dataset(a.id)
            d["columns"] = _slim(d.get("columns", []), ["column_name", "type", "is_dttm", "verbose_name", "expression"])
            d["metrics"] = _slim(d.get("metrics", []), ["metric_name", "expression", "verbose_name", "d3format"])
            _print(d)
        elif a.sub == "create":
            _print(c.create_dataset(a.database, a.table or a.name, a.schema, a.sql))
        elif a.sub == "update":
            _print(c.update_dataset(a.id, _load_json(a.json), a.override_columns))
        elif a.sub == "refresh":
            _print(c.refresh_dataset(a.id))
    elif a.cmd == "charts":
        _print(_slim(c.charts(a.search), ["id", "slice_name", "viz_type", "datasource_id", "datasource_name_text"]))
    elif a.cmd == "chart":
        if a.sub == "get":
            d = c.chart(a.id)
            for k in ("params", "query_context"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except ValueError:
                        pass
            _print(d)
        elif a.sub == "create":
            _print(c.create_chart(a.name, a.viz_type, a.dataset, _load_json(a.params), a.dashboard,
                                  query_context=_load_json(a.query_context)))
        elif a.sub == "update":
            fields: dict[str, Any] = {}
            if a.params:
                fields["params"] = _load_json(a.params)
            if a.name:
                fields["slice_name"] = a.name
            if a.dashboard:
                fields["dashboards"] = a.dashboard
            _print(c.update_chart(a.id, **fields))
        elif a.sub == "data":
            _print(c.chart_data(_load_json(a.query_context)))
        elif a.sub == "delete":
            _print(c.delete(f"/api/v1/chart/{a.id}"))
    elif a.cmd == "dashboards":
        _print(_slim(c.dashboards(a.search), ["id", "dashboard_title", "slug", "published", "url"]))
    elif a.cmd == "dashboard":
        if a.sub == "get":
            d = c.dashboard(a.id)
            for k in ("position_json", "json_metadata"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except ValueError:
                        pass
            _print(d)
        elif a.sub == "charts":
            _print(_slim(c.dashboard_charts(a.id), ["id", "slice_name", "form_data"]))
        elif a.sub == "create":
            _print(c.create_dashboard(a.title, a.slug, a.published))
        elif a.sub == "update":
            fields = {}
            if a.position:
                fields["position_json"] = _load_json(a.position)
            if a.metadata:
                fields["json_metadata"] = _load_json(a.metadata)
            if a.title:
                fields["dashboard_title"] = a.title
            if a.published:
                fields["published"] = a.published == "true"
            _print(c.update_dashboard(a.id, **fields))
        elif a.sub == "filters":
            _print(c.set_native_filters(a.id, _load_json(a.json)))
        elif a.sub == "export":
            print(c.export_dashboard(a.id, a.out))
        elif a.sub == "import":
            _print(c.import_dashboard(a.zip, a.overwrite))
        elif a.sub == "screenshot":
            print(c.dashboard_screenshot(a.id, a.out) or "screenshot not available (check WEBDRIVER/Playwright config)")
        elif a.sub == "delete":
            _print(c.delete(f"/api/v1/dashboard/{a.id}"))
    elif a.cmd == "raw":
        _print(c.request(a.method.upper(), a.path, json_body=_load_json(a.json)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SupersetError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
