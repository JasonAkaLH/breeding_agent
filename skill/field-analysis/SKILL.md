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

使用此 Skill 对以下田间测试表型数据进行分析：

- `rcbd`: randomized complete block design。
- `diagonal`: diagonal augmented design。

此 Skill 默认运行完整报告，不会在开始前要求用户选择统计模块。

```text
scripts/run_field_analysis.py -> scripts/run_field_analysis.R -> field-analysis-report-v1 JSON
```

此 Skill 面向大规模田间测试和高级田间试验评估。默认输出中不要包含 GCA、parent combining ability、hybrid BLUP、heritability 或 variance components。

## 欢迎语

当此 Skill 启动面向用户的 field analysis 任务，或用户在输入文件和参数不完整时调用 `$field-analysis`，先用下面的精确文本用中文问候用户。不得改写、概括、扩写、缩短、本地化或添加额外句子。

```text
欢迎使用田间数据分析智能体。目前支持随机区组试验（RCBD）和对角线增广试验（Diagonal）的田间表型数据分析。你只需要提供田间数据文件，并告诉我是 RCBD 还是 Diagonal 设计，我会生成章节化分析报告，包含数据质量、性状统计、材料表现、check 对比、方差分析、LSD 分组、空间校正诊断和稳定性分析等内容。

需要的数据表推荐列名是：

loc_id,rep_num,entry_id,ped_id,trait,value,check_type,ranges,pass

可选列包括：

value_trend,env_id,plot_id,block,num,female_ped_name,male_ped_name

你可以直接上传 CSV/JSON 文件，并说明设计类型：rcbd 或 diagonal。
```

如果用户提供的信息不足，只询问缺失项：

- 上传的田间表型数据文件。
- 设计类型：`rcbd` 或 `diagonal`。
- 可选 `run_id`。输出文件通过平台托管的 Skill output directory 写入。

对于裸 `$field-analysis` 调用，只展示上面的精确欢迎语。必需输入可用前不要运行脚本。

当已声明的 Python wrapper 检测到缺少必需用户输入时，必须按 `Skill构建指南.md` 返回结构化 `missing_input` contract：`ok: false`、`is_error: true`、`error.type: missing_input`、使用 manifest 参数名的 `missing`，以及用户可读的 `answer`。

## 输入

必需列：

```text
loc_id,rep_num,entry_id,ped_id,trait,value,check_type,ranges,pass
```

可选兼容列：

```text
value_trend,env_id,plot_id,block,num,female_ped_name,male_ped_name
```

性状方向：

- 如果存在 `value_trend`，使用该字段：`1` 表示数值越高越好，`-1` 表示数值越低越好。
- 如果没有 `value_trend`，将 `T0166` 视为 lower-is-better，其他性状视为 higher-is-better。

## 运行

项目后端执行已声明的 Python wrapper。不要声明 `runtime: r`，也不要要求主代理执行 Markdown 代码块。wrapper 接收 JSON stdin，解析上传的 CSV/JSON 内容，通过 `Rscript` 调用随包 R 脚本，并返回包含 `answer` 以及可下载 JSON artifacts 的 JSON 对象。

从 workspace root 手工本地运行：

```powershell
Rscript `
  .\skill\field-analysis\scripts\run_field_analysis.R `
  --input <input.csv> `
  --design rcbd `
  --output-dir outputs\field-analysis `
  --run-id rcbd_demo
```

Diagonal 示例：

```powershell
Rscript `
  .\skill\field-analysis\scripts\run_field_analysis.R `
  --input <input.csv> `
  --design diagonal `
  --output-dir outputs\field-analysis `
  --run-id diagonal_demo
```

输出文件：

```text
field-analysis-<design>-full-report-<run_id>.json
field-analysis-summary-<design>-full-report-<run_id>.json
.field-analysis-session.json
```

## 报告结构

主 JSON 格式：

```text
field-analysis-report-v1
```

顶层对象：

```text
metadata
schema_note
chapters
traits
materials
locations
analyses
```

