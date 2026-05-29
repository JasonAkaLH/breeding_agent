# 表格上传编码兼容与表头规范化总纲 PRD

- **项目**：breeding_agent
- **范围**：父总纲 / 后端上传解析 / 表格预览 / Skill artifact 输入 / Interrupt sheet 选择 / prompt-safe 摘要上限 / API 文档
- **文档状态**：父总纲 PRD（已拆分为阶段 PRD，待逐阶段实施）
- **日期**：2026-05-29
- **关联模块**：`src/api/upload_store.py`、`src/api/routes/uploads.py`、`src/api/runtime.py`、`src/integrations/agent_skills/`、`src/capabilities/main_agent/`、`frontend/`
- **阶段目录**：`docs/prd/backend/table-upload-normalization/README.md`
- **关联文档**：
  - `docs/prd/backend/05-API与核心数据模型.md`
  - `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`
  - `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`
  - `docs/prd/backend/15-SkillExecutor实现需求PRD.md`
  - `docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md`
  - `docs/api/api-doc.html`

## 1. 一句话结论

系统应在上传层统一兼容 CSV / JSON / Excel 表格文件的常见编码与技术性表头污染，保留原始文件 bytes，但给 Skill 执行链路提供规范化后的 UTF-8 CSV / JSON 内容；多 sheet Excel 不自动猜测 sheet，而是通过 interrupt 让用户明确选择。

本 PRD 不做业务语义列名映射：`材料编号` 不会被系统猜成 `ped_id`。本 PRD 只解决“列名本来就是 `ped_id`，但被 BOM、外层引号、不可见字符、全角空格或编码格式污染”的问题。

## 2. 背景

当前上传链路中，`InMemoryUploadStore.save()` 对 CSV / JSON 文本文件只执行 `content.decode("utf-8")`，CSV 预览直接使用 `csv.DictReader` 的 `fieldnames`。因此 Excel 或其他工具导出的 CSV 只要带有 BOM、引号包装或非 UTF-8 编码，就可能出现表头无法被 Skill 识别的问题。

典型失败形态：

```text
预览列名：﻿"ped_id"、hyb_check、set
Skill 期望：ped_id、hyb_check、set
执行失败：输入数据缺少必需的列 ped_id
```

这类问题不是用户业务数据缺失，而是系统没有在进入 Skill 前完成文件格式兼容和表头技术清洗。若要求用户重新保存 UTF-8 without BOM 或手动改表头，会显著降低业务体验，也会让 Agent 给出错误诊断。

现有链路已经具备较清晰的系统边界：

1. API 上传层负责文件类型、大小、预览与 `UploadedFileRecord`。
2. `ApiRuntime.resolve_uploads_for_message()` 会把 prompt-safe 的 `uploaded_artifacts` 和执行专用的 `skill_artifacts` 注入任务 metadata。
3. Skill 执行器只消费 `skill_artifacts[].content` / `content_base64`，不应关心用户原始文件编码和 Excel sheet 解析。
4. 用户已明确要求不修改 `skill/**` 内的 Skill bundle。

因此，应把该能力放在系统上传 / 解析层，而不是让每个 Skill 重复兼容编码、BOM、Excel 或脏表头。

## 3. 目标

### 3.1 产品目标

1. 用户上传 Excel 导出的 CSV、GBK / GB18030 CSV、Big5 CSV、Shift-JIS CSV、`.xlsx` 或 `.xls` 后，系统按第 8 节文件类型规则与第 9 节编码候选规则识别并继续执行。
2. Agent 不再把 `﻿"ped_id"` 这类技术性表头污染误报为用户缺少 `ped_id`。
3. 多 sheet Excel 不盲目选择或合并；系统应向用户展示可选 sheet 并要求选择。
4. 用户仍只需要“上传文件 + 提问”，不需要理解编码、BOM、Excel 引擎或表头清洗细节。

### 3.2 工程目标

1. 新增系统级表格上传规范化边界，避免编码兼容逻辑散落在 Skill、主代理 prompt 或领域脚本里。
2. 原始上传 bytes 保留，`sha256` 仍基于原始 bytes，便于审计与排查。
3. Skill 执行通道只接收规范化后的 UTF-8 CSV / JSON 文本。
4. 预览、prompt-safe artifact、audit 和 API response 只暴露脱敏摘要，不暴露完整文件内容。
5. 不引入业务列名映射，不让系统凭语义猜测字段。
6. 支持 `.xlsx` / `.xls`，并以受控依赖方式读取 Excel。

### 3.3 受影响用户、系统与置信标准

受影响用户与系统：

