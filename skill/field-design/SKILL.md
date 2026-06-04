---
name: field-design
capability_id: skill.field_design
display_name: 试验设计智能体
description: >-
  基于规范的 CSV/Excel 材料清单生成田间试验设计，包括随机区组设计、对角线增广设计和间比法设计；适用于准备或运行试验设计流程、设置重复/区组、配置对照比例与对照起始位置/间隔、生成 fieldbook、种植顺序、蛇形或笛卡尔布局以及 HTML 田间布局预览。
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
public_usage:
  overview: >-
    使用上传的材料清单生成田间试验设计，支持随机区组、对角线增广和间比法；也可以只回答材料字段、设计参数和输出格式等用法问题。
  input_formats:
    - name: material_data
      required: true
      description: >-
        CSV 或 Excel（.xls/.xlsx）材料清单；每行代表一个试验材料或对照材料。推荐列名为 ped_id、hyb_check、set，对应中文含义是样本名称/材料代号、是否对照/材料类型标记、试验分组/集合。
      example_columns: [ped_id, hyb_check, set]
      notes:
        - hyb_check 可用于标记普通材料、对照材料或杂交检查分类；具体取值应与用户数据字典保持一致。
    - name: design
      required: true
      description: 设计类型，可选随机区组、对角线增广或间比法。
      examples: [rcbd, diagonal, interval]
  parameters:
    - name: blocks
      description: 随机区组设计的重复数或区组数；用户说“3 个重复”时通常对应 blocks=3。
    - name: ncols
      description: 田间布局列数；未提供时可由系统按默认布局策略处理。
    - name: ck_spec
      description: 间比法或对照布置规则，例如对照起始位置、间隔或对照材料组合。
    - name: planter
      description: 种植路径，默认蛇形排布，也可按用户要求使用直线或笛卡尔布局。
  examples:
    - /field-design hyb_check 怎么填？
    - /field-design 用这个 CSV 做 RCBD，3 个重复
    - /field-design 生成对角线增广设计，并给出 HTML 布局预览
  outputs:
    - 田间 fieldbook CSV
    - 种植顺序和小区布局说明
    - HTML 田间布局预览
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
      - '(?:blocks?|区组数|区组|重复数|重复|reps?|replications?)\s*[:：=]?\s*([零〇一二两三四五六七八九十百千万萬壹贰叁肆伍陆柒捌玖拾佰仟]+)'
      - '([零〇一二两三四五六七八九十百千万萬壹贰叁肆伍陆柒捌玖拾佰仟]+)\s*(?:个|次)?(?:区组|重复|rep|reps|blocks?)'
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

使用此 Skill 通过已声明的 Python wrapper 和随包 R 脚本运行三类 field-design 工作流：

- `RCBD`: randomized complete block design / 随机完全区组设计。
- `Diagonal`: diagonal augmented design / 对角线增广设计。
- `Interval`: interval contrast design / 间比法设计。

项目后端执行 `scripts/run_field_design.py`。不要声明 `runtime: r`，也不要要求主代理执行 Markdown 代码块。wrapper 接收 JSON stdin，解析用户上传的 CSV/Excel 材料数据，通过 `Rscript` 调用随包 R 脚本，并返回包含 `answer`、10 行预览以及可下载 CSV/HTML artifact 的 JSON 对象。

## 欢迎语

当此 Skill 启动面向用户的试验设计任务，或用户在材料清单和参数不完整时调用 `$field-design`，先用下面的精确文本用中文问候用户。不得改写、概括、扩写、缩短、本地化或添加额外句子。

```text
欢迎使用试验设计智能体。目前支持随机区组试验设计（RCBD）、对角线增广试验设计和间比法试验设计（Interval）。你只需要提供试验材料清单，并告诉我要做哪一种设计即可开始：如果做 RCBD，请提供区组数/重复数；如果做对角线增广设计，请提供田块列数 ncols；如果做间比法设计，请先提供材料清单和田块列数 ncols，我会识别 CK 后请你按编号补充每个 CK 的起始位置和间隔数量。

需要的材料表推荐列名是：

ped_id,hyb_check,set

对应中文含义是：

样本名称/材料代号(ped_id),是否对照/材料类型标记(hyb_check),试验分组/集合(set)

你可以直接上传 CSV/Excel 材料文件，或者把材料表粘贴过来。
```

如果用户提供的信息不足，只询问缺失项：

