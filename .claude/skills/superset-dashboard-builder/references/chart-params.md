# Chart `params` (form_data) catalogue — Superset 7.0 viz_types

`scripts/chart_builder.py` generates these from the compact spec; this file is for reading/editing
raw params, choosing a `viz_type`, and handling types the builder does not template.

## Common building blocks

```jsonc
// saved metric (defined on the dataset)       -> "n_subjects"
// adhoc SIMPLE metric
{"expressionType":"SIMPLE","column":{"column_name":"subject_id"},"aggregate":"COUNT_DISTINCT",
 "label":"Subjects","hasCustomLabel":true,"optionName":"metric_subjects"}
// adhoc SQL metric
{"expressionType":"SQL","sqlExpression":"SUM(CASE WHEN serious=1 THEN 1 ELSE 0 END)","label":"SAEs","optionName":"metric_sae"}
// aggregates: SUM COUNT COUNT_DISTINCT AVG MIN MAX  (others via SQL)

// adhoc filters
{"expressionType":"SIMPLE","subject":"status","operator":"IN","operatorId":"IN","comparator":["Enrolled"],"clause":"WHERE"}
{"expressionType":"SIMPLE","subject":"enrolled_date","operator":"TEMPORAL_RANGE","operatorId":"TEMPORAL_RANGE","comparator":"Last year","clause":"WHERE"}
{"expressionType":"SQL","sqlExpression":"grade >= 3","clause":"WHERE"}
// operators: == != > < >= <= IN "NOT IN" LIKE ILIKE "IS NULL" "IS NOT NULL" TEMPORAL_RANGE
// time_range strings: "No filter", "Last day|week|month|quarter|year", "previous calendar month",
//   "2025-01-01 : 2025-12-31", "DATEADD(DATETIME(\"today\"), -30, day) : today"
// time_grain_sqla: PT1H P1D P1W P1M P3M P1Y
// number formats (d3): SMART_NUMBER ",d" ".1f" ".1%" "$,.2f" ; dates: smart_date "%Y-%m-%d" "%b %Y"
```

Every chart must carry `"datasource": "<dataset_id>__table"` and `"viz_type"`. A **TEMPORAL_RANGE
adhoc filter on the chart's date column is what lets a dashboard time filter reach the chart** —
the builder adds one (comparator `No filter`) whenever `x` is temporal or `time_column` is set.

## viz_type catalogue (7.0, ECharts-based unless noted)

| Family | viz_type | Key params | Tableau equivalent |
|---|---|---|---|
| Bar | `echarts_timeseries_bar` | `x_axis`, `time_grain_sqla`, `metrics[]`, `groupby[]`, `orientation` vertical/horizontal, `stack` null/"Stack"/"Stream"/"Expand", `show_value`, `only_total` | bar / stacked bar / horizontal bar |
| Line | `echarts_timeseries_line` (also `_smooth`, `_step`, `_scatter`) | as bar + `markerEnabled`, `area`, `opacity`, `zoomable` | line / step / dual-line |
| Area | `echarts_area` | as line, `stack` "Stack"/"Expand" | area / stacked area / 100 % |
| Combo | `mixed_timeseries` | query A (`metrics`,`groupby`,`seriesType`) + query B (`metrics_b`,`groupby_b`,`seriesTypeB`,`yAxisIndexB:1`) | dual-axis bar+line |
| Pie/Donut | `pie` | `groupby[]`, `metric`, `donut`, `label_type` key/value/percent/key_value/key_percent/key_value_percent, `show_labels_threshold` | pie / donut |
| KPI | `big_number_total` | `metric`, `subheader`, `y_axis_format` | text/KPI tile |
| KPI + trend | `big_number` | `metric`, `x_axis`, `time_grain_sqla`, `compare_lag`, `compare_suffix`, `show_trend_line` | KPI with sparkline |
| Period over period | `pop_kpi` | `metric`, `time_comparison` (`r`,`y`,`m`,`w`), `comparison_type` | KPI vs prior period |
| Table | `table` | `query_mode` aggregate/raw, `groupby[]`+`metrics[]` or `all_columns[]`, `percent_metrics[]`, `server_pagination`, `conditional_formatting[]`, `column_config{}` | text table / crosstab (flat) |
| Pivot | `pivot_table_v2` | `groupbyRows[]`, `groupbyColumns[]`, `metrics[]`, `metricsLayout` COLUMNS/ROWS, `rowTotals`, `colTotals`, `conditional_formatting[]` | crosstab / highlight table |
| Grid table | `ag-grid-table` | like `table`, interactive sort/filter | text table |
| Heatmap | `heatmap_v2` | `x_axis`, `groupby` (single column), `metric`, `linear_color_scheme`, `normalize_across` heatmap/x/y, `show_values` | highlight table / heat map |
| Box plot | `box_plot` | `columns[]` (distribute across), `groupby[]` (series), `metrics[]`, `whiskerOptions` | box-and-whisker |
| Histogram | `histogram_v2` | `column`, `bins`, `groupby[]`, `normalize`, `cumulative` | histogram |
| Scatter/bubble | `bubble_v2` | `entity`, `series`, `x`, `y`, `size` (metrics), `max_bubble_size` | scatter / bubble |
| Funnel | `funnel` | `groupby[]`, `metric`, `percent_calculation_type` | funnel |
| Gauge | `gauge_chart` | `metric`, `min_val`, `max_val`, `intervals`, `interval_color_indices` | gauge / bullet |
| Bullet | `bullet` | `metric`, `ranges`, `markers` | bullet |
| Treemap | `treemap_v2` | `groupby[]` (hierarchy), `metric` | treemap |
| Sunburst | `sunburst_v2` | `columns[]` (hierarchy), `metric`, `secondary_metric` | sunburst |
| Sankey | `sankey_v2` | `source`, `target`, `metric` | sankey (ext.) |
| Radar | `radar` | `groupby[]`, `metrics[]` | radar |
| Waterfall | `waterfall` | `x_axis`, `groupby`, `metric` | waterfall |
| Word cloud | `word_cloud` | `series`, `metric` | word cloud |
| Maps | `world_map`, `country_map`, `deck_scatter`, `deck_polygon`, `deck_geojson`, `cartodiagram` | entity/country columns, lat/lon | filled map / symbol map |
| Graph | `graph_chart`, `tree_chart` | `source`, `target`, `metric` | — |
| Text | `handlebars`, markdown layout component | template | text object |
| Filters (not charts) | `filter_select`, `filter_range`, `filter_time`, `filter_timegrain`, `filter_timecolumn` | used inside `native_filter_configuration` | quick filters |

