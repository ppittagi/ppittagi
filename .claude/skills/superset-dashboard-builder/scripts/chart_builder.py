#!/usr/bin/env python3
"""
Build Superset chart `params` (form_data) and `query_context` from a compact chart spec.

Spec shape (YAML/JSON, see assets/spec.example.yaml):

  viz_type: echarts_timeseries_bar
  x: visit_date            # x axis column (time or category)
  time_grain: P1M          # PT1H P1D P1W P1M P3M P1Y (only for temporal x)
  metrics:                 # metric shorthand forms, mix freely
    - count                #   saved metric name
    - {col: subject_id, agg: COUNT_DISTINCT, label: Subjects}
    - {sql: "SUM(ae_cnt)*1.0/COUNT(DISTINCT subject_id)", label: AE per subject}
  groupby: [site]          # series / breakdown dimensions; {sql: "CASE ... END", label: Band} = calculated dimension
  b:                       # mixed_timeseries only: second query
    metrics: [{col: profit, agg: SUM, label: Profit}]
    kind: line             # bar|line|smooth|scatter
    secondary_axis: true
    title: Profit          # secondary axis title / format / markers
    format: "$.2s"
    markers: true
  filters:                 # chart-level filters (dashboard native filters come on top)
    - {col: status, op: IN, val: [Enrolled, Completed]}
    - "ae_grade >= 3"      #   free SQL WHERE snippet
  time_range: "Last year"  # applied to `x` when temporal (default: No filter)
  orientation: horizontal  # bar only
  stack: true              # true | false | Stream | Expand
  show_value: true
  legend: {show: true, position: top}   # top|bottom|left|right
  x_title: "Visit month"   # title of the category/time axis (vertical when orientation=horizontal)
  y_title: "Subjects"      # title of the metric axis
  y_format: ",d"           # d3 number format (SMART_NUMBER default)
  sort: {metric: Subjects, desc: true}   # category sort; desc = largest first (on top for horizontal bars)
                                          # or {x: asc|desc} to sort by the category label; or series_by: name|sum
  row_limit: 1000
  color_scheme: supersetColors
  label_colors: {Seoul: "#4E79A7", Busan: "#F28E2B"}   # exact series colors
  annotations:             # XY charts: reference lines and shaded periods
    - {type: formula, name: Median, value: "y = 0", style: dashed, color: "#999999"}
    - {type: interval, name: Baseline, layer: "Baseline period", color: "#E8DEC2", opacity: medium,
       intervals: [{start: "1961-01-01", end: "1990-12-31", label: "Relative to 1961-1990 avg"}]}
  extra: {}                # raw form_data keys merged last (full control)

Python API:
  from chart_builder import build_params, build_query_context
  params = build_params(spec, dataset_id=12)
  qc     = build_query_context(params, dataset_id=12)
"""
from __future__ import annotations

import copy
import json
import re
import sys
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Base form_data templates per viz_type (Superset 5.x/6.x/7.x ECharts stack)
# --------------------------------------------------------------------------- #
_XY_COMMON: dict[str, Any] = {
    "x_axis_sort_asc": True,
    "adhoc_filters": [],
    "order_desc": True,
    "row_limit": 10000,
    "truncate_metric": True,
    "show_empty_columns": True,
    "comparison_type": "values",
    "annotation_layers": [],
    "forecastPeriods": 10,
    "forecastInterval": 0.8,
    "x_axis_title_margin": 30,
    "y_axis_title_margin": 30,
    "y_axis_title_position": "Left",
    "sort_series_type": "name",        # series/stack order: alphabetical like Tableau (7.0 key)
    "sort_series_ascending": True,
    "color_scheme": "supersetColors",
    "show_value": False,
    "only_total": True,
    "show_legend": True,
    "legendType": "scroll",
    "legendOrientation": "top",
    "x_axis_time_format": "smart_date",
    "xAxisLabelRotation": 0,
    "y_axis_format": "SMART_NUMBER",
    "truncateXAxis": True,
    "y_axis_bounds": [None, None],
    "rich_tooltip": True,
    "showTooltipTotal": False,
    "tooltipTimeFormat": "smart_date",
    "extra_form_data": {},
}

