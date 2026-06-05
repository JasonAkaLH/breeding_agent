# 阶段三：SkillResourceService 按需读取 PRD

- **状态**：待实施
- **父总纲**：`00-SkillContract渐进式披露与显式执行总纲PRD.md`
- **依赖**：阶段一 contract registry
- **目标模块**：SkillResourceService、resource policy、audit、脱敏与裁剪
- **目标结果**：主代理、补槽生成器和 runtime 可通过统一服务按需读取 Skill bundle 内资源；bundle 内默认可读，但 prompt-facing 读取必须受黑名单、路径边界、大小、脱敏和审计约束。

## 1. 范围

### 1.1 In scope

- 新增 `SkillResourceService / SkillResourceReader`。
- 支持 `resource_id` 与 bundle-relative `path` 读取。
- 实现 audience：`main_agent`、`slot_question`、`runtime`、`schema_selector`。
- 实现默认 allow + denylist 策略。
- 实现路径归一化、bundle 根目录限制、symlink 越界拒绝。
- 实现 prompt-facing 扩展名/目录拒绝、max bytes/tokens、脱敏。
- 记录 `skill.resource_read` audit event，包括拒绝事件。
- 从 `SKILL.md` body 提取相对路径候选索引时，也必须走同一读取策略。

### 1.2 Out of scope

- 不改主代理 prompt 使用资源的逻辑。
- 不改 SkillExecutor 执行逻辑。
- 不实现全文搜索/RAG；只做受控文件读取与裁剪。

## 2. 功能需求

| ID | Requirement | 验收 |
| --- | --- | --- |
| C3-001 | resource_id 读取 contract 声明的 public resource。 | `usage` / `interval_help` 可读取并裁剪。 |
| C3-002 | path 读取限制在 bundle 内。 | `../`、绝对路径、symlink 越界被拒。 |
| C3-003 | main_agent/slot_question 拒读内部实现。 | `scripts/`、`runtime/`、`schemas/`、`native/`、`config.yaml` 被拒。 |
| C3-004 | runtime audience 可读取更多 bundle 内资源但仍受硬黑名单。 | `.env`、`.git`、secret/token/credential 仍拒绝。 |
| C3-005 | 内容裁剪和脱敏生效。 | 超大文件 truncated；token/password/base_url 被脱敏。 |
| C3-006 | 每次读取/拒绝均审计。 | `skill.resource_read` payload 不含文件原文。 |
| C3-007 | `SKILL.md` 索引只是导航。 | 索引到 allowed 文档可读；索引到 denied 路径被拒；不推导 contract。 |

## 3. 安全策略

- 全局硬黑名单不可由 contract 放开。
- prompt-facing audience 不得读取 machine schema 原文。
- 非文本文件不得原样进入 prompt。
- 审计只记录路径摘要、resource id、audience、truncated、redaction_count、denied_reason。

## 4. 测试计划

- `tests/integrations/agent_skills/test_skill_resource_service.py`
- API/runtime smoke 覆盖 audit event。

## 5. 完成门禁

- 所有资源读取安全测试通过。
- 无 secret/raw content 进入 audit。
- public profile 尚未消费全文，只能消费 resource index。
- CHANGELOG 记录 License Requirement。
