---
name: superset-dashboard-builder
description: >
  Build Apache Superset (7.0 / 6.x) datasets, charts and dashboards through the REST API — including
  tabbed dashboards and dashboard-wide native filters — from a chart description or from a screenshot
  of an existing chart/dashboard (typically Tableau) that must be reproduced with the same chart
  type, colours, sort, labels and filters. Use this skill whenever the user mentions Superset, a
  Superset dashboard/chart/dataset, migrating Tableau (or Power BI / Looker) charts to Superset,
  "superset api", native filters, dashboard tabs, position_json / json_metadata, or hands over a
  chart image and wants it rebuilt in Superset — even if they just say "이 차트 수퍼셋으로 만들어줘".
---

# Superset dashboard builder

Verified end-to-end against `apache/superset:7.0.0rc1` and 6.1.0 (the example spec, all tabs
rendered, filters scoped, export/import).

Turn "I want this chart/dashboard in Superset" into created objects, reproducibly:
**spec.yaml → `scripts/build_dashboard.py` → datasets, charts, dashboard (tabs, layout), native
filters, colours**. The spec is idempotent (re-running updates the same objects: the dashboard by
slug/title, charts by the ids in `--state` or by name *within that dashboard*, datasets by table
name) so it can live in git next to the Tableau screenshots it was derived from. Always pass the
same `--state state.json` on re-runs; a same-named chart on another dashboard is never touched. Answer the user in their language (Korean here);
keep YAML keys and Superset terms in English.

## Workflow

1. **Connect & discover.** Needs `SUPERSET_URL`, `SUPERSET_USERNAME`, `SUPERSET_PASSWORD`
   (or `SUPERSET_TOKEN`). Ask for them if missing — never guess credentials.
   ```bash
   pip install requests pyyaml            # once
   python scripts/superset_client.py whoami
   python scripts/superset_client.py databases
   python scripts/superset_client.py tables  --database 1 --schema public
   python scripts/superset_client.py columns --database 1 --table subjects
   python scripts/superset_client.py datasets --search subj     # reuse existing datasets when possible
   ```
   Knowing the real column names and types *before* designing charts avoids most failures
   (`chart/data` errors are almost always a column/metric name mismatch).

2. **Understand what to build.**
   * *User picks a chart type / describes it* → map to a `viz_type` with `references/chart-params.md`.
   * *User sends an image (Tableau sheet or dashboard)* → read `references/tableau-to-superset.md`
     and fill its per-sheet card: mark type, x/y fields + aggregations, colour field, legend labels and
     swatch hex, stacking, sort, labels, axis titles, filter cards, tabs, layout proportions. State
     your inferences explicitly; ask only about things the image cannot show (column names, metric
     definitions, date grain if ambiguous). One confirmation round, then build.
   * Several dashboards / sheet tabs → one `layout.tabs` entry each. Filter cards → `filters:`.

3. **Write `spec.yaml`** (start from `assets/spec.example.yaml`; schema in the docstrings of
   `scripts/chart_builder.py` and `scripts/dashboard_builder.py`). Decide:
   * datasets (physical table or virtual SQL; add `metrics:` for reusable measures, `main_dttm_col`)
   * charts: `viz_type`, `x`, `time_grain`, `metrics`, `groupby`, `filters`, `orientation`, `stack`,
     `sort`, `show_value`, `legend`, titles, formats, `label_colors`, and `time_column` so the
     dashboard time filter reaches the chart
   * dashboard: `title`, `slug`, `published: true`, `color_scheme`, `label_colors` (dashboard-wide
     colour pinning), `cross_filters`
   * layout: `tabs` → `rows` → `[ {chart, width, height} ]`, widths sum to 12 per row
   * filters: `select | range | time | timegrain | timecolumn | divider`, `datasets`, `default`,
     `depends_on`, `scope`

4. **Dry-run, then apply with validation.**
   ```bash
   python scripts/build_dashboard.py spec.yaml --dry-run        # inspect params / position_json / filters
   python scripts/build_dashboard.py spec.yaml --validate --state state.json
   ```
   `--validate` executes every chart query through `/api/v1/chart/data`; fix errors in the spec
   (not in the UI) and re-run. The script prints the dashboard URL, and `state.json` holds the real
   dataset/chart/dashboard ids (dry-run prints placeholder ids) for later `chart get` / screenshot calls.

5. **Verify & iterate.** Open the dashboard; compare with the source image (form, series order,
   colours, number formats, filters). If screenshots are configured on the server,
   `superset_client.py dashboard screenshot <id> --out after.png` gives you an image to compare
   yourself. Adjust the spec and re-apply. When done, `superset_client.py dashboard export <id>
   --out dash.zip` and commit spec + export so the dashboard is reproducible on another instance
   (`dashboard import … --overwrite`).

## Things that are easy to get wrong

* **7.0 renamed `owners` → `editors`/`viewers`.** The scripts send neither (creator becomes editor).
  Don't add `owners` to payloads; the ids are *subject* ids (users/roles/groups).