TEMPLATES: dict[str, dict[str, Any]] = {
    # ---- XY / time-series family ------------------------------------------
    "echarts_timeseries_bar": {**_XY_COMMON, "orientation": "vertical", "stack": None},
    "echarts_timeseries_line": {**_XY_COMMON, "markerEnabled": False, "markerSize": 6, "opacity": 0.2,
                                "zoomable": False, "minorTicks": False, "area": False, "stack": None},
    "echarts_timeseries_smooth": {**_XY_COMMON, "markerEnabled": False, "markerSize": 6, "opacity": 0.2,
                                  "zoomable": False, "stack": None},
    "echarts_timeseries_step": {**_XY_COMMON, "seriesType": "start", "markerEnabled": False,
                                "opacity": 0.2, "zoomable": False, "stack": None},
    "echarts_timeseries_scatter": {**_XY_COMMON, "markerSize": 6, "zoomable": False},
    "echarts_area": {**_XY_COMMON, "seriesType": "line", "opacity": 0.2, "markerEnabled": False,
                     "stack": "Stack", "show_extra_controls": True},
    "mixed_timeseries": {  # two queries: A (bar) + B (line) -> fields suffixed _b
        **_XY_COMMON, "seriesType": "bar", "seriesTypeB": "line", "yAxisIndex": 0, "yAxisIndexB": 1,
        "adhoc_filters_b": [], "metrics_b": [], "groupby_b": [], "order_desc_b": True, "row_limit_b": 10000,
        "truncate_metric_b": True, "opacity": 0.2, "opacityB": 0.2, "markerEnabled": False,
        "markerEnabledB": False, "markerSize": 6, "markerSizeB": 6, "y_axis_format_secondary": "SMART_NUMBER",
        "truncateYAxis": True, "stack": None, "stackB": None, "show_value": False, "show_valueB": False,
    },
    # ---- part-to-whole ------------------------------------------------------
    "pie": {
        "adhoc_filters": [], "row_limit": 100, "sort_by_metric": True, "color_scheme": "supersetColors",
        "show_labels_threshold": 5, "show_legend": True, "legendType": "scroll", "legendOrientation": "top",
        "label_type": "key_value_percent", "number_format": "SMART_NUMBER", "date_format": "smart_date",
        "show_labels": True, "labels_outside": True, "label_line": True, "show_total": False,
        "outerRadius": 70, "donut": False, "innerRadius": 30, "extra_form_data": {},
    },
    "funnel": {
        "adhoc_filters": [], "row_limit": 10, "sort_by_metric": True, "color_scheme": "supersetColors",
        "show_legend": False, "legendOrientation": "top", "label_type": "key_value_percent",
        "tooltip_label_type": "key_value_percent", "number_format": "SMART_NUMBER", "show_labels": True,
        "show_tooltip_labels": True, "percent_calculation_type": "first_step", "extra_form_data": {},
    },
    "treemap_v2": {
        "adhoc_filters": [], "row_limit": 100, "sort_by_metric": True, "color_scheme": "supersetColors",
        "show_labels": True, "show_upper_labels": True, "label_type": "key_value", "number_format": "SMART_NUMBER",
        "date_format": "smart_date", "extra_form_data": {},
    },
    "sunburst_v2": {
        "adhoc_filters": [], "row_limit": 10000, "sort_by_metric": True, "color_scheme": "supersetColors",
        "linear_color_scheme": "schemeBlues", "show_labels": True, "show_labels_threshold": 5,
        "show_total": False, "label_type": "key", "number_format": "SMART_NUMBER", "extra_form_data": {},
    },
    # ---- single value / KPI -------------------------------------------------
    "big_number_total": {
        "adhoc_filters": [], "header_font_size": 0.4, "subheader_font_size": 0.15,
        "y_axis_format": "SMART_NUMBER", "time_format": "smart_date", "subheader": "", "extra_form_data": {},
    },
    "big_number": {  # KPI with trend line; needs x (temporal) + time_grain
        "adhoc_filters": [], "compare_lag": "", "compare_suffix": "", "show_timestamp": False,
        "show_trend_line": True, "start_y_axis_at_zero": True, "header_font_size": 0.4,
        "subheader_font_size": 0.15, "y_axis_format": "SMART_NUMBER", "time_format": "smart_date",
        "rolling_type": "None", "aggregation": "LAST_VALUE", "extra_form_data": {},
    },
    "gauge_chart": {
        "adhoc_filters": [], "row_limit": 10, "sort_by_metric": True, "color_scheme": "supersetColors",
        "start_angle": 225, "end_angle": -45, "font_size": 14, "value_formatter": "{value}",
        "show_pointer": True, "animation": True, "show_axis_tick": False, "show_split_line": False,
        "split_number": 10, "show_progress": True, "overlap": True, "round_cap": False, "extra_form_data": {},
    },
    # ---- tables -------------------------------------------------------------
    "table": {
        "query_mode": "aggregate", "all_columns": [], "percent_metrics": [], "adhoc_filters": [],
        "order_by_cols": [], "row_limit": 1000, "server_page_length": 10, "order_desc": True,
        "table_timestamp_format": "smart_date", "show_cell_bars": True, "color_pn": True,
        "allow_render_html": True, "column_config": {}, "conditional_formatting": [], "extra_form_data": {},
    },
    "pivot_table_v2": {
        "groupbyColumns": [], "groupbyRows": [], "adhoc_filters": [], "metricsLayout": "COLUMNS",
        "row_limit": 10000, "series_limit": 0, "order_desc": True, "rowTotals": False, "colTotals": False,
        "transposePivot": False, "combineMetric": False, "valueFormat": "SMART_NUMBER",
        "date_format": "smart_date", "rowOrder": "key_a_to_z", "colOrder": "key_a_to_z",
        "conditional_formatting": [], "extra_form_data": {},
    },
    # ---- distributions ------------------------------------------------------
    "heatmap_v2": {
        "adhoc_filters": [], "row_limit": 10000, "sort_x_axis": "alpha_asc", "sort_y_axis": "alpha_desc",  # ECharts y axis is bottom-up: desc = first row on top
        "normalize_across": "heatmap", "legend_type": "continuous", "linear_color_scheme": "blue_white_yellow",
        "xscale_interval": -1, "yscale_interval": -1, "left_margin": "auto", "bottom_margin": "auto",
        "value_bounds": [None, None], "y_axis_format": "SMART_NUMBER", "x_axis_time_format": "smart_date",
        "show_legend": True, "show_percentage": True, "show_values": True, "normalized": False,
        "extra_form_data": {},
    },
    "box_plot": {  # `groupby` = boxes (x axis), `columns` = distribute across (per-row id)
        "adhoc_filters": [], "whiskerOptions": "Tukey", "x_ticks_layout": "auto",
        "number_format": "SMART_NUMBER", "date_format": "smart_date", "color_scheme": "supersetColors",
        # 7.0 always draws a "boxplot / outlier" legend; keep it below the axis title so labels stay readable
        "legendOrientation": "bottom", "x_axis_title_margin": 30, "extra_form_data": {},
    },
    "histogram_v2": {
        "adhoc_filters": [], "row_limit": 10000, "bins": 10, "normalize": False, "cumulative": False,
        "color_scheme": "supersetColors", "show_legend": True, "show_value": False, "x_axis_title": "",
        "y_axis_title": "", "extra_form_data": {},
    },
    "bubble_v2": {
        "adhoc_filters": [], "row_limit": 10000, "color_scheme": "supersetColors", "show_legend": True,
        "legendOrientation": "top", "max_bubble_size": "25", "x_axis_format": "SMART_NUMBER",
        "y_axis_format": "SMART_NUMBER", "tooltipSizeFormat": "SMART_NUMBER", "extra_form_data": {},
    },
    "waterfall": {
        "adhoc_filters": [], "row_limit": 10000, "show_value": True, "show_legend": True,
        "increase_color": {"r": 90, "g": 193, "b": 137, "a": 1},
        "decrease_color": {"r": 224, "g": 67, "b": 85, "a": 1},
        "total_color": {"r": 102, "g": 102, "b": 102, "a": 1},
        "y_axis_format": "SMART_NUMBER", "x_axis_time_format": "smart_date", "extra_form_data": {},
    },
    "radar": {
        "adhoc_filters": [], "row_limit": 10, "color_scheme": "supersetColors", "show_legend": True,
        "legendOrientation": "top", "label_type": "value", "number_format": "SMART_NUMBER",
        "is_circle": False, "show_labels": True, "extra_form_data": {},
    },
    "sankey_v2": {
        "adhoc_filters": [], "row_limit": 1000, "sort_by_metric": True, "color_scheme": "supersetColors",
        "extra_form_data": {},
    },
    "word_cloud": {
        "adhoc_filters": [], "row_limit": 100, "size_from": 10, "size_to": 70, "rotation": "square",
        "color_scheme": "supersetColors", "extra_form_data": {},
    },
}

