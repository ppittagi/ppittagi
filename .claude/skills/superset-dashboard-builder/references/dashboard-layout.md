# Dashboard layout (`position_json`) — grid, rows, tabs

The dashboard canvas is a tree of components keyed by id. `scripts/dashboard_builder.py`
(`LayoutBuilder`) generates it from the compact `layout:` spec; read this when you need to edit a
layout by hand, merge into an existing dashboard, or debug "my chart doesn't show".

## Component tree

```
ROOT_ID (ROOT)
└── GRID_ID (GRID)
    ├── HEADER-xxx   (section title, optional)
    ├── TABS-xxx     (TABS)            ← tabbed dashboard
    │   ├── TAB-a (TAB meta.text="Overview")
    │   │   ├── ROW-1 ── CHART-1 (width 3) ── CHART-2 (width 3) ── MARKDOWN-1 (width 6)
    │   │   └── ROW-2 ── CHART-3 (width 12)
    │   └── TAB-b (TAB meta.text="Safety")
    │       └── TABS-yyy (nested tabs are allowed)
    └── ROW-3 ...    (rows directly in the grid = untabbed area)
HEADER_ID (HEADER meta.text = dashboard title; top bar, not a child of anything)
DASHBOARD_VERSION_KEY: "v2"
```

Rules the frontend enforces:
* Every component: `{"type","id","children":[],"parents":[...ancestor ids root→parent],"meta":{}}`.
  `parents` must list the full chain (`["ROOT_ID","GRID_ID","TABS-x","TAB-a","ROW-1"]`). The MCP
  tool and recent versions recompute `parents` from `children`, but the UI relies on them — fill them.
* `GRID_ID.children` holds ROW / TABS / HEADER / DIVIDER ids. ROW children are CHART / MARKDOWN /
  COLUMN. TABS children are TAB; TAB children are ROW / TABS / HEADER / DIVIDER / MARKDOWN.
* Row width: sum of children `meta.width` ≤ **12** (GRID_COLUMN_COUNT). Default chart width 4,
  min 1. Height `meta.height` in 8 px units (`50` → 400 px); KPI tiles ~`20-25`, charts `50-60`,
  tables `60-80`.
* A CHART needs `meta.chartId` (int) **and** the chart must be linked to the dashboard (`dashboards`
  on the chart or the `/charts` relation). `meta.uuid` and `meta.sliceName` are informational but
  the UI fills them, so include them. `meta.sliceNameOverride` renames the tile on this dashboard.
* Tab ids (`TAB-…`) are what native filter `scope.rootPath` / `tabsInScope` refer to. Keep them
  stable between runs if you have tab-scoped filters (the builder regenerates the whole layout and
  recomputes the scopes, so this only matters for hand edits).
* Max `position_json` size 65 535 chars (`DASHBOARD_POSITION_DATA_LIMIT`).

## Minimal example

```json
{
 "DASHBOARD_VERSION_KEY": "v2",
 "ROOT_ID": {"type":"ROOT","id":"ROOT_ID","children":["GRID_ID"]},
 "GRID_ID": {"type":"GRID","id":"GRID_ID","children":["TABS-1"],"parents":["ROOT_ID"]},
 "HEADER_ID": {"type":"HEADER","id":"HEADER_ID","meta":{"text":"Study Enrollment & Safety"}},
 "TABS-1": {"type":"TABS","id":"TABS-1","children":["TAB-ov","TAB-sf"],"parents":["ROOT_ID","GRID_ID"],"meta":{}},
 "TAB-ov": {"type":"TAB","id":"TAB-ov","children":["ROW-1"],"parents":["ROOT_ID","GRID_ID","TABS-1"],
            "meta":{"text":"Overview","defaultText":"Tab title","placeholder":"Tab title"}},
 "TAB-sf": {"type":"TAB","id":"TAB-sf","children":["ROW-2"],"parents":["ROOT_ID","GRID_ID","TABS-1"],
            "meta":{"text":"Safety","defaultText":"Tab title","placeholder":"Tab title"}},
 "ROW-1": {"type":"ROW","id":"ROW-1","children":["CHART-1","CHART-2"],"parents":["ROOT_ID","GRID_ID","TABS-1","TAB-ov"],
           "meta":{"background":"BACKGROUND_TRANSPARENT"}},
 "CHART-1": {"type":"CHART","id":"CHART-1","children":[],"parents":["ROOT_ID","GRID_ID","TABS-1","TAB-ov","ROW-1"],
             "meta":{"chartId":34,"uuid":"…","width":4,"height":25,"sliceName":"Enrolled subjects"}},
 "CHART-2": {"type":"CHART","id":"CHART-2","children":[],"parents":["ROOT_ID","GRID_ID","TABS-1","TAB-ov","ROW-1"],
             "meta":{"chartId":35,"uuid":"…","width":8,"height":50,"sliceName":"Monthly enrollment"}},
 "ROW-2": {"type":"ROW","id":"ROW-2","children":["MARKDOWN-1"],"parents":["ROOT_ID","GRID_ID","TABS-1","TAB-sf"],
           "meta":{"background":"BACKGROUND_TRANSPARENT"}},
 "MARKDOWN-1": {"type":"MARKDOWN","id":"MARKDOWN-1","children":[],"parents":["ROOT_ID","GRID_ID","TABS-1","TAB-sf","ROW-2"],
                "meta":{"width":12,"height":20,"code":"## Safety\nData cut: 2026-10-01"}}
}
```

