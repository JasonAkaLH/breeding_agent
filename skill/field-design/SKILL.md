---
name: field-design
capability_id: skill.field_design
display_name: 试验设计智能体
description: >-
  基于规范的 CSV/JSON 材料清单生成田间试验设计，包括随机区组设计、对角线增广设计和间比法设计；适用于准备或运行试验设计流程、设置重复/区组、配置对照比例与对照起始位置/间隔、生成 fieldbook、种植顺序、蛇形或笛卡尔布局以及 HTML 田间布局预览。
triggers:
  - 试验设计
  - 田间试验设计
  - 随机区组设计
  - 随机完全区组设计
  - RCBD设计
  - 生成RCBD
  - 对角线增广设计
  - 对角线试验设计
  - 间比法设计
  - 生成fieldbook
  - 田间布局预览
  - 小区排布
  - field design
  - randomized complete block design
  - diagonal augmented design
  - interval contrast design
execution:
  mode: python_subprocess
  answer_mode: requires_finalizer
parameters:
  material_data:
    type: artifact
    required: true
    source: artifact
    aliases: [材料清单, 材料文件, 试验材料, material_data]
  design:
    type: string
    required: true
    aliases: [design, design_type, 设计类型, 试验设计]
    patterns:
      - '(rcbd|RCBD|随机区组|随机完全区组|完全随机区组)'
      - '(diagonal|Diagonal|对角线增广|对角线)'
      - '(interval|Interval|间比法|间比)'
  blocks:
    type: integer
    required: false
    aliases: [blocks, 区组数, 区组, 重复数, 重复, reps, replications]
    patterns:
      - '(?:blocks?|区组数|区组|重复数|重复|reps?|replications?)\s*[:：=]?\s*(\d+)'
      - '(\d+)\s*(?:个|次)?(?:区组|重复|rep|reps|blocks?)'
  ncols:
    type: integer
    required: false
    aliases: [ncols, 列数, 田块列数]
    patterns:
      - '(?:ncols|列数|田块列数)\s*[:：=]?\s*(\d+)'
      - '(\d+)\s*(?:列|columns?)'
  ck_spec:
    type: string
    required: false
    aliases: [ck_spec, ck-spec, CK参数, CK间隔]
  ck_ratio:
    type: string
    required: false
    default: A
    aliases: [ck_ratio, ck-ratio, 对照比例]
  planter:
    type: string
    required: false
    default: serpentine
    aliases: [planter, 种植路径, 排布方式]
  randomize:
    type: string
    required: false
    default: "true"
    aliases: [randomize, 是否随机, 随机化]
  seed:
    type: integer
    required: false
    aliases: [seed, 随机种子]
  run_id:
    type: string
    required: false
    aliases: [run_id, run-id, 运行编号]
outputs:
  required:
    - answer
  files:
    - extensions: [.csv]
      mime_types: [text/csv]
    - extensions: [.html]
      mime_types: [text/html]
scripts:
  - name: run_field_design
    path: scripts/run_field_design.py
    runtime: python
    auto_run: true
    timeout_seconds: 300
    inputs:
      required:
        - query
    outputs:
      required:
        - answer
---

# 试验设计智能体

Use this skill to run three field-design workflows through the declared Python
wrapper and bundled R scripts:

- `RCBD`: randomized complete block design / 随机完全区组设计.
- `Diagonal`: diagonal augmented design / 对角线增广设计.
- `Interval`: interval contrast design / 间比法设计.

The project backend executes `scripts/run_field_design.py`. Do not declare
`runtime: r` or ask the main agent to execute Markdown code blocks. The wrapper
receives JSON stdin, resolves uploaded CSV/JSON material data, calls the bundled
R scripts with `Rscript`, and returns a JSON object with `answer`, a 10-row
preview, and downloadable CSV/HTML artifacts.

## Welcome Message

When this skill starts a user-facing field design task, or when the user invokes `$field-design` without a complete material list and parameters, first greet the user in Chinese with the exact message below. Do not rewrite, summarize, expand, shorten, localize, or add extra sentences to this welcome message.

```text
欢迎使用试验设计智能体。目前支持随机区组试验设计（RCBD）、对角线增广试验设计和间比法试验设计（Interval）。你只需要提供试验材料清单，并告诉我要做哪一种设计即可开始：如果做 RCBD，请提供区组数/重复数；如果做对角线增广设计，请提供田块列数 ncols；如果做间比法设计，请先提供材料清单和田块列数 ncols，我会识别 CK 后请你按编号补充每个 CK 的起始位置和间隔数量。

需要的材料表推荐列名是：

ped_id,hyb_check,set

你可以直接上传 CSV/JSON 材料文件，或者把材料表粘贴过来。
```