XY_TYPES = {"echarts_timeseries_bar", "echarts_timeseries_line", "echarts_timeseries_smooth",
            "echarts_timeseries_step", "echarts_timeseries_scatter", "echarts_area", "mixed_timeseries"}
SINGLE_METRIC_TYPES = {"pie", "funnel", "treemap_v2", "sunburst_v2", "big_number_total", "big_number",
                       "gauge_chart", "heatmap_v2", "word_cloud", "sankey_v2"}
TIME_GRAINS = {"PT1S", "PT1M", "PT5M", "PT10M", "PT15M", "PT30M", "PT1H", "P1D", "P1W", "P1M", "P3M", "P1Y",
               "1969-12-28T00:00:00Z/P1W", "1969-12-29T00:00:00Z/P1W"}


# --------------------------------------------------------------------------- #
# metric / filter normalisation
# --------------------------------------------------------------------------- #
def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower() or "metric"


def metric_obj(m: Any) -> Any:
    """Expand shorthand metric into Superset's adhoc metric structure."""
    if isinstance(m, str):
        return m  # saved metric name (e.g. "count")
    if not isinstance(m, dict):
        raise ValueError(f"bad metric: {m!r}")
    if "expressionType" in m:
        return m  # already an adhoc metric
    if "sql" in m:
        label = m.get("label") or m["sql"]
        return {"expressionType": "SQL", "sqlExpression": m["sql"], "label": label,
                "hasCustomLabel": bool(m.get("label")), "optionName": f"metric_{_slug(label)}"}
    col = m.get("col") or m.get("column")
    agg = (m.get("agg") or m.get("aggregate") or "SUM").upper()
    label = m.get("label") or f"{agg}({col})"
    return {"expressionType": "SIMPLE", "column": {"column_name": col}, "aggregate": agg,
            "label": label, "hasCustomLabel": bool(m.get("label")), "optionName": f"metric_{_slug(label)}"}


