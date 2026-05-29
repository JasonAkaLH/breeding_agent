# 表格上传编码兼容与表头规范化 PRD（已拆分入口）

- **状态**：已拆分为阶段 PRD；本文件保留为历史兼容入口。
- **阶段目录**：`docs/prd/backend/table-upload-normalization/README.md`
- **父总纲 PRD**：`docs/prd/backend/table-upload-normalization/00-表格上传编码兼容与表头规范化总纲PRD.md`
- **日期**：2026-05-29

## 说明

原单文件 PRD 已拆分到 `docs/prd/backend/table-upload-normalization/` 目录。后续计划、实施、验收和代码修改应以该目录中的父总纲和阶段 PRD 为准；本文件只用于避免旧链接失效。

## 阶段 PRD

| 阶段 | 文件 | 目标 |
|---|---|---|
| 总纲 | `docs/prd/backend/table-upload-normalization/00-表格上传编码兼容与表头规范化总纲PRD.md` | 统一目标、跨阶段不变量、总体验收矩阵和风险。 |
| 阶段零 | `docs/prd/backend/table-upload-normalization/01-阶段零-测试基线与旧行为锁定PRD.md` | tests-only 锁定旧行为与目标风险，不改生产 runtime。 |
| 阶段一 | `docs/prd/backend/table-upload-normalization/02-阶段一-CSV与JSON规范化核心PRD.md` | CSV / JSON 多编码、表头技术清洗、prompt-safe 摘要上限、执行 artifact 投影。 |
| 阶段二 | `docs/prd/backend/table-upload-normalization/03-阶段二-Excel解析与Spreadsheet元数据PRD.md` | `.xlsx` / `.xls` 解析、单 sheet CSV 转换、多 sheet metadata 与资源限制。 |
| 阶段三 | `docs/prd/backend/table-upload-normalization/04-阶段三-Sheet选择Interrupt与ResumePRD.md` | sheet selection interrupt / answer / resume 执行闭环。 |
| 阶段四 | `docs/prd/backend/table-upload-normalization/05-阶段四-前端API文档与发布门禁PRD.md` | 前端 UI / 类型、API 静态文档、发布与回滚门禁。 |

## 保留的不变量摘要

- 不修改 `skill/**`。
- 不做业务语义列名映射。
- 原始 bytes 与原始 `sha256` 保留。
- `uploaded_artifacts` 只作为 prompt-safe 摘要；`skill_artifacts` 才能携带执行专用规范化内容。
- 多 sheet Excel 未选择 sheet 时必须 fail closed，不得由系统或 LLM 猜测。