- 上传的材料清单文件或粘贴的材料表。
- 设计类型：`RCBD`、`Diagonal` 或 `Interval`。
- 设计参数，例如 RCBD 所需的 `blocks`，Diagonal 所需的 `ncols`、`ck_ratio`、`planter`、`randomize` 和 `seed`，或 Interval 所需的 `ncols` 与 CK interval 参数。首次交互不要询问 Interval 的 `reps`。

对于裸 `$field-design` 调用，只展示上面的精确欢迎语。必需输入可用前不要运行脚本。

当已声明的 Python wrapper 检测到缺少必需用户输入时，必须按 `Skill构建指南.md` 返回结构化 `missing_input` contract：`ok: false`、`is_error: true`、`error.type: missing_input`、使用 manifest 参数名的 `missing`，以及用户可读的 `answer`。对于 Interval 设计，CK 查询提示（`status: needs_ck_parameters`）仍属于缺失输入，不是成功设计结果。

## 选择工作流

当请求提到 randomized complete block design、随机区组、RCBD、blocks、reps、replicates，或每个 entry 都重复出现在完整区组中时，使用 `RCBD`。

当请求提到 diagonal augmented design、对角线增广设计、diagonal checks、ck_ratio，或要求按田块列数 `ncols` 沿对角线布置对照时，使用 `Diagonal`。

当请求提到 interval contrast design、间比法、CK 起始位置、check intervals，或按起始位置和间隔固定插入 CK 时，使用 `Interval`。

## 工作流

每个设计请求都按以下步骤处理：

1. 确认输入材料清单、设计类型和必需参数。
2. 运行已声明的 Python wrapper，由它调用匹配的随包 R 设计脚本。
3. 将生成的 JSON 只视为内部中间结果。
4. 从生成的 JSON 渲染匹配的 HTML layout preview。
5. 从生成的 JSON 导出完整 planting-order fieldbook CSV。
6. 在对话中只用 Markdown table 展示前 10 行 planting-order 结果。
7. 提供完整 CSV fieldbook 和 HTML layout preview 的链接。

默认用户回答中不要展示或链接 JSON 结果。只有用户明确索要内部源文件或调试细节时，才提及 JSON 路径。

## 输出策略

使用渐进式输出流程：

1. `*_result.json`：用于渲染和导出的内部事实源。
2. `*_layout.html`：面向用户的完整可视化 layout preview。
3. `*_fieldbook.csv`：面向用户的完整 planting-order fieldbook。

最终回复先说明设计模式和核心参数，再展示 10 行预览表，最后提供 CSV 和 HTML 输出链接。默认不要把 JSON 放进回答。

## 输入 Schema

新的输入文件优先使用且只使用以下列：

```text
ped_id,hyb_check,set
```

字段解释：

- `ped_id`：样本名称/材料代号；material ID。
- `hyb_check`：是否对照/材料类型标记；check marker。
- `set`：试验分组/集合；group ID；优先使用 `A`、`B`、`C` 等字母，不要只用数字型 set。

对于 `RCBD`，将 `hyb_check = 0` 解释为试验材料，非零值解释为 checks。RCBD 也接受 `ped_id + design_check`，以及 legacy `plot_id + hyb_check` schema。如果缺少 `set`，RCBD 会补充 `set = "A"`。

对于 `Diagonal`，要求 `ped_id,hyb_check,set`。将 `hyb_check = 0` 解释为试验材料，`1` 解释为在此工作流中按普通材料处理的非对角线标记，`2` 解释为 diagonal check material。至少需要一个 `hyb_check = 2`，且至少需要一个非 `2` entry。

对于 `Interval`，要求 `ped_id,hyb_check,set`。将 `hyb_check = 0` 解释为试验材料，非零值解释为 CK。CK 的 `ped_id` 必须全局唯一。Interval 设计按每个 `set` 独立生成，CK 参数通过唯一 `ped_id` 匹配。

## 运行 RCBD

必需参数：

- `--input`：CSV 或 Excel 材料清单。
- `--blocks`：重复数/区组数。RCBD 当前只支持 `1`、`2` 或 `3`。

使用 `--planter serpentine` 表示蛇形排列，使用 `--planter cartesian` 表示顺序排列 / 笛卡尔排列。

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

渲染 RCBD HTML preview：

