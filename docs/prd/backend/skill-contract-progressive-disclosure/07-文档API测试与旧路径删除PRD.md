# 阶段七：文档、API、测试与旧路径删除 PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **依赖**：阶段一至阶段六
- **目标模块**：文档、API docs、测试矩阵、v1 manifest 注册/执行路径删除
- **目标结果**：正式文档、API 说明、测试命令和旧路径删除收口；新 Skill 编写者只按 v2 结构写 Skill，平台不提供 v1 Skill manifest 兼容。

## 1. 范围

### 1.1 In scope

- 更新 `git@gitee.com:biobin/breeding-skill-builder.git` 的 `references/Skill构建指南.md`。
- 更新静态 API 文档中的 Skill 调用、capability、interrupt/resource 说明。
- 更新 PRD 索引与 README。
- 形成 v2 Skill 模板或示例片段。
- 建立全量测试矩阵和推荐命令。
- 删除或禁用旧 `SKILL.md` frontmatter 中 `capability_id`、`public_usage`、`parameters`、`scripts`、`execution`、`auto_run` 作为平台契约的生产路径；如果代码保留解析结构，只能用于 diagnostic 或历史测试 fixture。
- 明确无 `skill.contract.yaml` 的 Skill 不注册、不执行、不进入 capability 列表。

### 1.2 Out of scope

- 不提供 v1 到 v2 的自动转换工具。
- 不保留 v1 manifest adapter。
- 不新增前端专属业务卡片。

## 2. 现有文档与 API 锚点

| 锚点 | 当前事实 | 本阶段约束 |
| --- | --- | --- |
| `git@gitee.com:biobin/breeding-skill-builder.git` 的 `references/Skill构建指南.md` | 当前仍描述大 frontmatter manifest。 | 指南必须改成轻量 `SKILL.md` + contract + schemas + references 的 v2-only 结构。 |
| `docs/api/api-doc.html` 与开发者文档测试 | API 文档已有 endpoint 说明回归。 | 必须补充 capability、skill execution、interrupt/resume、resource read 的外部调用边界。 |
| `tests/api/test_developer_docs.py` | 当前锁定 API 文档说明质量。 | v2 Skill contract/API 说明必须被文档回归覆盖。 |

## 3. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| C7-001 | `git@gitee.com:biobin/breeding-skill-builder.git` 的 `references/Skill构建指南.md` 只推荐 v2 结构。 | frontmatter 示例只含 name/description；平台契约全部在 `skill.contract.yaml`。 |
| C7-002 | API 文档说明 direct skill execution、slash soft binding、natural language planning。 | 外部调用方知道不能直接硬提交 `skill.*`，而是提交用户消息/附件/interrupt answer。 |
| C7-003 | 文档说明 ResourceService 按需读取边界。 | 主代理不能读取 scripts/runtime/schemas/config 原文。 |
| C7-004 | 测试矩阵可重复执行。 | backend integration/API/e2e 命令列明。 |
| C7-005 | 旧注册路径删除。 | capability registry 不再从旧 metadata/name 派生公开 Skill capability；无 contract Skill 不注册；保留的旧解析代码不得影响 public registry。 |
| C7-006 | 旧执行路径删除。 | main-agent `_run_auto_scripts` 生产路径、SkillExecutor manifest execution/scripts/parameters 路径不再执行项目 Skill；保留代码只能返回 diagnostic/fail-closed。 |
| C7-007 | v2 模板不包含 v1 字段。 | 示例中不得出现 `public_usage`、顶层 `parameters`、`scripts[].auto_run`、`execution` 或 `run_by_default`。 |

## 4. 旧路径删除验收

专题完成时必须满足：

1. 所有项目级公开 Skill 均完成 v2 contract 迁移。
2. 无生产路径依赖 `manifest.scripts[].auto_run`、旧 `parameters`、旧 `execution` 或旧 `public_usage`。
3. `/api/v1/capabilities` 与 planner/replanner 全部通过 contract registry 获取公开 Skill。
4. field-design、field-analysis、rice-genie、OCR、SQLQuery e2e 全绿。
5. 无 contract 的用户级或测试 Skill 在能力池中不可见，并有明确 diagnostic。
6. 文档和模板明确要求用户按 v2 重新制作 Skill。

## 5. 完成门禁

- 文档、API、PRD、CHANGELOG 全部同步。
- 迁移后 full targeted regression 通过。
- License Requirement 记录完整。
- 文档 grep 门禁通过：v2 示例不包含 `auto_run` / `run_by_default` / 顶层 `parameters` / `scripts` / `execution` / `public_usage` 作为平台契约。
- 代码 grep 门禁通过：旧 manifest 执行/注册生产路径已删除或只保留为 diagnostic/fail-closed，不再注册或执行项目 Skill。