| 对象 | 影响 |
|---|---|
| 终端业务用户 | 可上传常见 CSV 编码与 Excel 文件；多 sheet Excel 需要明确选择 sheet。 |
| API 客户端 / 前端 | 上传响应新增向后兼容字段；多 sheet 场景会出现 `sheet_selection_required` interrupt。 |
| API 上传层 | 负责文件类型判定、编码解码、表头技术清洗、Excel sheet metadata 与安全预览。 |
| 任务 runtime | 负责把 prompt-safe `uploaded_artifacts` 与执行专用 `skill_artifacts` 分开，并在未选择 sheet 时阻断 Skill 执行。 |
| Skill 执行适配层 | 只消费规范化后的执行 artifact；不承担编码、Excel 或业务列名映射。 |
| 主代理 prompt / 对话记忆 | 只能看到有上限的脱敏摘要，不能看到完整文件内容。 |
| 文档与测试 | API 静态文档、前后端类型、API/runtime/前端回归需要同步。 |

实施置信标准：

1. 每个阶段都必须有可独立运行的回归测试，且不能依赖修改 `skill/**`。
2. prompt-safe 上传摘要必须有硬上限，避免宽表、多 sheet 或大量清洗记录进入主代理 prompt 后放大上下文。
3. 未完成 sheet 选择时，系统必须 fail closed：不得把未确定 sheet 的 Excel 传给 Skill，也不得让 LLM 猜测。
4. 规范化只改变文件格式技术噪声；业务字段语义不变。
5. 所有错误、审计与事件 payload 不得包含完整文件行数据、规范化全文或原始 bytes。

## 4. 非目标

1. 不做业务语义表头映射：不把 `材料编号`、`品种编号`、`是否对照` 等自动映射为 `ped_id`、`hyb_check` 或 `design_check`。
2. 不读取或复用 `SKILL.md` 的 `parameters.aliases` 来处理文件内部列名；参数别名与文件表头是两条独立语义链路。
3. 不修改 `skill/**` 内任何 Skill bundle。
4. 不合并多个 Excel sheet。
5. 不让 LLM 推断表头映射或 sheet 选择。
6. 不执行 Excel 宏、不刷新公式、不访问外部链接。
7. 不在本 PRD 中实现持久化上传文件存储；仍沿用当前 upload store 生命周期，除非后续另有 artifact/upload 存储专题。
8. 不把完整文件内容写入 audit、SSE 或主代理 prompt。

## 5. 术语

| 术语 | 定义 |
|---|---|
| 原始上传 bytes | 用户上传的原始文件字节，`sha256` 基于它计算。 |
| 规范化内容 | 系统解析后生成的 UTF-8 CSV / JSON 文本，供 Skill 执行使用。 |
| 技术噪声清洗 | 清除 BOM、外层成对引号、首尾空白、全角空格、不可见控制字符、Unicode 兼容字符差异等非业务污染。 |
| 业务表头映射 | 把一个业务字段名推断为另一个字段名，例如 `材料编号 -> ped_id`。本 PRD 不做。 |
| 有效 sheet | Excel 中存在可用表头行的 sheet；空 sheet 不进入选择列表。 |
| sheet 选择 interrupt | 多 sheet Excel 被引用执行时，系统发出的用户选择 sheet 的补充输入请求。 |

## 6. 当前代码证据

1. `src/api/upload_store.py` 当前支持的上传类型为 `json`、`csv`、`image`、`pdf`。
2. CSV / JSON 文本文件当前只按 UTF-8 解码；非 UTF-8 文本会被拒绝。
3. CSV 预览当前直接使用 `csv.DictReader(StringIO(content_text))`，不会清理 BOM、外层引号或不可见字符。
4. `UploadedFileRecord.to_summary()` 生成 prompt-safe 上传摘要；`to_skill_artifact()` 会把 `content_text` 放入执行专用 artifact。
5. `ApiRuntime.resolve_uploads_for_message()` 已把 `uploaded_artifacts` 与 `skill_artifacts` 分开注入 metadata。
6. `src/integrations/agent_skills/execution.py` 的执行专用 artifact allowlist 当前包含 `content` / `content_base64` / `encoding`，但还没有 normalized filename / original filename 等字段。
7. `requirements.txt` 当前已有 `pandas==3.0.2`，没有 `openpyxl` / `xlrd`。

这些证据表明：最小改造点应在上传解析与 artifact 投影层，不应进入 Skill 包内部。

## 7. 总体设计

### 7.1 新增表格上传规范化模块

新增窄模块，例如：

```text
src/api/table_upload_normalizer.py
```

该模块只负责：

1. 文件类型为 CSV / JSON / Excel 时解析原始 bytes。
2. 识别文本编码。
3. 清洗表头技术噪声。
4. 生成预览摘要与规范化内容。
5. 对 Excel 生成 sheet metadata 与可选的规范化 CSV 内容。

它不负责：

- 用户鉴权；
- upload quota；
- Skill 参数解析；
- 业务列名映射；
- LLM prompt；
- 文件持久化；
- 前端交互。

`InMemoryUploadStore.save()` 调用该模块后再创建 `UploadedFileRecord`。

### 7.2 原始文件与规范化内容分离

`UploadedFileRecord` 应保留原始字段：

- `filename`：用户上传的原始文件名；
- `content_type`：原始 content type；
- `file_type`：原始文件语义类型，可扩展为 `spreadsheet`；
- `content_bytes`：原始 bytes；
- `sha256`：原始 bytes 的 sha256。

