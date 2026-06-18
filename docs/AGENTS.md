# docs/AGENTS.md

本文件用于快速定位文档。除非本文件另有说明，继续遵守仓库根目录 `AGENTS.md`。

## 目录速览

- `api/`：静态 API 文档与 API 更新日志。
- `prd/`：产品 / 架构 PRD，按 backend、frontend、rust、MCP 等主题组织。
- `runbooks/`：运维与部署运行手册。
- `checkpoint/`：checkpoint、time-travel、thread event 等设计与实施计划。
- `superpowers/specs/`：本地设计草案、spec 与阶段性方案。
- 根目录 Markdown / PNG：架构图、流程图、能力接入指南、任务状态图、周报模板等项目级说明。

## Future Work

本栏记录已经成文、但尚未实施或尚未在 PRD 内标记为完成的后续工作。实施、拆分、废弃或标记完成时，必须同步更新本栏、对应 PRD 索引和 `CHANGELOG.md`。

| PRD | 状态 | 后续动作 |
|---|---|---|
| `prd/backend/21-聊天式会话文件智能选择PRD.md` | 设计已确认，待实施 | 实现平台级 `ConversationFileSelector`、`file_selection` / `file_intent` 契约扩展、interrupt 消歧和 `breeding-skill-builder` 同步。 |
| `prd/backend/22-Skill运行闭环Workbench总纲PRD.md` | 总体设计稿，待拆分阶段实施 PRD | 拆分并实施 Workbench 内部 capability、固定 DAG MVP、确定性 runtime replanner 与 contract / policy 驱动策略。 |
