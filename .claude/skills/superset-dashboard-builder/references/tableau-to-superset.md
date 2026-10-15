# Tableau screenshot → Superset spec

Use this when the user hands over an image (Tableau worksheet or dashboard). The goal is a filled-in
`spec.yaml` that reproduces *the same chart form, colours, sort, labels and filters* — not a
pixel-perfect copy. Work through the checklist per sheet, write down what you inferred, and ask the
user only for what the image cannot tell you (mostly: table/column names and metric definitions).

## 1. Inventory the image

For a **dashboard** image, first list the sheets (tiles), their relative sizes (fraction of width
and height) and whether there are **tabs** (Tableau sheet tabs at the bottom, story points, or
several dashboards sent as separate images → one Superset tab each). Note filter cards, legends,
parameter controls, titles and text objects.

For **each sheet**, fill this card:

```
title:            "Enrollment by Site"            (Tableau sheet title; becomes chart name)
mark type:        bar | line | area | circle | square | text | pie | map | gantt | polygon
orientation:      vertical | horizontal           (bars going right = horizontal)
x / columns:      field + type (date→time grain: day/week/month/quarter/year, or category)
y / rows:         measure(s) + aggregation shown in the pill: SUM(Sales) CNT(Subject) CNTD(Id) AVG(Age) MEDIAN(...)
color:            field (dimension → series; measure → sequential colour) + legend labels & swatch hex
size / detail:    fields (bubble size, extra granularity)
stacking:         stacked | side-by-side (dodged) | 100% (axis ends at 100%)
sort:             by measure desc/asc | alphabetical | manual
labels:           values shown on marks? totals only? number format (1,234 / 12.3% / $1.2K)
axis titles:      x title / y title / hidden
reference lines:  constant / average / target bands
filters applied:  visible filter cards + their types (list, dropdown, slider, date range, "only relevant values")
dual axis:        yes/no (second measure on right axis)
tooltip/other:    annotations, trend lines, forecast
```

Tableau pill grammar: `SUM(x)`, `AVG(x)`, `CNT(x)` = COUNT, `CNTD(x)` = COUNT_DISTINCT, `MIN/MAX`,
`MEDIAN` → SQL metric (`PERCENTILE_CONT` / `MEDIAN` depending on the DB), `ATTR` ≈ MAX,
`YEAR(d)`/`QUARTER(d)`/`MONTH(d)`/`DAY(d)` → `time_grain` P1Y/P3M/P1M/P1D, `WEEK(d)` → P1W.
Calculated fields and LOD expressions (`{FIXED [Subject]: ...}`) → either a dataset metric with SQL
or a virtual dataset (SQL with window/sub-query). Table calcs (running total, % of total, rank)
→ virtual dataset SQL (window functions) is the robust choice; `% of total` in bars can also be
`stack: Expand`; running totals in XY charts can use `extra: {cumulative: true}` only for some
types — prefer SQL.

## 2. Choose the Superset viz_type

