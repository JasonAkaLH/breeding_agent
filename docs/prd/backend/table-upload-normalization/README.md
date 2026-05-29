# 表格上传编码兼容与表头规范化分步 PRD 索引

- **日期**：2026-05-29
- **状态**：待实施
- **父总纲 PRD**：`docs/prd/backend/table-upload-normalization/00-表格上传编码兼容与表头规范化总纲PRD.md`
- **兼容入口**：`docs/prd/backend/19-表格上传编码兼容与表头规范化PRD.md`
- **总目标**：在系统上传层统一兼容 CSV / JSON / Excel 表格文件的常见编码与技术性表头污染，保留原始 bytes 和原始 sha256，并只向 Skill 执行链路提供规范化后的 UTF-8 CSV / JSON 内容；多 sheet Excel 必须由用户明确选择 sheet 后才能执行。

## 目录级置信标准

本目录是一组可逐阶段实施、验证和回滚的 PRD，而不是互不相干的文档集合。实施和评审时必须同时满足：

1. **目标继承**：所有阶段必须服务于父总纲，不引入业务语义列名映射，不让 LLM 推断列名或 sheet。
2. **测试先行**：每个阶段必须先补能描述目标行为的测试；阶段零只允许 tests-only 或文档校验改动。
3. **Skill 包隔离**：任何阶段都不得修改 `skill/**`；Skill 只消费平台提供的规范化执行 artifact。
4. **上下文安全**：完整文件内容、规范化全文、原始 bytes、base64 内容不得进入主代理 prompt、SSE、普通 audit、对话记忆或错误详情。
5. **执行 fail closed**：未完成 sheet 选择、表头清洗后为空 / 重复、编码无法完整解码、越权 / 过期 upload、资源超限时不得继续执行 Skill。
6. **阶段门禁**：下游阶段只能在上游阶段指定回归全绿后实施；每个阶段必须记录 License Requirement。

## 拆分原则

1. 父总纲保留统一目标、跨阶段不变量、总体验收矩阵和开放风险。
2. 阶段 PRD 只描述本阶段新增 / 收窄的范围、测试、验收与完成门禁。
3. CSV / JSON 规范化先于 Excel 依赖；Excel 解析先于 sheet interrupt / resume；前端和 API 文档最后统一收口。
4. prompt-safe 摘要上限和 no-raw 约束从第一阶段生产改动开始即为强制门禁，不允许等到后续补救。

## 阶段文件

| 阶段 | PRD | 目标 | 实施优先级 |
|---|---|---|---|
| 阶段零 | `01-阶段零-测试基线与旧行为锁定PRD.md` | 用 tests-only 锁定当前上传、artifact、prompt-safe 与多 sheet 目标行为，不改生产 runtime。 | P0 |
| 阶段一 | `02-阶段一-CSV与JSON规范化核心PRD.md` | 新增表格规范化核心，接入 CSV / JSON 多编码、表头技术清洗、prompt-safe 摘要上限与执行 artifact 投影。 | P1 |
| 阶段二 | `03-阶段二-Excel解析与Spreadsheet元数据PRD.md` | 新增 `.xlsx` / `.xls` 依赖与解析，支持单 sheet 转 CSV、多 sheet metadata、类型判定和资源限制。 | P2 |
| 阶段三 | `04-阶段三-Sheet选择Interrupt与ResumePRD.md` | 未选择 sheet 时阻断执行并创建 interrupt，answer/resume 校验后按任务作用域生成所选 sheet 的规范化 CSV。 | P3 |
| 阶段四 | `05-阶段四-前端API文档与发布门禁PRD.md` | 前端类型/UI、静态 API 文档、回归命令、License Requirement 与 rollout 证据收口。 | P4 |

## 跨阶段不变量

- 原始上传 bytes 与原始 `sha256` 必须保留；规范化内容不得替代原始 hash。
- `uploaded_artifacts` 是 prompt-safe 摘要通道；`skill_artifacts` 是执行专用通道。两个通道不得混用。
- `uploaded_artifacts` 中列名、sheet、清洗映射必须按父总纲的硬上限裁剪，并用 count / `*_truncated` 保持可解释性。
- 多 sheet Excel 未选择 sheet 时，`skill_artifacts` 不得包含可执行 `content`。
- `application/vnd.ms-excel` 无 Excel 后缀 / magic bytes 时继续按 CSV 兼容处理，避免破坏既有 CSV 上传。
- 新增 `openpyxl` / `xlrd` 只允许在阶段二引入；新增依赖必须同步 `requirements.txt` 并记录许可风险检查结果。
- 本专题不做数据库 schema 迁移；upload store 仍沿用当前内存 / TTL 生命周期，除非后续另立持久化上传存储 PRD。