同时新增或派生规范化字段：

- `normalized_content_text`：供 Skill 使用的 UTF-8 CSV / JSON 文本；
- `normalized_content_type`：例如 `text/csv`、`application/json`；
- `normalized_filename`：例如 `materials.csv` 或 `materials.Sheet1.csv`；
- `normalization`：编码、sheet、表头清洗、冲突、是否需要用户选择等摘要。

现有 `content_text` 可作为兼容字段继续承载规范化内容，但实现时应明确其语义：它不再等同于原始文件文本，而是“执行用规范化文本”。

### 7.3 Prompt-safe 与执行专用 artifact 投影

`uploaded_artifacts` 仍是 prompt-safe 摘要，不包含完整内容。字段示例：

```json
{
  "upload_id": "upl-xxx",
  "filename": "diagonal_test_20ncols.csv",
  "file_type": "csv",
  "content_type": "text/csv",
  "size_bytes": 1234,
  "sha256": "...",
  "preview": {
    "row_count": 20,
    "columns": ["ped_id", "hyb_check", "set"],
    "shape": "table",
    "source_encoding": "utf-8-sig",
    "column_normalizations": [
      {"original": "﻿\"ped_id\"", "normalized": "ped_id", "reason": "bom_and_outer_quotes"}
    ]
  }
}
```

`skill_artifacts` 是执行专用通道，可包含规范化内容：

```json
{
  "upload_id": "upl-xxx",
  "filename": "diagonal_test_20ncols.csv",
  "original_filename": "diagonal_test_20ncols.csv",
  "normalized_filename": "diagonal_test_20ncols.csv",
  "file_type": "csv",
  "content_type": "text/csv",
  "content": "ped_id,hyb_check,set\nA001,0,A\n"
}
```

对于 Excel 文件，prompt-safe 摘要保留原始 `filename`；执行专用 artifact 的 `filename` 应使用 `normalized_filename`（`.csv`），同时保留 `original_filename`，避免脚本看到 `.xlsx` 文件名却收到 CSV 文本。

投影不变量：

1. `uploaded_artifacts` 是唯一允许进入主代理 prompt、对话记忆候选、SSE 可见事件和普通 audit 的上传摘要通道；它必须剔除 `content`、`content_base64`、`raw`、`text`、本地路径、storage ref 与任何完整行数据。
2. `skill_artifacts` 是执行专用通道；它可以包含规范化 `content`，但不得被主代理 prompt、对话记忆、安全审计摘要或 SSE 直接消费。
3. 执行适配层的 artifact allowlist 必须显式加入本 PRD 新增且脚本确实需要的安全字段，例如 `original_filename`、`normalized_filename`、`file_type`、`normalized_content_type`；prompt-safe allowlist 不得因此放开完整内容字段。
4. 多 sheet Excel 未选择 sheet 时，执行专用 artifact 不得包含可执行 `content`；runtime 必须先进入 sheet 选择 interrupt。

### 7.4 Prompt-safe 摘要上限与上下文安全

为避免宽表、多 sheet 或大量清洗记录导致主代理上下文膨胀，所有进入 `uploaded_artifacts` 的表格摘要必须执行上限裁剪。默认口径：

| 字段 | 默认上限 | 超限行为 |
|---|---:|---|
| `preview.columns` | 50 列 | 只保留前 50 个规范化列名，并记录 `column_count` 与 `columns_truncated=true`。 |
| `preview.original_columns` | 50 列 | 与 `preview.columns` 同步裁剪，并记录真实列数。 |
| `preview.column_normalizations` | 50 条 | 只保留前 50 条变更映射，并记录 `changed_column_count` 与 `column_normalizations_truncated=true`。 |
| `preview.excel_sheets` | 20 个 sheet | 有效 sheet 不超过 20 个时完整列出；若有效 sheet 超过上限，上传应 fail closed 或要求用户拆分文件，不得只展示前 20 个后继续执行。 |
| `preview.excel_sheets[].columns` | 每个 sheet 50 列 | 与顶层列裁剪一致，记录每个 sheet 的 `column_count` / `columns_truncated`。 |

要求：

1. 上限值必须作为实现中的集中常量或配置项，不得散落硬编码。
2. `uploaded_artifacts` 的摘要裁剪不得影响 `skill_artifacts` 的规范化执行内容；执行内容仍受上传大小、解析大小与资源限制控制。
3. 审计与 API response 可以暴露同一套脱敏摘要字段，但不得暴露完整文件内容。
4. 测试必须覆盖宽表与多 sheet 摘要裁剪，确认主代理 prompt 中不存在 `content` / `content_base64`。

### 7.5 Sheet 选择状态与执行投影

多 sheet 选择必须按任务 / 本次提交作用域处理，不应把用户某一次任务中的选择永久写回原始 upload record：

