suppressPackageStartupMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("R package 'jsonlite' is required for the RCBD/Interval layout renderer.")
  }
})

args <- commandArgs(trailingOnly = TRUE)

parse_args <- function(args) {
  out <- list()
  i <- 1
  while (i <= length(args)) {
    key <- args[[i]]
    if (!startsWith(key, "--")) {
      stop(sprintf("Unexpected argument: %s", key), call. = FALSE)
    }
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

extract_fieldbook <- function(payload) {
  if (!is.null(payload$out_design)) {
    return(as_fieldbook_df(payload$out_design))
  }
  if (!is.null(payload$results) && length(payload$results) > 0) {
    return(as_fieldbook_df(payload$results[[1]]$out_design))
  }
  stop("Input JSON does not contain out_design.", call. = FALSE)
}

as_fieldbook_df <- function(x) {
  if (is.data.frame(x)) {
    return(as.data.frame(x, stringsAsFactors = FALSE))
  }
  if (is.list(x) && length(x) > 0 && is.list(x[[1]])) {
    rows <- lapply(x, function(row) {
      as.data.frame(row, stringsAsFactors = FALSE, check.names = FALSE)
    })
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
if (is.null(title)) title <- "RCBD Field Layout"

root_dir <- skill_dir()
input_path <- resolve_input_path(input, root_dir)
output_path <- resolve_output_path(output, root_dir)
payload <- jsonlite::fromJSON(input_path, simplifyVector = FALSE)
if (isFALSE(payload$ok)) {
  stop("Cannot render layout for a failed design result.", call. = FALSE)
}

fieldbook <- extract_fieldbook(payload)
if (!"ped_id" %in% names(fieldbook) && "trt" %in% names(fieldbook)) {
  names(fieldbook)[names(fieldbook) == "trt"] <- "ped_id"
}
if (!"hyb_check" %in% names(fieldbook) && "design_check" %in% names(fieldbook)) {
  names(fieldbook)[names(fieldbook) == "design_check"] <- "hyb_check"
}
required <- c("plots", "ranges", "pass", "r", "set", "ped_id", "hyb_check", "hyb_type")
missing_required <- setdiff(required, names(fieldbook))
if (length(missing_required) > 0) {
  stop(sprintf("out_design is missing required columns: %s", paste(missing_required, collapse = ", ")), call. = FALSE)
}

fieldbook$plots <- as.integer(fieldbook$plots)
fieldbook$ranges <- as.integer(fieldbook$ranges)
fieldbook$pass <- as.integer(fieldbook$pass)
fieldbook$r <- as.integer(fieldbook$r)
fieldbook$set <- as.character(fieldbook$set)
fieldbook$ped_id <- as.character(fieldbook$ped_id)
fieldbook$hyb_check <- as.character(fieldbook$hyb_check)
fieldbook$hyb_type <- as.character(fieldbook$hyb_type)
fieldbook <- fieldbook[order(fieldbook$ranges, fieldbook$pass), , drop = FALSE]

meta <- list(
  source = basename(input_path),
  rows = nrow(fieldbook),
  sets = sort(unique(fieldbook$set), method = "radix"),
  blocks = sort(unique(fieldbook$r)),
  ranges = range(fieldbook$ranges),
  passes = range(fieldbook$pass),
  parameters = payload$parameters
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
  color-scheme: light;
  --bg: #f7f8fa;
  --panel: #ffffff;
  --line: #d9dde5;
  --text: #1f2933;
  --muted: #64748b;
  --check: #f4b84a;
  --test-a: #78aee8;
  --test-b: #7bbf8e;
  --active: #355c9a;
  --shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Segoe UI", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
}
header {
  padding: 18px 22px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
h1 {
  margin: 0;
  font-size: 22px;
  line-height: 1.2;
  font-weight: 650;
}
.meta {
  margin-top: 8px;
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: 13px;
}
.toolbar {
  display: grid;
  grid-template-columns: repeat(4, minmax(120px, 1fr));
  gap: 10px;
  padding: 14px 22px;
  background: #eef2f6;
  border-bottom: 1px solid var(--line);
}
label {
  display: grid;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
select, input {
  height: 34px;
  border: 1px solid #c8ced8;
  border-radius: 6px;
  background: #fff;
  color: var(--text);
  padding: 0 10px;
  font-size: 14px;
}
main {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  min-height: calc(100vh - 118px);
}
.layout-wrap {
  overflow: auto;
  padding: 16px 18px 24px;
}
.grid {
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
}
.cell {
  width: clamp(46px, calc((100vw - 420px) / var(--cols)), 82px);
  aspect-ratio: 1.18 / 1;
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
}
.cell.hidden {
  opacity: 0.12;
  filter: grayscale(0.8);
}
.cell.ck {
  background: linear-gradient(180deg, var(--ck-light, #ffe08a), var(--ck-color, #f4b84a));
  border-color: var(--ck-border, #d89522);
}
.cell.hyb {
  background: linear-gradient(180deg, var(--set-light, #dcebff), var(--set-color, #78aee8));
  border-color: var(--set-border, #4f87c4);
}
.cell:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.18);
  z-index: 2;
}
.cell.selected {
  outline: 3px solid var(--active);
  outline-offset: 1px;
}
.coord {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  font-size: 10px;
  color: rgba(31,41,51,0.74);
}
.set-repeat {
  font-weight: 700;
}
.cell.even-rep .set-repeat {
  color: #b42318;
}
.trt {
  align-self: start;
  font-size: clamp(10px, 1.1vw, 13px);
  font-weight: 700;
  overflow-wrap: anywhere;
  line-height: 1.05;
  color: #1f2933;
}
.cell.even-rep .trt {
  color: #1f2933;
}
.tags {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  font-size: 11px;
}
.badge {
  padding: 2px 5px;
  border-radius: 4px;
  background: rgba(255,255,255,0.7);
  border: 1px solid rgba(31,41,51,0.12);
}
.tooltip {
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
}
.tooltip strong {
  display: block;
  font-size: 14px;
  margin-bottom: 6px;
}
.tooltip-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 3px 0;
  border-top: 1px solid #eef1f5;
}
.tooltip-row span:first-child {
  color: var(--muted);
}
aside {
  border-left: 1px solid var(--line);
  background: var(--panel);
  padding: 18px;
}
.detail-title {
  font-size: 15px;
  font-weight: 700;
  margin-bottom: 12px;
}
.detail-row {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  padding: 8px 0;
  border-bottom: 1px solid #edf0f4;
  font-size: 13px;
}
.detail-row span:first-child {
  color: var(--muted);
}
.legend {
  display: flex;
  gap: 10px;
  margin-top: 18px;
  font-size: 12px;
  color: var(--muted);
}
.swatch {
  width: 14px;
  height: 14px;
  border-radius: 3px;
  display: inline-block;
  vertical-align: -2px;
  margin-right: 5px;
}
.swatch.ck { background: var(--check); }
.swatch.hyb { background: linear-gradient(90deg, var(--test-a), var(--test-b)); }
@media (max-width: 900px) {
  .toolbar { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
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
  <label>重复<select id="blockFilter"></select></label>
  <label>类型<select id="typeFilter"><option value="all">全部</option><option value="ck">对照</option><option value="hyb">测试材料</option></select></label>
  <label>搜索<input id="searchBox" type="search" placeholder="ped_id / plot"></label>
</section>
<main>
  <section class="layout-wrap">
    <div class="grid" id="grid"></div>
  </section>
  <aside>
    <div class="detail-title">Plot Detail</div>
    <div id="detail"></div>
    <div class="legend">
      <span><i class="swatch ck"></i>对照</span>
      <span><i class="swatch hyb"></i>测试材料按 set 着色</span>
    </div>
  </aside>
</main>
<div class="tooltip" id="tooltip"></div>
<script>
const rows = ', fieldbook_json, ';
const meta = ', meta_json, ';
const byId = id => document.getElementById(id);
const sets = [...new Set(rows.map(d => d.set))].sort();
const blocks = [...new Set(rows.map(d => d.r))].sort((a, b) => a - b);
const maxPass = Math.max(...rows.map(d => d.pass));
const maxRange = Math.max(...rows.map(d => d.ranges));
const planter = meta.parameters && meta.parameters.planter ? meta.parameters.planter : "cartesian";
const setPalette = [
  { color: "#78aee8", light: "#dcebff", border: "#4f87c4" },
  { color: "#7bbf8e", light: "#def4e4", border: "#4f9463" },
  { color: "#c59be8", light: "#f0e4fb", border: "#8a61ba" },
  { color: "#e58f7d", light: "#fde3dc", border: "#b76454" },
  { color: "#d2b45f", light: "#f7edc8", border: "#9f8332" },
  { color: "#78bfc7", light: "#d9f2f4", border: "#4f8f96" }
];
const setColors = Object.fromEntries(sets.map((set, index) => [set, setPalette[index % setPalette.length]]));
const ckPalette = [
  { color: "#f4b84a", light: "#ffe08a", border: "#d89522" },
  { color: "#e7a936", light: "#ffd776", border: "#c98218" },
  { color: "#f0c35d", light: "#ffecac", border: "#cc9a2b" },
  { color: "#d99a2b", light: "#f8cf74", border: "#ad7415" },
  { color: "#f6c86a", light: "#fff0bd", border: "#c89124" },
  { color: "#e0ad3f", light: "#f9dc88", border: "#b9821d" }
];
const ckNames = [...new Set(rows.filter(d => d.hyb_type === "ck").map(d => d.ped_id))].sort();
const ckColors = Object.fromEntries(ckNames.map((name, index) => [name, ckPalette[index % ckPalette.length]]));
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
  const planterLabel = planter === "serpentine" ? "蛇形排列" : "顺序排列（笛卡尔排列）";
  byId("meta").innerHTML = [
    `来源 ${meta.source}`,
    `排列方式 ${planterLabel}`,
    `小区 ${meta.rows}`,
    `Set ${sets.join(", ")}`,
    `重复 ${blocks.join(", ")}`,
    `行 ${meta.ranges[0]}-${meta.ranges[1]}`,
    `列 ${meta.passes[0]}-${meta.passes[1]}`
  ].map(x => `<span>${x}</span>`).join("");
}

function passesForRange(range) {
  return Array.from({ length: maxPass }, (_, i) => i + 1).map(pass =>
    rows.find(d => d.ranges === range && d.pass === pass)
  );
}

function isVisible(d) {
  if (!d) return false;
  const setValue = byId("setFilter").value;
  const blockValue = byId("blockFilter").value;
  const typeValue = byId("typeFilter").value;
  const search = byId("searchBox").value.trim().toLowerCase();
  if (setValue !== "all" && d.set !== setValue) return false;
  if (blockValue !== "all" && String(d.r) !== blockValue) return false;
  if (typeValue !== "all" && d.hyb_type !== typeValue) return false;
  if (search && !`${d.ped_id} ${d.plots}`.toLowerCase().includes(search)) return false;
  return true;
}

function renderGrid() {
  const grid = byId("grid");
  grid.style.setProperty("--cols", maxPass);
  grid.style.gridTemplateColumns = `repeat(${maxPass}, minmax(46px, 1fr))`;
  grid.innerHTML = "";
  for (let range = 1; range <= maxRange; range++) {
    passesForRange(range).forEach(d => {
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cell";
      if (!d) {
        cell.classList.add("hidden");
        cell.disabled = true;
        grid.appendChild(cell);
        return;
      }
      cell.classList.add(d.hyb_type === "ck" ? "ck" : "hyb");
      cell.classList.add(d.r % 2 === 0 ? "even-rep" : "odd-rep");
      if (d.hyb_type === "ck") {
        const color = ckColors[d.ped_id] || ckPalette[0];
        cell.style.setProperty("--ck-color", color.color);
        cell.style.setProperty("--ck-light", color.light);
        cell.style.setProperty("--ck-border", color.border);
      } else {
        const color = setColors[d.set];
        cell.style.setProperty("--set-color", color.color);
        cell.style.setProperty("--set-light", color.light);
        cell.style.setProperty("--set-border", color.border);
      }
      if (!isVisible(d)) cell.classList.add("hidden");
      if (selected && selected.plots === d.plots) cell.classList.add("selected");
      cell.setAttribute("aria-label", `${d.ped_id}, range ${d.ranges}, pass ${d.pass}`);
      cell.innerHTML = `
        <div class="trt">${d.ped_id}</div>
        <div></div>
        <div class="coord"><span>R${d.ranges} P${d.pass}</span><span class="set-repeat">${d.set}/${d.r}</span></div>
      `;
      cell.addEventListener("mouseenter", event => showTooltip(event, d));
      cell.addEventListener("mousemove", event => moveTooltip(event));
      cell.addEventListener("mouseleave", hideTooltip);
      cell.addEventListener("click", () => {
        selected = d;
        renderGrid();
        renderDetail();
      });
      grid.appendChild(cell);
    });
  }
}

function tooltipHtml(d) {
  return `
    <strong>${d.ped_id}</strong>
    <div class="tooltip-row"><span>plot</span><b>${d.plots}</b></div>
    <div class="tooltip-row"><span>range / pass</span><b>${d.ranges} / ${d.pass}</b></div>
    <div class="tooltip-row"><span>set / repeat</span><b>${d.set} / ${d.r}</b></div>
    <div class="tooltip-row"><span>type</span><b>${d.hyb_type}</b></div>
    <div class="tooltip-row"><span>hyb_check</span><b>${d.hyb_check}</b></div>
  `;
}

function showTooltip(event, d) {
  const tip = byId("tooltip");
  tip.innerHTML = tooltipHtml(d);
  tip.style.display = "block";
  moveTooltip(event);
}

function moveTooltip(event) {
  const tip = byId("tooltip");
  const pad = 14;
  const rect = tip.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + rect.width > window.innerWidth) x = event.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight) y = event.clientY - rect.height - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}

function hideTooltip() {
  byId("tooltip").style.display = "none";
}

function renderDetail() {
  const d = selected;
  if (!d) {
    byId("detail").innerHTML = "";
    return;
  }
  byId("detail").innerHTML = [
    ["plot", d.plots],
    ["ped_id", d.ped_id],
    ["set", d.set],
    ["repeat", d.r],
    ["range", d.ranges],
    ["pass", d.pass],
    ["type", d.hyb_type],
    ["hyb_check", d.hyb_check]
  ].map(([k, v]) => `<div class="detail-row"><span>${k}</span><strong>${v}</strong></div>`).join("");
}

fillSelect(byId("setFilter"), sets, "全部");
fillSelect(byId("blockFilter"), blocks, "全部");
["setFilter", "blockFilter", "typeFilter", "searchBox"].forEach(id => {
  byId(id).addEventListener("input", renderGrid);
});
renderMeta();
renderGrid();
renderDetail();
</script>
</body>
</html>
')

dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
writeLines(html, output_path, useBytes = TRUE)
