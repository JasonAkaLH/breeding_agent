suppressPackageStartupMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("R package 'jsonlite' is required for the mini diagonal layout renderer.")
  }
})

args <- commandArgs(trailingOnly = TRUE)

parse_args <- function(args) {
  out <- list()
  i <- 1
  while (i <= length(args)) {
    key <- args[[i]]
    if (!startsWith(key, "--")) stop(sprintf("Unexpected argument: %s", key), call. = FALSE)
    name <- sub("^--", "", key)
    if (i == length(args) || startsWith(args[[i + 1]], "--")) {
      out[[name]] <- "true"
      i <- i + 1
    } else {
      out[[name]] <- args[[i + 1]]
      i <- i + 2
    }
  }
  out
}

script_dir <- function() {
  file_arg <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  if (length(file_arg) > 0) {
    return(normalizePath(dirname(sub("^--file=", "", file_arg[[1]])), winslash = "/", mustWork = FALSE))
  }
  normalizePath(".", winslash = "/", mustWork = FALSE)
}

skill_dir <- function() {
  normalizePath(file.path(script_dir(), ".."), winslash = "/", mustWork = FALSE)
}

is_absolute_path <- function(path) {
  grepl("^([A-Za-z]:[/\\\\]|/|\\\\\\\\)", path)
}

resolve_input_path <- function(path, root_dir) {
  if (is_absolute_path(path) || file.exists(path)) {
    return(normalizePath(path, winslash = "/", mustWork = TRUE))
  }
  normalizePath(file.path(root_dir, path), winslash = "/", mustWork = TRUE)
}

resolve_output_path <- function(path, root_dir) {
  if (is_absolute_path(path)) return(normalizePath(path, winslash = "/", mustWork = FALSE))
  normalizePath(path, winslash = "/", mustWork = FALSE)
}

html_escape <- function(x) {
  x <- gsub("&", "&amp;", x, fixed = TRUE)
  x <- gsub("<", "&lt;", x, fixed = TRUE)
  x <- gsub(">", "&gt;", x, fixed = TRUE)
  x <- gsub('"', "&quot;", x, fixed = TRUE)
  x
}

as_fieldbook_df <- function(x) {
  if (is.data.frame(x)) return(as.data.frame(x, stringsAsFactors = FALSE))
  if (is.list(x) && length(x) > 0 && is.list(x[[1]])) {
    rows <- lapply(x, function(row) as.data.frame(row, stringsAsFactors = FALSE, check.names = FALSE))
    out <- do.call(rbind, rows)
    row.names(out) <- NULL
    return(out)
  }
  as.data.frame(x, stringsAsFactors = FALSE, check.names = FALSE)
}

opts <- parse_args(args)
input <- opts[["input"]]
output <- opts[["output"]]
title <- opts[["title"]]
if (is.null(input)) stop("Missing required argument --input", call. = FALSE)
if (is.null(output)) stop("Missing required argument --output", call. = FALSE)
if (is.null(title)) title <- "Diagonal Field Layout"

root_dir <- skill_dir()
input_path <- resolve_input_path(input, root_dir)
output_path <- resolve_output_path(output, root_dir)

payload <- jsonlite::fromJSON(input_path, simplifyVector = FALSE)
if (isFALSE(payload$ok)) stop("Cannot render layout for a failed design result.", call. = FALSE)
if (is.null(payload$out_design)) stop("Input JSON does not contain out_design.", call. = FALSE)

fieldbook <- as_fieldbook_df(payload$out_design)
if (!"ped_id" %in% names(fieldbook) && "trt" %in% names(fieldbook)) {
  names(fieldbook)[names(fieldbook) == "trt"] <- "ped_id"
}

required <- c("plots", "ped_id", "hyb_type", "ranges", "pass", "set", "design_check")
missing_required <- setdiff(required, names(fieldbook))
if (length(missing_required) > 0) {
  stop(sprintf("out_design is missing required columns: %s", paste(missing_required, collapse = ", ")), call. = FALSE)
}