1. `UploadedFileRecord` 保存原始 bytes、原始 hash、文件级 preview、sheet metadata，以及必要时可复算所选 sheet 规范化 CSV 的信息。
2. `resolve_uploads_for_message()` 需要能接收来自 `metadata.upload_sheet_selections` 或已接受 interrupt answer 的选择映射；没有选择且引用了多有效 sheet Excel 时，返回 pending sheet selection 信息，而不是返回可执行 `content`。
3. 提交消息或 interrupt resume 中如果同时引用多个未选择 sheet 的 Excel，系统可以用一个 interrupt 收集全部待选 upload 的 sheet 选择；在全部选择有效前不得执行 Skill。
4. 选择结果只对当前任务 / 当前提交的执行 metadata 生效；后续新任务复用同一 upload 时，如果客户端没有重新传入选择，仍应再次要求选择。
5. 删除 upload、权限校验、TTL 过期与 conversation owner 隔离仍以原始 upload record 为准。

## 8. 文件类型支持

### 8.1 CSV

支持来源：

- `.csv` 后缀；
- `text/csv`、`application/csv`、`application/vnd.ms-excel` 等 content type。

行为：

1. 在 preview byte limit 内进行编码识别与 CSV 解析。
2. 允许常见 CSV dialect：逗号、Tab、分号；输出统一为逗号分隔 UTF-8 CSV。
3. 第一行作为表头。
4. 表头清洗后生成新的第一行，数据行值不做业务变换。
5. 清洗后表头为空或重复时 fail closed。

### 8.2 JSON

支持来源：

- `.json` 后缀；
- `application/json`、`text/json`。

行为：

1. 走同一套多编码文本解码。
2. JSON 顶层为 object 或 array 时保持当前支持口径。
3. 顶层 object 的 key、以及顶层 array 中每个 object row 的 key 做技术噪声清洗；同一 object 内清洗后 key 冲突时 fail closed。
4. 值不做业务变换。
5. 规范化输出为 `json.dumps(..., ensure_ascii=False)` 的 UTF-8 JSON 文本。
6. 嵌套 object 默认不递归清洗，避免改变用户业务载荷内部结构；如果未来需要递归清洗，应另起 PRD 明确兼容风险。

### 8.3 Excel `.xlsx` / `.xls`

新增支持：

- `.xlsx`：依赖 `openpyxl`；
- `.xls`：依赖 `xlrd`。

要求：

1. 同步更新 `requirements.txt`，新增 `openpyxl` 与 `xlrd` 固定版本。
2. `.xlsx` 解析必须使用只读 / 不执行宏 / 不刷新外部链接的模式；公式只读取已有缓存值，不执行计算；无缓存公式按第 8.3 条第 9 点处理。
3. `.xls` 解析只读取单元格内容，不执行宏。
4. 每个 sheet 的第一条非空行作为候选表头；后续行作为数据行。
5. 有且仅有一个有效 sheet 时，自动选择该 sheet 并转为规范化 CSV。
6. 多个有效 sheet 时，上传成功但标记为 `requires_sheet_selection=true`；执行引用该上传时必须先发起 sheet 选择 interrupt。
7. 没有有效 sheet 时返回 400。
8. Excel 解析仍受上传文件大小、preview byte limit、最大 sheet 数、最大扫描行列数限制。
9. `.xlsx` 读取应使用只读和不执行计算的模式；公式单元格只读取已有缓存值。若候选表头行中的公式没有可用缓存值，则该 sheet 视为无有效表头；数据行中的无缓存公式按空值处理并记录 warning。
10. Sheet 名称进入 API / prompt-safe 摘要前必须作为普通字符串处理，不得作为路径或脚本参数拼接；生成 `normalized_filename` 时必须进行文件名安全化。

### 8.4 文件类型判定优先级

为兼容当前 `application/vnd.ms-excel` 既可能表示 CSV 也可能表示 `.xls` 的现实情况，类型判定必须遵守：

1. 明确后缀优先：`.json` -> `json`，`.csv` -> `csv`，`.xlsx` / `.xls` -> `spreadsheet`，图片 / PDF 保持现状。
2. 后缀缺失或不可信时，可结合 magic bytes 判定 Excel：`.xlsx`/Office Open XML zip 结构、`.xls` OLE2 结构可判为 `spreadsheet`。
3. 仅凭 `application/vnd.ms-excel` 且无 Excel 后缀 / magic bytes 时，为保持现有 CSV 兼容，应继续按 CSV 处理。
4. 无法判定的文本或二进制仍返回 400，不新增泛化文本上传。

## 9. 编码识别要求

文本表格应按确定性候选列表尝试解码，不使用替换字符静默吞错。

候选顺序：

1. `utf-8-sig`
2. `utf-8`
3. `gb18030`
4. `gbk`（可作为兼容别名；若实现认为 `gb18030` 已覆盖，可不重复尝试）
5. `big5`
6. `shift_jis`
7. `cp932`

要求：

