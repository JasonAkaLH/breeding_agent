# 阶段三：Sheet 选择 Interrupt 与 Resume PRD

- **父总纲**：`docs/prd/backend/table-upload-normalization/00-表格上传编码兼容与表头规范化总纲PRD.md`
- **状态**：待实施
- **前置阶段**：阶段二 Excel 解析与 spreadsheet metadata 完成且回归全绿
- **实施范围**：`src/api/runtime.py`、interrupt answer/resume、orchestration metadata、API/runtime 测试、必要的 lifecycle 集成测试

## 1. 目标

当任务引用多有效 sheet 且尚未选择 sheet 的 Excel upload 时，系统必须先创建结构化 `sheet_selection_required` interrupt；用户回答后按任务作用域校验选择并生成所选 sheet 的规范化 CSV，继续原任务执行。

## 2. 本阶段范围

### 2.1 In scope

1. `resolve_uploads_for_message()` 支持读取 `metadata.upload_sheet_selections` 和已接受 interrupt answer 中的 `upload_sheet_selections`。
2. 未选择 sheet 的 spreadsheet 返回 pending sheet selection metadata，不返回可执行 content。
3. 提交消息或 resume 时如果存在 pending sheet selection，创建 open interrupt，`reason_code=sheet_selection_required`。
4. interrupt `required_fields.upload_sheet_selections` 使用 `required_upload_ids`、`options_by_upload_id`、`labels_by_upload_id` 的稳定结构。
5. 支持一个 interrupt 一次收集多个未选择 upload 的 sheet 选择。
6. answer payload 校验 upload owner、conversation、TTL、sheet 存在、表头有效、无清洗冲突。
7. 选择结果只进入当前任务 / 当前提交 metadata，不永久改写 upload record。
8. resume 复用原 interrupted Skill 节点 / finalizer 节点的既有机制，不额外创建重复 Skill 节点。

### 2.2 Out of scope

1. 不做前端图形化 sheet 选择 UI；阶段三可通过 API payload 测试闭环。
2. 不保存 conversation 级默认 sheet 偏好。
3. 不修改 `skill/**`。
4. 不新增 Excel 解析能力；解析能力来自阶段二。

## 3. 功能需求

| ID | 需求 |
|---|---|
| P3-FR-01 | 未选择 sheet 时不得调度 Skill 执行器，也不得让 LLM 猜测 sheet。 |
| P3-FR-02 | `answer_payload.upload_sheet_selections` 缺失、类型错误、选项不在 allowlist、越权、过期、sheet 无效时必须 fail closed。 |
| P3-FR-03 | 成功选择后，同一任务 resume 使用所选 sheet 的规范化 CSV，并保留原始 bytes / sha256 不变。 |
| P3-FR-04 | 多个 upload 中任一选择无效时，整个 resume 不得部分执行。 |
| P3-FR-05 | interrupt question 只能展示文件名、sheet 名、表头预览、行列数等脱敏摘要，不得包含行数据。 |
| P3-FR-06 | 已接受的上传 answer payload 与 sheet selection payload 在多轮 interrupt 中可合并复用，但不能接受用户伪造内部 resume metadata。 |

## 4. 验收标准

| ID | 验收标准 |
|---|---|
| P3-AC-01 | 引用多 sheet Excel 的任务产生 `sheet_selection_required` open interrupt，不执行 Skill。 |
| P3-AC-02 | interrupt response 的 `required_fields` 包含 `required_upload_ids`、`options_by_upload_id`、`labels_by_upload_id`。 |
| P3-AC-03 | 用户选择合法 sheet 后，任务 resume 并使用所选 sheet 的规范化 CSV 继续执行。 |
| P3-AC-04 | 无效 sheet、缺失选择、越权 upload、过期 upload、表头冲突均 fail closed。 |
| P3-AC-05 | 多个未选择 Excel upload 可由一个 interrupt 收集全部选择，任一无效不执行。 |
| P3-AC-06 | 同一 upload 在新任务中复用时，若没有显式选择，仍再次要求选择。 |

## 5. 测试计划

```bash
conda run -n multi_agent python -m unittest tests.api.test_uploads
conda run -n multi_agent python -m unittest tests.api.test_pending_skill_context
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

## 6. 完成门禁

- 未选择 sheet 不可执行、选择后可 resume、无效选择 fail closed 三条路径都有自动化证据。
- no-raw prompt / SSE / audit 回归继续通过。
- License Requirement：无新增依赖 / 许可变更，未触发 cargo-deny 风险。
