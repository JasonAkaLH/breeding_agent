# 阶段六：项目级 Skill 迁移 PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **依赖**：阶段一至阶段五
- **目标模块**：`skill/field-design`、`skill/field-analysis`、`skill/rice-genie`、`skill/ocr`、`skill/sql-query`
- **目标结果**：现有项目级公开 Skill 全量迁移到 v2 结构，删除 v1 manifest 平台字段，并通过真实 Skill 回归。

## 1. 现有代码锚点

| Skill | 当前事实 | 迁移关注点 |
| --- | --- | --- |
| `skill/field-design/SKILL.md` | 当前有 `public_usage`、全局 `parameters`、`scripts[].auto_run`。 | RCBD/Diagonal/Interval schema 必须拆分，Interval 动态必填由 schema 表达。 |
| `skill/field-analysis/SKILL.md` | 当前由旧 manifest 描述分析入口。 | 设计类型与输入 field data artifact 要进入独立 schema。 |
| `skill/rice-genie/SKILL.md` | 当前 qtn check 与 gene check report 混在旧 manifest。 | VCF/VCF.GZ 与 gene_check JSON 要通过 schema selector 区分。 |
| `skill/ocr/SKILL.md` | 当前 document/file path 输入与输出格式在旧 manifest 中。 | any_of 文件来源与 output_format enum 要由 schema 表达。 |
| `skill/sql-query/SKILL.md` | 当前 platform_service 与 public usage 在 frontmatter。 | contract 要注册 platform_service，但 SQL guard 仍留在 handler 内。 |

## 2. 迁移原则

- 以单个 Skill 为原子切换单位。
- 只有存在合法 `skill.contract.yaml` 的 Skill 才会注册为公开 capability。
- 不允许半迁移：`SKILL.md`、contract、schemas、references、tests 必须同 PR 完整提交。
- 回滚方式是移除该 Skill 的 v2 文件并接受该 Skill 暂不注册；不恢复 v1 执行路径。

## 3. Skill 目标形态

### 3.1 field-design

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

### 3.2 field-analysis

文件：

```text
schemas/rcbd-analysis.input.yaml
schemas/diagonal-analysis.input.yaml
references/field-data.md
```

验收：RCBD/Diagonal schema selection；JSON report output contract。

### 3.3 rice-genie

文件：

```text
schemas/qtn-check.input.yaml
schemas/report-from-gene-check.input.yaml
references/vcf-input.md
references/gene-check-json.md
```

验收：VCF/VCF.GZ 选 qtn-check；gene_check JSON 选 report schema；Markdown report output contract。

### 3.4 OCR

文件：

```text
schemas/document-ocr.input.yaml
references/supported-files.md
references/output-formats.md
```

验收：上传文件或 `file_path` any_of；`output_format` enum；缺文件补槽；OCR 错误码保留。

### 3.5 SQLQuery

文件：

```text
schemas/readonly-query.input.yaml
references/query-examples.md
references/data-boundaries.md
runtime/sql_query_skill/
```

验收：platform_service contract 注册；query-only schema；服务 allowlist；SQL guard 保持在 handler 内部。

## 4. 测试计划

- 每个 Skill 现有 integration tests 改为新结构 fixture。
- API e2e 覆盖 field-design interval 补槽、OCR any_of、SQLQuery platform_service。
- public profile/resource tests 覆盖每个 Skill 的 references。
- 每个 Skill 都要有迁移前后行为等价用例：同样输入产生同类 output payload/artifact，差异只允许来自新 contract/schema 的显式状态字段。

## 5. 完成门禁

- 五个项目级 Skill 均无 v1 平台字段：`auto_run`、`run_by_default`、顶层 `parameters`、`scripts`、`execution`、`public_usage`。
- 五个 Skill 均能通过 `/api/v1/capabilities` 注册。
- 五个 Skill 的核心执行/补槽/output tests 全绿。
- 每个 Skill 的 `SKILL.md` 只保留轻量 frontmatter、用途摘要和资源索引；正式指南更新归属阶段七。


## 6. 迁移顺序

1. 先迁移 field-design，作为 selected schema、动态必填和 output contract 的标准样板。
2. 再迁移 OCR，验证 any_of 输入来源、artifact-only 字段和错误码保留。
3. 再迁移 SQLQuery，验证 platform_service contract 与服务 allowlist。
4. 最后迁移 field-analysis 与 rice-genie，复用前面形成的 schema selector、artifact、report output 模式。

任一 Skill 迁移失败时，只回滚该 Skill 的 `skill.contract.yaml`、`schemas/`、`references/` 与轻量 `SKILL.md` 改动；该 Skill 在回滚期间不注册、不执行，不影响已迁移且通过门禁的其他 Skill。
