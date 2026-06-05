# 阶段七：文档、API、测试与 Legacy 下线门禁 PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **依赖**：阶段一至阶段六
- **目标模块**：文档、API docs、测试矩阵、legacy `auto_run` 下线门禁
- **目标结果**：正式文档、API 说明、测试命令和 legacy 兼容/下线标准收口；新 Skill 编写者只按新结构写 Skill。

## 1. 范围

### 1.1 In scope

- 更新 `Skill构建指南.md`。
- 更新静态 API 文档中的 Skill 调用、capability、interrupt/resource 说明。
- 更新 PRD 索引与 README。
- 形成新 Skill 模板或示例片段。
- 建立全量测试矩阵和推荐命令。
- 明确 legacy frontmatter execution/parameters/scripts 与 `auto_run` 的兼容、告警和下线门禁。

### 1.2 Out of scope

- 不立即删除 legacy adapter，除非所有迁移和门禁满足。
- 不新增前端专属业务卡片。

## 2. 现有文档与 API 锚点

| 锚点 | 当前事实 | 本阶段约束 |
| --- | --- | --- |
| `Skill构建指南.md` | 当前仍描述大 frontmatter manifest。 | 指南必须改成轻量 `SKILL.md` + contract + schemas + references 的新结构。 |
| `docs/api/api-doc.html` 与开发者文档测试 | API 文档已有 endpoint 说明回归。 | 必须补充 capability、skill execution、interrupt/resume、resource read 的外部调用边界。 |
| `tests/api/test_developer_docs.py` | 当前锁定 API 文档说明质量。 | 新 Skill contract/API 说明必须被文档回归覆盖。 |

## 3. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| C7-001 | `Skill构建指南.md` 只推荐新结构。 | frontmatter 示例只含 name/description。 |
| C7-002 | API 文档说明 direct skill execution、slash soft binding、natural language planning。 | 外部调用方知道不能直接硬提交 `skill.*`。 |
| C7-003 | 文档说明 ResourceService 按需读取边界。 | 主代理不能读取 scripts/runtime/schemas/config 原文。 |
| C7-004 | 测试矩阵可重复执行。 | backend integration/API/e2e 命令列明。 |
| C7-005 | Legacy 下线门禁明确。 | 只有全部项目级 Skill 迁移且回归全绿后，才允许另 PR 删除 legacy auto-run。 |
| C7-006 | 文档不误导外部调用方硬调 `skill.*`。 | API 文档说明外部调用方提交用户消息/附件/interrupt answer，`skill.*` 由后端 planner/replanner/task graph 产生。 |
| C7-007 | 新模板不包含 legacy 字段。 | 示例中不得出现 `public_usage.parameters`、顶层 `parameters`、`scripts[].auto_run` 或 `run_by_default`。 |

## 4. Legacy 下线门禁

满足以下条件后，才允许另立 PR 删除旧 main-agent 隐式 `auto_run` 路径：

1. 所有项目级公开 Skill 均完成新 contract 迁移。
2. 无生产路径依赖 `manifest.scripts[].auto_run`。
3. `/api/v1/capabilities` 与 planner/replanner 全部通过 contract registry 获取公开 Skill。
4. field-design、field-analysis、rice-genie、OCR、SQLQuery e2e 全绿。
5. 旧格式用户级或测试 Skill 有明确兼容替代或保留说明。

## 5. 完成门禁

- 文档、API、PRD、CHANGELOG 全部同步。
- 迁移后 full targeted regression 通过。
- License Requirement 记录完整。
- 文档 grep 门禁通过：新格式示例不包含 `auto_run` / `run_by_default` / 旧 `parameters` manifest。
