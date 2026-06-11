#!/usr/bin/env python3
"""Render a breedcore PCA result as a self-contained interactive HTML plot."""

from __future__ import annotations

import argparse
import html
import json
import sys
from typing import Any


PALETTE = [
    "#2f7d68",
    "#a64f3c",
    "#4f6fa8",
    "#b47a2b",
    "#6f5aa8",
    "#238194",
    "#9a4f78",
    "#5d7d3a",
]


def load_result(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def pc_sort_key(name: str) -> tuple[int, str]:
    suffix = name[2:]
    if suffix.isdigit():
        return (int(suffix), name)
    return (10_000, name)


def available_pcs(scores: list[dict[str, Any]]) -> list[str]:
    if not scores:
        return []
    pcs = []
    for key, value in scores[0].items():
        if key.startswith("PC") and isinstance(value, (int, float)):
            pcs.append(key)
    return sorted(pcs, key=pc_sort_key)


def variance_labels(result: dict[str, Any], pcs: list[str]) -> dict[str, str]:
    labels = {pc: "" for pc in pcs}
    for item in result.get("tables", {}).get("eigenvalues", []):
        component = item.get("component")
        ratio = item.get("variance_ratio")
        if component in labels and isinstance(ratio, (int, float)):
            labels[component] = f"{ratio * 100:.2f}%"
    return labels


def render_options(pcs: list[str], selected: str) -> str:
    options = []
    for pc in pcs:
        flag = " selected" if pc == selected else ""
        options.append(f'<option value="{html.escape(pc)}"{flag}>{html.escape(pc)}</option>')
    return "".join(options)


def render_html(result: dict[str, Any], title: str) -> str:
    scores = result.get("tables", {}).get("scores", [])
    if not scores:
        raise SystemExit("No PCA scores found at result.tables.scores")

    pcs = available_pcs(scores)
    if len(pcs) < 2:
        raise SystemExit("At least two numeric PC columns are required in result.tables.scores")

    labels = variance_labels(result, pcs)
    default_x = "PC1" if "PC1" in pcs else pcs[0]
    default_y = "PC2" if "PC2" in pcs else pcs[1]

    populations = sorted({str(row.get("population") or "材料") for row in scores})
    color_by_pop = {pop: PALETTE[i % len(PALETTE)] for i, pop in enumerate(populations)}

    points = []
    for row in scores:
        pop = str(row.get("population") or "材料")
        point = {
            "id": str(row.get("sample_id", "")),
            "population": pop,
            "color": color_by_pop[pop],
        }
        for pc in pcs:
            point[pc.lower()] = float(row[pc])
        points.append(point)

    summary = result.get("summary", {})
    sample_count = summary.get("sample_count") or summary.get("input_sample_count") or len(points)
    marker_count = summary.get("input_marker_count") or summary.get("marker_count")
    pc1_label = labels.get("PC1", "")
    pc2_label = labels.get("PC2", "")
    pc12 = ""
    try:
        pc12_value = sum(
            item.get("variance_ratio", 0)
            for item in result.get("tables", {}).get("eigenvalues", [])
            if item.get("component") in {"PC1", "PC2"}
        )
        if pc12_value:
            pc12 = f"{pc12_value * 100:.2f}%"
    except TypeError:
        pc12 = ""

    legend = ""
    if 1 < len(populations) <= 12:
        legend_items = []
        for pop in populations:
            color = html.escape(color_by_pop[pop])
            legend_items.append(
                f'<span class="legend-item"><i style="background:{color}"></i>{html.escape(pop)}</span>'
            )
        legend = f'<div class="legend">{"".join(legend_items)}</div>'

    data_json = json.dumps(points, ensure_ascii=False).replace("<", "\\u003c")
    labels_json = json.dumps(labels, ensure_ascii=False).replace("<", "\\u003c")
    pcs_json = json.dumps(pcs, ensure_ascii=False).replace("<", "\\u003c")
    x_options = render_options(pcs, default_x)
    y_options = render_options(pcs, default_y)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title)}</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #f7f7f4;
    --ink: #1f2722;
    --muted: #61706a;
    --panel: #ffffff;
    --line: #cfd8d1;
    --grid: #e3e8e4;
    --axis: #7f8b86;
    --accent: #2f7d68;
    --accent-strong: #a64f3c;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: Arial, "Microsoft YaHei", sans-serif;
  }}
  main {{
    max-width: 1180px;
    margin: 0 auto;
    padding: 28px 20px 36px;
  }}
  h1 {{
    margin: 0 0 8px;
    font-size: 26px;
    line-height: 1.25;
    font-weight: 700;
    letter-spacing: 0;
  }}
  p {{
    margin: 0 0 18px;
    color: var(--muted);
    line-height: 1.6;
  }}
  .toolbar {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    margin: 14px 0 16px;
  }}
  label {{
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--muted);
    font-size: 14px;
  }}
  select, input {{
    min-height: 36px;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 8px 10px;
    background: #fff;
    color: var(--ink);
    font: inherit;
  }}
  input {{ min-width: 190px; }}
  .wrap {{
    display: grid;
    grid-template-columns: minmax(0, 1fr) 280px;
    gap: 18px;
    align-items: start;
  }}
  #plotBox {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 12px;
    box-shadow: 0 12px 28px rgba(31, 39, 34, 0.06);
  }}
  svg {{
    display: block;
    width: 100%;
    height: auto;
  }}
  .side {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 14px;
  }}
  .side h2 {{
    margin: 0 0 10px;
    font-size: 16px;
    line-height: 1.3;
  }}
  .metric {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
    border-bottom: 1px solid #edf0ed;
    padding: 8px 0;
    font-size: 14px;
  }}
  .metric span:first-child {{ color: var(--muted); }}
  .legend {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px 14px;
    margin-top: 12px;
    color: var(--muted);
    font-size: 13px;
  }}
  .legend-item {{
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }}
  .legend-item i {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    box-shadow: 0 0 0 2px rgba(255,255,255,.9);
  }}
  .tooltip {{
    position: fixed;
    z-index: 10;
    pointer-events: none;
    transform: translate(10px, 10px);
    border-radius: 6px;
    padding: 7px 9px;
    background: #1f2722;
    color: white;
    font-size: 12px;
    line-height: 1.45;
    white-space: nowrap;
    opacity: 0;
    transition: opacity 90ms ease;
  }}
  .point {{
    opacity: .78;
    stroke: white;
    stroke-width: 1.4;
    cursor: pointer;
    transition: opacity 120ms ease, r 120ms ease, stroke-width 120ms ease;
  }}
  .point:hover,
  .point.active {{
    opacity: 1;
    fill: var(--accent-strong);
    stroke: var(--ink);
    stroke-width: 2.2;
  }}
  .axis {{
    stroke: var(--axis);
    stroke-width: 1;
  }}
  .grid {{
    stroke: var(--grid);
    stroke-width: 1;
  }}
  .axis-label {{
    fill: #4f5d57;
    font-size: 16px;
    font-weight: 650;
  }}
  @media (max-width: 860px) {{
    main {{ padding: 20px 12px; }}
    .wrap {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<main>
  <h1>{html.escape(title)}</h1>
  <p>{sample_count} 份材料{f"，{marker_count} 个可用 SNP" if marker_count else ""}。可切换横轴和纵轴对应的主成分，也可以搜索材料名定位点位。</p>
  <div class="toolbar">
    <label>横轴 <select id="xpc">{x_options}</select></label>
    <label>纵轴 <select id="ypc">{y_options}</select></label>
    <label>搜索 <input id="search" placeholder="如 B73、NC352" /></label>
  </div>
  <div class="wrap">
    <section id="plotBox">
      <svg id="plot" viewBox="0 0 820 560" role="img" aria-label="{html.escape(title)}"></svg>
      {legend}
    </section>
    <aside class="side">
      <h2>结果摘要</h2>
      <div class="metric"><span>材料数</span><strong>{sample_count}</strong></div>
      <div class="metric"><span>可用标记</span><strong>{marker_count or "NA"}</strong></div>
      <div class="metric"><span>PC1 解释率</span><strong>{html.escape(pc1_label or "NA")}</strong></div>
      <div class="metric"><span>PC2 解释率</span><strong>{html.escape(pc2_label or "NA")}</strong></div>
      <div class="metric"><span>PC1+PC2</span><strong>{html.escape(pc12 or "NA")}</strong></div>
      <p style="margin-top:14px">鼠标悬停点位可查看材料名、群体标签和坐标。搜索框会高亮匹配材料。</p>
    </aside>
  </div>
</main>
<div class="tooltip" id="tip"></div>
<script>
const data = {data_json};
const labels = {labels_json};
const pcs = {pcs_json};
const svg = document.getElementById('plot');
const tip = document.getElementById('tip');
const xSel = document.getElementById('xpc');
const ySel = document.getElementById('ypc');
const search = document.getElementById('search');
const W = 820;
const H = 560;
const m = {{ l: 70, r: 24, t: 24, b: 62 }};
const iw = W - m.l - m.r;
const ih = H - m.t - m.b;

function valueOf(point, pc) {{
  return point[pc.toLowerCase()];
}}

function scale(domain, range) {{
  const [d0, d1] = domain;
  const [r0, r1] = range;
  return value => r0 + (value - d0) * (r1 - r0) / (d1 - d0 || 1);
}}

function paddedDomain(values) {{
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const pad = (hi - lo) * 0.08 || 0.01;
  return [lo - pad, hi + pad];
}}

function add(name, attrs, parent = svg) {{
  const el = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [key, value] of Object.entries(attrs)) {{
    el.setAttribute(key, value);
  }}
  parent.appendChild(el);
  return el;
}}

function normalizedAxes() {{
  if (xSel.value === ySel.value) {{
    ySel.value = xSel.value === pcs[0] ? pcs[1] : pcs[0];
  }}
  return [xSel.value, ySel.value];
}}

function render() {{
  const [xpc, ypc] = normalizedAxes();
  const xs = data.map(point => valueOf(point, xpc));
  const ys = data.map(point => valueOf(point, ypc));
  const x = scale(paddedDomain(xs), [m.l, m.l + iw]);
  const y = scale(paddedDomain(ys), [m.t + ih, m.t]);
  const query = search.value.trim().toLowerCase();

  svg.innerHTML = '';
  for (let i = 0; i <= 6; i++) {{
    const gx = m.l + iw * i / 6;
    const gy = m.t + ih * i / 6;
    add('line', {{ x1: gx, y1: m.t, x2: gx, y2: m.t + ih, class: 'grid' }});
    add('line', {{ x1: m.l, y1: gy, x2: m.l + iw, y2: gy, class: 'grid' }});
  }}

  add('line', {{ x1: m.l, y1: m.t + ih, x2: m.l + iw, y2: m.t + ih, class: 'axis' }});
  add('line', {{ x1: m.l, y1: m.t, x2: m.l, y2: m.t + ih, class: 'axis' }});
  add('text', {{ x: m.l + iw / 2, y: H - 18, 'text-anchor': 'middle', class: 'axis-label' }}).textContent =
    xpc + (labels[xpc] ? ' (' + labels[xpc] + ')' : '');
  add('text', {{
    x: 18,
    y: m.t + ih / 2,
    transform: 'rotate(-90 18 ' + (m.t + ih / 2) + ')',
    'text-anchor': 'middle',
    class: 'axis-label'
  }}).textContent = ypc + (labels[ypc] ? ' (' + labels[ypc] + ')' : '');

  for (const point of data) {{
    const isHit = query && point.id.toLowerCase().includes(query);
    const circle = add('circle', {{
      cx: x(valueOf(point, xpc)),
      cy: y(valueOf(point, ypc)),
      r: isHit ? 11 : 7.5,
      class: isHit ? 'point active' : 'point',
      fill: isHit ? '#d33f2f' : point.color
    }});
    circle.addEventListener('mousemove', event => {{
      tip.style.opacity = 1;
      tip.style.left = event.clientX + 'px';
      tip.style.top = event.clientY + 'px';
      tip.innerHTML = '<strong>' + point.id + '</strong><br>' +
        point.population + '<br>' +
        xpc + '=' + valueOf(point, xpc).toFixed(4) + ', ' +
        ypc + '=' + valueOf(point, ypc).toFixed(4);
    }});
    circle.addEventListener('mouseleave', () => {{
      tip.style.opacity = 0;
    }});
  }}
}}

xSel.addEventListener('change', render);
ySel.addEventListener('change', render);
search.addEventListener('input', render);
render();
</script>
</body>
</html>
"""


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Render breedcore PCA JSON as user-facing interactive HTML")
    parser.add_argument("result_json", help="breedcore PCA result JSON path")
    args = parser.parse_args()

    result = load_result(args.result_json)
    rendered = render_html(result, "群体基因型 PCA 分析图")
    sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