* `params`, `query_context`, `position_json`, `json_metadata` are **JSON strings inside JSON**.
  The client does the `json.dumps` for you.
* A chart appears on a dashboard only when it is **linked** (`dashboards: [id]`) **and** placed in
  `position_json` (`meta.chartId`). The builder creates the dashboard first so charts can be linked
  at creation.
* Dashboard-wide filtering = one native filter with **one target per dataset** that has the column,
  `scope.rootPath: ["ROOT_ID"]`, and charts of other datasets excluded. A `time` filter only
  reaches charts that have a temporal x-axis or a `TEMPORAL_RANGE` adhoc filter (`time_column`).
* Exact colours: `label_colors` (label → hex) at dashboard level; `color_scheme` is only the
  fallback. There is no built-in Tableau palette — hex values are in `references/colors.md`.
* Row widths must sum to ≤ 12; heights are in 8 px units (`50` ≈ 400 px). Charts linked to the
  dashboard but missing from the layout are unlinked on apply (Superset would otherwise render them
  under the layout on every tab); pass `--keep-unplaced` to leave them.
* Datasets are matched by table name: the spec's `columns:`/`metrics:` patch whatever dataset
  matches, even one created by someone else. Keep those blocks additive (verbose names, `is_dttm`).
* Horizontal bars: `sort: {metric, desc: true}` means largest on top (the builder flips the ECharts
  order for you). One colour per bar: `groupby` the x column too; the builder aliases and stacks it.
* Swagger UI and `GET /api/v1/_openapi` are both off by default in 7.0 (see
  `references/api-reference.md` to enable them); `GET /api/v1/<resource>/_info?q=(keys:!(add_columns))`
  always works — read it when a field name is in doubt instead of guessing.
* Virtual datasets: after changing `sql`, columns must be refreshed (the builder does it). Mark date
  columns `is_dttm: true` or time grains/filters will not work.
* If a chart type is not templated in `chart_builder.py`, pass full `params:` for it (copy from an
  existing chart via `superset_client.py chart get <id>`, or from `references/chart-params.md`).
* Stacked/dodged bars **cannot be sorted by total** (Superset limitation with `groupby`); tell the
  user and pick a workaround from `references/chart-params.md` ("Sorting with breakdowns").
* No server-side screenshot setup? `scripts/screenshot_dashboard.py <slug> --all-tabs` renders the
  dashboard with a local headless Chromium (needs `pip install playwright`), one PNG per tab (one PNG
  when the dashboard has no tabs), which you can look at and compare with the Tableau image. Use
  `--wait 20` right after a layout change (charts load after the filter bar), and `--query
  "native_filters=(...)"` to capture a filter state.
* A dataset metric's `d3format` overrides chart number formats (Big Number, pivot). Format per chart
  with adhoc metrics, or leave `d3format` empty on shared metrics.

## References (read when needed)

| File | When |
|---|---|
| `references/api-reference.md` | endpoints, auth/CSRF, 7.0 breaking changes, error cheat-sheet, built-in MCP alternative |
| `references/chart-params.md` | viz_type catalogue, full params templates, metrics/filters/annotation syntax |
| `references/tableau-to-superset.md` | image analysis checklist, Tableau→Superset chart/filter/layout mapping |
| `references/dashboard-layout.md` | position_json tree, tabs, widths/heights, editing existing layouts |
| `references/native-filters.md` | native_filter_configuration schema, defaults, cascading, scopes, cross-filters |
| `references/colors.md` | Tableau palettes (hex), Superset schemes, label_colors, themes, CSS |
| `assets/spec.example.yaml` | complete clinical-trial example: 2 datasets, 7 charts, 3 tabs, 5 filters |

## Scripts

| Script | Purpose |
|---|---|
| `scripts/superset_client.py` | auth + CLI for every endpoint used (discover, CRUD, chart data, export/import, screenshot, `raw`) |
| `scripts/chart_builder.py` | spec → `params` (form_data) and `query_context` for ~25 viz_types |
| `scripts/dashboard_builder.py` | layout spec → `position_json`; filters spec → `native_filter_configuration`; `json_metadata` |
| `scripts/build_dashboard.py` | orchestrates everything idempotently; `--dry-run`, `--validate`, `--only`, `--state` |
| `scripts/screenshot_dashboard.py` | local headless-Chromium screenshots of a dashboard / each tab for visual comparison |

Prefer the scripts over ad-hoc `curl`: they handle CSRF, token refresh, rison lookups, column
checks and the chart↔dashboard linking order. For one-off calls use `superset_client.py raw METHOD
/api/v1/... --json <file | - | inline JSON>`.

## Alternative routes worth knowing

* **Superset's built-in MCP service** (`superset mcp run`, 6.1+/7.0): `generate_chart`,
  `generate_dashboard`, `execute_sql` — good for ad-hoc exploration, but it cannot author native
  filters or tabs. Use the REST spec for production dashboards.
* **Import bundle** (`dashboard export` zip = YAML files): promote a finished dashboard across
  environments (dev → prod) with `dashboard import --overwrite`; uuids keep identities stable.
