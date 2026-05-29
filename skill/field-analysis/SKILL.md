---
name: field-analysis
capability_id: skill.field_analysis
display_name: 田间数据分析智能体
description: >-
  分析随机区组、对角线增广等田间试验表型数据，并生成章节化 field-analysis-report-v1 JSON 报告；适用于田间试验数据、表型结果、随机区组试验、对角线增广设计、对照比较、描述统计、方差分析、LSD 分组、空间校正诊断或多地点稳定性分析等非育种选择类田间测试场景。
triggers:
  - 田间数据分析
  - 田间表型分析
  - 表型数据分析
  - RCBD数据分析
  - 随机区组数据分析
  - 随机区组方差分析
  - 对角线增广分析
  - 对角线试验分析
  - LSD分组
  - LSD grouping
  - check对比
  - 数据质量分析
  - 空间校正诊断
  - 多地点稳定性分析
  - field analysis
  - field trial analysis
  - phenotype analysis
public_usage:
  overview: >-
    分析田间试验表型数据，生成面向业务解释的统计报告；也可以只回答数据列、设计类型、对照比较或分析输出的用法问题。
  input_formats:
    - name: field_data
      required: true
      description: >-
        CSV、JSON 或表格型上传数据；每行通常代表一个小区、材料或观测记录。常见列包括材料编号、区组、重复、地点、性状值、对照标记和试验设计标记。
      example_columns: [entry_id, block, trait_value, check_flag]
  parameters:
    - name: design
      description: 分析所对应的试验设计；当前重点支持随机区组和对角线增广。
      examples: [rcbd, diagonal]
    - name: run_id
      description: 可选运行编号，便于用户区分多次分析结果。
  examples:
    - /field-analysis 这个 RCBD 表型数据怎么整理？
    - /field-analysis 分析上传表格，做随机区组方差分析和 LSD 分组
    - /field-analysis 对角线增广试验需要哪些列？
  outputs:
    - 章节化田间数据分析报告
    - 描述统计、方差分析和分组结果摘要
    - 数据质量和空间校正诊断建议
execution:
  mode: python_subprocess
  answer_mode: requires_finalizer
parameters:
  field_data:
    type: artifact
    required: true
    source: artifact
    aliases: [田间数据, 表型数据, 试验数据, field_data, phenotype_data]
  design:
    type: string
    required: true
    aliases: [design, design_type, 设计类型, 试验设计]
    patterns:
      - '(rcbd|RCBD|随机区组|随机完全区组)'
      - '(diagonal|Diagonal|对角线增广|对角线)'
  run_id:
    type: string
    required: false
    aliases: [run_id, run-id, 运行编号]
outputs:
  required:
    - answer
  files:
    - extensions: [.json]
      mime_types: [application/json]
scripts:
  - name: run_field_analysis
    path: scripts/run_field_analysis.py
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

# 田间数据分析智能体

Use this skill for field-testing phenotype analysis of:

- `rcbd`: randomized complete block design.
- `diagonal`: diagonal augmented design.

The skill runs a complete report by default. It does not ask users to choose statistical modules up front.

```text
scripts/run_field_analysis.py -> scripts/run_field_analysis.R -> field-analysis-report-v1 JSON
```

This skill is for large-scale field testing and advanced field experiment evaluation. Do not include GCA, parent combining ability, hybrid BLUP, heritability, or variance components in the default output.

## Welcome Message

When this skill starts a user-facing field analysis task, or when the user invokes `$field-analysis` without a complete input file and parameters, first greet the user in Chinese with the exact message below. Do not rewrite, summarize, expand, shorten, localize, or add extra sentences to this welcome message.

```text
欢迎使用田间数据分析智能体。目前支持随机区组试验（RCBD）和对角线增广试验（Diagonal）的田间表型数据分析。你只需要提供田间数据文件，并告诉我是 RCBD 还是 Diagonal 设计，我会生成章节化分析报告，包含数据质量、性状统计、材料表现、check 对比、方差分析、LSD 分组、空间校正诊断和稳定性分析等内容。

需要的数据表推荐列名是：

loc_id,rep_num,entry_id,ped_id,trait,value,check_type,ranges,pass

可选列包括：

value_trend,env_id,plot_id,block,num,female_ped_name,male_ped_name

你可以直接上传 CSV/JSON 文件，并说明设计类型：rcbd 或 diagonal。
```

If the user has not provided enough information, ask only for the missing items:

- uploaded input field phenotype data file.
- design type: `rcbd` or `diagonal`.
- optional `run_id`. Output files are written through the platform-managed Skill output directory.

For a bare `$field-analysis` invocation, show only the exact welcome message above. Do not run scripts until the required inputs are available.

When the declared Python wrapper detects missing required user input, it must return the structured `missing_input` contract from `Skill构建指南.md`: `ok: false`, `is_error: true`, `error.type: missing_input`, `missing` with manifest parameter names, and a user-readable `answer`.

## Inputs

Required columns:

```text
loc_id,rep_num,entry_id,ped_id,trait,value,check_type,ranges,pass
```

Optional compatibility columns:

```text
value_trend,env_id,plot_id,block,num,female_ped_name,male_ped_name
```

Trait direction:

