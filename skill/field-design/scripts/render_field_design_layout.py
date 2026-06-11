from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


DISPLAY_COLUMN_LABELS = {
    "plots": "小区编号",
    "r": "区组/重复",
    "ped_id": "材料编号",
    "ranges": "行号",
    "pass": "列号",
    "set": "组别",
    "hyb_check": "对照标记",
    "hyb_type": "材料类型",
    "design_check": "设计对照标记",
    "ck_no": "CK编号",
    "start_pos": "起始位置",
    "interval": "间隔数量",
}

DESIGN_DISPLAY_COLUMN_LABELS = {
    "rcbd": {"r": "区组"},
    "interval": {"r": "重复"},
}


def display_column_label(column: str, design: str | None = None) -> str:
    if design and column in DESIGN_DISPLAY_COLUMN_LABELS.get(design, {}):
        return DESIGN_DISPLAY_COLUMN_LABELS[design][column]
    return DISPLAY_COLUMN_LABELS.get(column, column)


def html_escape(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = {str(key): value for key, value in row.items()}
    if "ped_id" not in out and "trt" in out:
        out["ped_id"] = out.get("trt")
    if "hyb_check" not in out and "design_check" in out:
        out["hyb_check"] = out.get("design_check")
    for key in ("plots", "ranges", "pass", "r"):
        if key in out:
            out[key] = to_int(out.get(key))
    out["ped_id"] = str(out.get("ped_id") or "")
    out["set"] = str(out.get("set") or "")
    out["hyb_type"] = str(out.get("hyb_type") or ("ck" if str(out.get("hyb_check") or "") in {"1", "true", "TRUE"} else "hyb"))
    out["hyb_check"] = str(out.get("hyb_check") if out.get("hyb_check") is not None else out.get("design_check", ""))
    out["design_check"] = str(out.get("design_check") if out.get("design_check") is not None else out.get("hyb_check", ""))
    return out


def build_meta(
    *,
    rows: list[dict[str, Any]],
    design: str,
    parameters: Mapping[str, Any] | None,
    quality_control: Mapping[str, Any] | None,
) -> dict[str, Any]:
    ranges = [to_int(row.get("ranges")) for row in rows if to_int(row.get("ranges")) > 0]
    passes = [to_int(row.get("pass")) for row in rows if to_int(row.get("pass")) > 0]
    blocks = [to_int(row.get("r")) for row in rows if to_int(row.get("r")) > 0]
    sets = sorted({str(row.get("set") or "") for row in rows if str(row.get("set") or "")})
    return {
        "rows": len(rows),
        "design": design,
        "sets": sets,
        "blocks": sorted(set(blocks)),
        "ranges": [min(ranges), max(ranges)] if ranges else [0, 0],
        "passes": [min(passes), max(passes)] if passes else [0, 0],
        "parameters": dict(parameters or {}),
        "quality_control": dict(quality_control or {}),
    }


def render_layout_html(
    path: Path,
    *,
    title: str,
    rows: list[Mapping[str, Any]],
    columns: list[str],
    design: str,
    parameters: Mapping[str, Any] | None = None,
    quality_control: Mapping[str, Any] | None = None,
    max_rows: int | None = None,
) -> None:
    normalized_rows = [normalize_row(row) for row in rows]
    normalized_rows.sort(key=lambda row: (to_int(row.get("ranges")), to_int(row.get("pass")), to_int(row.get("plots"))))
    if max_rows is not None:
        normalized_rows = normalized_rows[:max_rows]
    meta = build_meta(rows=normalized_rows, design=design, parameters=parameters, quality_control=quality_control)
    title_text = title.replace("Field Design", "田间试验设计").replace("Layout", "布局图")
    rows_json = json.dumps(normalized_rows, ensure_ascii=False, separators=(",", ":"))
    meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    is_diagonal = design == "diagonal"
    has_repeat_filter = design in {"rcbd", "interval"}
    type_ck_label = "Diagonal check" if is_diagonal else "对照"
    type_hyb_label = "Test" if is_diagonal else "测试材料"
    body_class = "diagonal-layout" if is_diagonal else "block-layout"
    grid_frame_open = '<div class="grid-frame" id="gridFrame"><svg class="diag-overlay" id="diagOverlay" aria-hidden="true"></svg><div class="grid" id="grid"></div></div>' if is_diagonal else '<div class="grid" id="grid"></div>'
    repeat_filter = '<label>重复/区组<select id="blockFilter"></select></label>' if has_repeat_filter else ""
    detail_rows = '["design_check", d.design_check]' if is_diagonal else '["hyb_check", d.hyb_check]'

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_escape(title_text)}</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f7f8fa;
  --panel: #ffffff;
  --line: #d9dde5;
  --text: #1f2933;
  --muted: #64748b;
  --check: #f4b84a;
  --test-a: #78aee8;
  --test-b: #7bbf8e;
  --test-c: #c78d71;
  --active: #355c9a;
  --shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
}}
.diagonal-layout {{
  --bg: #f5f7f4;
  --line: #cfd8cf;
  --text: #1f2a24;
  --muted: #66736b;
  --active: #285f50;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: "Aptos", "Segoe UI", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
}}
body.diagonal-layout {{
  background: linear-gradient(180deg, #eef3ed, var(--bg));
}}
header {{
  padding: 18px 22px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}}
h1 {{
  margin: 0;
  font-size: 22px;
  line-height: 1.2;
  font-weight: 650;
}}
.meta {{
  margin-top: 8px;
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: 13px;
}}
.toolbar {{
  display: grid;
  grid-template-columns: repeat(4, minmax(120px, 1fr));
  gap: 10px;
  padding: 14px 22px;
  background: #eef2f6;
  border-bottom: 1px solid var(--line);
}}
.diagonal-layout .toolbar {{ background: #e7eee6; grid-template-columns: repeat(3, minmax(140px, 1fr)); }}
label {{
  display: grid;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}}
select, input {{
  height: 34px;
  border: 1px solid #c8ced8;
  border-radius: 6px;
  background: #fff;
  color: var(--text);
  padding: 0 10px;
  font-size: 14px;
}}
main {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) 290px;
  min-height: calc(100vh - 118px);
}}
.layout-wrap {{
  overflow: auto;
  padding: 16px 18px 24px;
}}
.grid-frame {{
  position: relative;
  display: inline-block;
  width: fit-content;
}}
.diag-overlay {{
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 4;
  overflow: visible;
}}
.diag-reference {{
  fill: none;
  stroke: #285f50;
  stroke-width: 2;
  stroke-opacity: 0.28;
  stroke-dasharray: 9 7;
  vector-effect: non-scaling-stroke;
}}
.diag-reference.main {{
  stroke-width: 2.5;
  stroke-opacity: 0.42;
}}
.grid {{
  position: relative;
  z-index: 1;
  display: grid;
  row-gap: 5px;
  column-gap: 34px;
  align-items: stretch;
  width: fit-content;
  min-width: min(100%, 900px);
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background:
    linear-gradient(90deg, rgba(31,41,51,0.04) 1px, transparent 1px),
    linear-gradient(180deg, rgba(31,41,51,0.04) 1px, transparent 1px),
    #fbfcfd;
  background-size: 24px 24px;
  box-shadow: var(--shadow);
}}
.diagonal-layout .grid {{
  gap: 5px;
  min-width: min(100%, 880px);
  background:
    linear-gradient(90deg, rgba(31,42,36,0.05) 1px, transparent 1px),
    linear-gradient(180deg, rgba(31,42,36,0.05) 1px, transparent 1px),
    #fbfcfa;
  box-shadow: none;
}}
.cell {{
  width: clamp(46px, calc((100vw - 430px) / var(--cols)), 84px);
  aspect-ratio: 1.16 / 1;
  min-height: 34px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: #fff;
  padding: 4px;
  cursor: pointer;
  display: grid;
  grid-template-rows: auto 1fr auto;
  gap: 2px;
  position: relative;
  transition: transform 120ms ease, box-shadow 120ms ease, opacity 120ms ease;
}}
.cell.hidden {{
  opacity: 0.12;
  filter: grayscale(0.8);
}}
.cell.ck {{
  background: linear-gradient(180deg, var(--ck-light, #ffe08a), var(--ck-color, #f4b84a));
  border-color: var(--ck-border, #d89522);
  box-shadow: inset 0 0 0 2px rgba(80, 56, 0, 0.12);
}}
.cell.hyb {{
  background: linear-gradient(180deg, var(--set-light, #dcebff), var(--set-color, #78aee8));
  border-color: var(--set-border, #4f87c4);
}}
.cell:hover {{
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.18);
  z-index: 5;
}}
.cell.selected {{
  outline: 3px solid var(--active);
  outline-offset: 1px;
}}
.trt {{
  align-self: start;
  font-size: clamp(10px, 1.1vw, 13px);
  font-weight: 700;
  overflow-wrap: anywhere;
  line-height: 1.05;
  color: #1f2933;
}}
.coord {{
  display: flex;
  justify-content: space-between;
  gap: 6px;
  font-size: 10px;
  color: rgba(31,41,51,0.74);
}}
.set-repeat {{ font-weight: 700; }}
.cell.even-rep .set-repeat {{ color: #b42318; }}
.tags {{
  display: flex;
  justify-content: space-between;
  gap: 6px;
  font-size: 11px;
}}
.badge {{
  padding: 2px 5px;
  border-radius: 4px;
  background: rgba(255,255,255,0.7);
  border: 1px solid rgba(31,41,51,0.12);
}}
.tooltip {{
  position: fixed;
  pointer-events: none;
  z-index: 10;
  display: none;
  min-width: 210px;
  border: 1px solid #b9c2d0;
  border-radius: 7px;
  background: rgba(255,255,255,0.98);
  box-shadow: 0 10px 28px rgba(15,23,42,0.2);
  padding: 10px;
  font-size: 12px;
}}
.tooltip strong {{
  display: block;
  font-size: 14px;
  margin-bottom: 6px;
}}
.tooltip-row {{
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 3px 0;
  border-top: 1px solid #eef1f5;
}}
.tooltip-row span:first-child {{ color: var(--muted); }}
aside {{
  border-left: 1px solid var(--line);
  background: var(--panel);
  padding: 18px;
}}
.detail-title {{
  font-size: 15px;
  font-weight: 700;
  margin-bottom: 12px;
}}
.detail-row {{
  display: flex;
  justify-content: space-between;
  gap: 14px;
  padding: 8px 0;
  border-bottom: 1px solid #edf0f4;
  font-size: 13px;
}}
.detail-row span:first-child {{ color: var(--muted); }}
.legend {{
  display: flex;
  gap: 10px;
  margin-top: 18px;
  font-size: 12px;
  color: var(--muted);
  flex-wrap: wrap;
}}
.swatch {{
  width: 14px;
  height: 14px;
  border-radius: 3px;
  display: inline-block;
  vertical-align: -2px;
  margin-right: 5px;
}}
.swatch.ck {{ background: var(--check); }}
.swatch.hyb {{ background: linear-gradient(90deg, var(--test-a), var(--test-b), var(--test-c)); }}
@media (max-width: 900px) {{
  .toolbar, .diagonal-layout .toolbar {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
  main {{ grid-template-columns: 1fr; }}
  aside {{ border-left: 0; border-top: 1px solid var(--line); }}
}}
</style>
</head>
<body class="{body_class}">
<header>
  <h1>{html_escape(title_text)}</h1>
  <div class="meta" id="meta"></div>
</header>
<section class="toolbar">
  <label>Set<select id="setFilter"></select></label>
  {repeat_filter}
  <label>类型<select id="typeFilter"><option value="all">全部</option><option value="ck">{type_ck_label}</option><option value="hyb">{type_hyb_label}</option></select></label>
  <label>搜索<input id="searchBox" type="search" placeholder="ped_id / plot"></label>
</section>
<main>
  <section class="layout-wrap">{grid_frame_open}</section>
  <aside>
    <div class="detail-title">Plot Detail</div>
    <div id="detail"></div>
    <div class="legend">
      <span><i class="swatch ck"></i>{type_ck_label}</span>
      <span><i class="swatch hyb"></i>测试材料按 set 着色</span>
    </div>
  </aside>
</main>
<div class="tooltip" id="tooltip"></div>
<script>
const rows = {rows_json};
const meta = {meta_json};
const design = {json.dumps(design)};
const hasRepeatFilter = {str(has_repeat_filter).lower()};
const byId = id => document.getElementById(id);
const sets = [...new Set(rows.map(d => d.set).filter(Boolean))].sort();
const blocks = [...new Set(rows.map(d => d.r).filter(Boolean))].sort((a, b) => a - b);
const maxPass = Math.max(...rows.map(d => Number(d.pass) || 0), 1);
const maxRange = Math.max(...rows.map(d => Number(d.ranges) || 0), 1);
const planter = meta.parameters && meta.parameters.planter ? meta.parameters.planter : "serpentine";
const setPalette = [
  {{ color: "#78aee8", light: "#dcebff", border: "#4f87c4" }},
  {{ color: "#7bbf8e", light: "#def4e4", border: "#4f9463" }},
  {{ color: "#c59be8", light: "#f0e4fb", border: "#8a61ba" }},
  {{ color: "#e58f7d", light: "#fde3dc", border: "#b76454" }},
  {{ color: "#d2b45f", light: "#f7edc8", border: "#9f8332" }},
  {{ color: "#78bfc7", light: "#d9f2f4", border: "#4f8f96" }}
];
const ckPalette = [
  {{ color: "#f4b84a", light: "#ffe08a", border: "#d89522" }},
  {{ color: "#e7a936", light: "#ffd776", border: "#c98218" }},
  {{ color: "#f0c35d", light: "#ffecac", border: "#cc9a2b" }},
  {{ color: "#d99a2b", light: "#f8cf74", border: "#ad7415" }},
  {{ color: "#f6c86a", light: "#fff0bd", border: "#c89124" }},
  {{ color: "#e0ad3f", light: "#f9dc88", border: "#b9821d" }}
];
const setColors = Object.fromEntries(sets.map((set, index) => [set, setPalette[index % setPalette.length]]));
const ckNames = [...new Set(rows.filter(d => d.hyb_type === "ck").map(d => d.ped_id))].sort();
const ckColors = Object.fromEntries(ckNames.map((name, index) => [name, ckPalette[index % ckPalette.length]]));
let selected = rows[0] || null;

function fillSelect(select, values, allLabel) {{
  if (!select) return;
  select.innerHTML = "";
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = allLabel;
  select.appendChild(all);
  values.forEach(value => {{
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = String(value);
    select.appendChild(option);
  }});
}}

function compact(value) {{
  return value === undefined || value === null || value === "" ? "NA" : value;
}}

function renderMeta() {{
  const p = meta.parameters || {{}};
  const planterLabel = planter === "serpentine" ? "蛇形排列" : "顺序排列";
  const items = [
    `设计 ${{design.toUpperCase()}}`,
    `排列方式 ${{planterLabel}}`,
    `小区 ${{meta.rows}}`,
    `Set ${{sets.join(", ") || "NA"}}`,
    `行 ${{meta.ranges[0]}}-${{meta.ranges[1]}}`,
    `列 ${{meta.passes[0]}}-${{meta.passes[1]}}`
  ];
  if (blocks.length) items.splice(4, 0, `重复/区组 ${{blocks.join(", ")}}`);
  if (design === "diagonal") {{
    items.push(`used ${{compact(p.used_ck_ratio)}}`);
    items.push(`actual ${{compact(p.actual_check_percent)}}`);
    items.push(`diags ${{compact(p.diagonal_count)}}/${{compact(p.target_diagonal_count)}}`);
    if (p.diagonal_angle_deg !== undefined) items.push(`angle ${{p.diagonal_angle_deg}}°`);
  }}
  byId("meta").innerHTML = items.map(x => `<span>${{x}}</span>`).join("");
}}

function rowAt(range, pass) {{
  return rows.find(row => Number(row.ranges) === range && Number(row.pass) === pass);
}}

function isVisible(d) {{
  if (!d) return false;
  const setValue = byId("setFilter").value;
  const blockValue = hasRepeatFilter && byId("blockFilter") ? byId("blockFilter").value : "all";
  const typeValue = byId("typeFilter").value;
  const search = byId("searchBox").value.trim().toLowerCase();
  if (setValue !== "all" && d.set !== setValue) return false;
  if (blockValue !== "all" && String(d.r) !== blockValue) return false;
  if (typeValue !== "all" && d.hyb_type !== typeValue) return false;
  if (search && !`${{d.ped_id}} ${{d.plots}} ${{d.set}}`.toLowerCase().includes(search)) return false;
  return true;
}}

function renderGrid() {{
  const grid = byId("grid");
  grid.style.setProperty("--cols", maxPass);
  grid.style.gridTemplateColumns = `repeat(${{maxPass}}, minmax(46px, 1fr))`;
  grid.innerHTML = "";
  for (let range = 1; range <= maxRange; range++) {{
    for (let pass = 1; pass <= maxPass; pass++) {{
      const d = rowAt(range, pass);
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cell";
      if (!d) {{
        cell.classList.add("hidden");
        cell.disabled = true;
        grid.appendChild(cell);
        continue;
      }}
      cell.classList.add(d.hyb_type === "ck" ? "ck" : "hyb");
      cell.classList.add(Number(d.r || 1) % 2 === 0 ? "even-rep" : "odd-rep");
      if (d.hyb_type === "ck") {{
        const color = ckColors[d.ped_id] || ckPalette[0];
        cell.style.setProperty("--ck-color", color.color);
        cell.style.setProperty("--ck-light", color.light);
        cell.style.setProperty("--ck-border", color.border);
      }} else {{
        const color = setColors[d.set] || setPalette[0];
        cell.style.setProperty("--set-color", color.color);
        cell.style.setProperty("--set-light", color.light);
        cell.style.setProperty("--set-border", color.border);
      }}
      if (!isVisible(d)) cell.classList.add("hidden");
      if (selected && selected.plots === d.plots) cell.classList.add("selected");
      cell.dataset.range = d.ranges;
      cell.dataset.pass = d.pass;
      cell.setAttribute("aria-label", `${{d.ped_id}}, range ${{d.ranges}}, pass ${{d.pass}}`);
      const repeatText = hasRepeatFilter ? `${{d.set}}/${{d.r || ""}}` : `${{d.set}}`;
      cell.innerHTML = `
        <div class="trt">${{d.ped_id}}</div>
        <div></div>
        <div class="coord"><span>R${{d.ranges}} P${{d.pass}}</span><span class="set-repeat">${{repeatText}}</span></div>
      `;
      cell.addEventListener("mouseenter", event => showTooltip(event, d));
      cell.addEventListener("mousemove", event => moveTooltip(event));
      cell.addEventListener("mouseleave", hideTooltip);
      cell.addEventListener("click", () => {{
        selected = d;
        renderGrid();
        renderDetail();
      }});
      grid.appendChild(cell);
    }}
  }}
  renderDiagonalOverlay();
}}

function tooltipHtml(d) {{
  return `
    <strong>${{d.ped_id}}</strong>
    <div class="tooltip-row"><span>plot</span><b>${{d.plots}}</b></div>
    <div class="tooltip-row"><span>range / pass</span><b>${{d.ranges}} / ${{d.pass}}</b></div>
    <div class="tooltip-row"><span>set / repeat</span><b>${{d.set}} / ${{d.r || "NA"}}</b></div>
    <div class="tooltip-row"><span>type</span><b>${{d.hyb_type}}</b></div>
    <div class="tooltip-row"><span>check</span><b>${{d.hyb_check || d.design_check || "NA"}}</b></div>
  `;
}}

function showTooltip(event, d) {{
  const tip = byId("tooltip");
  tip.innerHTML = tooltipHtml(d);
  tip.style.display = "block";
  moveTooltip(event);
}}

function moveTooltip(event) {{
  const tip = byId("tooltip");
  const pad = 14;
  const rect = tip.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + rect.width > window.innerWidth) x = event.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight) y = event.clientY - rect.height - pad;
  tip.style.left = `${{Math.max(8, x)}}px`;
  tip.style.top = `${{Math.max(8, y)}}px`;
}}

function hideTooltip() {{
  byId("tooltip").style.display = "none";
}}

function normalizedAxes() {{
  const p = meta.parameters || {{}};
  const axes = p.diagonal_axes;
  if (Array.isArray(axes)) return axes.map(Number).filter(Number.isFinite);
  if (Number.isFinite(Number(axes))) return [Number(axes)];
  return [];
}}

function clipDiagonal(axis) {{
  const points = [];
  const add = (u, v) => {{
    if (u >= -1e-9 && u <= 1 + 1e-9 && v >= -1e-9 && v <= 1 + 1e-9) {{
      const key = `${{u.toFixed(6)}},${{v.toFixed(6)}}`;
      if (!points.some(point => point.key === key)) points.push({{ u, v, key }});
    }}
  }};
  add(0, -axis);
  add(1, 1 - axis);
  add(axis, 0);
  add(1 + axis, 1);
  return points.slice(0, 2);
}}

function renderDiagonalOverlay() {{
  if (design !== "diagonal") return;
  const svg = byId("diagOverlay");
  const grid = byId("grid");
  if (!svg || !grid) return;
  const axes = normalizedAxes();
  if (axes.length === 0) {{
    svg.innerHTML = "";
    return;
  }}
  const gridRect = grid.getBoundingClientRect();
  const first = grid.querySelector(`[data-range="1"][data-pass="1"]`);
  const last = grid.querySelector(`[data-range="${{maxRange}}"][data-pass="${{maxPass}}"]`);
  if (!first || !last || gridRect.width === 0 || gridRect.height === 0) return;
  const firstRect = first.getBoundingClientRect();
  const lastRect = last.getBoundingClientRect();
  const left = firstRect.left - gridRect.left;
  const top = firstRect.top - gridRect.top;
  const right = lastRect.right - gridRect.left;
  const bottom = lastRect.bottom - gridRect.top;
  svg.setAttribute("viewBox", `0 0 ${{gridRect.width}} ${{gridRect.height}}`);
  svg.setAttribute("width", gridRect.width);
  svg.setAttribute("height", gridRect.height);
  svg.innerHTML = "";
  const toPoint = point => ({{ x: left + point.u * (right - left), y: top + point.v * (bottom - top) }});
  axes.forEach(axis => {{
    const clipped = clipDiagonal(Number(axis));
    if (clipped.length < 2) return;
    const a = toPoint(clipped[0]);
    const b = toPoint(clipped[1]);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", a.x.toFixed(2));
    line.setAttribute("y1", a.y.toFixed(2));
    line.setAttribute("x2", b.x.toFixed(2));
    line.setAttribute("y2", b.y.toFixed(2));
    line.setAttribute("class", Math.abs(axis) < 1e-9 ? "diag-reference main" : "diag-reference");
    svg.appendChild(line);
  }});
}}

function renderDetail() {{
  const d = selected;
  if (!d) {{
    byId("detail").innerHTML = "";
    return;
  }}
  byId("detail").innerHTML = [
    ["plot", d.plots],
    ["ped_id", d.ped_id],
    ["set", d.set],
    ["repeat", d.r || "NA"],
    ["range", d.ranges],
    ["pass", d.pass],
    ["type", d.hyb_type],
    {detail_rows}
  ].map(([k, v]) => `<div class="detail-row"><span>${{k}}</span><strong>${{compact(v)}}</strong></div>`).join("");
}}

fillSelect(byId("setFilter"), sets, "全部");
if (hasRepeatFilter) fillSelect(byId("blockFilter"), blocks, "全部");
["setFilter", "blockFilter", "typeFilter", "searchBox"].forEach(id => {{
  const element = byId(id);
  if (element) element.addEventListener("input", renderGrid);
}});
window.addEventListener("resize", renderDiagonalOverlay);
renderMeta();
renderGrid();
renderDetail();
</script>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