If the user has not provided enough information, ask only for the missing items:

- uploaded material list file or pasted material table.
- design type: `RCBD`, `Diagonal`, or `Interval`.
- design parameters, such as `blocks` for RCBD, `ncols`, `ck_ratio`, `planter`, `randomize`, and `seed` for Diagonal, or `ncols` and CK interval parameters for Interval. Do not ask for Interval `reps` in the first interaction.

For a bare `$field-design` invocation, show only the exact welcome message above. Do not run scripts until the required inputs are available.

When the declared Python wrapper detects missing required user input, it must return the structured `missing_input` contract from `Skill构建指南.md`: `ok: false`, `is_error: true`, `error.type: missing_input`, `missing` with manifest parameter names, and a user-readable `answer`. For Interval designs, the CK lookup prompt (`status: needs_ck_parameters`) is still missing input, not a successful design result.

## Choose The Workflow

Use `RCBD` when the request mentions randomized complete block design, 随机区组, RCBD, blocks, reps, replicates, or repeated complete sets of entries.

Use `Diagonal` when the request mentions diagonal augmented design, 对角线增广设计, diagonal checks, ck_ratio, or arranging checks along diagonal positions with a field column count `ncols`.

Use `Interval` when the request mentions interval contrast design, 间比法, start positions for CK, check intervals, or fixed CK insertion by starting position and interval.

## Workflow

For every design request:

1. Confirm the input material list, design type, and required parameters.
2. Run the declared Python wrapper, which calls the matching bundled R design script.
3. Treat the generated JSON as an internal intermediate result only.
4. Render the matching HTML layout preview from the generated JSON.
5. Export the full planting-order fieldbook CSV from the generated JSON.
6. In the conversation, show only the first 10 planting-order rows as a Markdown table.
7. Provide links to the full CSV fieldbook and HTML layout preview.

Do not show or link the JSON result in the normal user-facing answer. Mention
the JSON path only if the user explicitly asks for the internal source file or
debugging details.

## Output Policy

Use a progressive output flow:

1. `*_result.json`: internal source of truth for rendering and export.
2. `*_layout.html`: full visual layout preview for the user.
3. `*_fieldbook.csv`: full planting-order fieldbook for the user.

For the final response, lead with the design mode and core parameters, then the
10-row preview table, then links to the CSV and HTML outputs. Keep JSON out of
the answer by default.

## Input Schema

Prefer new input files with exactly these columns:

```text
ped_id,hyb_check,set
```

Interpret fields as:

- `ped_id`: material ID.
- `hyb_check`: check marker.
- `set`: group ID; prefer letters such as `A`, `B`, `C` instead of numeric-only sets.

For `RCBD`, interpret `hyb_check = 0` as test material and non-zero values as checks. RCBD also accepts `ped_id + design_check`, and the legacy `plot_id + hyb_check` schema. If `set` is missing, RCBD adds `set = "A"`.

For `Diagonal`, require `ped_id,hyb_check,set`. Interpret `hyb_check = 0` as test material, `1` as a non-diagonal marker that is treated like regular material in this workflow, and `2` as diagonal check material. Require at least one `hyb_check = 2` and at least one non-`2` entry.

For `Interval`, require `ped_id,hyb_check,set`. Interpret `hyb_check = 0` as test material and non-zero values as CK. CK `ped_id` values must be globally unique. Interval designs are generated independently per `set`, and CK parameters are matched by the unique `ped_id`.

## Run RCBD

Require:

- `--input`: CSV or JSON material list.
- `--blocks`: number of repeats/blocks. RCBD currently supports only `1`, `2`, or `3`.

Use `--planter serpentine` for 蛇形排列 and `--planter cartesian` for 顺序排列 / 笛卡尔排列.

```powershell
Set-Variable skillDir 'skill/field-design'
Set-Variable outDir 'outputs/field-design'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Rscript `
  "$skillDir/scripts/run_rcbd_local.R" `
  --input <input.csv> `
  --blocks 2 `
  --planter serpentine `
  --seed 20260512 `
  --output "$outDir/rcbd_result.json"
```

Render the RCBD HTML preview:

```powershell
Rscript `
  "$skillDir/scripts/render_rcbd_interval_layout_html.R" `
  --input "$outDir/rcbd_result.json" `
  --output "$outDir/rcbd_layout.html" `
  --title "Field Design RCBD Layout"
```

