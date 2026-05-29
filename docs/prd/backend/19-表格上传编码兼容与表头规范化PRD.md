# 表格上传编码兼容与表头规范化 PRD

- **项目**：breeding_agent
- **范围**：后端 / 上传解析 / 表格预览 / Skill artifact 输入 / Interrupt sheet 选择 / API 文档
- **文档状态**：PRD 草案（已完成 brainstorming 需求确认，待实施计划）
- **日期**：2026-05-29
- **关联模块**：`src/api/upload_store.py`、`src/api/routes/uploads.py`、`src/api/runtime.py`、`src/integrations/agent_skills/`、`src/capabilities/main_agent/`、`frontend/`
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

1. 用户上传 Excel 导出的 CSV、GBK / GB18030 CSV、Big5 CSV、Shift-JIS CSV、`.xlsx` 或 `.xls` 后，系统能尽量直接识别并继续执行。
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

`uploaded_artifacts` 仍是 prompt-safe 摘要，不包含完整内容。建议字段：

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
3. object key 做技术噪声清洗；同一 object 内清洗后 key 冲突时 fail closed。
4. 值不做业务变换。
5. 规范化输出为 `json.dumps(..., ensure_ascii=False)` 的 UTF-8 JSON 文本。

### 8.3 Excel `.xlsx` / `.xls`

新增支持：

- `.xlsx`：依赖 `openpyxl`；
- `.xls`：依赖 `xlrd`。

要求：

1. 同步更新 `requirements.txt`，新增 `openpyxl` 与 `xlrd` 固定版本。
2. `.xlsx` 解析必须使用只读 / 不执行宏 / 不刷新外部链接的模式；公式只读取缓存值或原始单元格显示结果，不执行计算。
3. `.xls` 解析只读取单元格内容，不执行宏。
4. 每个 sheet 的第一条非空行作为候选表头；后续行作为数据行。
5. 有且仅有一个有效 sheet 时，自动选择该 sheet 并转为规范化 CSV。
6. 多个有效 sheet 时，上传成功但标记为 `requires_sheet_selection=true`；执行引用该上传时必须先发起 sheet 选择 interrupt。
7. 没有有效 sheet 时返回 400。
8. Excel 解析仍受上传文件大小、preview byte limit、最大 sheet 数、最大扫描行列数限制。

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

1. Unicode 兼容规范化：建议 `unicodedata.normalize("NFKC", value)`。
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

如果同一表内表头清洗后出现重复，必须 fail closed。

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
4. `required_fields` 应包含稳定字段，例如：

