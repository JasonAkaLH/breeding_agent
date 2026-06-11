#!/usr/bin/env python3
"""Render a breedcore genetic-distance result as user-facing interactive HTML."""

from __future__ import annotations

import argparse
import html
import json
import sys
from typing import Any


POINT_PALETTE = ["#2f7d68", "#a64f3c", "#4f6fa8", "#b47a2b", "#6f5aa8", "#238194"]
VIRIDIS = ["#440154", "#414487", "#2a788e", "#22a884", "#7ad151", "#fde725"]


def load_result(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def matrix_payload(matrix: dict[str, Any]) -> tuple[list[str], list[list[float]]]:
    samples = matrix.get("samples", [])
    rows = matrix.get("matrix", [])
    if not samples or not rows:
        raise SystemExit("No distance matrix found at result.tables.distance_matrix")
    by_sample = {row.get("sample_id"): row.get("distances", {}) for row in rows}
    values = []
    for sample in samples:
        distances = by_sample.get(sample, {})
        values.append([float(distances.get(other, 0) or 0) for other in samples])
    return [str(s) for s in samples], values


def axis_label(explained: list[Any], index: int) -> str:
    label = f"Axis{index + 1}"
    if len(explained) > index and isinstance(explained[index], (int, float)):
        label += f" ({explained[index] * 100:.1f}%)"
    return label


def render_html(result: dict[str, Any]) -> str:
    tables = result.get("tables", {})
    summary = result.get("summary", {})
    samples, values = matrix_payload(tables.get("distance_matrix", {}))
    ordination = tables.get("ordination", [])
    similar_pairs = tables.get("similar_pairs", [])
    explained = summary.get("explained_variance_ratio") or []
    sample_count = summary.get("sample_count") or len(samples)
    method = summary.get("method", "ibs")
    pair_count = summary.get("pair_count") or len(similar_pairs)

    max_distance = max((value for row in values for value in row), default=1)
    if max_distance <= 0:
        max_distance = 1

    pair_distances = [
        pair["distance"] for pair in similar_pairs if isinstance(pair.get("distance"), (int, float))
    ]
    min_pair_distance = min(pair_distances, default=0)
    max_pair_distance = max(pair_distances, default=0)
    default_threshold = f"{max_pair_distance:.4f}" if max_pair_distance else ""
    pair_range = f"{min_pair_distance:.4f} - {max_pair_distance:.4f}" if similar_pairs else "NA"

    axis1 = axis_label(explained, 0)
    axis2 = axis_label(explained, 1)
    axis1_short = html.escape(axis1.split(" ", 1)[-1].strip("()") if "(" in axis1 else "NA")
    method_text = html.escape(str(method).upper())

    data_json = json.dumps(
        {
            "samples": samples,
            "matrix": values,
            "ordination": ordination,
            "pairs": similar_pairs,
            "explained": explained,
            "maxDistance": max_distance,
            "pointPalette": POINT_PALETTE,
            "viridis": VIRIDIS,
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>遗传距离与 PCoA 分析图</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #f7f7f4;
    --panel: #fff;
    --ink: #1f2722;
    --muted: #61706a;
    --line: #d7ded8;
    --grid: #e8ece9;
    --axis: #7f8b86;
    --accent-strong: #a64f3c;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: Arial, "Microsoft YaHei", sans-serif; }}
  main {{ max-width: 1500px; margin: 0 auto; padding: 28px 22px 38px; }}
  h1 {{ margin: 0 0 8px; font-size: 28px; line-height: 1.25; letter-spacing: 0; }}
  p {{ margin: 0 0 16px; color: var(--muted); line-height: 1.65; }}
  .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 12px 28px rgba(31,39,34,.06); padding: 16px; }}
  .analysis-grid {{ display: grid; grid-template-columns: minmax(0, 1fr) 520px; gap: 18px; align-items: start; }}
  .main-column {{ display: grid; gap: 18px; }}
  .matrix-section {{ margin-top: 18px; }}
  .panel-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 12px; }}
  h2 {{ margin: 0; font-size: 19px; line-height: 1.3; }}
  .meta {{ color: var(--muted); font-size: 13px; white-space: nowrap; }}
  .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 16px 0 18px; }}
  .stat {{ background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 11px 12px; }}
  .stat span {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 5px; }}
  .stat strong {{ font-size: 18px; }}
  svg {{ display: block; width: 100%; height: auto; }}
  .axis {{ stroke: var(--axis); stroke-width: 1; }}
  .grid-line {{ stroke: var(--grid); stroke-width: 1; }}
  .axis-label {{ fill: #4f5d57; font-size: 16px; font-weight: 650; }}
  .point {{ opacity: .78; stroke: white; stroke-width: 2; cursor: pointer; filter: drop-shadow(0 5px 8px rgba(31,39,34,.18)); }}
  .point:hover {{ opacity: 1; stroke: #1f2722; stroke-width: 2.6; }}
  .bar {{ fill: #4f6fa8; opacity: .82; }}
  .bar:hover {{ fill: var(--accent-strong); opacity: 1; }}
  .controls {{ display: grid; grid-template-columns: 1fr 120px; gap: 8px; margin-bottom: 10px; }}
  input {{ min-height: 36px; border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; background: #fff; color: var(--ink); font: inherit; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ border-bottom: 1px solid #edf0ed; padding: 8px 7px; text-align: left; white-space: nowrap; }}
  th {{ color: var(--muted); font-weight: 650; position: sticky; top: 0; background: #fff; z-index: 1; }}
  td:last-child, th:last-child {{ text-align: right; }}
  .pair-scroll {{ overflow: visible; }}
  .heatmap-wrap {{ width: 100%; overflow: auto; margin-top: 14px; }}
  #heatmap {{ display: block; width: min(100%, 1180px); height: auto; aspect-ratio: 1 / 1; margin: 0 auto; border: 1px solid #edf0ed; border-radius: 6px; background: #fff; }}
  .legend-row {{ display: flex; align-items: center; justify-content: center; gap: 10px; margin-top: 12px; color: var(--muted); font-size: 13px; }}
  .ramp {{ width: 280px; height: 12px; border-radius: 999px; background: linear-gradient(90deg, #440154, #414487, #2a788e, #22a884, #7ad151, #fde725); border: 1px solid #d7ded8; }}
  .tip {{ position: fixed; z-index: 10; pointer-events: none; transform: translate(10px,10px); padding: 8px 10px; border-radius: 6px; background: #1f2722; color: white; font-size: 12px; line-height: 1.45; opacity: 0; white-space: nowrap; }}
  @media (max-width: 980px) {{
    main {{ padding: 20px 12px; }}
    .analysis-grid, .stats {{ grid-template-columns: 1fr; }}
    .panel-head {{ align-items: flex-start; flex-direction: column; }}
    .controls {{ grid-template-columns: 1fr; }}
    #heatmap {{ width: 980px; }}
  }}
</style>
</head>
<body>
<main>
  <h1>遗传距离与 PCoA 分析图</h1>
  <p>{sample_count} 份材料，距离方法：{method_text}。本阶段聚焦两两距离、近重复材料、二维距离结构和距离分布；聚类树移入单独的遗传聚类/系统发育树分析阶段展示。</p>

  <section class="stats" aria-label="summary statistics">
    <div class="stat"><span>材料数</span><strong>{sample_count}</strong></div>
    <div class="stat"><span>最近材料对</span><strong>Top {pair_count}</strong></div>
    <div class="stat"><span>PCoA Axis1</span><strong>{axis1_short}</strong></div>
    <div class="stat"><span>Top 距离范围</span><strong>{html.escape(pair_range)}</strong></div>
  </section>

  <div class="analysis-grid">
    <div class="main-column">
      <section class="panel">
        <div class="panel-head">
          <h2>PCoA 二维分布</h2>
          <div class="meta">{html.escape(axis1)} / {html.escape(axis2)}</div>
        </div>
        <svg id="ordination" viewBox="0 0 880 590" role="img" aria-label="PCoA ordination plot"></svg>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>距离分布</h2>
          <div class="meta">全矩阵上三角</div>
        </div>
        <svg id="histogram" viewBox="0 0 880 440" role="img" aria-label="distance histogram"></svg>
      </section>
    </div>

    <section class="panel">
      <div class="panel-head">
        <h2>最近材料对</h2>
        <div class="meta">可搜索/按距离过滤</div>
      </div>
      <div class="controls">
        <input id="pairSearch" placeholder="搜索材料名，如 B73" />
        <input id="maxDistance" type="number" step="0.001" min="0" value="{html.escape(default_threshold)}" title="最大距离" />
      </div>
      <div class="pair-scroll">
        <table>
          <thead><tr><th>#</th><th>材料 1</th><th>材料 2</th><th>距离</th></tr></thead>
          <tbody id="pairBody"></tbody>
        </table>
      </div>
    </section>
  </div>

  <section class="panel matrix-section">
    <div class="panel-head">
      <h2>距离矩阵热图（viridis 配色）</h2>
      <div class="meta">{sample_count} × {sample_count} matrix</div>
    </div>
    <div class="heatmap-wrap">
      <canvas id="heatmap" width="1180" height="1180" aria-label="distance matrix heatmap"></canvas>
    </div>
    <div class="legend-row">
      <span>近</span>
      <span class="ramp"></span>
      <span>远</span>
    </div>
  </section>
</main>
<div class="tip" id="tip"></div>
<script>
const payload = {data_json};
const tip = document.getElementById('tip');
const pairBody = document.getElementById('pairBody');
const pairSearch = document.getElementById('pairSearch');
const maxDistance = document.getElementById('maxDistance');
const heatmap = document.getElementById('heatmap');
const heatCtx = heatmap.getContext('2d');

function showTip(event, content) {{
  tip.innerHTML = content;
  tip.style.left = event.clientX + 'px';
  tip.style.top = event.clientY + 'px';
  tip.style.opacity = 1;
}}
function hideTip() {{ tip.style.opacity = 0; }}
function extent(rows, key) {{
  const vals = rows.map(row => Number(row[key])).filter(Number.isFinite);
  return [Math.min(...vals), Math.max(...vals)];
}}
function scale(domain, range) {{
  const [d0, d1] = domain;
  const [r0, r1] = range;
  return value => r0 + (value - d0) * (r1 - r0) / (d1 - d0 || 1);
}}
function addSvg(svg, name, attrs) {{
  const el = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  svg.appendChild(el);
  return el;
}}
function drawOrdination() {{
  const rows = payload.ordination || [];
  const svg = document.getElementById('ordination');
  const W = 880, H = 590;
  const m = {{ l: 82, r: 24, t: 26, b: 70 }};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  svg.innerHTML = '';
  if (!rows.length) {{
    addSvg(svg, 'text', {{ x: 30, y: 60, class: 'axis-label' }}).textContent = '当前结果未包含 PCoA 坐标';
    return;
  }}
  const [x0, x1] = extent(rows, 'Axis1');
  const [y0, y1] = extent(rows, 'Axis2');
  const sx = scale([x0 - (x1 - x0 || 1) * 0.08, x1 + (x1 - x0 || 1) * 0.08], [m.l, m.l + iw]);
  const sy = scale([y0 - (y1 - y0 || 1) * 0.08, y1 + (y1 - y0 || 1) * 0.08], [m.t + ih, m.t]);
  for (let i = 0; i <= 6; i++) {{
    const x = m.l + iw * i / 6;
    const y = m.t + ih * i / 6;
    addSvg(svg, 'line', {{ x1: x, y1: m.t, x2: x, y2: m.t + ih, class: 'grid-line' }});
    addSvg(svg, 'line', {{ x1: m.l, y1: y, x2: m.l + iw, y2: y, class: 'grid-line' }});
  }}
  addSvg(svg, 'line', {{ x1: m.l, y1: m.t + ih, x2: m.l + iw, y2: m.t + ih, class: 'axis' }});
  addSvg(svg, 'line', {{ x1: m.l, y1: m.t, x2: m.l, y2: m.t + ih, class: 'axis' }});
  rows.forEach((row, index) => {{
    const color = payload.pointPalette[index % payload.pointPalette.length];
    const sample = String(row.sample_id || '');
    const axis1 = Number(row.Axis1);
    const axis2 = Number(row.Axis2);
    addSvg(svg, 'circle', {{
      cx: sx(axis1), cy: sy(axis2), r: 8.8, fill: color, class: 'point',
      'data-tip': '<strong>' + sample + '</strong><br>Axis1=' + axis1.toFixed(4) + '<br>Axis2=' + axis2.toFixed(4)
    }});
  }});
  addSvg(svg, 'text', {{ x: W / 2, y: H - 18, 'text-anchor': 'middle', class: 'axis-label' }}).textContent = '{html.escape(axis1)}';
  addSvg(svg, 'text', {{ x: 20, y: H / 2, 'text-anchor': 'middle', transform: 'rotate(-90 20 ' + H / 2 + ')', class: 'axis-label' }}).textContent = '{html.escape(axis2)}';
}}
function filteredPairs() {{
  const q = pairSearch.value.trim().toLowerCase();
  const max = Number(maxDistance.value);
  return payload.pairs.filter(pair => {{
    const s1 = String(pair.sample_1 || '');
    const s2 = String(pair.sample_2 || '');
    const d = Number(pair.distance);
    return (!q || s1.toLowerCase().includes(q) || s2.toLowerCase().includes(q)) &&
      (!Number.isFinite(max) || d <= max);
  }});
}}
function renderPairTable() {{
  const rows = filteredPairs();
  pairBody.innerHTML = '';
  if (!rows.length) {{
    pairBody.innerHTML = '<tr><td colspan="4">没有符合条件的材料对</td></tr>';
    return;
  }}
  rows.forEach((pair, index) => {{
    const tr = document.createElement('tr');
    const d = Number(pair.distance);
    tr.innerHTML = '<td>' + (index + 1) + '</td><td>' + pair.sample_1 + '</td><td>' + pair.sample_2 + '</td><td>' + d.toFixed(4) + '</td>';
    tr.dataset.tip = '<strong>' + pair.sample_1 + ' × ' + pair.sample_2 + '</strong><br>distance=' + d.toFixed(4);
    pairBody.appendChild(tr);
  }});
}}
function allUpperDistances() {{
  const vals = [];
  for (let i = 0; i < payload.matrix.length; i++) {{
    for (let j = i + 1; j < payload.matrix[i].length; j++) vals.push(Number(payload.matrix[i][j] || 0));
  }}
  return vals;
}}
function drawHistogram() {{
  const svg = document.getElementById('histogram');
  const W = 880, H = 440;
  const m = {{ l: 68, r: 22, t: 24, b: 62 }};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  svg.innerHTML = '';
  const vals = allUpperDistances();
  const minV = Math.min(...vals), maxV = Math.max(...vals);
  const bins = 28;
  const counts = Array.from({{ length: bins }}, () => 0);
  vals.forEach(value => {{
    const idx = Math.min(bins - 1, Math.floor((value - minV) / (maxV - minV || 1) * bins));
    counts[idx] += 1;
  }});
  const maxCount = Math.max(...counts);
  for (let i = 0; i <= 5; i++) {{
    const y = m.t + ih * i / 5;
    addSvg(svg, 'line', {{ x1: m.l, y1: y, x2: m.l + iw, y2: y, class: 'grid-line' }});
  }}
  addSvg(svg, 'line', {{ x1: m.l, y1: m.t + ih, x2: m.l + iw, y2: m.t + ih, class: 'axis' }});
  addSvg(svg, 'line', {{ x1: m.l, y1: m.t, x2: m.l, y2: m.t + ih, class: 'axis' }});
  counts.forEach((count, i) => {{
    const x = m.l + iw * i / bins;
    const w = iw / bins - 2;
    const h = ih * count / (maxCount || 1);
    const lo = minV + (maxV - minV) * i / bins;
    const hi = minV + (maxV - minV) * (i + 1) / bins;
    addSvg(svg, 'rect', {{
      x, y: m.t + ih - h, width: w, height: h, class: 'bar',
      'data-tip': '<strong>距离区间</strong><br>' + lo.toFixed(3) + ' - ' + hi.toFixed(3) + '<br>材料对=' + count
    }});
  }});
  addSvg(svg, 'text', {{ x: W / 2, y: H - 14, 'text-anchor': 'middle', class: 'axis-label' }}).textContent = 'IBS distance';
  addSvg(svg, 'text', {{ x: 18, y: H / 2, 'text-anchor': 'middle', transform: 'rotate(-90 18 ' + H / 2 + ')', class: 'axis-label' }}).textContent = '材料对数量';
}}
function viridisColor(value) {{
  const t = Math.max(0, Math.min(1, value / payload.maxDistance));
  const stops = [[68,1,84],[65,68,135],[42,120,142],[34,168,132],[122,209,81],[253,231,37]];
  const scaled = t * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(scaled));
  const f = scaled - i;
  const a = stops[i], b = stops[i + 1];
  return 'rgb(' + Math.round(a[0] + (b[0]-a[0])*f) + ',' + Math.round(a[1] + (b[1]-a[1])*f) + ',' + Math.round(a[2] + (b[2]-a[2])*f) + ')';
}}
function drawHeatmap() {{
  const n = payload.samples.length;
  const W = heatmap.width, H = heatmap.height;
  const cell = Math.floor(Math.min(W, H) / n);
  const size = cell * n;
  const left = Math.floor((W - size) / 2);
  const top = Math.floor((H - size) / 2);
  heatCtx.clearRect(0, 0, W, H);
  heatCtx.fillStyle = '#fff';
  heatCtx.fillRect(0, 0, W, H);
  for (let i = 0; i < n; i++) {{
    for (let j = 0; j < n; j++) {{
      heatCtx.fillStyle = viridisColor(Number(payload.matrix[i][j] || 0));
      heatCtx.fillRect(left + j * cell, top + i * cell, cell, cell);
    }}
  }}
}}
function heatmapHit(event) {{
  const rect = heatmap.getBoundingClientRect();
  const n = payload.samples.length;
  const scaleX = heatmap.width / rect.width;
  const scaleY = heatmap.height / rect.height;
  const x = (event.clientX - rect.left) * scaleX;
  const y = (event.clientY - rect.top) * scaleY;
  const cell = Math.floor(Math.min(heatmap.width, heatmap.height) / n);
  const size = cell * n;
  const left = Math.floor((heatmap.width - size) / 2);
  const top = Math.floor((heatmap.height - size) / 2);
  const col = Math.floor((x - left) / cell);
  const row = Math.floor((y - top) / cell);
  if (row < 0 || col < 0 || row >= n || col >= n) {{
    hideTip();
    return;
  }}
  const distance = Number(payload.matrix[row][col] || 0);
  showTip(event, '<strong>' + payload.samples[row] + ' × ' + payload.samples[col] + '</strong><br>distance=' + distance.toFixed(4));
}}
pairSearch.addEventListener('input', renderPairTable);
maxDistance.addEventListener('input', renderPairTable);
heatmap.addEventListener('mousemove', heatmapHit);
heatmap.addEventListener('mouseleave', hideTip);
document.addEventListener('mousemove', event => {{
  const target = event.target.closest('[data-tip]');
  if (target && target !== heatmap) showTip(event, target.dataset.tip);
}});
document.addEventListener('mouseleave', hideTip);
drawOrdination();
renderPairTable();
drawHistogram();
drawHeatmap();
</script>
</body>
</html>
"""


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Render breedcore genetic-distance JSON as user-facing interactive HTML")
    parser.add_argument("result_json", help="breedcore genetic-distance result JSON path")
    args = parser.parse_args()
    sys.stdout.write(render_html(load_result(args.result_json)))


if __name__ == "__main__":
    main()
