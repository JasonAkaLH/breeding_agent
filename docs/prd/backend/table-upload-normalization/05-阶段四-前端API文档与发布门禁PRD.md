# 阶段四：前端、API 文档与发布门禁 PRD

- **父总纲**：`docs/prd/backend/table-upload-normalization/00-表格上传编码兼容与表头规范化总纲PRD.md`
- **状态**：待实施
- **前置阶段**：阶段三 sheet selection interrupt / resume 完成且 API 回归全绿
- **实施范围**：`frontend/`、`docs/api/api-doc.html`、API 文档测试、发布 / 回滚说明、CHANGELOG

## 1. 目标

将后端上传规范化能力完整暴露给用户和 API 客户端：前端能展示上传规范化摘要和 sheet 选择状态，能提交结构化 sheet selection answer；静态 API 文档与真实 DTO 行为一致，发布门禁明确可回滚和可验收。

## 2. 本阶段范围

### 2.1 In scope

1. 更新 `frontend/src/api/types.ts` 中 `UploadPreviewResponse` / `UploadFileResponse` 类型。
2. 上传卡片展示 `source_encoding`、`requires_sheet_selection`、`selected_sheet`、列数 / sheet 数裁剪提示和 warnings；可先保持简洁展示，但不得崩溃。
3. interrupt UI 支持 `upload_sheet_selections.options_by_upload_id` 嵌套结构，允许用户为每个 upload 选择一个 sheet。
4. 提交 `answer_payload.upload_sheet_selections` 时只包含 upload_id -> sheet_name 映射，不提交客户端伪造的内部 metadata。
5. 更新 `docs/api/api-doc.html`：上传支持范围、preview 字段、prompt-safe 上限、multi-sheet interrupt、错误语义、curl / response 示例。
6. API 文档测试覆盖 `/api-doc` 静态文档与 OpenAPI path / schema 同步。
7. 更新 `CHANGELOG.md` 与最终验证说明。

### 2.2 Out of scope

1. 不在前端实现复杂 Excel 内容预览或在线 sheet 数据查看。
2. 不做 conversation 级默认 sheet 选择偏好。
3. 不修改 `skill/**`。
4. 不新增后端解析能力；后端能力来自阶段一至阶段三。

## 3. 功能需求

| ID | 需求 |
|---|---|
| P4-FR-01 | 前端类型必须包含所有新增 preview 可选字段，旧响应仍可正常渲染。 |
| P4-FR-02 | 上传列表 / pending upload tag 不得把 `content` 或行数据展示到 UI。 |
| P4-FR-03 | Sheet 选择 interrupt UI 必须能处理多个 upload，每个 upload 独立选择 sheet。 |
| P4-FR-04 | Sheet 选择提交时必须保持与后端 PRD 一致的 answer payload 结构。 |
| P4-FR-05 | API 文档必须说明 `application/vnd.ms-excel` 兼容口径和 preview `*_truncated` 字段语义。 |
| P4-FR-06 | 发布说明必须记录新增 Python 依赖、回归命令、License Requirement 与回滚边界。 |

## 4. 验收标准

| ID | 验收标准 |
|---|---|
| P4-AC-01 | `frontend/src/api/types.ts` 与后端 DTO 新字段兼容，旧测试不回归。 |
| P4-AC-02 | 多 sheet interrupt 在前端可见且可提交合法 `upload_sheet_selections` answer payload。 |
| P4-AC-03 | 宽表 / 多 sheet truncated 提示可见或至少不会导致 UI 崩溃。 |
| P4-AC-04 | `docs/api/api-doc.html` 覆盖 Excel 支持、多编码支持、多 sheet interrupt、prompt-safe preview 上限和新增字段。 |
| P4-AC-05 | 前端测试、API 文档测试和后端指定回归全绿。 |
| P4-AC-06 | CHANGELOG 记录最终交付和 License Requirement。 |

## 5. 回归命令

```bash
conda run -n multi_agent python -m unittest tests.api.test_uploads
conda run -n multi_agent python -m unittest tests.api.test_pending_skill_context
conda run -n multi_agent python -m unittest tests.api.test_developer_docs
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
cd frontend && npm test -- --run
cd frontend && npm run build
```

## 6. 发布 / 回滚门禁

1. 如果阶段一至阶段三已全部进入生产代码，阶段四发布前必须确认未选择 sheet 的 Excel 不会绕过 interrupt 执行。
2. 如果前端未能按期上线 sheet 选择 UI，后端仍必须通过 API interrupt fail closed；用户可通过 API 或后续前端补丁完成选择，不得降级为自动猜测 sheet。
3. 新增依赖许可检查结果必须写入最终验证说明。
4. License Requirement：如仅涉及 Python 依赖，记录依赖许可核查；如触及 `native/` / Rust 依赖，按仓库规则运行 `cargo deny check`。