```json
{
  "upload_sheet_selections": {
    "type": "object",
    "required": ["upl-xxx"],
    "options": {
      "upl-xxx": ["Sheet1", "材料表", "说明"]
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

### 11.4 可选预选择

如果前端未来想在提交消息前让用户选择 sheet，可通过 `metadata.upload_sheet_selections` 传入相同结构。后端仍必须按 11.3 校验，不能信任客户端。

## 12. API / DTO 影响

### 12.1 `UploadPreviewResponse`

现有字段：

- `row_count`
- `columns`
- `shape`

建议新增可选字段：

- `source_encoding: string | null`
- `original_columns: list[string]`
- `column_normalizations: list[object]`
- `normalized_content_type: string | null`
- `normalized_filename: string | null`
- `selected_sheet: string | null`
- `requires_sheet_selection: boolean`
- `excel_sheets: list[object]`
- `warnings: list[string]`

新增字段必须向后兼容：旧客户端忽略即可。

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

## 13. 安全、隐私与资源限制

1. 原始文件内容不得进入主代理 prompt、SSE、普通 audit 或错误详情。
2. 表头、编码、sheet 名、行列数、清洗映射可以进入 prompt-safe 上传摘要；但仍不得包含真实文件全文。
3. Excel 读取不得执行宏、外部链接或公式计算。
4. `.xlsx` 解压与解析必须受上传文件大小、preview byte limit、最大 sheet 数、最大扫描行列数约束，防止压缩炸弹或超大工作簿拖垮服务。
5. 表头冲突必须 fail closed。
6. 编码识别不得用 ignore / replace 模式静默吞错。
7. 新增依赖必须同步进入 `requirements.txt`，并在最终实现验证中记录 License Requirement。
8. 如果未来将上传 store 下沉到 Rust safety kernel，本 PRD 的表头清洗、编码选择、sheet 选择和 no-raw audit 口径必须保持同构。

## 14. 观测与审计

建议新增脱敏审计事件或扩展现有 upload audit metadata：

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
- 表头可记录，因为表头已在 preview 中展示；如未来表头可能含敏感信息，应增加脱敏开关。
- 编码失败、表头冲突、Excel 解析失败应记录错误类型，不记录文件内容。

## 15. 兼容与迁移

1. 现有 CSV / JSON 上传 happy path 不应改变用户可见行为；区别是 `content_text` 变为规范化文本。
2. image / PDF 上传行为不变。
3. 原有 `sha256` 仍基于原始 bytes，不能改为规范化文本 hash。
4. 当前 upload store 为内存/TTL 模型，本 PRD 不要求数据库迁移。
5. 前端可逐步展示新增 preview 字段；不展示时不影响执行。
6. Skill bundle 不需要修改；如果某个 Skill 仍因真实业务列缺失而失败，该失败应保留。

## 16. 验收标准

| ID | 验收标准 |
|---|---|
| AC-01 | 上传 `﻿"ped_id",hyb_check,set` 表头的 CSV 后，preview columns 为 `ped_id,hyb_check,set`，Skill artifact content 第一行为 `ped_id,hyb_check,set`。 |
| AC-02 | 上传 GB18030 / GBK CSV 后，中文数据与表头可正常预览，`source_encoding` 被记录。 |
| AC-03 | 上传 Big5 / Shift-JIS CSV 后，若内容可完整解码，则正常预览并生成规范化 UTF-8 CSV。 |
| AC-04 | 表头清洗后重复时返回 400，不生成 upload record。 |
| AC-05 | JSON object / object array 的 key 经过技术清洗；同级 key 清洗后冲突时返回 400。 |
| AC-06 | 上传单 sheet `.xlsx` 后，public response 说明原始文件为 spreadsheet，执行专用 artifact 是规范化 CSV。 |
| AC-07 | 上传单 sheet `.xls` 后，行为与 `.xlsx` 一致。 |
| AC-08 | 上传多有效 sheet Excel 后，上传成功但执行引用时产生 `sheet_selection_required` interrupt，不直接运行 Skill。 |
| AC-09 | 用户选择 sheet 后，任务 resume 使用所选 sheet 的规范化 CSV 继续执行。 |
| AC-10 | 原始 `sha256` 与原始 bytes 一致，不因规范化改变。 |
| AC-11 | prompt-safe 上传摘要不包含完整文件内容。 |
| AC-12 | 现有 CSV / JSON / image / PDF 上传、列表、删除、权限隔离行为不回归。 |
| AC-13 | `docs/api/api-doc.html` 与前端 API 类型同步新增 Excel / normalization 字段。 |
| AC-14 | 不修改 `skill/**`。 |

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

### 17.2 API / runtime 测试

1. `POST /api/v1/conversations/uploads` 返回新增 preview 字段。
2. `resolve_uploads_for_message()` 对 CSV 返回规范化 `skill_artifacts[].content`。
3. `resolve_uploads_for_message()` 对未选择 sheet 的 Excel 不返回可执行 `content`，并暴露 pending sheet selection metadata。
4. 提交引用多 sheet Excel 的消息时生成 open interrupt。
5. `POST /api/v1/tasks/interrupts/answer` 选择 sheet 后继续执行同一 task。
6. 上传越权、删除、列表行为不变。

### 17.3 前端 / API 文档测试

1. `frontend/src/api/types.ts` 更新 `UploadPreviewResponse` / `UploadFileResponse` 类型。
2. 上传卡片可展示 sheet 选择状态；若不实现 UI 展示，也不得崩溃。
3. `docs/api/api-doc.html` 包含 Excel 支持、多编码支持与多 sheet interrupt 说明。
4. API 文档测试继续覆盖 `/api-doc` 静态文档与 OpenAPI path 同步。

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

### P1：CSV / JSON 规范化核心

- 新增 `table_upload_normalizer`。
- 接入多编码解码、CSV 表头清洗、JSON key 清洗。
- 更新 preview 与 `skill_artifacts`。

### P2：Excel 支持

- 新增 `openpyxl` / `xlrd` 依赖。
- 支持单 sheet Excel 转规范化 CSV。
- 支持多 sheet metadata 与 pending selection。

### P3：Sheet 选择 interrupt / resume

- 在任务引用未选择 sheet upload 时创建 interrupt。
- answer payload 校验 sheet 选择。
- resume 时生成所选 sheet 的规范化 CSV。

### P4：前端与文档同步

- 更新前端类型与上传卡片展示。
- 更新 `docs/api/api-doc.html`。
- 更新 `CHANGELOG.md` 与验证说明。

## 19. 已确认决策

1. 选择系统级统一兼容，而不是只修单个 Skill。
2. 本轮支持 CSV / JSON / `.xlsx` / `.xls`。
3. Excel 多 sheet 需要用户选择，不自动猜测或合并。
4. 原始文件保留，Skill 使用规范化 UTF-8 CSV / JSON。
5. 表头只做技术噪声清洗，不做业务语义映射。
6. 新增 `openpyxl` + `xlrd` 依赖。
7. 不修改 `skill/**`。

## 20. 开放风险

1. `.xls` 生态较老，`xlrd` 对不同来源文件兼容性可能不如 `.xlsx`；实现需给出明确错误提示。
2. Excel 单元格公式如果没有缓存值，读取结果可能为空或公式文本；系统不得执行公式计算。
3. CSV dialect sniff 可能误判分隔符；实现应在候选分隔符内保持保守，并在失败时返回明确错误。
4. 如果用户真实列名含外层引号字符且业务上不可剥离，本 PRD 的清洗会改变表头；这是为了修复常见导出污染，需通过 `column_normalizations` 可观测。
5. 如果未来需要 `材料编号 -> ped_id` 这类业务映射，应另起 PRD，且必须有 per-capability 明确配置和风险提示。