不要把旧表名作为面向用户的 schema 暴露。`ExpSummary2`、`ExpStat2`、`LocSummary2` 和 `LocStat2` 仅作为 legacy migration references。

## 章节

优先把 `chapters` 作为读取入口：

- `data_overview`：trial scale 和 inventory。
- `data_quality`：CV、coverage、check distribution 和 risk notes。
- `descriptive_stats`：trait、material 和 location summaries。
- `check_comparison`：相对 checks 的表现。
- `anova`：ANOVA model 和 significance。
- `lsd_grouping`：ANOVA 后的 LSD grouping，沿用旧脚本的分析思路。
- `spatial_adjustment`：ranges/pass coverage 和轻量空间校正诊断。
- `stability`：当 location count 支持时进行 multi-location stability 分析。

章节状态：

```text
completed
completed_with_warnings
not_applicable
failed
skipped
```

回复用户时，先总结章节状态，并邀请用户继续追问。默认不要倾倒所有记录。

## 渐进式追问

对于手工本地 R 运行，runner 会在 output directory 中写入 active session file：

```text
.field-analysis-session.json
```

如果用户提出追问且没有提供新的 input file，读取这个 active session 并使用其中的 `active_report`。不要再次要求用户提供 JSON path。

常见追问使用 query helper：

```powershell
Rscript `
  .\skill\field-analysis\scripts\query_active_report.R `
  --output-dir outputs\field-analysis `
  --trait T002 `
  --section quality,descriptive `
  --top-n 10
```

在项目后端 `python_subprocess` 路径中，输出由 Skill artifacts 管理。对于需要完整报告事实的追问，优先使用当前对话的结构化摘要；如果运行中的 skill 无法访问 active report，再要求用户上传/提供之前的 `field-analysis-report-v1` JSON。

查询映射：

- 质量问题：读取 `chapters.data_quality`、`traits.trait_summary` 和 `locations.summary_by_trait`。
- 描述统计问题：读取 `traits`、`materials` 和 `locations`。
- check comparison 问题：读取 `materials.by_trait` 和 `locations.materials_by_trait`。
- ANOVA 问题：读取 `analyses.anova`。
- LSD 问题：读取 `analyses.lsd_grouping`。
- 空间问题：读取 `analyses.spatial_adjustment`。
- 稳定性问题：读取 `analyses.stability`。

如果 active session file 缺失，要求用户先运行分析或提供 report file。

## 数据对象

`traits` 替代旧 experiment-stat table：

```text
trait_summary_fields
trait_summary
```

`materials` 替代旧 experiment-material summary：

```text
material_summary_fields
by_trait
```

`locations` 替代旧 location stat 和 location-material summary tables：

```text
location_summary_fields
summary_by_trait
material_location_fields
materials_by_trait
```

`analyses` 包含 model results：

```text
anova
lsd_grouping
spatial_adjustment
stability
```

重复数据集使用 catalog/matrix pattern：`*_fields` 只定义一次字段顺序，records 是按该顺序排列的数组。字段名必须是有意义的 snake_case。

## 输出策略

默认输出保持稀疏：

- 删除空 sections。
- 删除全为 null 的字段。
- 不输出空 message 字段。
- 将重复值移动到 grouping 或 section metadata。
- 只有当部分缺失值有意义时才保留 `null`。

## 解读指引

按以下顺序开始：

1. data quality 和 CV risks。
2. 按 trait 排名的 top materials。
3. check comparison。
4. 仅在 model status 支持时展示 ANOVA/LSD。
5. 将 spatial adjustment 和 stability 作为 follow-up chapters。

可主动提供的追问：

- "哪些材料产量最高？"
- "哪些性状数据质量最好？"
- "哪些材料超过 check？"
- "哪些性状材料差异显著？"
- "空间校正后排名有没有变化？"
- "哪些材料跨地点表现稳定？"

## 文件

所有脚本都直接位于 `scripts/` 下：

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

已注册 Skill 仅保留 runtime 文件：`SKILL.md`、`agents/openai.yaml` 和 `scripts/`。