def metric_label(m: Any) -> str:
    return m if isinstance(m, str) else m.get("label", "")


def column_obj(c: Any) -> Any:
    """Dimension shorthand: a column name, or {sql: "<expression>", label: "..."} for a calculated dimension."""
    if isinstance(c, dict) and "sql" in c:
        label = c.get("label") or c["sql"]
        return {"expressionType": "SQL", "sqlExpression": c["sql"], "label": label, "hasCustomLabel": True}
    return c


def filter_obj(f: Any) -> dict:
    """Expand shorthand filter into an adhoc filter."""
    if isinstance(f, str):
        return {"expressionType": "SQL", "sqlExpression": f, "clause": "WHERE", "filterOptionName": f"filter_{_slug(f)}"}
    if "expressionType" in f:
        return f
    if "sql" in f:
        return {"expressionType": "SQL", "sqlExpression": f["sql"], "clause": f.get("clause", "WHERE"),
                "filterOptionName": f"filter_{_slug(f['sql'])}"}
    col, op, val = f.get("col") or f.get("column"), str(f.get("op", "==")).upper(), f.get("val", f.get("value"))
    op_map = {"=": "==", "EQ": "==", "!=": "!=", "NE": "!=", "IN": "IN", "NOT IN": "NOT IN", ">": ">", "<": "<",
              ">=": ">=", "<=": "<=", "LIKE": "LIKE", "ILIKE": "ILIKE", "IS NULL": "IS NULL",
              "IS NOT NULL": "IS NOT NULL", "TEMPORAL_RANGE": "TEMPORAL_RANGE"}
    op = op_map.get(op, op)
    op_id = {"==": "EQUALS", "!=": "NOT_EQUALS", "IN": "IN", "NOT IN": "NOT_IN", ">": "GREATER_THAN",
             "<": "LESS_THAN", ">=": "GREATER_THAN_OR_EQUAL", "<=": "LESS_THAN_OR_EQUAL", "LIKE": "LIKE",
             "ILIKE": "ILIKE", "IS NULL": "IS_NULL", "IS NOT NULL": "IS_NOT_NULL",
             "TEMPORAL_RANGE": "TEMPORAL_RANGE"}.get(op, op)
    return {"expressionType": "SIMPLE", "subject": col, "operator": op, "operatorId": op_id,
            "comparator": val, "clause": "WHERE", "filterOptionName": f"filter_{_slug(col)}_{_slug(op)}"}


def temporal_filter(col: str, time_range: str = "No filter") -> dict:
    """The adhoc filter that lets dashboard *time* native filters reach the chart."""
    return {"expressionType": "SIMPLE", "subject": col, "operator": "TEMPORAL_RANGE",
            "operatorId": "TEMPORAL_RANGE", "comparator": time_range, "clause": "WHERE",
            "filterOptionName": f"filter_time_{_slug(col)}"}


def annotation_layer(a: dict) -> dict:
    """
    Spec -> Superset annotation layer entry.
      {type: formula, name: Median, value: "y = 0", style: dashed, color: "#999999", width: 1}
      {type: interval|event, name: Baseline, layer: "<annotation layer name>", color: "#E8DEC2",
       opacity: low|medium|high, intervals: [{start: 1961-01-01, end: 1990-12-31, label: "..."}]}
    `layer_id` is filled in by build_dashboard (it creates the layer + annotations through the API).
    """
    kind = str(a.get("type", "formula")).upper()
    entry: dict[str, Any] = {
        "name": a.get("name", kind.title()), "annotationType": kind,
        "sourceType": "" if kind == "FORMULA" else "NATIVE",
        "value": a.get("value", "") if kind == "FORMULA" else a.get("layer_id"),
        "overrides": {"time_range": None}, "show": True, "showLabel": bool(a.get("show_label", False)),
        "titleColumn": "", "descriptionColumns": [], "timeColumn": "", "intervalEndColumn": "",
        "color": a.get("color"), "opacity": {"low": "opacityLow", "medium": "opacityMedium", "high": "opacityHigh"}.get(
            str(a.get("opacity", "")).lower(), ""),
        "style": a.get("style", "solid"), "width": a.get("width", 1), "showMarkers": False, "hideLine": False,
    }
    return entry