Export the full RCBD fieldbook CSV:

```powershell
Rscript `
  "$skillDir/scripts/json_to_fieldbook_csv.R" `
  --input "$outDir/rcbd_result.json" `
  --design rcbd `
  --output "$outDir/rcbd_fieldbook.csv"
```

Report only the first 10 planting-order fieldbook rows in the conversation,
then link the full CSV and HTML preview. Do not link or display the JSON result
unless the user asks for it. Do not sort the conversation fieldbook by
`ranges,pass` unless the user asks for physical layout order.

## Run Diagonal

Require:

- `--input`: CSV material list with `ped_id,hyb_check,set`.
- `--ncols`: field column count.

Defaults:

- `--ck-ratio A`: low-density checks.
- `--planter serpentine`: serpentine planting order.
- `--randomize true`: randomize test materials.

Use `--randomize false` only when the user asks to preserve list order, e.g. “按我的清单顺序”, “不要随机”, or “保持原始顺序”.

```powershell
Set-Variable skillDir 'skill/field-design'
Set-Variable outDir 'outputs/field-design'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Rscript `
  "$skillDir/scripts/run_diagonal_local.R" `
  --input <input.csv> `
  --ncols 20 `
  --ck-ratio A `
  --planter serpentine `
  --randomize true `
  --seed 20260512 `
  --output "$outDir/diagonal_result.json"
```

Render the diagonal HTML preview:

```powershell
Rscript `
  "$skillDir/scripts/render_diagonal_layout_html.R" `
  --input "$outDir/diagonal_result.json" `
  --output "$outDir/diagonal_layout.html" `
  --title "Field Design Diagonal Layout"
```

Export the full diagonal fieldbook CSV:

```powershell
Rscript `
  "$skillDir/scripts/json_to_fieldbook_csv.R" `
  --input "$outDir/diagonal_result.json" `
  --design diagonal `
  --output "$outDir/diagonal_fieldbook.csv"
```

After generating the internal diagonal result JSON, always display only the
first 10 diagonal design rows in the conversation as a Markdown table. Read
`out_design` from the generated JSON and preserve its planting order; do not
re-sort by `ranges,pass` unless the user explicitly asks for physical layout
order. Include these columns:

```text
plots | ped_id | hyb_type | ranges | pass | set | design_check
```

For every diagonal design, show the first 10 planting-order rows in the
conversation, then provide the full CSV fieldbook path/link and HTML preview
path/link. Do not skip this table just because the HTML preview exists. Do not
display or link raw JSON in the conversation unless the user explicitly asks
for it.

Use `ck_ratio` levels as:

- `A`: target `5% - 10%` checks.
- `B`: target `10% - 15%` checks.
- `C`: target `15% - 20%` checks.

Start with the requested level, defaulting to `A`. If the requested level cannot satisfy the diagonal layout, the script automatically upgrades from `A` to `B`, then to `C`. Always tell the user the requested level, used level, whether it was auto-upgraded, and the actual check ratio/percent.

Do not expose `filler`, `filler.end`, or multi-site parameters for diagonal design. For multiple independent diagonal designs, run `run_diagonal_local.R` multiple times with different seeds and return separate design tables.

## Run Interval

Require:

- `--input`: CSV material list with `ped_id,hyb_check,set`.
- `--ncols`: field column count.
- CK parameters after CK listing: `ck_no,start_pos,interval`.

Defaults:

- `--planter serpentine`: serpentine planting order.
- `--randomize true`: randomize test materials.

### Step 1: List CK Materials

After the user provides the material list and asks for Interval design, run the CK listing step before asking for CK parameters:

```powershell
Set-Variable skillDir 'skill/field-design'
Set-Variable outDir 'outputs/field-design'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Rscript `
  "$skillDir/scripts/run_interval_contrast_local.R" `
  --input <input.csv> `
  --list-checks true `
  --output "$outDir/interval_ck_table.json"
```

Read the generated `ck_table` and show it to the user as:

```text
ck_no | ped_id | set
```

Then ask the user to provide CK parameters in this format:

```text
ck_no,start_pos,interval
```

Multiple CKs can be separated with semicolons, for example:

```text
1,1,10; 2,5,10
```

### Step 2: Confirm CK Parameters

After the user provides `ck_no,start_pos,interval` values, parse them and show a confirmation table before running the design:

```text
ck_no | ped_id | set | start_pos | interval
```

Use this Chinese confirmation sentence:

```text
请确认以上 CK 起始位置和间隔数量是否正确。确认后我再开始生成间比法设计。
```

Do not run the final Interval design until the user confirms.

### Step 3: Generate Interval Design

```powershell
Set-Variable skillDir 'skill/field-design'
Set-Variable outDir 'outputs/field-design'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Rscript `
  "$skillDir/scripts/run_interval_contrast_local.R" `
  --input <input.csv> `
  --ncols 10 `
  --ck-spec "1,1,10; 2,5,10" `
  --planter serpentine `
  --randomize true `
  --seed 20260519 `
  --output "$outDir/interval_result.json"
```

The Interval script generates one single-run design per call. After the first design is complete, if the user asks for multiple repeats, run the same command once per repeat with different seeds and output names. Add or preserve `r` as the repeat number in each repeat fieldbook. For example, if the user later asks for `reps = 2`, run repeat 1 with `--seed 20260519 --output "$outDir/interval_result_r1.json"` and repeat 2 with `--seed 20260520 --output "$outDir/interval_result_r2.json"`.

Render the Interval HTML preview using the shared RCBD/Interval layout renderer because both designs use the same visible fieldbook columns:

```powershell
Rscript `
  "$skillDir/scripts/render_rcbd_interval_layout_html.R" `
  --input "$outDir/interval_result.json" `
  --output "$outDir/interval_layout.html" `
  --title "Field Design Interval Layout"
```

Export the full Interval fieldbook CSV:

```powershell
Rscript `
  "$skillDir/scripts/json_to_fieldbook_csv.R" `
  --input "$outDir/interval_result.json" `
  --design interval `
  --output "$outDir/interval_fieldbook.csv"
```

For every Interval design, show only the first 10 planting-order rows in the conversation with these columns:

```text
plots | r | ped_id | ranges | pass | set | hyb_check | hyb_type
```

For Interval layout previews, keep all sets in one continuous field layout. Do not force a new row just because the set changes; the next set should continue from the next available planting position. CK cells should remain in the existing yellow check color family, but use interpolated shade variations by CK `ped_id` so different CKs are visually distinguishable without leaving the check color system.

After the first Interval design is complete, ask the user whether they need multiple repeats. Do not ask this before the first design is complete. Use this wording in Chinese:

```text
当前已完成 1 轮间比法设计。是否需要生成多个重复设计？如果需要，请告诉我重复数。例如 3 个重复将生成 3 个单独结果，且重复之间使用不同随机种子。
```

For Interval, `reps` means `重复` only. Do not call it blocks or 区组. If the user asks for `reps = 3`, generate 3 separate Interval results by calling the single-run script 3 times with different seeds and output names. Keep each repeat as a separate result unless the user explicitly asks to merge them.

### Interval Validation Rules

Before final design:

- material data must contain `ped_id,hyb_check,set`.
- `ncols` must be available.
- every CK in `ck_table` must receive `start_pos` and `interval`.
- `start_pos` and `interval` must be positive integers.
- each `set` is checked independently for CK position overlaps.

If a CK position overlap is detected, stop and ask the user to adjust the related CK start position or interval.

## Report Outputs

For every run, report:

- design mode used: `RCBD`, `Diagonal`, or `Interval`.
- input path, core parameters, and seed.
- full CSV fieldbook path and HTML preview path.
- for `Diagonal`, requested `ck_ratio`, used `ck_ratio`, `auto_upgraded`, and actual check percent.
- for `Interval`, the confirmed CK parameter table.
- a concise fieldbook table with only the first 10 planting-order rows. Generate this table from JSON `out_design`, but do not display the raw JSON result.

Use the generated JSON as the internal source of truth and as input for HTML and CSV rendering. Important output fields include:

- RCBD CSV columns: `plots`, `r`, `ped_id`, `ranges`, `pass`, `set`, `hyb_check`, `hyb_type`.
- Diagonal CSV columns: `plots`, `ped_id`, `hyb_type`, `ranges`, `pass`, `set`, `design_check`.
- Interval CSV columns: `plots`, `r`, `ped_id`, `ranges`, `pass`, `set`, `hyb_check`, `hyb_type`.

When reporting a result, never paste JSON content by default. Provide the CSV and HTML links as the user-facing complete outputs.

## Environment

Use `Rscript` from the current environment `PATH`. Do not hardcode a
machine-specific R installation path in normal workflow examples. The current
validated Windows environment uses R `4.5.3`; target Mac deployments can use
the current Mac R release if `jsonlite` is installed.

Required R package:

- `jsonlite`