Removed before 7.0 (do not use): `line`, `bar`, `dist_bar`, `area`, `histogram`, `heatmap`, `bubble`,
`sankey`, `sunburst`, `treemap`, `pivot_table`, `event_flow`, `paired_ttest`, `rose`, `partition`,
`time_pivot`, `big_number_period_over_period` (→ `pop_kpi`).

## Full templates (copy, then edit)

### `echarts_timeseries_bar` — stacked bar by month, series = site
```json
{"datasource":"12__table","viz_type":"echarts_timeseries_bar",
 "x_axis":"enrolled_date","time_grain_sqla":"P1M",
 "metrics":["n_subjects"],"groupby":["site"],
 "adhoc_filters":[{"expressionType":"SIMPLE","subject":"enrolled_date","operator":"TEMPORAL_RANGE","operatorId":"TEMPORAL_RANGE","comparator":"No filter","clause":"WHERE"}],
 "order_desc":true,"row_limit":10000,"truncate_metric":true,"show_empty_columns":true,
 "comparison_type":"values","annotation_layers":[],"forecastPeriods":10,"forecastInterval":0.8,
 "orientation":"vertical","stack":"Stack","show_value":false,"only_total":true,
 "x_axis_title":"Month","x_axis_title_margin":15,"y_axis_title":"Subjects","y_axis_title_margin":15,"y_axis_title_position":"Left",
 "sort_series_type":"sum","color_scheme":"supersetColors","label_colors":{"Seoul":"#4E79A7"},
 "show_legend":true,"legendType":"scroll","legendOrientation":"top",
 "x_axis_time_format":"smart_date","xAxisLabelRotation":0,"y_axis_format":",d",
 "truncateXAxis":true,"y_axis_bounds":[null,null],"rich_tooltip":true,"tooltipTimeFormat":"smart_date",
 "extra_form_data":{}}
```
Horizontal bars sorted by value: `"orientation":"horizontal"`, non-temporal `x_axis` (no
`time_grain_sqla`), `"x_axis_sort":"<metric label>"`, `"x_axis_sort_asc":false`.
Categorical x with many series: `"xAxisLabelRotation":45`.
Value labels on each segment: `"show_value":true,"only_total":false`.

