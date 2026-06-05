# 阶段六：项目级 Skill 迁移 PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **依赖**：阶段一至阶段五
- **目标模块**：`skill/field-design`、`skill/field-analysis`、`skill/rice-genie`、`skill/ocr`、`skill/sql-query`
- **目标结果**：现有项目级公开 Skill 迁移到新结构，删除新格式中的 `auto_run`，并通过真实 Skill 回归。

## 1. 迁移原则

- 以单个 Skill 为原子切换单位。
- 一旦存在 `skill.contract.yaml`，该 Skill 使用新 contract 为事实源。
- 不允许半迁移：`SKILL.md`、contract、schemas、references、tests 必须同 PR 完整提交。
- 回滚方式是移除/禁用该 Skill 的 `skill.contract.yaml`，恢复 legacy adapter。

## 2. Skill 目标形态

### 2.1 field-design

文件：

```text
skill/field-design/SKILL.md
skill/field-design/skill.contract.yaml
skill/field-design/schemas/rcbd.input.yaml
skill/field-design/schemas/diagonal.input.yaml
skill/field-design/schemas/interval.input.yaml
skill/field-design/references/usage.md
skill/field-design/references/material-data.md
skill/field-design/references/rcbd.md
skill/field-design/references/diagonal.md
skill/field-design/references/interval.md
```

验收：RCBD 不要求 `ck_spec`；Interval 要 `ncols/ck_spec`；CSV/HTML output contract 生效。

### 2.2 field-analysis

文件：

```text
schemas/rcbd-analysis.input.yaml
schemas/diagonal-analysis.input.yaml
references/field-data.md
```

验收：RCBD/Diagonal schema selection；JSON report output contract。

### 2.3 rice-genie

文件：

```text
schemas/qtn-check.input.yaml
schemas/report-from-gene-check.input.yaml
references/vcf-input.md
references/gene-check-json.md
```

验收：VCF/VCF.GZ 选 qtn-check；gene_check JSON 选 report schema；Markdown report output contract。

### 2.4 OCR

文件：

```text
schemas/document-ocr.input.yaml
references/supported-files.md
references/output-formats.md
```

验收：上传文件或 `file_path` any_of；`output_format` enum；缺文件补槽；OCR 错误码保留。

### 2.5 SQLQuery

文件：

```text
schemas/readonly-query.input.yaml
references/query-examples.md
references/data-boundaries.md
runtime/sql_query_skill/
```

验收：platform_service contract 注册；query-only schema；服务 allowlist；SQL guard 保持在 handler 内部。

## 3. 测试计划

- 每个 Skill 现有 integration tests 改为新结构 fixture。
- API e2e 覆盖 field-design interval 补槽、OCR any_of、SQLQuery platform_service。
- public profile/resource tests 覆盖每个 Skill 的 references。

## 4. 完成门禁

- 五个项目级 Skill 均无新格式 `auto_run`。
- 五个 Skill 均能通过 `/api/v1/capabilities` 注册。
- 五个 Skill 的核心执行/补槽/output tests 全绿。
- `Skill构建指南.md` 示例同步为新结构。
