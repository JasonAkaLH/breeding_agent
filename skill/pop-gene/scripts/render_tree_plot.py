#!/usr/bin/env python3
"""Render a breedstat2 phylogenetics/tree result as user-facing interactive HTML."""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    name: str = ""
    length: float = 0.0
    children: list["Node"] = field(default_factory=list)
    x: float = 0.0
    y: float = 0.0
    node_id: int = 0


def load_result(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


class NewickParser:
    def __init__(self, text: str):
        self.text = text.strip()
        self.i = 0

    def peek(self) -> str:
        return self.text[self.i] if self.i < len(self.text) else ""

    def consume(self, char: str | None = None) -> str:
        value = self.peek()
        if char and value != char:
            raise ValueError(f"Expected {char!r}, got {value!r}")
        self.i += 1
        return value

    def parse(self) -> Node:
        node = self.parse_node()
        if self.peek() == ";":
            self.consume(";")
        return node

    def parse_node(self) -> Node:
        node = Node()
        if self.peek() == "(":
            self.consume("(")
            while True:
                node.children.append(self.parse_node())
                if self.peek() == ",":
                    self.consume(",")
                    continue
                break
            self.consume(")")
        node.name = self.parse_name()
        if self.peek() == ":":
            self.consume(":")
            node.length = self.parse_length()
        return node

    def parse_name(self) -> str:
        if self.peek() in {"'", '"'}:
            quote = self.consume()
            chars = []
            while self.peek() and self.peek() != quote:
                chars.append(self.consume())
            if self.peek() == quote:
                self.consume(quote)
            return "".join(chars).strip()
        start = self.i
        while self.peek() and self.peek() not in ":,();":
            self.i += 1
        return self.text[start:self.i].strip()

    def parse_length(self) -> float:
        start = self.i
        while self.peek() and self.peek() not in ",();":
            self.i += 1
        raw = self.text[start:self.i].strip()
        try:
            return float(raw)
        except ValueError:
            return 0.0


def leaves(node: Node) -> list[Node]:
    if not node.children:
        return [node]
    out: list[Node] = []
    for child in node.children:
        out.extend(leaves(child))
    return out


def assign_positions(node: Node, depth: float, leaf_order: list[Node]) -> None:
    node.x = depth
    if not node.children:
        node.y = leaf_order.index(node)
        return
    for child in node.children:
        assign_positions(child, depth + max(child.length, 0.0), leaf_order)
    node.y = sum(child.y for child in node.children) / len(node.children)


def assign_node_ids(node: Node, start: int = 0) -> int:
    node.node_id = start
    next_id = start + 1
    for child in node.children:
        next_id = assign_node_ids(child, next_id)
    return next_id


def collect_nodes(node: Node) -> list[Node]:
    out = [node]
    for child in node.children:
        out.extend(collect_nodes(child))
    return out


def collect_edges(node: Node) -> list[tuple[Node, Node]]:
    edges = []
    for child in node.children:
        edges.append((node, child))
        edges.extend(collect_edges(child))
    return edges


def max_x(node: Node) -> float:
    return max([node.x] + [max_x(child) for child in node.children])


def tree_newick(result: dict[str, Any]) -> str:
    data = result.get("data", {})
    candidates = [
        data.get("newick") if isinstance(data, dict) else None,
        data.get("tree") if isinstance(data, dict) else None,
        result.get("newick"),
        result.get("tree"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise SystemExit("No Newick tree found at result.data.newick")


def summary_value(result: dict[str, Any], key: str, default: Any = None) -> Any:
    summary = result.get("summary", {})
    data = result.get("data", {})
    if isinstance(summary, dict) and summary.get(key) is not None:
        return summary.get(key)
    if isinstance(data, dict) and data.get(key) is not None:
        return data.get(key)
    return result.get(key, default)


def render_html(result: dict[str, Any]) -> str:
    newick = tree_newick(result)
    root = NewickParser(newick).parse()
    leaf_nodes = leaves(root)
    if len(leaf_nodes) < 2:
        raise SystemExit("Tree must contain at least two leaves")
    assign_positions(root, 0.0, leaf_nodes)
    assign_node_ids(root)

    method = str(summary_value(result, "method", "bionj")).upper()
    engine = str(summary_value(result, "engine", "ape"))
    sample_count = int(summary_value(result, "sample_count", len(leaf_nodes)) or len(leaf_nodes))
    xmax = max_x(root) or 1.0
    leaf_count = len(leaf_nodes)

    nodes_payload = []
    for node in collect_nodes(root):
        nodes_payload.append(
            {
                "id": node.node_id,
                "name": node.name or "",
                "depth": node.x,
                "order": node.y,
                "length": node.length,
                "leaf": not node.children,
            }
        )

    edges_payload = []
    for parent, child in collect_edges(root):
        edges_payload.append(
            {
                "parent": parent.node_id,
                "child": child.node_id,
                "length": child.length,
            }
        )

    payload = {
        "nodes": nodes_payload,
        "edges": edges_payload,
        "root": root.node_id,
        "leafCount": leaf_count,
        "xmax": xmax,
        "viewport": {"width": 1280, "height": 760},
    }
    data_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    compact_checked = "checked" if sample_count > 160 else ""

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>遗传聚类树分析图</title>
<style>
  :root {{
    --bg:#f6f7f4; --panel:#fff; --ink:#1f2722; --muted:#65716b; --line:#d9dfd9;
    --branch:#51645b; --leaf:#287d8e; --hit:#d1495b; --grid:#ebefeb;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-family:Arial,"Microsoft YaHei",sans-serif; }}
  main {{ max-width:1480px; margin:0 auto; padding:26px 22px 36px; }}
  h1 {{ margin:0 0 8px; font-size:28px; line-height:1.25; letter-spacing:0; }}
  h2 {{ margin:0; font-size:18px; line-height:1.35; }}
  p {{ margin:0; color:var(--muted); line-height:1.65; }}
  button, select, input {{ font-family:inherit; }}
  .intro {{ max-width:1040px; margin-bottom:14px; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 16px; }}
  .chip {{ display:inline-flex; gap:7px; align-items:baseline; padding:7px 10px; border:1px solid var(--line); border-radius:999px; background:#fff; font-size:13px; color:var(--muted); }}
  .chip strong {{ color:var(--ink); font-size:14px; }}
  .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:0 12px 26px rgba(31,39,34,.055); padding:15px; }}
  .panel-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:10px; }}
  .controls {{ display:flex; flex-wrap:wrap; align-items:center; gap:10px; color:var(--muted); font-size:13px; }}
  input[type="search"], select {{ height:34px; border:1px solid var(--line); border-radius:7px; background:#fff; color:var(--ink); padding:0 10px; font-size:14px; }}
  input[type="search"] {{ width:220px; }}
  label {{ display:inline-flex; align-items:center; gap:7px; white-space:nowrap; }}
  .icon-btn {{ height:34px; border:1px solid var(--line); border-radius:7px; background:#fff; color:var(--ink); padding:0 10px; font-size:13px; cursor:pointer; }}
  .icon-btn:hover {{ border-color:#b9c4bb; background:#fafbf9; }}
  .tree-wrap {{ position:relative; overflow:hidden; border:1px solid #edf0ed; border-radius:7px; background:#fff; height:min(76vh, 780px); min-height:520px; cursor:grab; }}
  .tree-wrap.dragging {{ cursor:grabbing; }}
  svg {{ display:block; width:100%; height:100%; user-select:none; touch-action:none; }}
  .branch {{ fill:none; stroke:var(--branch); stroke-width:1.35; stroke-linecap:round; opacity:.78; }}
  .leaf {{ fill:var(--leaf); opacity:.72; stroke:white; stroke-width:1.5; cursor:pointer; }}
  .leaf:hover {{ opacity:1; stroke:#1f2722; stroke-width:2.1; }}
  .leaf-label {{ fill:var(--ink); font-size:11.5px; dominant-baseline:middle; pointer-events:none; }}
  .compact .leaf-label {{ display:none; }}
  .leaf-label.hit-label {{ display:block; fill:var(--hit); font-weight:700; font-size:13px; }}
  .compact .leaf-label.radial-visible {{ display:block; }}
  .leaf.hit {{ fill:var(--hit); opacity:1; stroke:#8a2638; stroke-width:2.2; }}
  .axis-label {{ fill:var(--muted); font-size:12px; }}
  .scale-line {{ stroke:var(--muted); stroke-width:1.2; }}
  .scale-overlay {{ position:absolute; left:12px; bottom:10px; width:250px; height:34px; pointer-events:none; background:rgba(255,255,255,.88); border:1px solid #edf0ed; border-radius:7px; }}
  .zoom-help {{ position:absolute; right:12px; bottom:10px; color:var(--muted); font-size:12px; background:rgba(255,255,255,.86); border:1px solid #edf0ed; border-radius:7px; padding:6px 8px; pointer-events:none; }}
  .tip {{ position:fixed; z-index:10; pointer-events:none; transform:translate(10px,10px); padding:8px 10px; border-radius:6px; background:#1f2722; color:white; font-size:12px; line-height:1.45; opacity:0; white-space:nowrap; box-shadow:0 10px 22px rgba(0,0,0,.18); }}
  @media (max-width:720px) {{
    main {{ padding:20px 12px 30px; }}
    .panel-head {{ align-items:flex-start; flex-direction:column; }}
    .tree-wrap {{ height:70vh; min-height:460px; }}
  }}
</style>
</head>
<body>
<main>
  <h1>遗传聚类树分析图</h1>
  <p class="intro">基于遗传距离矩阵构建的系统发育/遗传聚类树，用于观察材料间的亲缘聚类关系。可在矩形树和环形树之间切换，支持鼠标滚轮缩放、拖拽平移和材料搜索高亮。</p>

  <div class="chips">
    <span class="chip">材料数 <strong>{sample_count}</strong></span>
    <span class="chip">方法 <strong>{html.escape(method)}</strong></span>
    <span class="chip">计算引擎 <strong>{html.escape(engine)}</strong></span>
    <span class="chip">叶节点 <strong>{leaf_count}</strong></span>
  </div>

  <section class="panel">
    <div class="panel-head">
      <h2>交互式遗传聚类树</h2>
      <div class="controls">
        <label for="layout">树形</label>
        <select id="layout">
          <option value="rect">矩形树</option>
          <option value="radial">环形树</option>
        </select>
        <input id="search" type="search" placeholder="搜索材料名" />
        <label><input id="compact" type="checkbox" {compact_checked} /> 紧凑模式</label>
        <label><input id="labels" type="checkbox" /> 显示全部标签</label>
        <button class="icon-btn" id="zoomIn" type="button">放大</button>
        <button class="icon-btn" id="zoomOut" type="button">缩小</button>
        <button class="icon-btn" id="resetView" type="button">重置视图</button>
      </div>
    </div>
    <div class="tree-wrap" id="treeWrap">
      <svg id="tree" viewBox="0 0 1280 760" role="img" aria-label="phylogenetic tree">
        <g id="zoomLayer"></g>
      </svg>
      <svg class="scale-overlay" id="scaleOverlay" viewBox="0 0 250 34" aria-hidden="true">
        <line class="scale-line" x1="12" y1="14" x2="112" y2="14"></line>
        <text class="axis-label" x="12" y="28">branch length scale</text>
      </svg>
      <div class="zoom-help">滚轮缩放，拖拽平移；搜索后匹配材料会高亮</div>
    </div>
  </section>
</main>
<div class="tip" id="tip"></div>
<script>
const payload = {data_json};
const tree = document.getElementById('tree');
const wrap = document.getElementById('treeWrap');
const zoomLayer = document.getElementById('zoomLayer');
const tip = document.getElementById('tip');
const nodes = new Map(payload.nodes.map(node => [node.id, node]));
const leaves = payload.nodes.filter(node => node.leaf);
const V = payload.viewport;
let transform = {{x:0, y:0, k:1}};
let dragging = false;
let dragStart = {{x:0, y:0, tx:0, ty:0}};

function esc(value) {{
  return String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}

function clamp(value, min, max) {{
  return Math.max(min, Math.min(max, value));
}}

function applyTransform() {{
  zoomLayer.setAttribute('transform', `translate(${{transform.x}} ${{transform.y}}) scale(${{transform.k}})`);
}}

function rectPoint(node) {{
  const rowH = Math.max(18, Math.min(26, 600 / Math.max(18, payload.leafCount)));
  const contentH = 56 + rowH * Math.max(1, payload.leafCount - 1);
  const left = 52, right = 260, top = 32;
  const plotW = V.width - left - right;
  return {{
    x: left + (Number(node.depth) / (payload.xmax || 1)) * plotW,
    y: top + Number(node.order) * rowH,
    contentH
  }};
}}

function radialPoint(node) {{
  const cx = V.width / 2;
  const cy = V.height / 2;
  const radiusMax = Math.min(V.width, V.height) * 0.40;
  const radiusMin = 16;
  const angle = -Math.PI / 2 + (Number(node.order) / Math.max(1, payload.leafCount)) * Math.PI * 2;
  const radius = radiusMin + (Number(node.depth) / (payload.xmax || 1)) * (radiusMax - radiusMin);
  return {{
    x: cx + Math.cos(angle) * radius,
    y: cy + Math.sin(angle) * radius,
    angle,
    radius,
    cx,
    cy
  }};
}}

function setInitialView() {{
  const layout = document.getElementById('layout').value;
  if (layout === 'rect') {{
    transform = {{x:0, y:18, k:1}};
  }} else {{
    transform = {{x:0, y:0, k:1}};
  }}
  applyTransform();
}}

function edgePath(edge, layout) {{
  const parent = nodes.get(edge.parent);
  const child = nodes.get(edge.child);
  if (layout === 'radial') {{
    const p = radialPoint(parent);
    const c = radialPoint(child);
    return `M ${{p.x.toFixed(2)}} ${{p.y.toFixed(2)}} L ${{c.x.toFixed(2)}} ${{c.y.toFixed(2)}}`;
  }}
  const p = rectPoint(parent);
  const c = rectPoint(child);
  return `M ${{p.x.toFixed(2)}} ${{p.y.toFixed(2)}} H ${{c.x.toFixed(2)}} V ${{c.y.toFixed(2)}}`;
}}

function labelAttrs(point, layout) {{
  if (layout !== 'radial') {{
    return {{x: point.x + 9, y: point.y + 4, anchor: 'start', rotate: ''}};
  }}
  const deg = point.angle * 180 / Math.PI;
  const flip = Math.cos(point.angle) < 0;
  const labelRadius = point.radius + 10;
  const x = point.cx + Math.cos(point.angle) * labelRadius;
  const y = point.cy + Math.sin(point.angle) * labelRadius;
  const rotation = flip ? deg + 180 : deg;
  return {{
    x,
    y,
    anchor: flip ? 'end' : 'start',
    rotate: ` transform="rotate(${{rotation.toFixed(2)}} ${{x.toFixed(2)}} ${{y.toFixed(2)}})"`
  }};
}}

function radialLabelStep() {{
  const base = payload.leafCount > 220 ? 10 : payload.leafCount > 140 ? 7 : payload.leafCount > 80 ? 5 : 3;
  const zoomBonus = Math.max(1, Math.floor(transform.k * 1.7));
  return Math.max(1, Math.ceil(base / zoomBonus));
}}

function labelFontSize(layout, hit=false) {{
  if (hit) return layout === 'radial' ? 12.5 : 13;
  if (layout !== 'radial') return 11.5;
  return clamp(12 / Math.sqrt(Math.max(0.65, transform.k)), 7.5, 11.5);
}}

function shouldShowLabel(leaf, index, layout, hit, labelsOn, compact) {{
  if (hit) return true;
  if (layout !== 'radial') return !compact || labelsOn;
  if (labelsOn) {{
    const step = Math.max(1, Math.ceil(radialLabelStep() / Math.max(1, Math.floor(transform.k))));
    return index % step === 0;
  }}
  return index % radialLabelStep() === 0;
}}

function drawTree(reset=false) {{
  const layout = document.getElementById('layout').value;
  const search = document.getElementById('search').value.trim().toLowerCase();
  const compact = document.getElementById('compact').checked && !document.getElementById('labels').checked;
  zoomLayer.classList.toggle('compact', compact);
  zoomLayer.innerHTML = '';
  for (const edge of payload.edges) {{
    zoomLayer.insertAdjacentHTML('beforeend', `<path class="branch" d="${{edgePath(edge, layout)}}" />`);
  }}
  const labelsOn = document.getElementById('labels').checked;
  for (const [index, leaf] of leaves.entries()) {{
    const point = layout === 'radial' ? radialPoint(leaf) : rectPoint(leaf);
    const hit = search && leaf.name.toLowerCase().includes(search);
    const klass = hit ? 'leaf hit' : 'leaf';
    const showLabel = shouldShowLabel(leaf, index, layout, hit, labelsOn, compact);
    const labelClass = hit ? 'leaf-label hit-label radial-visible' : (showLabel ? 'leaf-label radial-visible' : 'leaf-label');
    const label = labelAttrs(point, layout);
    const fontSize = labelFontSize(layout, hit);
    zoomLayer.insertAdjacentHTML('beforeend', `<circle class="${{klass}}" cx="${{point.x.toFixed(2)}}" cy="${{point.y.toFixed(2)}}" r="${{hit ? 6.8 : 4.2}}" data-tip="<strong>${{esc(leaf.name)}}</strong><br>branch=${{Number(leaf.length).toFixed(5)}}" />`);
    zoomLayer.insertAdjacentHTML('beforeend', `<text class="${{labelClass}}" style="font-size:${{fontSize.toFixed(1)}}px" x="${{label.x.toFixed(2)}}" y="${{(label.y + 4).toFixed(2)}}" text-anchor="${{label.anchor}}"${{label.rotate}}>${{esc(leaf.name)}}</text>`);
  }}
  if (reset) setInitialView();
  else applyTransform();
}}

function zoomAt(clientX, clientY, factor) {{
  const rect = tree.getBoundingClientRect();
  const x = (clientX - rect.left) / rect.width * V.width;
  const y = (clientY - rect.top) / rect.height * V.height;
  const nextK = clamp(transform.k * factor, 0.08, 9);
  const scale = nextK / transform.k;
  transform.x = x - (x - transform.x) * scale;
  transform.y = y - (y - transform.y) * scale;
  transform.k = nextK;
  applyTransform();
  if (document.getElementById('layout').value === 'radial') drawTree(false);
}}

tree.addEventListener('wheel', event => {{
  event.preventDefault();
  zoomAt(event.clientX, event.clientY, event.deltaY < 0 ? 1.16 : 0.86);
}}, {{passive:false}});

tree.addEventListener('pointerdown', event => {{
  dragging = true;
  wrap.classList.add('dragging');
  dragStart = {{x:event.clientX, y:event.clientY, tx:transform.x, ty:transform.y}};
  tree.setPointerCapture(event.pointerId);
}});

tree.addEventListener('pointermove', event => {{
  if (!dragging) return;
  const rect = tree.getBoundingClientRect();
  const sx = V.width / rect.width;
  const sy = V.height / rect.height;
  transform.x = dragStart.tx + (event.clientX - dragStart.x) * sx;
  transform.y = dragStart.ty + (event.clientY - dragStart.y) * sy;
  applyTransform();
}});

tree.addEventListener('pointerup', event => {{
  dragging = false;
  wrap.classList.remove('dragging');
  tree.releasePointerCapture(event.pointerId);
}});

tree.addEventListener('pointerleave', () => {{
  dragging = false;
  wrap.classList.remove('dragging');
}});

document.getElementById('layout').addEventListener('change', () => drawTree(true));
document.getElementById('search').addEventListener('input', () => drawTree(false));
document.getElementById('compact').addEventListener('change', () => drawTree(false));
document.getElementById('labels').addEventListener('change', () => drawTree(false));
document.getElementById('resetView').addEventListener('click', () => drawTree(true));
document.getElementById('zoomIn').addEventListener('click', () => zoomAt(window.innerWidth / 2, window.innerHeight / 2, 1.25));
document.getElementById('zoomOut').addEventListener('click', () => zoomAt(window.innerWidth / 2, window.innerHeight / 2, 0.8));

document.addEventListener('mousemove', event => {{
  const target = event.target.closest('[data-tip]');
  if (!target) {{
    tip.style.opacity = 0;
    return;
  }}
  tip.innerHTML = target.dataset.tip;
  tip.style.left = event.clientX + 'px';
  tip.style.top = event.clientY + 'px';
  tip.style.opacity = 1;
}});

drawTree(true);
</script>
</body>
</html>
"""


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Render breedstat2 Newick tree JSON as user-facing interactive HTML")
    parser.add_argument("result_json", help="breedstat2 phylogenetics/tree result JSON path")
    args = parser.parse_args()
    sys.stdout.write(render_html(load_result(args.result_json)))


if __name__ == "__main__":
    main()