### `mixed_timeseries` — bars (A) + line on secondary axis (B)
```json
{"datasource":"12__table","viz_type":"mixed_timeseries","x_axis":"enrolled_date","time_grain_sqla":"P1M",
 "metrics":["n_subjects"],"groupby":[],"seriesType":"bar","stack":null,"yAxisIndex":0,
 "metrics_b":[{"expressionType":"SQL","sqlExpression":"SUM(n) OVER ()","label":"Cumulative"}],"groupby_b":[],
 "seriesTypeB":"line","yAxisIndexB":1,"markerEnabledB":true,
 "adhoc_filters":[],"adhoc_filters_b":[],"row_limit":10000,"row_limit_b":10000,
 "color_scheme":"supersetColors","show_legend":true,"legendOrientation":"top",
 "y_axis_format":"SMART_NUMBER","y_axis_format_secondary":"SMART_NUMBER","extra_form_data":{}}
```
Cumulative lines: easier via a virtual dataset with a window function than via Superset post-processing.

### `pie`
```json
{"datasource":"12__table","viz_type":"pie","groupby":["status"],"metric":"n_subjects","adhoc_filters":[],
 "row_limit":100,"sort_by_metric":true,"color_scheme":"supersetColors","show_labels_threshold":5,
 "show_legend":true,"legendType":"scroll","legendOrientation":"right","label_type":"key_value_percent",
 "number_format":"SMART_NUMBER","date_format":"smart_date","show_labels":true,"labels_outside":true,
 "label_line":true,"show_total":true,"outerRadius":70,"donut":true,"innerRadius":40,"extra_form_data":{}}
```

### `big_number_total`
```json
{"datasource":"12__table","viz_type":"big_number_total","metric":"n_subjects",
 "adhoc_filters":[{"expressionType":"SIMPLE","subject":"status","operator":"IN","operatorId":"IN","comparator":["Enrolled","Completed"],"clause":"WHERE"}],
 "header_font_size":0.4,"subheader_font_size":0.15,"subheader":"enrolled","y_axis_format":",d","time_format":"smart_date","extra_form_data":{}}
```

### `table` (aggregate)
```json
{"datasource":"12__table","viz_type":"table","query_mode":"aggregate","groupby":["site","country"],
 "metrics":["n_subjects",{"expressionType":"SIMPLE","column":{"column_name":"age"},"aggregate":"AVG","label":"Mean age"}],
 "all_columns":[],"percent_metrics":[],"adhoc_filters":[],"order_by_cols":[],"row_limit":1000,
 "server_page_length":10,"order_desc":true,"timeseries_limit_metric":"n_subjects",
 "table_timestamp_format":"smart_date","show_cell_bars":true,"color_pn":true,
 "column_config":{"Mean age":{"d3NumberFormat":".1f"}},
 "conditional_formatting":[{"column":"n_subjects","operator":">","targetValue":50,"colorScheme":"#ACE1C4"}],
 "extra_form_data":{}}
```
Raw rows instead: `"query_mode":"raw","all_columns":["subject_id","site"],"order_by_cols":["[\"site\", true]"]`.

### `pivot_table_v2`
```json
{"datasource":"12__table","viz_type":"pivot_table_v2","groupbyRows":["site"],"groupbyColumns":["arm"],
 "metrics":["n_subjects"],"metricsLayout":"COLUMNS","adhoc_filters":[],"row_limit":10000,"series_limit":0,
 "order_desc":true,"rowTotals":true,"colTotals":true,"transposePivot":false,"combineMetric":false,
 "valueFormat":",d","date_format":"smart_date","rowOrder":"key_a_to_z","colOrder":"key_a_to_z",
 "conditional_formatting":[],"extra_form_data":{}}
```

### `heatmap_v2`
```json
{"datasource":"13__table","viz_type":"heatmap_v2","x_axis":"site","groupby":"grade","metric":"ae_count",
 "adhoc_filters":[],"row_limit":10000,"sort_x_axis":"alpha_asc","sort_y_axis":"alpha_asc",
 "normalize_across":"heatmap","legend_type":"continuous","linear_color_scheme":"blue_white_yellow",
 "xscale_interval":-1,"yscale_interval":-1,"left_margin":"auto","bottom_margin":"auto","value_bounds":[null,null],
 "y_axis_format":"SMART_NUMBER","x_axis_time_format":"smart_date","show_legend":true,"show_percentage":false,
 "show_values":true,"normalized":false,"extra_form_data":{}}
```
`linear_color_scheme` ids: `blue_white_yellow`, `fire`, `white_black`, `black_white`, `dark_blue`,
`pink_grey`, `greens`, `purples`, `oranges`, `red_yellow_blue`, `brown_white_green`, `purple_white_green`,
`schemeBlues`, `schemeGreens`, `schemeReds`, `schemeOranges`, `schemePurples`, `schemeGreys`,
`schemeViridis`, `schemeInferno`, `schemeMagma`, `schemePlasma`, `schemeRdYlBu`, `schemeRdBu`,
`schemePRGn`, `schemeBrBG`, `schemeYlOrRd`, `schemeYlGnBu`, `superset_seq_1`, `superset_seq_2`.