- 只有完整解码成功才可接受；不得用 `errors="ignore"` 或 `errors="replace"` 伪成功。
- 记录 `source_encoding` 到 preview / audit 摘要。
- 如果多个编码都成功，使用候选顺序中最靠前者。
- 仍失败时返回 400：无法识别文本编码，请另存为 UTF-8 CSV 或 Excel 后重新上传。

## 10. 表头技术噪声清洗要求

### 10.1 清洗范围

对 CSV、JSON object key、Excel sheet 表头执行同一套技术清洗：

1. Unicode 兼容规范化：使用 `unicodedata.normalize("NFKC", value)`。
2. 移除 BOM：包括 `U+FEFF` 出现在任意表头位置的情况。
3. 移除零宽字符：例如 `U+200B`、`U+200C`、`U+200D`、`U+2060`。
4. 移除不可见控制字符；保留普通可见字符。
5. 全角空格转普通空格，并进行首尾 strip。
6. 移除外层成对引号：例如 `"ped_id"`、`'ped_id'`、`“ped_id”`、`‘ped_id’`。
7. 外层引号可重复剥离，直到不再完整包裹。

### 10.2 明确不做的事情

1. 不统一大小写：`Ped_ID` 不自动变成 `ped_id`。
2. 不把空格或横杠改成下划线：`Ped ID`、`ped-id` 不自动变成 `ped_id`。
3. 不做中文 / 英文业务别名映射。
4. 不基于 Skill 名称、Skill 参数、LLM 或用户意图推断列名。

因此：

| 原始表头 | 规范化后 | 原因 |
|---|---|---|
| `﻿"ped_id"` | `ped_id` | BOM + 外层引号 |
| `　ped_id　` | `ped_id` | 全角空格 + strip |
| `ped_id​` | `ped_id` | 零宽字符 |
| `Ped ID` | `Ped ID` | 不做命名猜测 |
| `材料编号` | `材料编号` | 不做业务映射 |

### 10.3 冲突处理

如果同一表内表头清洗后为空或出现重复，必须 fail closed。

示例：

```text
原始列：﻿"ped_id", ped_id
清洗后：ped_id, ped_id
结果：400，表头清洗后重复，用户需修正文件
```

不得保留第一个、覆盖第二个或自动重命名为 `ped_id_2`，因为这会污染业务数据。

## 11. 多 sheet Excel 交互要求

### 11.1 上传响应

多 sheet Excel 上传不应直接失败。上传响应应包含：

- `file_type=spreadsheet`；
- `preview.requires_sheet_selection=true`；
- `preview.excel_sheets[]`，每项包含：
  - `sheet_name`；
  - `row_count`；
  - `columns`；
  - `column_normalizations`；
  - `has_header`。

### 11.2 执行时 interrupt

当任务引用了仍未选择 sheet 的 Excel upload：

1. 不应直接执行 Skill。
2. 系统应创建 open interrupt，`reason_code=sheet_selection_required`。
3. interrupt question 应列出文件名、可选 sheet、每个 sheet 的表头预览和行数。
4. `required_fields` 应包含稳定字段；嵌套 options 必须可被前端明确渲染，不能只依赖自由文本，例如：

```json
{
  "upload_sheet_selections": {
    "type": "object",
    "required_upload_ids": ["upl-xxx"],
    "options_by_upload_id": {
      "upl-xxx": ["Sheet1", "材料表", "说明"]
    },
    "labels_by_upload_id": {
      "upl-xxx": "diagonal.xlsx"
    }
  }
}
```

### 11.3 Resume

用户回答后，`answer_payload.upload_sheet_selections` 必须校验：

- upload_id 属于当前 conversation / user；
- sheet_name 存在；
- sheet 解析后未发生空表头或表头冲突；
- 选择结果只影响本 upload 的规范化执行内容，不修改原始 bytes。

成功后，同一任务 resume 时使用所选 sheet 的规范化 CSV 继续执行。

answer payload 示例：

```json
{
  "upload_sheet_selections": {
    "upl-xxx": "材料表"
  }
}
```

如果选择缺失、sheet 不存在、upload 不属于当前 conversation / user、upload 已过期，或者选择后表头无效 / 冲突，系统必须 fail closed，不得继续执行 Skill。

### 11.4 可选预选择

如果前端未来想在提交消息前让用户选择 sheet，可通过 `metadata.upload_sheet_selections` 传入相同结构。后端仍必须按 11.3 校验，不能信任客户端。

## 12. API / DTO 影响

### 12.1 `UploadPreviewResponse`

现有字段：

- `row_count`
- `columns`
- `shape`

必须新增可选字段：

- `source_encoding: string | null`
- `original_columns: list[string]`
- `column_normalizations: list[object]`
- `normalized_content_type: string | null`
- `normalized_filename: string | null`
- `selected_sheet: string | null`
- `requires_sheet_selection: boolean`
- `excel_sheets: list[object]`
- `warnings: list[string]`
- `column_count: int | null`
- `changed_column_count: int | null`
- `sheet_count: int | null`
- `columns_truncated: boolean`
- `column_normalizations_truncated: boolean`
- `excel_sheets_truncated: boolean`

