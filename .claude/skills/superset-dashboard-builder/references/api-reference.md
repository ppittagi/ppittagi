# Superset 7.0 REST API — what this skill relies on

All endpoints live under `{SUPERSET_URL}/api/v1/`. The live OpenAPI spec of *your* instance is at
`GET /api/v1/_openapi` and the Swagger UI at `/swagger/v1`. **Both are off by default in 7.0**
(verified on 7.0.0rc1: 404 until enabled): set the env var `SUPERSET_ENABLE_SWAGGER_UI=true` or
`FAB_API_SWAGGER_UI = True` in `superset_config.py`, restart, and run `superset init` once more so
the `OpenApi` permission exists (otherwise `_openapi` answers 403). When in doubt, read `_openapi`
rather than guessing — field names move between versions. A quick alternative that needs no config
change: `GET /api/v1/<resource>/_info?q=(keys:!(add_columns,edit_columns))` lists the writable
fields of a resource (e.g. `dashboard/_info` on 7.0 shows `editors`, `viewers`, `theme_id`).

## Authentication

```
POST /api/v1/security/login      {"username","password","provider":"db"|"ldap","refresh":true}
      -> {"access_token","refresh_token"}
GET  /api/v1/security/csrf_token/   (Authorization: Bearer <access_token>) -> {"result": "<csrf>"}
POST /api/v1/security/refresh       (Authorization: Bearer <refresh_token>)
```

Send on every mutating call: `Authorization: Bearer …`, `X-CSRFToken: …`, `Referer: {SUPERSET_URL}/`,
and keep the cookie jar (a `requests.Session`). Missing CSRF → `400 "The CSRF token is missing"`.
Tokens expire (default 15 min for access); `superset_client.py` re-logs-in on 401.
SSO (OAuth/OIDC) deployments: either enable `AUTH_USER_REGISTRATION`-independent DB login for a
service account, or issue a long-lived JWT and pass it via `SUPERSET_TOKEN`.

## 7.0 changes that bite scripts written for 5.x/6.x

| Area | 7.0 behaviour |
|---|---|
| Ownership | `owners` is **replaced by `editors` / `viewers`** (lists of subject ids: users, roles, groups) on dashboards, charts, datasets, alerts. Do not send `owners`. Creator is editor automatically. |
| Swagger / OpenAPI | Both `/swagger/v1` and `/api/v1/_openapi` are off by default (see top of this file); `/api/v1/<resource>/_info` always works. |
| Soft delete | `SOFT_DELETE` on by default: `DELETE` archives; `POST /<uuid>/restore`, `POST /<uuid>/purge`. |
| Version history | `VERSION_HISTORY` on by default: `PUT` returns version metadata; `/versions` endpoints exist. |
| Externally managed | `PUT` on an entity with `is_managed_externally=true` → 403. Leave it false for API-built objects you want to edit in the UI. |
| Dashboard visibility | Datasource-based read fallback only for **published** dashboards → set `published: true`. |
| Charts | Legacy `explore_json` pipeline removed; all charts go through `/api/v1/chart/data`. nvd3 bubble → `bubble_v2`; percent-change → ECharts line. |
| Pivot table | per-table "Aggregation function" control removed; totals computed in DB. |
| Native filters | Incompatible filter targets are skipped and flagged "Incompatible" in the UI instead of silently ignored. |
| Themes | Ant Design 6 tokens; per-theme editors (`PUT /api/v1/theme/<id>` needs editor/Admin). |
| Graph null labels | `label_colors["<NULL>"]` instead of `"N/A"`. |
| MCP | Built-in MCP service (`superset mcp run`), see "Alternative: MCP" below. |

## Databases / tables (discovery)

```
GET /api/v1/database/                                  -> list (id, database_name, backend)
GET /api/v1/database/{id}/schemas/
GET /api/v1/database/{id}/tables/?q=(schema_name:'public',force:!f)
GET /api/v1/database/{id}/table_metadata/?name=subjects&schema=public   -> columns + types
GET /api/v1/datasource/                                 (7.0, combined listing)
```