# --------------------------------------------------------------------------- #
# params builder
# --------------------------------------------------------------------------- #
def build_params(spec: dict, dataset_id: Optional[int] = None) -> dict:
    viz = spec.get("viz_type")
    if viz not in TEMPLATES:
        if spec.get("params"):
            p = copy.deepcopy(spec["params"])
            p["viz_type"] = viz
            if dataset_id:
                p["datasource"] = f"{dataset_id}__table"
            return p
        raise ValueError(f"unknown viz_type {viz!r}; supported: {sorted(TEMPLATES)} "
                         f"(or give full `params` for other types)")

    p: dict[str, Any] = copy.deepcopy(TEMPLATES[viz])
    p["viz_type"] = viz
    if dataset_id:
        p["datasource"] = f"{dataset_id}__table"

    metrics = [metric_obj(m) for m in (spec.get("metrics") or ([spec["metric"]] if spec.get("metric") else []))]
    groupby = [column_obj(g) for g in (spec.get("groupby") or [])]
    x = column_obj(spec.get("x")) if spec.get("x") else None
    temporal_col = spec.get("time_column") or (x if spec.get("time_grain") or spec.get("x_is_time") else None)

    # ---- per-family slots ---------------------------------------------------
    if viz in XY_TYPES:
        if x:
            p["x_axis"] = x
        if spec.get("time_grain"):
            p["time_grain_sqla"] = spec["time_grain"]
        p["metrics"] = metrics
        # "colour every bar by its own category" = groupby the x column too. Superset rejects duplicate
        # labels, so the breakdown becomes an adhoc SQL column with a different label.
        p["groupby"] = [{"expressionType": "SQL", "sqlExpression": g, "label": f"{g} (series)",
                         "hasCustomLabel": True} if isinstance(g, str) and g == x else g for g in groupby]
        if x and x in groupby and "stack" not in spec and viz != "mixed_timeseries":
            p["stack"] = "Stack"   # one series per category: stacking keeps each bar full width (not dodged)
        if viz == "mixed_timeseries":
            b = spec.get("b", {})
            p["metrics_b"] = [metric_obj(m) for m in b.get("metrics", [])]
            p["groupby_b"] = list(b.get("groupby", []))
            p["adhoc_filters_b"] = [filter_obj(f) for f in b.get("filters", [])]
            if b.get("kind"):
                p["seriesTypeB"] = b["kind"]
            if "secondary_axis" in b:
                p["yAxisIndexB"] = 1 if b["secondary_axis"] else 0
            if b.get("title"):
                p["yAxisTitleSecondary"] = b["title"]          # camelCase in the plugin
            if b.get("format"):
                p["y_axis_format_secondary"] = b["format"]
            if "markers" in b:
                p["markerEnabledB"] = bool(b["markers"])
            if "stack" in b:
                p["stackB"] = "Stack" if b["stack"] is True else (None if b["stack"] in (False, None) else b["stack"])
            if "show_value" in b:
                p["show_valueB"] = bool(b["show_value"])
        if spec.get("orientation"):
            p["orientation"] = spec["orientation"]
        if "stack" in spec:
            s = spec["stack"]
            p["stack"] = "Stack" if s is True else (None if s in (False, None) else s)
        if spec.get("kind") and viz != "mixed_timeseries":
            p["seriesType"] = spec["kind"]  # line|smooth|start|middle|end|bar|scatter
        if spec.get("area"):
            p["area"] = True
    elif viz in ("pie", "funnel", "treemap_v2", "sunburst_v2", "radar", "word_cloud"):
        if viz == "sunburst_v2":
            p["columns"] = groupby
        elif viz == "word_cloud":
            p["series"] = groupby[0] if groupby else None
        else:
            p["groupby"] = groupby
        if viz == "radar":
            p["metrics"] = metrics
        else:
            p["metric"] = metrics[0] if metrics else None
        if viz == "sunburst_v2" and len(metrics) > 1:
            p["secondary_metric"] = metrics[1]
        if viz == "pie" and spec.get("donut"):
            p["donut"] = True
    elif viz in ("big_number_total", "big_number"):
        p["metric"] = metrics[0] if metrics else None
        if viz == "big_number":
            p["x_axis"] = x
            p["time_grain_sqla"] = spec.get("time_grain", "P1D")
        if spec.get("subheader"):
            p["subheader"] = spec["subheader"]
    elif viz == "gauge_chart":
        p["metric"] = metrics[0] if metrics else None
        p["groupby"] = groupby
        for k in ("min_val", "max_val", "intervals", "interval_color_indices", "value_formatter"):
            if k in spec:
                p[k] = spec[k]
    elif viz == "table":
        if spec.get("query_mode") == "raw" or (not metrics and spec.get("columns")):
            p["query_mode"] = "raw"
            p["all_columns"] = list(spec.get("columns") or groupby)
            p["order_by_cols"] = [json.dumps([c, not spec.get("sort", {}).get("desc", True)])
                                  for c in ([spec["sort"]["column"]] if spec.get("sort", {}).get("column") else [])]
        else:
            p["query_mode"] = "aggregate"
            p["groupby"] = groupby
            p["metrics"] = metrics
            p["percent_metrics"] = [metric_obj(m) for m in spec.get("percent_metrics", [])]
        if spec.get("time_grain") and x:
            p["groupby"] = [x] + [g for g in groupby if g != x]
            p["time_grain_sqla"] = spec["time_grain"]
        if spec.get("page_length"):
            p["server_page_length"] = spec["page_length"]
            p["server_pagination"] = True
    elif viz == "pivot_table_v2":
        p["groupbyRows"] = list(spec.get("rows") or groupby)
        p["groupbyColumns"] = list(spec.get("columns") or ([x] if x else []))
        p["metrics"] = metrics
        for k_spec, k_fd in (("row_totals", "rowTotals"), ("col_totals", "colTotals"),
                             ("transpose", "transposePivot"), ("metrics_layout", "metricsLayout")):
            if k_spec in spec:
                p[k_fd] = spec[k_spec]
        if spec.get("time_grain"):
            p["time_grain_sqla"] = spec["time_grain"]
    elif viz == "heatmap_v2":
        p["x_axis"] = x
        p["groupby"] = groupby[0] if groupby else None
        p["metric"] = metrics[0] if metrics else None
        if spec.get("time_grain"):
            p["time_grain_sqla"] = spec["time_grain"]
        if spec.get("linear_color_scheme"):
            p["linear_color_scheme"] = spec["linear_color_scheme"]
    elif viz == "box_plot":
        # Superset box plot: `groupby` = one box per value (the x axis), `columns` = "distribute across"
        # (the dimension whose per-value aggregates form the distribution, e.g. an order/sale id).
        p["groupby"] = [x] if x else groupby
        p["columns"] = list(spec.get("distribute") or spec.get("columns") or [])
        p["metrics"] = metrics
        if not p["columns"]:
            print(f"WARNING: box_plot needs `distribute: [<row id column>]` (Superset 'Distribute across'); "
                  f"without it every box collapses to a single value.", file=sys.stderr)
        if spec.get("time_grain"):
            p["time_grain_sqla"] = spec["time_grain"]
    elif viz == "histogram_v2":
        p["column"] = spec.get("column") or x
        p["groupby"] = groupby
        if spec.get("bins"):
            p["bins"] = spec["bins"]
    elif viz == "bubble_v2":
        p["entity"] = spec.get("entity") or (groupby[0] if groupby else None)
        p["series"] = spec.get("series") or (groupby[1] if len(groupby) > 1 else None)
        p["x"] = metric_obj(spec["x_metric"]) if spec.get("x_metric") else (metrics[0] if metrics else None)
        p["y"] = metric_obj(spec["y_metric"]) if spec.get("y_metric") else (metrics[1] if len(metrics) > 1 else None)
        p["size"] = metric_obj(spec["size_metric"]) if spec.get("size_metric") else (metrics[2] if len(metrics) > 2 else None)
    elif viz == "waterfall":
        p["x_axis"] = x
        p["groupby"] = groupby[0] if groupby else None
        p["metric"] = metrics[0] if metrics else None
        if spec.get("time_grain"):
            p["time_grain_sqla"] = spec["time_grain"]
    elif viz == "sankey_v2":
        p["source"] = spec.get("source") or (groupby[0] if groupby else None)
        p["target"] = spec.get("target") or (groupby[1] if len(groupby) > 1 else None)
        p["metric"] = metrics[0] if metrics else None

    # ---- filters -------------------------------------------------------------
    filters = [filter_obj(f) for f in spec.get("filters", [])]
    if temporal_col and not any(f.get("operator") == "TEMPORAL_RANGE" for f in filters):
        filters.append(temporal_filter(temporal_col, spec.get("time_range", "No filter")))
    p["adhoc_filters"] = filters
    if viz == "mixed_timeseries" and temporal_col and \
            not any(f.get("operator") == "TEMPORAL_RANGE" for f in p.get("adhoc_filters_b", [])):
        # query B has its own filter list; without this the dashboard time filter reaches only query A
        p["adhoc_filters_b"] = list(p.get("adhoc_filters_b", [])) + [temporal_filter(temporal_col, spec.get("time_range", "No filter"))]
    if spec.get("time_range") and not temporal_col:
        p["time_range"] = spec["time_range"]

    # ---- cosmetics -------------------------------------------------------------
    if spec.get("color_scheme"):
        p["color_scheme"] = spec["color_scheme"]
    if spec.get("label_colors"):
        p["label_colors"] = dict(spec["label_colors"])
    if "show_value" in spec:
        p["show_value"] = bool(spec["show_value"])
        if viz in XY_TYPES:
            p["only_total"] = bool(spec.get("only_total", True))
    legend = spec.get("legend")
    if isinstance(legend, dict):
        p["show_legend"] = legend.get("show", True)
        if legend.get("position"):
            p["legendOrientation"] = legend["position"]
        if legend.get("type"):
            p["legendType"] = legend["type"]
    elif isinstance(legend, bool):
        p["show_legend"] = legend
    if spec.get("x_title"):
        p["x_axis_label" if viz == "bubble_v2" else "x_axis_title"] = spec["x_title"]
    if spec.get("y_title"):
        p["y_axis_label" if viz == "bubble_v2" else "y_axis_title"] = spec["y_title"]
    if spec.get("y_format"):
        for k in ("y_axis_format", "number_format", "valueFormat"):
            if k in p:
                p[k] = spec["y_format"]
    if spec.get("x_format"):
        # XY charts format a temporal x axis with x_axis_time_format; histogram_v2 formats its bin
        # edges with x_axis_format.
        p["x_axis_format" if viz == "histogram_v2" else "x_axis_time_format"] = spec["x_format"]
    if spec.get("row_limit"):
        p["row_limit"] = spec["row_limit"]
    if spec.get("label_rotation") is not None:
        p["xAxisLabelRotation"] = spec["label_rotation"]
    if spec.get("y_bounds"):
        p["y_axis_bounds"] = spec["y_bounds"]
    if spec.get("log_axis"):
        p["logAxis"] = True
    sort = spec.get("sort")
    if isinstance(sort, dict):
        if sort.get("metric") is not None and viz in XY_TYPES | {"pie", "table", "funnel", "treemap_v2"}:
            # XY: timeseries_limit_metric sorts series/categories; others sort by metric
            if viz in XY_TYPES:
                p["timeseries_limit_metric"] = metric_obj(sort["metric"]) if isinstance(sort["metric"], dict) else \
                    next((m for m in metrics if metric_label(m) == sort["metric"]), sort["metric"])
                p["order_desc"] = bool(sort.get("desc", True))
                if groupby:
                    # Superset's sort post-processing only supports single-series charts: with a
                    # breakdown the categories stay in pivot (alphabetical) order and only the
                    # series/legend order can be sorted ("Sort series by").
                    p["sort_series_type"] = sort.get("series_by", "sum")   # name|min|max|sum|avg
                    p["sort_series_ascending"] = not sort.get("desc", True)
                    print(f"WARNING: {viz} with groupby {groupby}: categories cannot be sorted by a metric "
                          f"(Superset limitation). Options: drop groupby, or pre-rank the category in a "
                          f"virtual dataset (see references/chart-params.md 'Sorting with breakdowns').",
                          file=sys.stderr)
                else:
                    p["x_axis_sort"] = metric_label(p["timeseries_limit_metric"])
                    # ECharts draws a horizontal category axis bottom-up, so "largest first" (desc) needs
                    # ascending sort order to put the largest bar on top, as Tableau does.
                    desc = bool(sort.get("desc", True))
                    p["x_axis_sort_asc"] = desc if spec.get("orientation") == "horizontal" else not desc
            else:
                p["order_desc"] = bool(sort.get("desc", True))
        elif sort.get("x") is not None and viz in XY_TYPES:
            p["x_axis_sort"] = x if isinstance(x, str) else x.get("label")
            asc = str(sort["x"]).lower() != "desc"
            # horizontal bars: ECharts draws the first category at the bottom, so flip to keep
            # "asc" = first value on top (reading order), like the metric sort above
            p["x_axis_sort_asc"] = (not asc) if spec.get("orientation") == "horizontal" else asc

    # ---- annotations (XY charts): formula lines, native interval/event layers --------------
    if spec.get("annotations") and viz in XY_TYPES:
        p["annotation_layers"] = [annotation_layer(a) for a in spec["annotations"]]

    # ---- escape hatch ------------------------------------------------------------
    for k, v in (spec.get("extra") or {}).items():
        p[k] = v
    return p