新增字段必须向后兼容：旧客户端忽略即可。

`selected_sheet` 字段在上传 / 列表响应中只表达 upload record 自身状态：单有效 sheet 自动选择时可填该 sheet；多有效 sheet 未选择时为 `null`。通过 interrupt answer 或 `metadata.upload_sheet_selections` 得到的 task-scoped 选择不得永久改写 upload list 中的 `selected_sheet`。

### 12.2 `UploadFileResponse`

`file_type` 应扩展支持：

- `json`
- `csv`
- `spreadsheet`
- `image`
- `pdf`

Excel 上传的 public response `filename` 保留原始文件名；执行专用 artifact 使用 `normalized_filename`。

### 12.3 API 文档

`docs/api/api-doc.html` 必须补充：

- CSV/JSON 多编码兼容；
- CSV/JSON/Excel 表头技术清洗；
- Excel `.xlsx` / `.xls` 支持；
- 多 sheet Excel 的 interrupt 行为；
- `UploadPreviewResponse` 新增字段。
- prompt-safe preview 上限与 `*_truncated` 字段语义。

## 13. 安全、隐私与资源限制

1. 原始文件内容、规范化全文、原始 bytes、base64 内容不得进入主代理 prompt、SSE、普通 audit、对话记忆或错误详情。
2. 表头、编码、sheet 名、行列数、清洗映射可以进入 prompt-safe 上传摘要；但必须先按 7.4 的硬上限裁剪，且不得包含真实文件全文。
3. Excel 读取不得执行宏、外部链接或公式计算。
4. `.xlsx` 解压与解析必须受上传文件大小、preview byte limit、最大 sheet 数、最大扫描行列数约束，防止压缩炸弹或超大工作簿拖垮服务。
5. 表头冲突必须 fail closed。
6. 编码识别不得用 ignore / replace 模式静默吞错。
7. 新增依赖必须同步进入 `requirements.txt`，并在最终实现验证中记录 License Requirement。
8. 如果未来将上传 store 下沉到 Rust safety kernel，本 PRD 的表头清洗、编码选择、sheet 选择和 no-raw audit 口径必须保持同构。
9. 任何错误详情只允许包含错误类型、文件名、sheet 名、列数 / 行数等摘要；不得回显行数据或完整表头列表以外的单元格内容。

## 14. 观测与审计

必须新增脱敏审计事件或扩展现有 upload audit metadata：

```json
{
  "event": "upload.table_normalized",
  "upload_id": "upl-xxx",
  "file_type": "csv",
  "source_encoding": "utf-8-sig",
  "selected_sheet": null,
  "requires_sheet_selection": false,
  "column_count": 3,
  "row_count": 20,
  "changed_column_count": 1,
  "normalization_reasons": ["bom", "outer_quotes"]
}
```

要求：

- 不记录完整 `content`。
- 不记录原始表格行数据。
- 表头可记录，因为表头已在 preview 中展示；但必须遵守 7.4 的列数 / sheet 数裁剪上限。如未来表头可能含敏感信息，应增加脱敏开关。
- 编码失败、表头冲突、Excel 解析失败应记录错误类型，不记录文件内容。
- 对于 prompt-safe 摘要裁剪，审计必须记录真实数量与 `*_truncated` 标记，便于排查“为什么前端 / prompt 只看到部分列或 sheet”。

## 15. 兼容与迁移

1. 现有 CSV / JSON 上传 happy path 不应改变用户可见行为；区别是 `content_text` 变为规范化文本。
2. image / PDF 上传行为不变。
3. 原有 `sha256` 仍基于原始 bytes，不能改为规范化文本 hash。
4. 当前 upload store 为内存/TTL 模型，本 PRD 不要求数据库迁移。
5. 前端可逐步展示新增 preview 字段；不展示时不影响执行。
6. Skill bundle 不需要修改；如果某个 Skill 仍因真实业务列缺失而失败，该失败应保留。
7. `application/vnd.ms-excel` 无后缀 / 无 Excel magic bytes 的历史 CSV 上传行为应保持兼容，不能被误判为 `.xls`。
8. 多 sheet 选择不写成全局 upload 状态；同一 upload 在新任务中复用时仍需要显式选择或由客户端传入选择映射。

## 16. 验收标准