| What you see in Tableau | viz_type | spec hints |
|---|---|---|
| Vertical bars over time | `echarts_timeseries_bar` | `x: <date>`, `time_grain`, `groupby` for colour |
| Vertical bars over categories | `echarts_timeseries_bar` | `x: <category>` (no time_grain), `sort`; bars coloured by the same field → add `groupby: [<category>]` |
| Horizontal bars (ranked) | `echarts_timeseries_bar` | `orientation: horizontal`, `sort: {metric, desc: true}` (desc = largest on top; the builder handles ECharts' bottom-up axis) |
| Stacked bars | same | `stack: true`; 100 % → `stack: Expand` |
| Side-by-side bars (dodged) | same | `stack: false` |
| Line, multiple lines | `echarts_timeseries_line` | `groupby` = colour field |
| Smooth line / step line | `echarts_timeseries_smooth` / `_step` | |
| Area, stacked area | `echarts_area` | `stack: true` |
| Bar + line dual axis | `mixed_timeseries` | `metrics` (A, bar) + `b: {metrics, kind: line, secondary_axis: true}` |
| Pie / donut | `pie` | `donut: true`; label style via `extra: {label_type: key_percent}` |
| Single big number (text mark, one cell) | `big_number_total` | `subheader` for the caption |
| Big number with sparkline | `big_number` | `x: <date>`, `time_grain` |
| Text table (rows × measures) | `table` | `groupby` + `metrics`; `sort` |
| Crosstab (rows × columns × measure) | `pivot_table_v2` | `rows`, `columns`, `metrics`, `row_totals` |
| Highlight table (cells coloured by value) | `heatmap_v2` (one measure) or `pivot_table_v2` + `conditional_formatting` | |
| Heat map / calendar-ish grid | `heatmap_v2` | `x`, `groupby` (single), `linear_color_scheme` |
| Scatter (circle marks, 2 measures) | `bubble_v2` | `entity`, `series`, `x_metric`, `y_metric`, `size_metric` |
| Histogram | `histogram_v2` | `column`, `bins` |
| Box-and-whisker (with or without jittered points) | `box_plot` | `x` = category (one box each), `distribute: [row id]`, `metrics` = measure aggregated per row id (SUM(sales) per order) |
| Funnel (sorted horizontal bars shrinking) | `funnel` | |
| Gauge / bullet | `gauge_chart` / `bullet` | `min_val`, `max_val`, `intervals` |
| Treemap | `treemap_v2` | `groupby` = hierarchy |
| Filled map / symbol map | `country_map` / `world_map` / `deck_scatter` | needs ISO codes or lat/lon |
| Gantt (visit windows) | `gantt` (7.0) or `echarts_timeseries_bar` horizontal with start/end virtual dataset | |
| Text object / notes | `layout` `markdown` row | |
| Reference line / band, shaded period | `annotations:` on XY charts (formula line, native interval layer) | see chart-params.md |
| Continuous colour on marks (scatter) | band column in SQL + `groupby: [band]` + `label_colors` | |
| Year × month heatmap (calendar) | `heatmap_v2`, `x: year`, `groupby: [month label with space prefix]`, `xscale_interval` | |
| Highlight parameters | no equivalent; `select` filters are the nearest (they filter, not highlight) | |

Quarter labels: d3 has no quarter directive, so `QUARTER(d)` as "2023-Q1" needs a calculated column in
the dataset (`strftime('%Y', d) || '-Q' || ((CAST(strftime('%m', d) AS INT) + 2) / 3)`) used as a
categorical `x`, with `time_column: d` so time filters still apply; a temporal `P3M` axis shows month labels.
Legend titles and legends inside the plot area do not exist in Superset (legend is top/bottom/left/right).
Filter cards always live in the left (or top) filter bar; tabs always sit on top of the canvas.
Ridgeline / joy plots: no equivalent — overlaid translucent area curves from a binned virtual dataset
(`assets/examples/ridgeline-customers.yaml`) or one box plot per category are the closest forms.
Survey dashboards (donut per answer, Likert diverging bars, rank matrices): `assets/examples/wow-survey.yaml`.

Things Superset cannot do 1:1 (say so, then pick the closest): stacked/dodged bars sorted by total
(categories stay alphabetical when a colour breakdown exists — see "Sorting with breakdowns" in
`chart-params.md`), dual-colour encodings on one mark,
Tableau parameters that swap measures (→ separate charts or tabs), sheet-level "Pages" animation
(→ time filter), tooltips with embedded vizzes, conditional mark shapes, custom polygon maps without
GeoJSON.

## 3. Map colours

Read legend label ↔ swatch; produce `label_colors: {label: "#hex"}` and set it at the **dashboard**
level so the same site/arm gets the same colour on every tab. Snap to the Tableau palettes in
`colors.md` when swatches are slightly off due to image compression. For measures encoded as
colour (highlight tables, maps) choose the closest `linear_color_scheme`.

## 4. Map filters

Each Tableau filter card → one native filter (`references/native-filters.md`). Group them with a
`divider` if the Tableau dashboard had sections. Date filter cards → one `type: time` filter; make
sure every chart has `time_column` (or a temporal `x`) so it reacts. "Apply to selected worksheets"
→ `scope.tabs`/`exclude_charts`. Dashboard actions (click to filter) → `dashboard.cross_filters: true`.

## 5. Map layout

Dashboard width fractions × 12 → `width`; keep each row ≤ 12. Heights: KPI row 20–25, charts
50–60, tables 60–90. Tableau tabs / multiple dashboards → `layout.tabs`. Titles/captions → `header`
rows, text objects → `markdown`.

## 5b. Layout tile titles

The dashboard tile shows the chart's `name`; when several dashboards reproduce the same Tableau
sheet, keep chart names unique per dashboard (e.g. "Sales by Region (Overview)") and set the Tableau
title on the tile with `title:` in the layout item (`{chart: key, width: 6, title: "Sales by Region"}`).

## 6. Resolve data bindings (ask the user when needed)

The image gives field *display names*; the dataset has *column names*. Pull the dataset columns
(`superset_client.py dataset get <id>` or `columns --database … --table …`) and map
case-insensitively/fuzzily ("Subject ID" → `subject_id`, "Enrolled Date" → `enrolled_date`). Define
missing measures as dataset metrics (`metrics:` block) so every chart reuses the same definition.
Confirm with the user in one message: field mapping table + anything the image did not specify
(number formats, exact sort, date grain if ambiguous).

## 7. Produce the spec, apply, compare

Write `spec.yaml`, run `build_dashboard.py spec.yaml --dry-run` (inspect params), then apply with
`--validate` (every chart query executes). Open the dashboard URL, or fetch
`superset_client.py dashboard screenshot <id> --out after.png` when the instance has Playwright
screenshots configured, and compare with the Tableau image: chart form, series order, colours,
number formats, filters present. Iterate on the spec (it is idempotent) rather than on the UI, so the
result stays reproducible and can be committed.

## Worked example (one sheet)

Image: horizontal stacked bars, one bar per "System Organ Class", segments coloured by "Grade
Group" (legend: Grade 1-2 light green, Grade 3+ red), values shown at bar ends, sorted descending,
filter card "Site" (multi-select) on the right.

```yaml
charts:
  - key: ae_by_soc
    name: AEs by system organ class
    dataset: ae
    viz_type: echarts_timeseries_bar
    x: soc
    metrics: [{col: ae_id, agg: COUNT, label: AEs}]
    groupby: [grade_group]
    orientation: horizontal
    stack: true
    show_value: true          # totals at bar end (only_total default true)
    sort: {metric: AEs, desc: true}
    label_colors: {"Grade 1-2": "#8CD17D", "Grade 3+": "#E15759"}
    time_column: onset_date
filters:
  - {name: Site, type: select, column: site, datasets: [ae], multi: true}
```