```powershell
Rscript `
  "$skillDir/scripts/render_rcbd_interval_layout_html.R" `
  --input "$outDir/rcbd_result.json" `
  --output "$outDir/rcbd_layout.html" `
  --title "Field Design RCBD Layout"
```

导出完整 RCBD fieldbook CSV：

```powershell
Rscript `
  "$skillDir/scripts/json_to_fieldbook_csv.R" `
  --input "$outDir/rcbd_result.json" `
  --design rcbd `
  --output "$outDir/rcbd_fieldbook.csv"
```

在对话中只报告前 10 行 planting-order fieldbook，然后链接完整 CSV 和 HTML preview。除非用户索要，不要链接或展示 JSON 结果。除非用户要求 physical layout order，否则不要按 `ranges,pass` 重新排序对话中的 fieldbook。

## 运行 Diagonal

必需参数：

- `--input`：包含 `ped_id,hyb_check,set` 的 CSV 材料清单。
- `--ncols`：田块列数。

默认值：

- `--ck-ratio A`：低密度 checks。
- `--planter serpentine`：蛇形 planting order。
- `--randomize true`：随机化试验材料。

只有当用户要求保留清单顺序时才使用 `--randomize false`，例如“按我的清单顺序”、“不要随机”或“保持原始顺序”。

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

渲染 diagonal HTML preview：

```powershell
Rscript `
  "$skillDir/scripts/render_diagonal_layout_html.R" `
  --input "$outDir/diagonal_result.json" `
  --output "$outDir/diagonal_layout.html" `
  --title "Field Design Diagonal Layout"
```

导出完整 diagonal fieldbook CSV：

```powershell
Rscript `
  "$skillDir/scripts/json_to_fieldbook_csv.R" `
  --input "$outDir/diagonal_result.json" `
  --design diagonal `
  --output "$outDir/diagonal_fieldbook.csv"
```

生成内部 diagonal 结果 JSON 后，始终在对话中只用 Markdown table 展示前 10 行 diagonal design。读取生成 JSON 中的 `out_design` 并保持 planting order；除非用户明确要求 physical layout order，否则不要按 `ranges,pass` 重新排序。包含以下列：

```text
plots | ped_id | hyb_type | ranges | pass | set | design_check
```

每个 diagonal design 都要在对话中展示前 10 行 planting-order 结果，然后提供完整 CSV fieldbook 路径/链接和 HTML preview 路径/链接。不要因为已有 HTML preview 就跳过该表。除非用户明确索要，不要在对话中展示或链接原始 JSON。

`ck_ratio` 等级含义：

- `A`：目标 `5% - 10%` checks。
- `B`：目标 `10% - 15%` checks。
- `C`：目标 `15% - 20%` checks。

从用户请求的等级开始，默认使用 `A`。如果请求等级无法满足 diagonal layout，脚本会自动从 `A` 升级到 `B`，再升级到 `C`。始终告知用户请求等级、实际使用等级、是否自动升级，以及实际 check ratio/percent。

不要为 diagonal design 暴露 `filler`、`filler.end` 或 multi-site 参数。对于多个独立 diagonal designs，应以不同 seeds 多次运行 `run_diagonal_local.R`，并返回相互独立的设计表。

## 运行 Interval

必需参数：

- `--input`：包含 `ped_id,hyb_check,set` 的 CSV 材料清单。
- `--ncols`：田块列数。
- CK 列表之后提供的 CK 参数：`ck_no,start_pos,interval`。

默认值：

- `--planter serpentine`：蛇形 planting order。
- `--randomize true`：随机化试验材料。

### 步骤 1：列出 CK 材料

用户提供材料清单并请求 Interval design 后，先运行 CK 列表步骤，再询问 CK 参数：

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

读取生成的 `ck_table`，并按以下形式展示给用户：

```text
ck_no | ped_id | set
```

然后要求用户按以下格式提供 CK 参数：

```text
ck_no,start_pos,interval
```

多个 CK 可用分号分隔，例如：

```text
1,1,10; 2,5,10
```

### 步骤 2：确认 CK 参数

用户提供 `ck_no,start_pos,interval` 值后，解析这些值，并在运行设计前展示确认表：

```text
ck_no | ped_id | set | start_pos | interval
```

使用下面这句中文确认语：

```text
请确认以上 CK 起始位置和间隔数量是否正确。确认后我再开始生成间比法设计。
```

用户确认前不要运行最终 Interval design。