| ID | 验收标准 |
|---|---|
| AC-01 | 上传 `﻿"ped_id",hyb_check,set` 表头的 CSV 后，preview columns 为 `ped_id,hyb_check,set`，Skill artifact content 第一行为 `ped_id,hyb_check,set`。 |
| AC-02 | 上传 GB18030 / GBK CSV 后，中文数据与表头可正常预览，`source_encoding` 被记录。 |
| AC-03 | 上传 Big5 / Shift-JIS CSV 后，若内容可完整解码，则正常预览并生成规范化 UTF-8 CSV。 |
| AC-04 | 表头清洗后为空或重复时返回 400，不生成 upload record。 |
| AC-05 | JSON 顶层 object / 顶层 object array 的 key 经过技术清洗；同级 key 清洗后冲突时返回 400；嵌套 object 不被递归改写。 |
| AC-06 | 上传单 sheet `.xlsx` 后，public response 说明原始文件为 spreadsheet，执行专用 artifact 是规范化 CSV。 |
| AC-07 | 上传单 sheet `.xls` 后，行为与 `.xlsx` 一致。 |
| AC-08 | 上传多有效 sheet Excel 后，上传成功但执行引用时产生 `sheet_selection_required` interrupt，不直接运行 Skill。 |
| AC-09 | 用户选择 sheet 后，任务 resume 使用所选 sheet 的规范化 CSV 继续执行。 |
| AC-10 | 原始 `sha256` 与原始 bytes 一致，不因规范化改变。 |
| AC-11 | prompt-safe 上传摘要不包含完整文件内容。 |
| AC-12 | 现有 CSV / JSON / image / PDF 上传、列表、删除、权限隔离行为不回归。 |
| AC-13 | `docs/api/api-doc.html` 与前端 API 类型同步新增 Excel / normalization 字段。 |
| AC-14 | 不修改 `skill/**`。 |
| AC-15 | 宽表或多 sheet 文件的 `uploaded_artifacts` 摘要按 7.4 裁剪并带 `*_truncated` / count 字段；主代理 prompt 中仍无 `content` / `content_base64`。 |
| AC-16 | 多 sheet Excel 未选择 sheet 时，`skill_artifacts` 不包含可执行 `content`，任务必须先进入 `sheet_selection_required` interrupt。 |
| AC-17 | `answer_payload.upload_sheet_selections` 的缺失、越权、过期 upload、无效 sheet 或清洗后冲突都 fail closed，不继续执行 Skill。 |
| AC-18 | 执行专用 artifact 可以携带 `original_filename` / `normalized_filename` / `file_type`，但这些字段的 allowlist 更新不得让完整内容进入 prompt-safe 通道。 |
| AC-19 | 仅带 `application/vnd.ms-excel` content type 但无 Excel 后缀 / magic bytes 的 CSV 仍按 CSV 兼容处理。 |

## 17. 测试计划

### 17.1 单元测试

新增或扩展 `tests/api/test_uploads.py`：

1. UTF-8 BOM + 外层引号 CSV 表头清洗。
2. GB18030 / GBK CSV 解码。
3. Big5 CSV 解码。
4. Shift-JIS CSV 解码。
5. CSV dialect sniff：Tab / 分号输入输出为标准逗号 CSV。
6. 表头清洗后重复 fail closed。
7. 空表头 fail closed。
8. JSON key 技术清洗与冲突检测。
9. `.xlsx` 单 sheet 转规范化 CSV。
10. `.xls` 单 sheet 转规范化 CSV。
11. Excel 多 sheet 产出 `requires_sheet_selection` metadata。
12. Excel 空 workbook / 空 sheet 返回 400。
13. `application/vnd.ms-excel` + `.csv` 或无 Excel magic bytes 时仍按 CSV 处理。
14. 宽表 preview columns / original_columns / column_normalizations 按上限裁剪，并记录真实数量与 truncated 标记。
15. Excel 有效 sheet 超过上限时 fail closed，并给出不包含文件内容的安全错误。
16. 公式表头无缓存值时该 sheet 不作为有效 sheet；公式数据无缓存值时按空值处理并记录 warning。

### 17.2 API / runtime 测试

1. `POST /api/v1/conversations/uploads` 返回新增 preview 字段。
2. `resolve_uploads_for_message()` 对 CSV 返回规范化 `skill_artifacts[].content`。
3. `resolve_uploads_for_message()` 对未选择 sheet 的 Excel 不返回可执行 `content`，并暴露 pending sheet selection metadata。
4. 提交引用多 sheet Excel 的消息时生成 open interrupt。
5. `POST /api/v1/tasks/interrupts/answer` 选择 sheet 后继续执行同一 task。
6. 上传越权、删除、列表行为不变。
7. 多个未选择 Excel upload 可一次 interrupt 收集全部选择；任一选择无效时不执行 Skill。
8. 主代理 prompt、conversation memory 候选、普通 audit 与 SSE 事件中不出现 `skill_artifacts[].content` 或原始行数据。
9. 执行专用 artifact allowlist 更新后，脚本能看到 `normalized_filename` / `original_filename`，prompt-safe 通道仍看不到完整内容。

### 17.3 前端 / API 文档测试

1. `frontend/src/api/types.ts` 更新 `UploadPreviewResponse` / `UploadFileResponse` 类型。
2. 上传卡片可展示 sheet 选择状态、列数 / sheet 数裁剪提示；若暂不展示详细字段，也不得崩溃。
3. interrupt UI 能渲染 `upload_sheet_selections.options_by_upload_id` 的嵌套 sheet 选择，并提交规定 answer payload。
4. `docs/api/api-doc.html` 包含 Excel 支持、多编码支持、多 sheet interrupt、prompt-safe preview 上限与 truncated 字段说明。
5. API 文档测试继续覆盖 `/api-doc` 静态文档与 OpenAPI path 同步。

