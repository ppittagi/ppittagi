# Colours — matching a Tableau chart exactly

Three layers, from strongest to weakest:

1. **`label_colors`** (per chart `params`, and dashboard `json_metadata.label_colors` +
   `shared_label_colors`): label → hex. Exact. Use this to reproduce Tableau's category colours.
2. **`color_scheme`** (categorical scheme id) per chart / dashboard: used for labels not pinned.
3. **Theme** (`theme_id`, Ant Design tokens): fonts, primary colour, backgrounds — not series colours.

Dashboard-level `label_colors` is applied to every chart on the dashboard, so one mapping (site →
colour, arm → colour) keeps colours consistent across tabs. **Inside a dashboard only the
dashboard-level mapping is honoured** — on 7.0 chart-level `params.label_colors` is ignored both on
the dashboard canvas and in Explore (verified on 7.0.0rc1; check colours on the dashboard, not in Explore). `build_dashboard.py` therefore merges every
chart's `label_colors` into `json_metadata.label_colors`; if you write metadata by hand, do the same.
Null category label is `<NULL>` in 7.0.

Two 7.0 quirks seen in testing: a series whose name equals a metric/column `verbose_name` used
elsewhere on the dashboard is looked up through the dataset's verbose map before the colour lookup, so
pin the raw name as well (`sales` and `Sales`), or give single-series charts a unique metric label;
and the same label used as a metric label on one chart and a series on another may not pin — unique
labels per role avoid both.

## Reading colours from a Tableau screenshot

* Identify the legend: label ↔ swatch. Pick the swatch's hex (the image analysis step; if unsure,
  snap to the nearest colour in the palettes below — Tableau dashboards almost always use one).
* Tableau 10 (default since Tableau 10): `#4E79A7 #F28E2B #E15759 #76B7B2 #59A14F #EDC948 #B07AA1 #FF9DA7 #9C755F #BAB0AC`
* Tableau Classic 10 (≤ 9.x, = d3 category10 = Superset `d3Category10`):
  `#1F77B4 #FF7F0E #2CA02C #D62728 #9467BD #8C564B #E377C2 #7F7F7F #BCBD22 #17BECF`
* Tableau 20: `#4E79A7 #A0CBE8 #F28E2B #FFBE7D #59A14F #8CD17D #B6992D #F1CE63 #499894 #86BCB6 #E15759 #FF9D9A #79706E #BAB0AC #D37295 #FABFD2 #B07AA1 #D4A6C8 #9D7660 #D7B5A6`
* Tableau Color Blind 10: `#1170AA #FC7D0B #A3ACB9 #57606C #5FA2CE #C8D0D9 #8CC2CA #FFBC79 #C85200 #A3CCE9`
* Tableau Seattle Grays: `#767F8B #B3B7B8 #5C6068 #D3D3D3 #989CA3`
* Tableau Traffic Light: `#B10318 #DBA13A #309343 #D82526 #FFC156 #69B764 #F26C64 #FFDD71 #9FCD99`
* Tableau sequential: Blue `#B4D4DA→#2A5783`, Orange `#F7D2B5→#A2360F`, Green `#C5DECD→#265F2B`, Red-Green diverging `#D53E4F…#1A9850`
* Tableau 10's order maps to the order categories appear in the legend (usually alphabetical),
  **not** to the data order — so always pin by label, never by index.

## Superset categorical schemes (built in)

| id | note |
|---|---|
| `supersetColors` | default 20 colours (`#1FA8C9 #454E7C #5AC189 #FF7F44 #666666 #E04355 #FCC700 #A868B7 #3CCCCB #A38F79 …`) |
| `d3Category10` / `d3Category20` / `d3Category20b` / `d3Category20c` | d3 classics (`d3Category10` = Tableau Classic 10) |
| `googleCategory10c` / `googleCategory20c` | Google charts |
| `lyftColors`, `presetColors`, `bnbColors`, `airbnb`, `echarts4Colors`, `echarts5Colors`, `modernSunset`, `blueToGreen`, `colorsOfRainbow`, `superset_seq_*` | others |

There is **no built-in "Tableau 10" scheme**. Options:
* Pin with `label_colors` (no server change, exact, recommended).
* Register a scheme once in `superset_config.py` (needs restart; then `color_scheme: "tableau10"`
  works everywhere and new labels inherit it):
  ```python
  EXTRA_CATEGORICAL_COLOR_SCHEMES = [{
      "id": "tableau10", "description": "", "label": "Tableau 10", "isDefault": False,
      "colors": ["#4E79A7","#F28E2B","#E15759","#76B7B2","#59A14F","#EDC948","#B07AA1","#FF9DA7","#9C755F","#BAB0AC"]}]
  EXTRA_SEQUENTIAL_COLOR_SCHEMES = [{
      "id": "tableau_blue", "description": "", "label": "Tableau Blue", "isDiverging": False,
      "colors": ["#B4D4DA","#8DB8C4","#6A9EB0","#4B839B","#2A5783"]}]
  ```

## Sequential / diverging (heatmaps, maps, tables)

`linear_color_scheme` ids: `blue_white_yellow`, `fire`, `white_black`, `black_white`, `dark_blue`,
`pink_grey`, `greens`, `purples`, `oranges`, `red_yellow_blue`, `brown_white_green`,
`purple_white_green`, `schemeBlues`, `schemeGreens`, `schemeReds`, `schemeOranges`, `schemePurples`,
`schemeGreys`, `schemeViridis`, `schemeInferno`, `schemeMagma`, `schemePlasma`, `schemeRdYlBu`,
`schemeRdBu`, `schemePRGn`, `schemeBrBG`, `schemeYlOrRd`, `schemeYlGnBu`, `superset_seq_1`,
`superset_seq_2`. Tableau "Blue" ≈ `schemeBlues`, "Orange-Blue diverging" ≈ `schemeRdYlBu`
(reversed via `value_bounds`/`normalize`), "Red-Green" ≈ `schemeRdYlGn` if present, else `red_yellow_blue`.

## Themes (7.0, Ant Design 6 tokens)

```json
{"token": {"colorPrimary": "#4E79A7", "colorBgBase": "#FFFFFF", "fontFamily": "Pretendard, Inter, sans-serif",
           "fontSize": 13, "borderRadius": 4, "brandAppName": "Clinical BI"},
 "algorithm": "default"}
```
`POST /api/v1/theme/` → `theme_id` on the dashboard. Dark mode: `"algorithm": "dark"`. Use a theme
for corporate look-and-feel; keep series colours in `label_colors`.

## CSS (dashboard `css`)

For things tokens don't cover (hide the filter bar title, tighten tile padding):
```css
.dashboard-component-chart-holder { box-shadow: none; }
.header-title { font-weight: 600; }
```
7.0 renamed many `.ant-*` internals; prefer Superset's own class names and test after upgrades.