### `box_plot`
```json
{"datasource":"12__table","viz_type":"box_plot","columns":["arm"],"groupby":[],"metrics":[{"expressionType":"SIMPLE","column":{"column_name":"age"},"aggregate":"AVG","label":"age"}],
 "adhoc_filters":[],"whiskerOptions":"Tukey","x_ticks_layout":"auto","number_format":"SMART_NUMBER","date_format":"smart_date","color_scheme":"supersetColors","extra_form_data":{}}
```
Box plot needs the **raw** values: put the measure in `metrics` with an aggregate that keeps one row
per entity (e.g. a virtual dataset with one row per subject, metric `AVG(age)` grouped by subject
via `groupby:["subject_id"]`).

### `bubble_v2`
```json
{"datasource":"12__table","viz_type":"bubble_v2","entity":"site","series":"country",
 "x":{"expressionType":"SIMPLE","column":{"column_name":"age"},"aggregate":"AVG","label":"Mean age"},
 "y":"n_subjects","size":{"expressionType":"SIMPLE","column":{"column_name":"subject_id"},"aggregate":"COUNT","label":"n"},
 "adhoc_filters":[],"row_limit":10000,"color_scheme":"supersetColors","show_legend":true,"legendOrientation":"top",
 "max_bubble_size":"25","x_axis_format":"SMART_NUMBER","y_axis_format":"SMART_NUMBER","tooltipSizeFormat":"SMART_NUMBER","extra_form_data":{}}
```

### `funnel`, `gauge_chart`, `treemap_v2`, `sunburst_v2`, `histogram_v2`, `waterfall`, `radar`
See the `TEMPLATES` dict in `scripts/chart_builder.py`; each is a copy of the Explore defaults with
the data slots (`groupby`/`columns`/`metric(s)`/`x_axis`) filled by the spec.

## Sorting with breakdowns (stacked bars sorted by total)

`x_axis_sort` / `x_axis_sort_asc` (sort categories by a metric) only work when `groupby` is empty —
the frontend's `sortOperator` skips multi-series charts, so stacked/dodged bars keep the pivot's
alphabetical category order (verified on 6.1; `x_axis_sort_series` only orders the *series*).
Tableau sorts stacked bars by total all the time, so when that matters:

* **Pre-rank in a virtual dataset** and sort by the x column (index sort):
  ```sql
  SELECT soc, grade_group, ae_id, onset_date,
         DENSE_RANK() OVER (ORDER BY COUNT(*) OVER (PARTITION BY soc) DESC) AS soc_rank,
         printf('%02d %s', DENSE_RANK() OVER (ORDER BY COUNT(*) OVER (PARTITION BY soc) DESC), soc) AS soc_ranked
  FROM adverse_events
  ```
  then `x: soc_ranked`, `extra: {x_axis_sort: soc_ranked, x_axis_sort_asc: true}` (labels carry the
  rank prefix; use `verbose_name` or strip it in the SQL label if the DB supports it).
* Or drop the breakdown (single series sorts fine) and show the breakdown in a second chart /
  `pivot_table_v2` (which does sort by any metric).

## Reference lines, targets, annotations (Tableau reference lines)

ECharts XY charts accept `annotation_layers`:
```json
"annotation_layers":[{"name":"Target","annotationType":"FORMULA","sourceType":"","value":"y = 120",
  "style":"dashed","color":"#E04355","width":2,"opacity":"","show":true,"showLabel":true,"overrides":{"time_range":null}}]
```
(`value` is a formula in `x`, e.g. `y = 120` for a horizontal target line.) Event/interval
annotations need an annotation layer created via `/api/v1/annotation_layer/`.

## Conditional formatting (Tableau colour-by-measure in tables)

`table` / `pivot_table_v2`: `"conditional_formatting":[{"column":"<metric label>","operator":">|<|≥|≤|=|≠|between|
Not between","targetValue":n,"targetValueLeft":a,"targetValueRight":b,"colorScheme":"#RRGGBB"}]`.

## Time comparison (Tableau "vs previous period")

XY charts: `"time_compare":["1 year ago"],"comparison_type":"values|difference|percentage|ratio"`.
`pop_kpi`: `"time_comparison":"y"` (`r`=inherit range, `y`,`m`,`w`,`c` custom).

## Per-chart colour pinning

`"label_colors":{"Seoul":"#4E79A7","Busan":"#F28E2B"}` inside `params` beats `color_scheme`
for those labels. Dashboard-level `json_metadata.label_colors` + `shared_label_colors` keeps the same
label → colour across all charts on the dashboard. See `colors.md`.