- Use `value_trend` when present: `1` means higher is better, `-1` means lower is better.
- Without `value_trend`, treat `T0166` as lower-is-better and other traits as higher-is-better.

## Run

The project backend executes the declared Python wrapper. Do not declare `runtime: r`
or ask the main agent to execute Markdown code blocks. The wrapper receives JSON
stdin, resolves uploaded CSV/JSON content, calls the bundled R script with
`Rscript`, and returns a JSON object with `answer` plus downloadable JSON
artifacts.

Manual local run from the workspace root:

```powershell
Rscript `
  .\skill\field-analysis\scripts\run_field_analysis.R `
  --input <input.csv> `
  --design rcbd `
  --output-dir outputs\field-analysis `
  --run-id rcbd_demo
```

Diagonal sample:

```powershell
Rscript `
  .\skill\field-analysis\scripts\run_field_analysis.R `
  --input <input.csv> `
  --design diagonal `
  --output-dir outputs\field-analysis `
  --run-id diagonal_demo
```

Output files:

```text
field-analysis-<design>-full-report-<run_id>.json
field-analysis-summary-<design>-full-report-<run_id>.json
.field-analysis-session.json
```

## Report Structure

Primary JSON format:

```text
field-analysis-report-v1
```

Top-level objects:

```text
metadata
schema_note
chapters
traits
materials
locations
analyses
```

Do not expose old table names as the user-facing schema. `ExpSummary2`, `ExpStat2`, `LocSummary2`, and `LocStat2` are legacy migration references only.

## Chapters

Use `chapters` as the first reading surface:

- `data_overview`: trial scale and inventory.
- `data_quality`: CV, coverage, check distribution, and risk notes.
- `descriptive_stats`: trait, material, and location summaries.
- `check_comparison`: performance relative to checks.
- `anova`: ANOVA model and significance.
- `lsd_grouping`: LSD grouping after ANOVA, following the old script's analysis idea.
- `spatial_adjustment`: ranges/pass coverage and lightweight spatial correction diagnostics.
- `stability`: multi-location stability when location count supports it.

Chapter statuses:

```text
completed
completed_with_warnings
not_applicable
failed
skipped
```

When replying to users, summarize chapter status first and invite follow-up questions. Do not dump all records by default.

## Progressive Follow-Up

For manual local R runs, the runner writes an active session file in the output
directory:

```text
.field-analysis-session.json
```

If the user asks a follow-up question and does not provide a new input file, read this active session and use its `active_report`. Do not ask the user to provide the JSON path again.

Use the query helper for common follow-ups:

```powershell
Rscript `
  .\skill\field-analysis\scripts\query_active_report.R `
  --output-dir outputs\field-analysis `
  --trait T002 `
  --section quality,descriptive `
  --top-n 10
```

In the project backend `python_subprocess` path, outputs are managed as Skill
artifacts. For follow-up questions that require the full report facts, use the
current conversation's structured summary or ask the user to upload/provide the
previous `field-analysis-report-v1` JSON if the active report is not available
to the running skill.

Query mapping:

- quality questions: read `chapters.data_quality`, `traits.trait_summary`, and `locations.summary_by_trait`.
- descriptive questions: read `traits`, `materials`, and `locations`.
- check comparison questions: read `materials.by_trait` and `locations.materials_by_trait`.
- ANOVA questions: read `analyses.anova`.
- LSD questions: read `analyses.lsd_grouping`.
- spatial questions: read `analyses.spatial_adjustment`.
- stability questions: read `analyses.stability`.

If the active session file is missing, ask the user to run analysis first or provide a report file.

## Data Objects

`traits` replaces the old experiment-stat table:

```text
trait_summary_fields
trait_summary
```

`materials` replaces the old experiment-material summary:

```text
material_summary_fields
by_trait
```

`locations` replaces the old location stat and location-material summary tables:

```text
location_summary_fields
summary_by_trait
material_location_fields
materials_by_trait
```

`analyses` contains model results:

```text
anova
lsd_grouping
spatial_adjustment
stability
```

Repeated datasets use a catalog/matrix pattern: `*_fields` defines the field order once, and records are arrays in that order. Field names must be meaningful snake_case.

## Output Policy

Default output is sparse:

- Drop empty sections.
- Drop all-null fields.
- Do not emit empty message fields.
- Move repeated values into grouping or section metadata.
- Keep `null` only when a partially missing value is meaningful.

## Interpretation Guidance

Start with:

1. data quality and CV risks.
2. top materials by trait.
3. check comparison.
4. ANOVA/LSD only if model status supports it.
5. spatial adjustment and stability as follow-up chapters.

Useful prompts to offer:

- "哪些材料产量最高？"
- "哪些性状数据质量最好？"
- "哪些材料超过 check？"
- "哪些性状材料差异显著？"
- "空间校正后排名有没有变化？"
- "哪些材料跨地点表现稳定？"

## Files

All scripts live directly under `scripts/`:

```text
utils.R
io.R
trait_metadata.R
summaries.R
models.R
report_builder.R
run_field_analysis.R
query_active_report.R
```

The registered skill keeps runtime files only: `SKILL.md`, `agents/openai.yaml`, and `scripts/`.