### 步骤 3：生成 Interval Design

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

Interval 脚本每次调用只生成一个 single-run design。第一次设计完成后，如果用户要求多个重复，则用不同 seeds 和输出名按每个重复运行同一命令一次。在每个重复的 fieldbook 中添加或保留 `r` 作为重复编号。例如，如果用户随后要求 `reps = 2`，重复 1 使用 `--seed 20260519 --output "$outDir/interval_result_r1.json"`，重复 2 使用 `--seed 20260520 --output "$outDir/interval_result_r2.json"`。

使用共享的 RCBD/Interval layout renderer 渲染 Interval HTML preview，因为两个设计使用相同的可见 fieldbook 列：

```powershell
Rscript `
  "$skillDir/scripts/render_rcbd_interval_layout_html.R" `
  --input "$outDir/interval_result.json" `
  --output "$outDir/interval_layout.html" `
  --title "Field Design Interval Layout"
```

导出完整 Interval fieldbook CSV：

```powershell
Rscript `
  "$skillDir/scripts/json_to_fieldbook_csv.R" `
  --input "$outDir/interval_result.json" `
  --design interval `
  --output "$outDir/interval_fieldbook.csv"
```

每个 Interval design 都只在对话中展示前 10 行 planting-order rows，并使用以下列：

```text
plots | r | ped_id | ranges | pass | set | hyb_check | hyb_type
```

对于 Interval layout previews，将所有 sets 保持在一个连续 field layout 中。不要因为 set 变化就强制换到新行；下一个 set 应从下一个可用 planting position 继续。CK 单元格应保留现有黄色 check 色系，但按 CK `ped_id` 使用插值色阶变化，使不同 CK 在不脱离 check 色系的前提下可区分。

第一次 Interval design 完成后，再询问用户是否需要多个重复。不要在第一次设计完成前询问。使用下面这句中文：

```text
当前已完成 1 轮间比法设计。是否需要生成多个重复设计？如果需要，请告诉我重复数。例如 3 个重复将生成 3 个单独结果，且重复之间使用不同随机种子。
```

对于 Interval，`reps` 只表示 `重复`。不要称为 blocks 或区组。如果用户要求 `reps = 3`，通过以不同 seeds 和输出名调用 single-run script 3 次，生成 3 个独立 Interval results。除非用户明确要求合并，否则每个重复都保留为独立结果。

### Interval 校验规则

最终设计前：

- material data 必须包含 `ped_id,hyb_check,set`。
- `ncols` 必须可用。
- `ck_table` 中的每个 CK 都必须收到 `start_pos` 和 `interval`。
- `start_pos` 和 `interval` 必须是正整数。
- 每个 `set` 都独立检查 CK position overlap。

如果检测到 CK position overlap，停止并要求用户调整相关 CK 的起始位置或间隔。

## 报告输出

每次运行都报告：

- 使用的 design mode：`RCBD`、`Diagonal` 或 `Interval`。
- input path、核心参数和 seed。
- 完整 CSV fieldbook path 与 HTML preview path。
- 对于 `Diagonal`，报告 requested `ck_ratio`、used `ck_ratio`、`auto_upgraded` 和实际 check percent。
- 对于 `Interval`，报告已确认的 CK 参数表。
- 只包含前 10 行 planting-order rows 的简洁 fieldbook table。该表从 JSON `out_design` 生成，但不要展示原始 JSON result。

使用生成的 JSON 作为内部事实源，并作为 HTML 与 CSV 渲染输入。重要输出字段包括：

- RCBD CSV 列：`plots`, `r`, `ped_id`, `ranges`, `pass`, `set`, `hyb_check`, `hyb_type`。
- Diagonal CSV 列：`plots`, `ped_id`, `hyb_type`, `ranges`, `pass`, `set`, `design_check`。
- Interval CSV 列：`plots`, `r`, `ped_id`, `ranges`, `pass`, `set`, `hyb_check`, `hyb_type`。

报告结果时，默认绝不粘贴 JSON 内容。将 CSV 和 HTML 链接作为面向用户的完整输出。

## 环境

从当前环境 `PATH` 使用 `Rscript`。正常 workflow examples 中不要硬编码 machine-specific R installation path。当前已验证 Windows 环境使用 R `4.5.3`；目标 Mac 部署只要安装了 `jsonlite`，即可使用当前 Mac R release。

必需 R package：

- `jsonlite`