# --------------------------------------------------------------------------- #
# query_context builder (used to validate via POST /api/v1/chart/data and to store on the chart)
# --------------------------------------------------------------------------- #
def _axis_column(col: str, time_grain: Optional[str]) -> dict:
    c: dict[str, Any] = {"columnType": "BASE_AXIS", "sqlExpression": col, "label": col, "expressionType": "SQL"}
    if time_grain:
        c["timeGrain"] = time_grain
    return c


def build_query_context(params: dict, dataset_id: int, row_limit: Optional[int] = None) -> dict:
    viz = params.get("viz_type")
    p = params
    columns: list[Any] = []
    metrics: list[Any] = []
    orderby: list[Any] = []
    extras: dict[str, Any] = {"having": "", "where": ""}
    time_range: Optional[str] = None
    filters: list[dict] = []
    for f in p.get("adhoc_filters", []):
        if f.get("expressionType") == "SQL":
            key = "having" if f.get("clause") == "HAVING" else "where"
            extras[key] = (extras[key] + " AND " if extras[key] else "") + f"({f['sqlExpression']})"
        elif f.get("operator") == "TEMPORAL_RANGE":
            filters.append({"col": f["subject"], "op": "TEMPORAL_RANGE", "val": f.get("comparator") or "No filter"})
            time_range = f.get("comparator") or "No filter"
        else:
            filters.append({"col": f["subject"], "op": f["operator"], "val": f.get("comparator")})
    if p.get("time_range") and not time_range:
        time_range = p["time_range"]

    tg = p.get("time_grain_sqla")
    if viz in XY_TYPES or viz in ("big_number", "heatmap_v2", "waterfall"):
        if p.get("x_axis"):
            columns.append(_axis_column(p["x_axis"], tg))
        gb = p.get("groupby")
        columns += [gb] if isinstance(gb, str) else list(gb or [])
        metrics = list(p.get("metrics") or ([p["metric"]] if p.get("metric") else []))
    elif viz in ("pie", "funnel", "treemap_v2", "radar", "gauge_chart", "box_plot"):
        columns = list(p.get("columns") or []) + list(p.get("groupby") or [])
        metrics = list(p.get("metrics") or ([p["metric"]] if p.get("metric") else []))
    elif viz == "sunburst_v2":
        columns = list(p.get("columns") or [])
        metrics = [m for m in (p.get("metric"), p.get("secondary_metric")) if m]
    elif viz == "big_number_total":
        metrics = [p["metric"]] if p.get("metric") else []
    elif viz == "table":
        if p.get("query_mode") == "raw":
            columns = list(p.get("all_columns") or [])
        else:
            columns = list(p.get("groupby") or [])
            metrics = list(p.get("metrics") or []) + list(p.get("percent_metrics") or [])
    elif viz == "pivot_table_v2":
        columns = list(p.get("groupbyRows") or []) + list(p.get("groupbyColumns") or [])
        metrics = list(p.get("metrics") or [])
    elif viz == "histogram_v2":
        columns = [p.get("column")] + list(p.get("groupby") or [])
    elif viz == "bubble_v2":
        columns = [c for c in (p.get("entity"), p.get("series")) if c]
        metrics = [m for m in (p.get("x"), p.get("y"), p.get("size")) if m]
    elif viz == "sankey_v2":
        columns = [p.get("source"), p.get("target")]
        metrics = [p["metric"]] if p.get("metric") else []
    elif viz == "word_cloud":
        columns = [p.get("series")]
        metrics = [p["metric"]] if p.get("metric") else []
    else:
        columns = list(p.get("groupby") or [])
        metrics = list(p.get("metrics") or ([p["metric"]] if p.get("metric") else []))

    if metrics:
        first = metrics[0]
        orderby = [[first, not p.get("order_desc", True)]]

    query: dict[str, Any] = {
        "filters": filters,
        "extras": extras,
        "applied_time_extras": {},
        "columns": [c for c in columns if c],
        "metrics": metrics,
        "orderby": orderby,
        "annotation_layers": [],
        "row_limit": row_limit or p.get("row_limit", 10000),
        "series_limit": 0,
        "order_desc": p.get("order_desc", True),
        "url_params": {},
        "custom_params": {},
        "custom_form_data": {},
        "post_processing": [],
    }
    if time_range:
        query["time_range"] = time_range
    if p.get("query_mode") == "raw":
        query["metrics"] = None
        query["orderby"] = [json.loads(o) if isinstance(o, str) else o for o in p.get("order_by_cols", [])]
    queries = [query]
    if viz == "mixed_timeseries":  # query B (metrics_b / groupby_b / adhoc_filters_b)
        qb = copy.deepcopy(query)
        qb["columns"] = ([_axis_column(p["x_axis"], tg)] if p.get("x_axis") else []) + list(p.get("groupby_b") or [])
        qb["metrics"] = list(p.get("metrics_b") or [])
        qb["filters"], qb["extras"] = [], {"having": "", "where": ""}
        for f in p.get("adhoc_filters_b", []):
            if f.get("expressionType") == "SQL":
                key = "having" if f.get("clause") == "HAVING" else "where"
                qb["extras"][key] = (qb["extras"][key] + " AND " if qb["extras"][key] else "") + f"({f['sqlExpression']})"
            else:
                qb["filters"].append({"col": f["subject"], "op": f["operator"], "val": f.get("comparator")})
        qb["orderby"] = [[qb["metrics"][0], False]] if qb["metrics"] else []
        qb["row_limit"] = row_limit or p.get("row_limit_b", 10000)
        queries.append(qb)
    return {
        "datasource": {"id": dataset_id, "type": "table"},
        "force": False,
        "queries": queries,
        "form_data": {**p, "datasource": f"{dataset_id}__table", "viz_type": viz, "result_format": "json",
                      "result_type": "full"},
        "result_format": "json",
        "result_type": "full",
    }


if __name__ == "__main__":  # quick manual check: python chart_builder.py spec.json [dataset_id]
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    ds = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    params = build_params(spec, ds)
    print(json.dumps({"params": params, "query_context": build_query_context(params, ds)}, indent=2, ensure_ascii=False))
