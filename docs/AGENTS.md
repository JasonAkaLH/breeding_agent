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
| `prd/backend/conversation-file-history-selection/README.md`（父入口：`prd/backend/21-对话文件历史与智能选择PRD.md`） | 阶段零、阶段一、阶段二已实施；阶段三至阶段五待实施 | 继续推进 FileRequirementProfile / selector shadow、selector interrupt/绑定、灰度发布与回归门禁；后续若新增 file_upload public 字段，仍需同步后端 sanitizer、memory 投影与前端安全卡片 allowlist。 |
| `prd/backend/22-Skill运行闭环Workbench总纲PRD.md` | 总体设计稿，待拆分阶段实施 PRD | 拆分并实施 Workbench 内部 capability、固定 DAG MVP、确定性 runtime replanner 与 contract / policy 驱动策略。 |
