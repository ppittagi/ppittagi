# Native filters — dashboard-wide filtering, defaults, cascading, tab scopes

Native filters live in `json_metadata.native_filter_configuration` (array). One filter can drive
**every chart on the dashboard** (that is what "필터 적용 시 전체 차트에 적용" needs): give it one
`target` per dataset that has the column, and leave `scope.rootPath = ["ROOT_ID"]`.
`scripts/dashboard_builder.py::build_native_filters` writes these from the compact `filters:` spec.

## Filter entry

```jsonc
{
  "id": "NATIVE_FILTER-site-3f2a1b",          // any unique string, stable across saves
  "name": "Site",
  "filterType": "filter_select",             // filter_select | filter_range | filter_time | filter_timegrain | filter_timecolumn
  "type": "NATIVE_FILTER",                   // or "DIVIDER" (then only id,title,description)
  "description": "",
  "targets": [                               // one per dataset the filter applies to
    {"datasetId": 12, "column": {"name": "site"}},
    {"datasetId": 13, "column": {"name": "site"}}
  ],
  "controlValues": {                         // per filterType, below
    "enableEmptyFilter": false, "defaultToFirstItem": false, "multiSelect": true,
    "searchAllOptions": false, "inverseSelection": false, "sortAscending": true, "creatable": false
  },
  "defaultDataMask": {                       // default value (empty when none)
    "extraFormData": {"filters": [{"col": "site", "op": "IN", "val": ["Seoul"]}]},
    "filterState": {"value": ["Seoul"]},
    "ownState": {}
  },
  "cascadeParentIds": [],                    // ids of parent filters (dependent/cascading)
  "scope": {"rootPath": ["ROOT_ID"], "excluded": []},   // rootPath: ROOT_ID = everywhere, or TAB ids
  "chartsInScope": [34, 35, 36],             // computed: chart ids affected
  "tabsInScope": ["TAB-ov", "TAB-sf"],       // computed: tabs containing those charts
  "sortMetric": null, "requiredFirst": false,
  "adhoc_filters": [],                       // optional: pre-filter the option list
  "time_range": "No filter"
}
```

### controlValues by type

| filterType | controlValues | defaultDataMask.extraFormData | filterState.value |
|---|---|---|---|
| `filter_select` | `multiSelect`, `enableEmptyFilter` (= required), `defaultToFirstItem`, `searchAllOptions`, `inverseSelection`, `sortAscending`, `creatable` | `{"filters":[{"col","op":"IN"|"NOT IN","val":[...]}]}` | `["A","B"]` |
| `filter_range` | `enableEmptyFilter`, `enableSingleValue` (`minimum`/`exact`/`maximum`, omit for a slider) | `{"filters":[{"col","op":">=","val":lo},{"col","op":"<=","val":hi}]}` | `[lo, hi]` |
| `filter_time` | `enableEmptyFilter` | `{"time_range":"Last month"}` | `"Last month"` |
| `filter_timegrain` | `enableEmptyFilter` | `{"time_grain_sqla":"P1M"}` | `["P1M"]` |
| `filter_timecolumn` | `enableEmptyFilter` | `{"granularity_sqla":"onset_date"}` | `["onset_date"]` |

`filter_time` has `targets: [{}]` (no dataset): it sets `time_range` on every chart in scope, and a
chart reacts only if it has a temporal x-axis or a `TEMPORAL_RANGE` adhoc filter on its date column
(the chart builder adds one when `time_column`/temporal `x` is set). `filter_timegrain` and
`filter_timecolumn` similarly only affect time-series charts.

### Scope

* `scope.rootPath`: `["ROOT_ID"]` = the whole dashboard; `["TAB-sf"]` = only that tab's charts.
* `scope.excluded`: chart ids to leave alone. Charts whose dataset is not in `targets` cannot be
  filtered — 7.0 marks them "Incompatible" in the UI; put them in `excluded` to keep the bar clean
  (the builder does this automatically).
* `chartsInScope` / `tabsInScope` are derived from the above; include them (the builder does) so the
  dashboard applies filters immediately on load without a round-trip through the config modal.

### Cascading (dependent) filters — Tableau "Only relevant values"

`cascadeParentIds: ["<parent filter id>"]` on the child filter (e.g. Site depends on Country). The
option list of the child is restricted by the parent's current selection. Both filters must target
the same dataset(s).

### Dividers / sections

`{"id":"NATIVE_FILTER_DIVIDER-…","type":"DIVIDER","title":"Safety","description":""}` groups
filters in the bar.

## Saving

Either embed in `json_metadata` on `PUT /api/v1/dashboard/{id}` (what the scripts do, works on
5.x–7.0), or `PUT /api/v1/dashboard/{id}/filters` with `{"modified":[...],"deleted":[...],"reordered":[...]}`
(incremental). Re-save with the *same* ids to keep users' saved URLs/permalinks valid.

## Cross-filters (click a bar → filter other charts; Tableau "Use as filter" actions)

```json
"cross_filters_enabled": true,
"global_chart_configuration": {"scope": {"rootPath": ["ROOT_ID"], "excluded": []}, "chartsInScope": [34,35,36]},
"chart_configuration": {"34": {"id": 34, "crossFilters": {"scope": "global", "chartsInScope": [35,36]}}, ...}
```
Set `cross_filters: true` in the dashboard spec; the builder emits this. Per-chart opt-out: give
that chart's entry an explicit `scope` with `excluded`, or set `"crossFilters": {"scope": {...}}`.

## Legacy keys to avoid

`default_filters`, `filter_scopes`, `show_native_filters` and the `filter_box` chart type are from
the pre-native-filter era. 7.0 strips/ignores them; don't write them.

## Tableau filter → native filter cheat-sheet

| Tableau | Superset filter spec |
|---|---|
| Multiple values (list/dropdown) | `type: select, multi: true` |
| Single value (dropdown/radio) | `type: select, multi: false` |
| "All" option / allow none | default; `required: true` to force a value |
| Wildcard search | `search_all: true` |
| Exclude mode | `inverse: true` |
| Range of values (slider) | `type: range` (numeric column) |
| Relative date / date range | `type: time` (one per dashboard is enough) |
| Only relevant values | `depends_on: [Parent filter name]` |
| Apply to worksheets: selected sheets | `scope: {tabs: [...], exclude_charts: [...]}` |
| Parameter controlling granularity | `type: timegrain` |
| Parameter switching the date field | `type: timecolumn` |
| Dashboard action: filter on click | `dashboard.cross_filters: true` |
| Parameter switching the measure | not supported natively — use separate charts/tabs, or a virtual dataset with the choice encoded as a column + `filter_select` |