Register a connection (what the UI's Data → Databases → + does); needs the driver on the server:
```
POST /api/v1/database/test_connection   {"sqlalchemy_uri":"postgresql://u:p@host:5432/dw","database_name":"DW"}
POST /api/v1/database/   {"database_name":"Clinical DW","sqlalchemy_uri":"postgresql://u:p@host:5432/dw",
                          "expose_in_sqllab":true,"extra":"{\"engine_params\":{\"connect_args\":{\"sslmode\":\"require\"}}}",
                          "allow_dml":false,"impersonate_user":false}
PUT  /api/v1/database/{id}   (GET returns the uri with the password masked as XXXXXXXXXX; PUT with the
                              masked value keeps the stored password)
```
Spec: `databases: [{name, uri | uri_env, expose_in_sqllab, extra, update}]` — matched by name,
created when missing, left untouched unless `update: true`. Drivers: `psycopg2` (PostgreSQL),
`pymssql`/`pyodbc` (SQL Server), `oracledb`, `mysqlclient`, `snowflake-sqlalchemy`, `trino`, …
must be installed in the Superset image (`pip install` in the Dockerfile), otherwise
`test_connection` answers "Could not load database driver".

## Datasets

```
GET  /api/v1/dataset/?q=(filters:!((col:table_name,opr:eq,value:'subjects')),page_size:100)
POST /api/v1/dataset/        {"database":1,"schema":"public","table_name":"subjects"}
POST /api/v1/dataset/        {"database":1,"table_name":"v_enroll","sql":"SELECT ..."}   # virtual
GET  /api/v1/dataset/{id}    -> columns[], metrics[], main_dttm_col ...
PUT  /api/v1/dataset/{id}    {"metrics":[{"metric_name","expression","verbose_name","d3format"}],
                              "columns":[{"column_name","is_dttm","verbose_name","expression"}],
                              "main_dttm_col": "enrolled_date"}
PUT  /api/v1/dataset/{id}/refresh      (re-read physical columns)
```

Rules: `table_name` is unique per (database, schema). A dataset always belongs to one database
connection; a chart reads one dataset, so data from two databases needs two datasets (or a virtual
dataset on a database that can reach both). Virtual datasets need `sql`; after changing
`sql` call `/refresh`. When sending `columns`/`metrics` in a PUT, include the existing ones (with `id`)
you want to keep — the list is replaced, not merged (the scripts handle this). Mark the date column
`is_dttm: true` and set `main_dttm_col` so time filters and `time_grain` work.

Column names with mixed case / special chars: set `normalize_columns: false` (default) and quote
nothing — Superset quotes for you.

## Charts

```
GET  /api/v1/chart/?q=(filters:!((col:slice_name,opr:eq,value:'Enrollment')))
POST /api/v1/chart/   {"slice_name","viz_type","datasource_id","datasource_type":"table",
                       "params": "<json string>", "dashboards":[7], "query_context":"<json string>",
                       "query_context_generation": true, "description", "cache_timeout"}
PUT  /api/v1/chart/{id}   same fields, all optional; `dashboards` *replaces* the link list
GET  /api/v1/chart/{id}
POST /api/v1/chart/data    {"datasource":{"id","type":"table"},"queries":[...],"form_data":{...},
                            "result_format":"json","result_type":"full"}   # run a query
GET  /api/v1/chart/{id}/data/      # run the *saved* query_context
```

* `params` = Explore form_data **as a JSON string**. `params.datasource` must be `"<id>__table"`,
  `params.viz_type` must match. See `chart-params.md` for per-type templates.
* `query_context` is optional for rendering (the frontend rebuilds queries from `params`) but is
  what thumbnails, alerts/reports, CSV export and cache warm-up use — store it when you can.
* The reliable way to know a chart works: `POST /api/v1/chart/data` with the query_context and
  check `result[0].error` is empty (what `build_dashboard.py --validate` does).
* To copy a chart someone built in the UI: `GET /api/v1/chart/{id}` → reuse its `params`.

## Dashboards

```
GET  /api/v1/dashboard/?q=(filters:!((col:dashboard_title,opr:eq,value:'Study KPI')))
GET  /api/v1/dashboard/{id_or_slug}            -> position_json, json_metadata (strings)
GET  /api/v1/dashboard/{id_or_slug}/charts     -> chart ids/form_data on the dashboard
GET  /api/v1/dashboard/{id_or_slug}/datasets
GET  /api/v1/dashboard/{id_or_slug}/tabs       -> tab tree (ids used by filter scopes)
POST /api/v1/dashboard/   {"dashboard_title","slug","published":true,"json_metadata":"{}","position_json":"{}",
                           "css","theme_id","description"}
PUT  /api/v1/dashboard/{id}   {"position_json":"<json string>","json_metadata":"<json string>", ...}
PUT  /api/v1/dashboard/{id}/filters              {"modified":[...filters],"deleted":[ids],"reordered":[ids]}
PUT  /api/v1/dashboard/{id}/colors               {"color_scheme","label_colors","shared_label_colors",...}
PUT  /api/v1/dashboard/{id}/chart_customizations
GET  /api/v1/dashboard/export/?q=!(7)            -> zip (YAML bundle; commit it to git)
POST /api/v1/dashboard/import/   multipart formData=<zip>, overwrite=true|false, passwords={...}
POST /api/v1/dashboard/{id}/copy
POST /api/v1/dashboard/{id}/cache_dashboard_screenshot/   then GET /api/v1/dashboard/{id}/screenshot/<key>/
```

Order that works everywhere: **create dashboard (empty) → create charts with `dashboards:[id]` →
PUT dashboard with `position_json` + `json_metadata`.** A chart shows up only if it is both linked
(`dashboards`) and referenced in `position_json` (`meta.chartId`). `position_json` and
`json_metadata` are JSON **strings** inside the JSON body. See `dashboard-layout.md`,
`native-filters.md`.

`json_metadata` keys (DashboardJSONMetadataSchema): `native_filter_configuration`,
`chart_configuration`, `global_chart_configuration`, `cross_filters_enabled`, `color_scheme`,
`label_colors`, `shared_label_colors` (list of labels), `map_label_colors`, `color_scheme_domain`,
`expanded_slices`, `refresh_frequency`, `timed_refresh_immune_slices`, `filter_bar_orientation`
(`VERTICAL|HORIZONTAL`), `show_chart_timestamps`, `async_mode`. Unknown keys → 400.

## Themes (6.0+/7.0)

```
GET/POST /api/v1/theme/     {"theme_name","json_data":"<Ant Design token JSON string>"}
PUT  /api/v1/theme/{id}/set_system_default | set_system_dark
```
`json_data` example: `{"token":{"colorPrimary":"#4E79A7","fontFamily":"Pretendard, sans-serif",
"borderRadius":4}}`. Attach to a dashboard with `theme_id`. Themes restyle chrome (fonts, primary
colour, backgrounds); series colours still come from `color_scheme` / `label_colors`.

## Error cheat-sheet

| Status / message | Cause → fix |
|---|---|
| 400 `CSRF token is missing` | add `X-CSRFToken` + cookies + `Referer` |
| 401 | token expired → login again |
| 403 on PUT | not editor; or `is_managed_externally=true`; or theme editorship (7.0) |
| 422 `Dataset ... already exists` | look it up and PUT instead |
| 422 on chart create `datasource_id ... not found` | dataset id wrong / no access |
| 400 `json_metadata ... Unknown field` | key not in DashboardJSONMetadataSchema |
| `chart/data` `error: "Column ... not found"` | column name mismatch → check `GET /api/v1/dataset/{id}` |
| Dashboard renders "incompatible" badge on a filter | filter targets a dataset/column some charts don't have — exclude them in `scope.excluded` or add a target per dataset |

## Alternative: the built-in MCP service

Superset 6.1+/7.0 ships an MCP server (`superset mcp run --port 5008`, config `MCP_AUTH_ENABLED`,
`MCP_JWT_PUBLIC_KEY`, dev-only `MCP_DEV_USERNAME`). Tools include `list_datasets`, `get_dataset_info`,
`generate_chart` (compact `chart_type` configs: xy, pie, table, big_number, …), `generate_dashboard`
(from chart ids; auto 2-column layout or explicit `position_json`), `update_dashboard`,
`add_chart_to_dashboard` (`target_tab`), `execute_sql`. It has **no native-filter or tab authoring**
(tabs only via raw `position_json`), so this skill's REST scripts remain the way to build the full
dashboard; MCP is a nice complement for ad-hoc "show me the AE count by site" questions.
