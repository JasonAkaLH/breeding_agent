# 阶段二：Excel 解析与 Spreadsheet 元数据 PRD

- **父总纲**：`docs/prd/backend/table-upload-normalization/00-表格上传编码兼容与表头规范化总纲PRD.md`
- **状态**：待实施
- **前置阶段**：阶段一 CSV / JSON 规范化核心完成且回归全绿
- **实施范围**：上传 store / normalizer、DTO、requirements、Excel 解析测试；不含最终 interrupt/resume UI 闭环

## 1. 目标

在不修改 Skill 包的前提下，支持 `.xlsx` / `.xls` 表格上传：单有效 sheet 自动转为规范化 CSV，多有效 sheet 只产生安全 sheet metadata 与 pending selection，不向 Skill 暴露未确定 sheet 的执行 content。

## 2. 本阶段范围

### 2.1 In scope

1. 新增固定版本 `openpyxl` 与 `xlrd` 到 `requirements.txt`。
2. `UploadFileType` / API DTO / 前端类型预期扩展 `spreadsheet`。
3. 文件类型判定遵守父总纲 8.4：后缀优先；必要时 magic bytes 判定；仅凭 `application/vnd.ms-excel` 且无 Excel 后缀 / magic bytes 时继续按 CSV。
4. `.xlsx` 使用只读、不执行宏、不刷新外部链接、不执行公式计算的模式读取已有缓存值。
5. `.xls` 只读取单元格内容，不执行宏。
6. 每个 sheet 第一条非空行作为候选表头；后续行作为数据行；空 sheet 不进入有效 sheet 列表。
7. 单有效 sheet 自动转规范化 CSV 并进入 `skill_artifacts[].content`。
8. 多有效 sheet 上传成功并返回 `requires_sheet_selection=true`、`excel_sheets[]`、count / truncated 字段；执行投影不得包含可执行 content。
9. 有效 sheet 超过上限、没有有效 sheet、资源超限、解析失败时 fail closed，并给出不含文件内容的安全错误。

### 2.2 Out of scope

1. 不实现 `sheet_selection_required` interrupt 的完整 answer/resume 闭环。
2. 不做前端 sheet 选择 UI。
3. 不持久化 upload 到数据库。
4. 不修改 `skill/**`。
5. 不执行公式计算，不访问外部链接。

## 3. 功能需求

| ID | 需求 |
|---|---|
| P2-FR-01 | `.xlsx` / `.xls` 支持必须受上传大小、preview byte limit、最大 sheet 数、最大扫描行列数约束。 |
| P2-FR-02 | Sheet 名称进入 API / prompt-safe 摘要前必须作为普通字符串处理；生成 normalized filename 时必须文件名安全化。 |
| P2-FR-03 | 候选表头行中的公式无可用缓存值时，该 sheet 视为无有效表头；数据行无缓存公式按空值并记录 warning。 |
| P2-FR-04 | 多有效 sheet 的 `excel_sheets` 摘要必须遵守 sheet / columns 上限；超过有效 sheet 上限时不得只展示部分选项后继续执行。 |
| P2-FR-05 | `normalized_filename` 对 Excel 单 sheet 使用 `.csv` 后缀，执行 artifact 的 `filename` 应指向规范化 CSV 文件名，同时保留 `original_filename`。 |
| P2-FR-06 | 未选择 sheet 的 spreadsheet 在 resolve 阶段必须返回 pending sheet selection metadata，而不是可执行 content。 |

## 4. 验收标准

| ID | 验收标准 |
|---|---|
| P2-AC-01 | 单 sheet `.xlsx` 上传返回 `file_type=spreadsheet`，preview columns 清洗正确，执行 artifact content 是规范化 CSV。 |
| P2-AC-02 | 单 sheet `.xls` 行为与 `.xlsx` 一致。 |
| P2-AC-03 | 多有效 sheet 上传返回 `requires_sheet_selection=true` 与 `excel_sheets[]`，但 `skill_artifacts` 不含可执行 content。 |
| P2-AC-04 | 空 workbook / 全空 sheet / 清洗后空表头 / 清洗后重复表头返回 400。 |
| P2-AC-05 | 有效 sheet 超过上限时 fail closed，不进入“只展示前 N 个后继续执行”的状态。 |
| P2-AC-06 | `application/vnd.ms-excel` + `.csv` 或无 Excel magic bytes 时仍按 CSV 处理。 |
| P2-AC-07 | 新增依赖版本固定，License Requirement 记录完成。 |

## 5. 测试计划

```bash
conda run -n multi_agent python -m unittest tests.api.test_uploads
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

如实现触及依赖或供应链策略，需补充：

```bash
# Python 依赖许可检查以项目当前依赖治理工具为准；如触及 native/Rust 依赖才运行 cargo deny。
```

## 6. 完成门禁

- `requirements.txt` 固定 `openpyxl` / `xlrd` 版本。
- Excel 解析失败、资源超限、公式无缓存等错误均不泄露文件内容。
- 阶段二完成后，未选择 sheet 的 Excel 仍不能真正执行；完整执行闭环必须等阶段三。