fieldbook$plots <- as.integer(fieldbook$plots)
fieldbook$ranges <- as.integer(fieldbook$ranges)
fieldbook$pass <- as.integer(fieldbook$pass)
fieldbook$ped_id <- as.character(fieldbook$ped_id)
fieldbook$hyb_type <- as.character(fieldbook$hyb_type)
fieldbook$set <- as.character(fieldbook$set)
fieldbook$design_check <- as.character(fieldbook$design_check)
fieldbook <- fieldbook[order(fieldbook$ranges, fieldbook$pass), , drop = FALSE]

meta <- list(
  source = basename(input_path),
  rows = nrow(fieldbook),
  sets = sort(unique(fieldbook$set), method = "radix"),
  ranges = range(fieldbook$ranges),
  passes = range(fieldbook$pass),
  parameters = payload$parameters,
  quality_control = payload$quality_control
)

fieldbook_json <- jsonlite::toJSON(fieldbook, dataframe = "rows", auto_unbox = TRUE, na = "null")
meta_json <- jsonlite::toJSON(meta, auto_unbox = TRUE, na = "null")

html <- paste0(
'<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>', html_escape(title), '</title>
<style>
:root {
  --bg: #f5f7f4;
  --panel: #ffffff;
  --line: #cfd8cf;
  --text: #1f2a24;
  --muted: #66736b;
  --check: #f1c453;
  --test-a: #71a7c7;
  --test-b: #7bb67a;
  --test-c: #c78d71;
  --active: #285f50;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Aptos", "Segoe UI", sans-serif;
  background: linear-gradient(180deg, #eef3ed, var(--bg));
  color: var(--text);
}
header {
  padding: 18px 22px 12px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
}
h1 { margin: 0; font-size: 22px; line-height: 1.2; }
.meta { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 14px; color: var(--muted); font-size: 13px; }
.toolbar {
  display: grid;
  grid-template-columns: repeat(3, minmax(140px, 1fr));
  gap: 10px;
  padding: 14px 22px;
  border-bottom: 1px solid var(--line);
  background: #e7eee6;
}
label { display: grid; gap: 4px; font-size: 12px; color: var(--muted); }
select, input {
  height: 34px;
  border: 1px solid #bbc8bd;
  border-radius: 6px;
  background: #fff;
  padding: 0 10px;
  font-size: 14px;
}
main { display: grid; grid-template-columns: minmax(0, 1fr) 290px; min-height: calc(100vh - 118px); }
.layout-wrap { overflow: auto; padding: 16px 18px 24px; }
.grid-frame {
  position: relative;
  display: inline-block;
  width: fit-content;
}
.diag-overlay {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 4;
  overflow: visible;
}
.diag-reference {
  fill: none;
  stroke: #285f50;
  stroke-width: 2;
  stroke-opacity: 0.28;
  stroke-dasharray: 9 7;
  vector-effect: non-scaling-stroke;
}
.diag-reference.main {
  stroke-width: 2.5;
  stroke-opacity: 0.42;
}
.grid {
  position: relative;
  z-index: 1;
  display: grid;
  gap: 5px;
  width: fit-content;
  min-width: min(100%, 880px);
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background:
    linear-gradient(90deg, rgba(31,42,36,0.05) 1px, transparent 1px),
    linear-gradient(180deg, rgba(31,42,36,0.05) 1px, transparent 1px),
    #fbfcfa;
  background-size: 24px 24px;
}
.cell {
  position: relative;
  z-index: 1;
  width: clamp(48px, calc((100vw - 430px) / var(--cols)), 84px);
  aspect-ratio: 1.12 / 1;
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 4px;
  display: grid;
  grid-template-rows: 1fr auto;
  background: linear-gradient(180deg, var(--set-light, #dcecff), var(--set-color, var(--test-a)));
  cursor: pointer;
}
.cell.ck {
  background: linear-gradient(180deg, #ffe497, var(--check));
  border-color: #ba8c1e;
  box-shadow: inset 0 0 0 2px rgba(80, 56, 0, 0.12);
}
.cell.hidden { opacity: 0.14; filter: grayscale(0.8); }
.cell.selected { outline: 3px solid var(--active); outline-offset: 1px; }
.ped-id { font-weight: 700; font-size: 12px; overflow-wrap: anywhere; line-height: 1.05; }
.coord { display: flex; justify-content: space-between; gap: 6px; font-size: 10px; color: rgba(31,42,36,0.75); }
aside { border-left: 1px solid var(--line); background: var(--panel); padding: 18px; }
.detail-title { font-size: 15px; font-weight: 700; margin-bottom: 12px; }
.detail-row { display: flex; justify-content: space-between; gap: 14px; padding: 8px 0; border-bottom: 1px solid #edf1ed; font-size: 13px; }
.detail-row span:first-child { color: var(--muted); }
.legend { display: flex; gap: 12px; margin-top: 18px; font-size: 12px; color: var(--muted); }
.swatch { width: 14px; height: 14px; border-radius: 3px; display: inline-block; vertical-align: -2px; margin-right: 5px; }
.swatch.ck { background: var(--check); }
.swatch.hyb { background: linear-gradient(90deg, var(--test-a), var(--test-b), var(--test-c)); }
@media (max-width: 880px) {
  .toolbar { grid-template-columns: 1fr; }
  main { grid-template-columns: 1fr; }
  aside { border-left: 0; border-top: 1px solid var(--line); }
}
</style>
</head>
<body>
<header>
  <h1>', html_escape(title), '</h1>
  <div class="meta" id="meta"></div>
</header>
<section class="toolbar">
  <label>Set<select id="setFilter"></select></label>
  <label>Type<select id="typeFilter"><option value="all">All</option><option value="ck">Diagonal check</option><option value="hyb">Test</option></select></label>
  <label>Search<input id="searchBox" type="search" placeholder="ped_id / plot"></label>
</section>
<main>
  <section class="layout-wrap"><div class="grid-frame" id="gridFrame"><svg class="diag-overlay" id="diagOverlay" aria-hidden="true"></svg><div class="grid" id="grid"></div></div></section>
  <aside>
    <div class="detail-title">Plot Detail</div>
    <div id="detail"></div>
    <div class="legend"><span><i class="swatch ck"></i>Diagonal check</span><span><i class="swatch hyb"></i>Test by set</span></div>
  </aside>
</main>
<script>
const rows = ', fieldbook_json, ';
const meta = ', meta_json, ';
const byId = id => document.getElementById(id);
const sets = [...new Set(rows.map(d => d.set))].sort();
const maxPass = Math.max(...rows.map(d => d.pass));
const maxRange = Math.max(...rows.map(d => d.ranges));
const palette = [
  { color: "#71a7c7", light: "#ddecf5" },
  { color: "#7bb67a", light: "#e1f2de" },
  { color: "#c78d71", light: "#f6e3d9" },
  { color: "#b7a461", light: "#f2ecd2" },
  { color: "#8aa980", light: "#e4f0df" }
];
const setColors = Object.fromEntries(sets.map((set, index) => [set, palette[index % palette.length]]));
let selected = rows[0];

function fillSelect(select, values, allLabel) {
  select.innerHTML = "";
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = allLabel;
  select.appendChild(all);
  values.forEach(value => {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = String(value);
    select.appendChild(option);
  });
}

function renderMeta() {
  const p = meta.parameters || {};
  byId("meta").innerHTML = [
    `source ${meta.source}`,
    `plots ${meta.rows}`,
    `sets ${sets.join(", ")}`,
    `rows ${meta.ranges[0]}-${meta.ranges[1]}`,
    `passes ${meta.passes[0]}-${meta.passes[1]}`,
    `used ${p.used_ck_ratio || ""}`,
    `actual ${p.actual_check_percent || ""}`,
    `diags ${p.diagonal_count || ""}/${p.target_diagonal_count || ""}`,
    `angle ${p.diagonal_angle_deg || ""}°`
  ].map(x => `<span>${x}</span>`).join("");
}

function isVisible(d) {
  const setValue = byId("setFilter").value;
  const typeValue = byId("typeFilter").value;
  const search = byId("searchBox").value.trim().toLowerCase();
  if (setValue !== "all" && d.set !== setValue) return false;
  if (typeValue !== "all" && d.hyb_type !== typeValue) return false;
  if (search && !`${d.ped_id} ${d.plots}`.toLowerCase().includes(search)) return false;
  return true;
}

function renderGrid() {
  const grid = byId("grid");
  grid.style.setProperty("--cols", maxPass);
  grid.style.gridTemplateColumns = `repeat(${maxPass}, minmax(48px, 1fr))`;
  grid.innerHTML = "";
  for (let range = 1; range <= maxRange; range++) {
    for (let pass = 1; pass <= maxPass; pass++) {
      const d = rows.find(row => row.ranges === range && row.pass === pass);
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cell";
      if (!d) {
        cell.classList.add("hidden");
        cell.disabled = true;
        grid.appendChild(cell);
        continue;
      }
      if (d.hyb_type === "ck") cell.classList.add("ck");
      else {
        const color = setColors[d.set];
        cell.style.setProperty("--set-color", color.color);
        cell.style.setProperty("--set-light", color.light);
      }
      if (!isVisible(d)) cell.classList.add("hidden");
      if (selected && selected.plots === d.plots) cell.classList.add("selected");
      cell.dataset.range = d.ranges;
      cell.dataset.pass = d.pass;
      cell.innerHTML = `<div class="ped-id">${d.ped_id}</div><div class="coord"><span>R${d.ranges} P${d.pass}</span><span>${d.set}</span></div>`;
      cell.addEventListener("click", () => { selected = d; renderGrid(); renderDetail(); });
      grid.appendChild(cell);
    }
  }
  renderDiagonalOverlay();
}

function normalizedAxes() {
  const p = meta.parameters || {};
  const axes = p.diagonal_axes;
  if (Array.isArray(axes)) return axes.map(Number).filter(Number.isFinite);
  if (Number.isFinite(Number(axes))) return [Number(axes)];
  return [];
}

function clipDiagonal(axis) {
  const points = [];
  const add = (u, v) => {
    if (u >= -1e-9 && u <= 1 + 1e-9 && v >= -1e-9 && v <= 1 + 1e-9) {
      const key = `${u.toFixed(6)},${v.toFixed(6)}`;
      if (!points.some(point => point.key === key)) points.push({ u, v, key });
    }
  };
  add(0, -axis);
  add(1, 1 - axis);
  add(axis, 0);
  add(1 + axis, 1);
  return points.slice(0, 2);
}

function renderDiagonalOverlay() {
  const svg = byId("diagOverlay");
  const grid = byId("grid");
  if (!svg || !grid) return;
  const axes = normalizedAxes();
  if (axes.length === 0) {
    svg.innerHTML = "";
    return;
  }

  const gridRect = grid.getBoundingClientRect();
  const first = grid.querySelector(`[data-range="1"][data-pass="1"]`);
  const last = grid.querySelector(`[data-range="${maxRange}"][data-pass="${maxPass}"]`);
  if (!first || !last || gridRect.width === 0 || gridRect.height === 0) return;

  const firstRect = first.getBoundingClientRect();
  const lastRect = last.getBoundingClientRect();
  const left = firstRect.left - gridRect.left;
  const top = firstRect.top - gridRect.top;
  const right = lastRect.right - gridRect.left;
  const bottom = lastRect.bottom - gridRect.top;
  const width = gridRect.width;
  const height = gridRect.height;

  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.innerHTML = "";

  const toPoint = point => ({
    x: left + point.u * (right - left),
    y: top + point.v * (bottom - top)
  });

  axes.forEach(axis => {
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
  });
}

function renderDetail() {
  const d = selected;
  byId("detail").innerHTML = [
    ["plot", d.plots], ["ped_id", d.ped_id], ["set", d.set], ["range", d.ranges],
    ["pass", d.pass], ["type", d.hyb_type], ["design_check", d.design_check]
  ].map(([k, v]) => `<div class="detail-row"><span>${k}</span><strong>${v}</strong></div>`).join("");
}

fillSelect(byId("setFilter"), sets, "All");
["setFilter", "typeFilter", "searchBox"].forEach(id => byId(id).addEventListener("input", renderGrid));
window.addEventListener("resize", renderDiagonalOverlay);
renderMeta();
renderGrid();
renderDetail();
</script>
</body>
</html>')

dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
writeLines(html, output_path, useBytes = TRUE)