Other components: `HEADER` (`meta.text`, `headerSize` SMALL_HEADER|MEDIUM_HEADER|LARGE_HEADER),
`DIVIDER` (no meta), `COLUMN` (`meta.width`, `background`; children stack vertically inside a row —
use it when a Tableau layout has a tall chart beside two stacked small ones).

## One panel for several charts (Tableau-style sheets without card borders)

Superset draws every chart as its own card (border, shadow, 16 px padding, title). Tableau sheets
sit next to each other inside one container. To get that look, make the **row** the panel:

```yaml
layout:
  rows:
    - charts: [ {chart: price_box, width: 7, height: 75}, {chart: homes_by_city, width: 5, height: 75} ]
      panel: true          # ROW meta.background = BACKGROUND_WHITE -> the row is the white panel
      hide_titles: true    # optional: no per-chart tile titles (put a `header:` row above instead)
```
`build_dashboard.py` adds dashboard CSS for the charts in such rows (class names verified on 7.0rc1):
```css
.dashboard-chart-id-30, .dashboard-chart-id-31 { box-shadow: none !important; border: none !important; background: transparent !important; }
.grid-row.background--white { border: 1px solid #d9d9d9; border-radius: 4px; }
.dashboard-chart-id-31 .slice-header { display: none; }     /* hide_titles */
```
`dashboard.seamless: true` flattens every chart card on the dashboard instead. Per-chart
`hide_title: true` inside a row item hides a single title. The chart's own `⋮` menu and the filter
badge stay (they belong to the chart holder); the `header:` row and the dashboard title carry the
Tableau titles. CSS is stored in the dashboard's `css` field together with any `dashboard.css` you
wrote, so UI edits of the CSS are overwritten on the next apply.

## Translating a Tableau dashboard layout

| Tableau | Superset |
|---|---|
| Dashboard with several sheets side by side | one tab (or untabbed grid) with rows; widths proportional to Tableau widths × 12 / total; `panel: true` on the row makes the sheets share one panel |
| Multiple dashboards / story points / sheet tabs | `TABS` → one `TAB` per dashboard |
| Vertical container with two sheets | `COLUMN` with two charts, or two rows |
| Title text object | `HEADER` row (LARGE_HEADER) or dashboard title |
| Text / annotations object | `MARKDOWN` component |
| Filter cards on the right | native filter bar (`filter_bar_orientation: VERTICAL` is the left-side bar; `HORIZONTAL` puts them above the charts) |
| Legend as separate object | per-chart legends (`show_legend`, `legendOrientation`) |

Size heuristic from a screenshot: measure each sheet's width as a fraction of the dashboard width,
multiply by 12, round, and fix rows so each sums to 12. Heights: KPI row ≈ 20–25 units, charts ≈
50–60, tall tables ≈ 70–90.

## Reading / modifying an existing layout

`GET /api/v1/dashboard/{id}` → `position_json` (string). To add a chart to an existing dashboard
without rebuilding: parse it, append a new `ROW-…`/`CHART-…` under the right TAB (or `GRID_ID`),
link the chart (`PUT /api/v1/chart/{id}` with `dashboards` including this dashboard), then PUT the
dashboard with the updated string. `GET /api/v1/dashboard/{id}/tabs` returns the tab tree if you only
need tab ids (for filter scopes).