### 17.4 回归命令

实施完成后至少运行：

```bash
conda run -n multi_agent python -m unittest tests.api.test_uploads
conda run -n multi_agent python -m unittest tests.api.test_pending_skill_context
conda run -n multi_agent python -m unittest tests.api.test_developer_docs
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
cd frontend && npm test -- --run
```

如新增依赖或触及 Rust / native 供应链，按仓库 License Requirement 补充对应 license gate；本 PRD 预计只新增 Python 依赖，需同步 `requirements.txt` 并记录许可风险检查结果。

## 18. 分阶段实施建议

### P0：测试基线

- 先补 CSV BOM / 外层引号失败测试。
- 补现有 UTF-8 CSV / JSON / image / PDF 不回归测试。
- 补 `.xlsx/.xls` 期望行为测试，可先标红。
- 补 prompt-safe 不含完整内容与宽表摘要上限测试，可先标红。
- P0 只允许 tests-only 或文档校验改动，不改生产行为。

### P1：CSV / JSON 规范化核心

- 新增 `table_upload_normalizer`。
- 接入多编码解码、CSV 表头清洗、JSON key 清洗。
- 更新 preview 与 `skill_artifacts`。
- 接入 prompt-safe 摘要上限与 no-raw prompt/audit/SSE 回归。
- 阶段门禁：CSV / JSON / image / PDF 回归全绿后，才进入 Excel 依赖阶段。

### P2：Excel 支持

- 新增 `openpyxl` / `xlrd` 依赖。
- 支持单 sheet Excel 转规范化 CSV。
- 支持多 sheet metadata 与 pending selection。
- 接入文件类型判定优先级、Excel 资源限制、公式缓存规则与 sheet metadata 裁剪。
- 阶段门禁：单 sheet `.xlsx/.xls`、空 workbook、超限 workbook、`application/vnd.ms-excel` 兼容测试全绿。

### P3：Sheet 选择 interrupt / resume

- 在任务引用未选择 sheet upload 时创建 interrupt。
- answer payload 校验 sheet 选择。
- resume 时生成所选 sheet 的规范化 CSV。
- 支持多个未选择 upload 的一次性选择收集。
- 保证选择结果只进入当前任务 / 当前提交 metadata，不永久改写 upload record。
- 阶段门禁：未选择不执行、选择后 resume、无效选择 fail closed、越权/过期 upload 测试全绿。

### P4：前端与文档同步

- 更新前端类型与上传卡片展示。
- 更新 interrupt UI 以支持 `options_by_upload_id` 嵌套 sheet 选择。
- 更新 `docs/api/api-doc.html`。
- 更新 `CHANGELOG.md` 与验证说明。
- 阶段门禁：`tests/api` 指定回归、API 文档测试、前端测试全绿，并记录 License Requirement。

## 19. 已确认决策

1. 选择系统级统一兼容，而不是只修单个 Skill。
2. 本轮支持 CSV / JSON / `.xlsx` / `.xls`。
3. Excel 多 sheet 需要用户选择，不自动猜测或合并。
4. 原始文件保留，Skill 使用规范化 UTF-8 CSV / JSON。
5. 表头只做技术噪声清洗，不做业务语义映射。
6. 新增 `openpyxl` + `xlrd` 依赖。
7. 不修改 `skill/**`。
8. prompt-safe 上传摘要必须有硬上限，执行内容只走 `skill_artifacts`。
9. 多 sheet 选择按任务 / 提交作用域生效，不永久写回 upload record。

## 20. 开放风险

1. `.xls` 生态较老，`xlrd` 对不同来源文件兼容性可能不如 `.xlsx`；实现需给出明确错误提示。
2. Excel 单元格公式如果没有缓存值，表头可能导致 sheet 无效，数据行会按空值处理；系统不得执行公式计算。
3. CSV dialect sniff 可能误判分隔符；实现应在候选分隔符内保持保守，并在失败时返回明确错误。
4. 如果用户真实列名含外层引号字符且业务上不可剥离，本 PRD 的清洗会改变表头；这是为了修复常见导出污染，需通过 `column_normalizations` 可观测。
5. 如果未来需要 `材料编号 -> ped_id` 这类业务映射，应另起 PRD，且必须有 per-capability 明确配置和风险提示。
6. prompt-safe 摘要裁剪可能导致主代理只看到部分列 / sheet；实现必须通过 count 与 truncated 标记保留可解释性，真实执行内容不受摘要裁剪影响。
7. `application/vnd.ms-excel` 的真实来源可能是 CSV 或 Excel；必须通过后缀 / magic bytes 优先级锁定兼容口径，避免破坏既有 CSV 上传。
8. 多 sheet 选择若未来改为 conversation 级默认偏好，会改变复用语义；本 PRD 暂不做该产品决策。
