#!/usr/bin/env python3
"""Render a breedcore ADMIXTURE result as user-facing interactive HTML."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any


PALETTE = [
    "#287D8E",
    "#FDE725",
    "#7AD151",
    "#414487",
    "#F8961E",
    "#35B779",
    "#440154",
    "#B5DE2B",
    "#21908C",
    "#D1495B",
    "#5E4FA2",
    "#F9C74F",
]


def load_result(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def q_sort_key(name: str) -> tuple[int, str]:
    match = re.match(r"Q(\d+)$", name)
    if match:
        return (int(match.group(1)), name)
    return (10_000, name)


def find_workspace_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "BrAPI").exists() and (parent / ".codex").exists():
            return parent
    return Path.cwd()


def resolve_work_path(path_value: Any, workspace_root: Path) -> Path | None:
    if not path_value:
        return None
    raw = str(path_value).replace("\\", "/")
    direct = Path(raw)
    if direct.exists():
        return direct
    if raw.startswith("/work/runs/"):
        mapped = workspace_root / "BrAPI" / "runtime" / "breedcore" / "runs" / raw[len("/work/runs/") :]
        return mapped if mapped.exists() else mapped
    if raw.startswith("/work/uploads/"):
        mapped = workspace_root / "BrAPI" / "runtime" / "breedcore" / "uploads" / raw[len("/work/uploads/") :]
        return mapped if mapped.exists() else mapped
    return direct if direct.exists() else None


def read_fam_ids(fam_path: Path) -> list[str]:
    sample_ids: list[str] = []
    with open(fam_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            parts = line.strip().split()
            if not parts:
                continue
            sample_ids.append(parts[1] if len(parts) > 1 else parts[0])
    return sample_ids


def read_q_matrix(q_path: Path) -> list[list[float]]:
    rows: list[list[float]] = []
    with open(q_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            parts = line.strip().split()
            if parts:
                rows.append([float(value) for value in parts])
    return rows


def build_ancestry_rows(
    sample_ids: list[str],
    q_matrix: list[list[float]],
    mixed_threshold: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    row_count = min(len(sample_ids), len(q_matrix))
    if row_count == 0:
        return [], []
    k = len(q_matrix[0])
    q_cols = [f"Q{i + 1}" for i in range(k)]
    rows: list[dict[str, Any]] = []
    for sample_id, q_values in zip(sample_ids[:row_count], q_matrix[:row_count]):
        values = [float(value) for value in q_values[:k]]
        max_index = max(range(len(values)), key=lambda idx: values[idx])
        rows.append(
            {
                "id": str(sample_id),
                "population": None,
                "assignment": q_cols[max_index],
                "assignmentIndex": max_index,
                "max_q": values[max_index],
                "is_mixed": values[max_index] < mixed_threshold,
                "q": values,
            }
        )
    rows.sort(key=lambda row: (row["assignmentIndex"], -float(row["max_q"]), row["id"]))
    return rows, q_cols


def build_ancestry_from_table(
    ancestry_rows: list[dict[str, Any]],
    mixed_threshold: float,
    *,
    source: str = "result.tables.structure_barplot",
) -> tuple[int | None, dict[str, Any] | None]:
    if not ancestry_rows:
        return None, None
    q_cols = sorted(
        [key for key in ancestry_rows[0].keys() if re.match(r"Q\d+$", key)],
        key=q_sort_key,
    )
    if not q_cols:
        return None, None
    samples = []
    for row in ancestry_rows:
        values = [float(row.get(col, 0) or 0) for col in q_cols]
        assignment = row.get("assignment")
        if not assignment or assignment not in q_cols:
            assignment = q_cols[max(range(len(values)), key=lambda idx: values[idx])]
        assignment_index = q_cols.index(str(assignment))
        max_q = float(row.get("max_q") if row.get("max_q") is not None else max(values))
        samples.append(
            {
                "id": str(row.get("sample_id", "")),
                "population": row.get("population"),
                "assignment": assignment,
                "assignmentIndex": assignment_index,
                "max_q": max_q,
                "is_mixed": bool(row.get("is_mixed", max_q < mixed_threshold)),
                "q": values,
            }
        )
    samples.sort(key=lambda row: (row["assignmentIndex"], -float(row["max_q"]), row["id"]))
    return len(q_cols), {
        "k": len(q_cols),
        "qCols": q_cols,
        "samples": samples,
        "mixedCount": sum(1 for row in samples if row["is_mixed"]),
        "source": source,
    }


def available_k_sets(result: dict[str, Any], result_json_path: Path) -> dict[str, dict[str, Any]]:
    workspace_root = find_workspace_root()
    summary = result.get("summary", {})
    tables = result.get("tables", {})
    files = result.get("files", {})
    mixed_threshold = float(summary.get("mixed_threshold", 0.8) or 0.8)
    k_sets: dict[str, dict[str, Any]] = {}

    structure_by_k = tables.get("structure_by_k")
    if isinstance(structure_by_k, dict):
        for k_value, rows in sorted(structure_by_k.items(), key=lambda item: int(item[0])):
            if not isinstance(rows, list):
                continue
            table_k, table_set = build_ancestry_from_table(
                rows,
                mixed_threshold,
                source=f"result.tables.structure_by_k.{k_value}",
            )
            if table_k is not None and table_set is not None:
                k_sets[str(k_value)] = table_set

    q_files = files.get("q_files") or {}
    for k_value, q_file in sorted(q_files.items(), key=lambda item: int(item[0])):
        q_path = resolve_work_path(q_file, workspace_root)
        if not q_path or not q_path.exists():
            continue
        fam_path = resolve_work_path(files.get("plink_fam"), workspace_root) or q_path.with_name("plink.fam")
        if not fam_path.exists():
            continue
        sample_ids = read_fam_ids(fam_path)
        q_matrix = read_q_matrix(q_path)
        rows, q_cols = build_ancestry_rows(sample_ids, q_matrix, mixed_threshold)
        if rows:
            k_sets[str(k_value)] = {
                "k": int(k_value),
                "qCols": q_cols,
                "samples": rows,
                "mixedCount": sum(1 for row in rows if row["is_mixed"]),
                "source": str(q_path),
            }

    table_k, table_set = build_ancestry_from_table(tables.get("structure_barplot", []), mixed_threshold)
    if table_k is not None and table_set is not None:
        k_sets.setdefault(str(table_k), table_set)

    if not k_sets:
        raise SystemExit(
            "No ADMIXTURE ancestry data found. Expected result.files.q_files with local Q files, "
            "or result.tables.structure_barplot."
        )

    return k_sets


def cv_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in result.get("tables", {}).get("cv_error", []):
        if row.get("k") is None or row.get("cv_error") is None:
            continue
        rows.append({"k": int(row["k"]), "cv_error": float(row["cv_error"])})
    rows.sort(key=lambda row: row["k"])
    previous: float | None = None
    for row in rows:
        row["delta"] = None if previous is None else previous - row["cv_error"]
        previous = row["cv_error"]
    return rows


def first_plateau_k(rows: list[dict[str, Any]], threshold: float = 0.001) -> int | None:
    for prev, cur in zip(rows, rows[1:]):
        improvement = float(prev["cv_error"]) - float(cur["cv_error"])
        if improvement <= threshold:
            return int(prev["k"])
    return None


def min_cv_k(rows: list[dict[str, Any]]) -> int | None:
    if not rows:
        return None
    return int(min(rows, key=lambda row: float(row["cv_error"]))["k"])


def choose_default_k(result: dict[str, Any], rows: list[dict[str, Any]], k_sets: dict[str, dict[str, Any]]) -> int:
    summary = result.get("summary", {})
    candidates = [
        first_plateau_k(rows),
        summary.get("recommended_k"),
        summary.get("best_k_min_cv"),
        min_cv_k(rows),
    ]
    for candidate in candidates:
        if candidate is not None and str(int(candidate)) in k_sets:
            return int(candidate)
    return int(sorted(k_sets.keys(), key=int)[-1])


def fmt_num(value: Any, digits: int = 5) -> str:
    if value is None:
        return "NA"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def render_html(result: dict[str, Any], result_json_path: Path) -> str:
    summary = result.get("summary", {})
    cv = cv_rows(result)
    k_sets = available_k_sets(result, result_json_path)
    default_k = choose_default_k(result, cv, k_sets)
    elbow_k = first_plateau_k(cv)
    best_min_cv_k = summary.get("best_k_min_cv") or min_cv_k(cv)
    recommended_k = summary.get("recommended_k") or best_min_cv_k
    sample_count = int(summary.get("sample_count") or len(next(iter(k_sets.values()))["samples"]))
    mixed_threshold = summary.get("mixed_threshold", 0.8)
    k_values = sorted([int(k) for k in k_sets.keys()])
    k_range_text = f"K={min(k_values)}..{max(k_values)}" if k_values else "K=NA"
    default_set = k_sets[str(default_k)]

    cv_lines = []
    for row in cv:
        delta = row.get("delta")
        delta_text = "起始值" if delta is None else f"较上一 K 降低 {float(delta):.5f}"
        mark = []
        if best_min_cv_k is not None and int(row["k"]) == int(best_min_cv_k):
            mark.append("最低 CV")
        if elbow_k is not None and int(row["k"]) == int(elbow_k):
            mark.append("拐点参考")
        cv_lines.append(
            "<tr>"
            f"<td>K={int(row['k'])}</td>"
            f"<td>{fmt_num(row['cv_error'])}</td>"
            f"<td>{html.escape(delta_text)}</td>"
            f"<td>{html.escape(' / '.join(mark))}</td>"
            "</tr>"
        )

    k_options = "".join(
        f'<option value="{k}" {"selected" if k == default_k else ""}>K={k}</option>' for k in k_values
    )
    summary_note = (
        "默认展示采用通用拐点启发：当相邻 K 的 CV error 改善已经很小，优先展示拐点 K；"
        "同时保留最低 CV K 作为统计参考。不同数据集可直接在页面中切换 K。"
        if elbow_k is not None
        else "当前结果未检测到明显 CV 平台期，默认展示服务推荐或最低 CV 的 K；不同数据集可直接在页面中切换 K。"
    )

    payload = {
        "cv": cv,
        "kSets": k_sets,
        "kValues": k_values,
        "defaultK": default_k,
        "recommendedK": recommended_k,
        "minCvK": best_min_cv_k,
        "elbowK": elbow_k,
        "sampleCount": sample_count,
        "mixedThreshold": mixed_threshold,
        "palette": PALETTE,
    }
    data_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ADMIXTURE 群体结构分析图</title>
<style>
  :root {{
    --bg:#f6f7f4; --panel:#fff; --ink:#1f2722; --muted:#65716b;
    --line:#d9dfd9; --grid:#ebefeb; --axis:#87928d; --accent:#287d8e; --warn:#d1495b;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-family:Arial,"Microsoft YaHei",sans-serif; }}
  main {{ max-width:1480px; margin:0 auto; padding:26px 22px 36px; }}
  h1 {{ margin:0 0 8px; font-size:28px; line-height:1.25; letter-spacing:0; }}
  h2 {{ margin:0; font-size:18px; line-height:1.35; }}
  p {{ margin:0; color:var(--muted); line-height:1.65; }}
  svg {{ display:block; width:100%; }}
  .intro {{ max-width:980px; margin-bottom:14px; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 18px; }}
  .chip {{ display:inline-flex; gap:7px; align-items:baseline; padding:7px 10px; border:1px solid var(--line); border-radius:999px; background:#fff; font-size:13px; color:var(--muted); }}
  .chip strong {{ color:var(--ink); font-size:14px; }}
  .top-grid {{ display:grid; grid-template-columns:minmax(0,1.55fr) minmax(340px,.75fr); gap:16px; align-items:stretch; }}
  .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:0 12px 26px rgba(31,39,34,.055); padding:15px; }}
  .panel-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:10px; }}
  .meta {{ color:var(--muted); font-size:13px; white-space:nowrap; }}
  .control-row {{ display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin:10px 0 6px; }}
  label {{ color:var(--muted); font-size:13px; }}
  select, input {{ height:34px; border:1px solid var(--line); border-radius:7px; background:#fff; color:var(--ink); padding:0 10px; font-size:14px; }}
  input {{ min-width:210px; }}
  .note {{ margin-top:10px; color:var(--muted); font-size:13px; line-height:1.6; }}
  .metrics {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:8px 0 10px; }}
  .metric {{ border:1px solid #edf0ed; border-radius:7px; padding:10px; min-height:62px; }}
  .metric span {{ display:block; color:var(--muted); font-size:12px; margin-bottom:4px; }}
  .metric strong {{ font-size:22px; line-height:1; }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; margin-top:10px; }}
  th, td {{ border-bottom:1px solid #edf0ed; padding:6px 5px; text-align:left; vertical-align:top; }}
  th {{ color:var(--muted); font-weight:650; }}
  .bar-panel {{ margin-top:16px; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:9px 14px; margin-top:10px; font-size:13px; color:var(--muted); }}
  .legend-item {{ display:inline-flex; align-items:center; gap:7px; }}
  .legend-item i {{ width:11px; height:11px; border-radius:3px; opacity:.9; }}
  .tip {{ position:fixed; z-index:10; pointer-events:none; transform:translate(10px,10px); padding:8px 10px; border-radius:6px; background:#1f2722; color:white; font-size:12px; line-height:1.45; opacity:0; white-space:nowrap; box-shadow:0 10px 22px rgba(0,0,0,.18); }}
  .axis {{ stroke:var(--axis); stroke-width:1; }}
  .grid-line {{ stroke:var(--grid); stroke-width:1; }}
  .label {{ fill:var(--muted); font-size:12px; }}
  .axis-label {{ fill:#4f5d57; font-size:14px; font-weight:650; }}
  .cv-dot {{ cursor:pointer; stroke:#fff; stroke-width:2; }}
  .bar-seg {{ opacity:.84; stroke:rgba(255,255,255,.72); stroke-width:.35; cursor:pointer; }}
  .bar-seg:hover {{ opacity:1; stroke:#1f2722; stroke-width:1.05; }}
  .sample-hit {{ stroke:#d1495b; stroke-width:1.4; opacity:1; }}
  .group-line {{ stroke:#39423e; stroke-width:1; stroke-dasharray:3 4; opacity:.45; }}
  @media (max-width:980px) {{
    main {{ padding:20px 12px 30px; }}
    .top-grid {{ grid-template-columns:1fr; }}
    .metrics {{ grid-template-columns:1fr 1fr; }}
  }}
  @media (max-width:620px) {{
    .metrics {{ grid-template-columns:1fr; }}
    .panel-head {{ align-items:flex-start; flex-direction:column; }}
  }}
</style>
</head>
<body>
<main>
  <h1>ADMIXTURE 群体结构分析图</h1>
  <p class="intro">该图用于通用 ADMIXTURE 结果展示：上方比较不同 K 的 CV error 和选 K 依据，下方展示当前 K 的排序群体结构柱状图。柱状图按每个材料的主导 Q 组分分组，并在组内按主导组分比例从高到低排序。</p>

  <div class="chips">
    <span class="chip">材料数 <strong>{sample_count}</strong></span>
    <span class="chip">扫描范围 <strong>{html.escape(k_range_text)}</strong></span>
    <span class="chip">默认展示 K <strong id="chipCurrentK">{default_k}</strong></span>
    <span class="chip">最低 CV K <strong>{html.escape(str(best_min_cv_k or "NA"))}</strong></span>
    <span class="chip">拐点参考 K <strong>{html.escape(str(elbow_k or "NA"))}</strong></span>
  </div>

  <div class="top-grid">
    <section class="panel">
      <div class="panel-head">
        <h2>CV Error 折线图</h2>
        <div class="meta">点选折线上的 K 可切换下方柱状图</div>
      </div>
      <svg id="cvPlot" viewBox="0 0 920 330" role="img" aria-label="CV error line plot"></svg>
    </section>

    <aside class="panel">
      <div class="panel-head">
        <h2>K 值与当前展示</h2>
        <div class="meta">通用绘图设置</div>
      </div>
      <div class="metrics">
        <div class="metric"><span>当前 K</span><strong id="metricK">{default_k}</strong></div>
        <div class="metric"><span>混合材料数</span><strong id="metricMixed">{default_set["mixedCount"]}</strong></div>
        <div class="metric"><span>最低 CV K</span><strong>{html.escape(str(best_min_cv_k or "NA"))}</strong></div>
        <div class="metric"><span>混合阈值</span><strong>{html.escape(str(mixed_threshold))}</strong></div>
      </div>
      <div class="control-row">
        <label for="kSelect">展示 K</label>
        <select id="kSelect">{k_options}</select>
        <label for="sampleSearch">搜索材料</label>
        <input id="sampleSearch" placeholder="输入材料名高亮" />
      </div>
      <p class="note">{html.escape(summary_note)}</p>
      <table>
        <thead><tr><th>K</th><th>CV error</th><th>变化</th><th>标记</th></tr></thead>
        <tbody>{"".join(cv_lines)}</tbody>
      </table>
    </aside>
  </div>

  <section class="panel bar-panel">
    <div class="panel-head">
      <h2>排序后的群体结构组成图</h2>
      <div class="meta" id="barMeta">当前展示：K={default_k}</div>
    </div>
    <svg id="barPlot" viewBox="0 0 1380 520" role="img" aria-label="ADMIXTURE sorted structure barplot"></svg>
    <div class="legend" id="legend"></div>
  </section>
</main>
<div class="tip" id="tip"></div>
<script>
const payload = {data_json};
const tip = document.getElementById('tip');
let currentK = String(payload.defaultK);

function esc(value) {{
  return String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}

function showTip(event, body) {{
  tip.innerHTML = body;
  tip.style.left = event.clientX + 'px';
  tip.style.top = event.clientY + 'px';
  tip.style.opacity = 1;
}}

function hideTip() {{
  tip.style.opacity = 0;
}}

function drawCv() {{
  const svg = document.getElementById('cvPlot');
  const rows = payload.cv || [];
  const W = 920, H = 330, m = {{l:72,r:34,t:28,b:58}}, iw = W-m.l-m.r, ih = H-m.t-m.b;
  svg.innerHTML = '';
  if (!rows.length) return;
  const xs = rows.map(d => Number(d.k));
  const ys = rows.map(d => Number(d.cv_error));
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const yPad = (maxY - minY || 0.01) * 0.12;
  const sx = x => m.l + (maxX === minX ? iw / 2 : (x - minX) / (maxX - minX) * iw);
  const sy = y => m.t + ih - (y - (minY - yPad)) / (maxY - minY + 2 * yPad) * ih;
  for (let i = 0; i <= 5; i++) {{
    const y = m.t + ih * i / 5;
    const tick = maxY + yPad - (maxY - minY + 2 * yPad) * i / 5;
    svg.insertAdjacentHTML('beforeend', `<line class="grid-line" x1="${{m.l}}" y1="${{y}}" x2="${{m.l+iw}}" y2="${{y}}" />`);
    svg.insertAdjacentHTML('beforeend', `<text class="label" x="10" y="${{y+4}}">${{tick.toFixed(4)}}</text>`);
  }}
  svg.insertAdjacentHTML('beforeend', `<line class="axis" x1="${{m.l}}" y1="${{m.t+ih}}" x2="${{m.l+iw}}" y2="${{m.t+ih}}" /><line class="axis" x1="${{m.l}}" y1="${{m.t}}" x2="${{m.l}}" y2="${{m.t+ih}}" />`);
  const path = rows.map((d, i) => `${{i ? 'L' : 'M'}} ${{sx(Number(d.k))}} ${{sy(Number(d.cv_error))}}`).join(' ');
  svg.insertAdjacentHTML('beforeend', `<path d="${{path}}" fill="none" stroke="#287d8e" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" />`);
  for (const d of rows) {{
    const x = sx(Number(d.k)), y = sy(Number(d.cv_error));
    const isCurrent = String(d.k) === String(currentK);
    const isMin = payload.minCvK && Number(d.k) === Number(payload.minCvK);
    const isElbow = payload.elbowK && Number(d.k) === Number(payload.elbowK);
    const r = isCurrent ? 10 : 7;
    const fill = isCurrent ? '#d1495b' : (isMin ? '#414487' : '#287d8e');
    const tags = [isCurrent ? '当前展示' : '', isMin ? '最低 CV' : '', isElbow ? '拐点参考' : ''].filter(Boolean).join(' / ');
    svg.insertAdjacentHTML('beforeend', `<circle class="cv-dot" cx="${{x}}" cy="${{y}}" r="${{r}}" fill="${{fill}}" data-k="${{d.k}}" data-tip="K=${{d.k}}<br>CV error=${{Number(d.cv_error).toFixed(5)}}${{tags ? '<br>' + esc(tags) : ''}}" />`);
    svg.insertAdjacentHTML('beforeend', `<text class="label" x="${{x}}" y="${{H-25}}" text-anchor="middle">K=${{d.k}}</text>`);
  }}
  svg.insertAdjacentHTML('beforeend', `<text class="axis-label" x="${{W/2}}" y="${{H-6}}" text-anchor="middle">K value</text><text class="axis-label" x="18" y="${{H/2}}" text-anchor="middle" transform="rotate(-90 18 ${{H/2}})">CV error</text>`);
}}

function drawBars() {{
  const svg = document.getElementById('barPlot');
  const pack = payload.kSets[currentK];
  const search = document.getElementById('sampleSearch').value.trim().toLowerCase();
  const samples = pack.samples || [];
  const qCols = pack.qCols || [];
  const W = 1380, H = 520, m = {{l:56,r:22,t:18,b:70}}, iw = W-m.l-m.r, ih = H-m.t-m.b;
  svg.innerHTML = '';
  if (!samples.length) return;
  for (let i = 0; i <= 4; i++) {{
    const y = m.t + ih * i / 4;
    svg.insertAdjacentHTML('beforeend', `<line class="grid-line" x1="${{m.l}}" y1="${{y}}" x2="${{m.l+iw}}" y2="${{y}}" />`);
    svg.insertAdjacentHTML('beforeend', `<text class="label" x="12" y="${{y+4}}">${{(1 - i/4).toFixed(2)}}</text>`);
  }}
  const bw = iw / samples.length;
  let previousGroup = null;
  samples.forEach((sample, i) => {{
    if (previousGroup !== null && sample.assignment !== previousGroup) {{
      const gx = m.l + i * bw;
      svg.insertAdjacentHTML('beforeend', `<line class="group-line" x1="${{gx}}" y1="${{m.t}}" x2="${{gx}}" y2="${{m.t+ih}}" />`);
    }}
    previousGroup = sample.assignment;
    let yTop = m.t + ih;
    sample.q.forEach((value, qIndex) => {{
      const h = Math.max(0, Number(value)) * ih;
      yTop -= h;
      const x = m.l + i * bw;
      const color = payload.palette[qIndex % payload.palette.length];
      const qName = qCols[qIndex];
      const hit = search && String(sample.id).toLowerCase().includes(search);
      const cls = hit ? 'bar-seg sample-hit' : 'bar-seg';
      svg.insertAdjacentHTML('beforeend', `<rect class="${{cls}}" x="${{x.toFixed(2)}}" y="${{yTop.toFixed(2)}}" width="${{Math.max(.8,bw).toFixed(2)}}" height="${{Math.max(0,h).toFixed(2)}}" fill="${{color}}" data-tip="<strong>${{esc(sample.id)}}</strong><br>${{esc(qName)}}=${{Number(value).toFixed(4)}}<br>主导组分=${{esc(sample.assignment || 'NA')}}<br>最大 Q=${{Number(sample.max_q).toFixed(4)}}<br>${{sample.is_mixed ? '混合材料' : '主导组分明确'}}" />`);
    }});
  }});
  svg.insertAdjacentHTML('beforeend', `<line class="axis" x1="${{m.l}}" y1="${{m.t+ih}}" x2="${{m.l+iw}}" y2="${{m.t+ih}}" /><line class="axis" x1="${{m.l}}" y1="${{m.t}}" x2="${{m.l}}" y2="${{m.t+ih}}" />`);
  svg.insertAdjacentHTML('beforeend', `<text class="axis-label" x="${{W/2}}" y="${{H-18}}" text-anchor="middle">Samples ordered by dominant ancestry component</text>`);
  document.getElementById('legend').innerHTML = qCols.map((q, i) => `<span class="legend-item"><i style="background:${{payload.palette[i % payload.palette.length]}}"></i>${{esc(q)}}</span>`).join('');
  document.getElementById('barMeta').textContent = `当前展示：K=${{currentK}}，按主导 Q 组分排序`;
  document.getElementById('metricK').textContent = currentK;
  document.getElementById('chipCurrentK').textContent = currentK;
  document.getElementById('metricMixed').textContent = pack.mixedCount;
}}

document.getElementById('kSelect').addEventListener('change', event => {{
  currentK = String(event.target.value);
  drawCv();
  drawBars();
}});

document.getElementById('sampleSearch').addEventListener('input', drawBars);

document.addEventListener('mousemove', event => {{
  const target = event.target.closest('[data-tip]');
  if (target) showTip(event, target.dataset.tip);
  else hideTip();
}});

document.addEventListener('click', event => {{
  const dot = event.target.closest('[data-k]');
  if (!dot) return;
  const nextK = String(dot.dataset.k);
  if (!payload.kSets[nextK]) return;
  currentK = nextK;
  document.getElementById('kSelect').value = nextK;
  drawCv();
  drawBars();
}});

drawCv();
drawBars();
</script>
</body>
</html>
"""


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Render breedcore ADMIXTURE JSON as user-facing interactive HTML")
    parser.add_argument("result_json", help="breedcore ADMIXTURE result JSON path")
    args = parser.parse_args()
    result_json_path = Path(args.result_json).resolve()
    sys.stdout.write(render_html(load_result(result_json_path), result_json_path))


if __name__ == "__main__":
    main()
