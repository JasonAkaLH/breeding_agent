# docs/AGENTS.md

本文件用于快速定位文档。除非本文件另有说明，继续遵守仓库根目录 `AGENTS.md`。

## 目录速览

- `api/`：静态 API 文档与 API 更新日志。
- `prd/`：产品 / 架构 PRD，按 backend、frontend、rust、MCP 等主题组织。
- `runbooks/`：运维与部署运行手册。
- `runbooks/user-mcp-gateway.md`：用户级 MCP Gateway 密钥、容量、基础发布和故障边界。
- `runbooks/user-mcp-phase3-rollout.md`：Phase 3 canonical routing、cohort/实例准入、8 条权威安全红线、PostgreSQL 分角色 HMAC evidence/ledger、consumer × capability schema-contract 连续性与同事务 durable migration audit、CP-0 恢复、回滚和 legacy 删除门禁。
- `checkpoint/`：checkpoint、time-travel、thread event 等设计与实施计划。
- `superpowers/specs/`：本地设计草案、spec 与阶段性方案。
- `superpowers/specs/2026-07-15-docker-cmd-local-only-protection-design.md`：`docker_cmd.md` 本地保留、Git 历史清理与防止重新跟踪的安全边界。
- `superpowers/specs/2026-08-12-user-scoped-mcp-routing-execution-design.md`：用户级 MCP 两级路由、授权、执行账本、在线租约与前端闭环实施设计。
- `superpowers/specs/2026-08-13-user-mcp-cp7-manual-retirement-design.md`：仅在 `main` 执行的 CP7-A assembly-off 候选、人工验收门禁与获批后 CP7-B 物理退役设计；不修改或宣称完成 `prod`。
- 根目录 Markdown / PNG：架构图、流程图、能力接入指南、任务状态图、周报模板等项目级说明。
- `prd/backend/23-能力缺失LLMFallback披露PRD.md`：能力库无匹配 Skill/MCP/capability 时的 LLM fallback、事实披露、Workbench 停止与历史提示契约父兼容入口。
- `prd/backend/capability-missing-fallback/`：能力缺失 LLM fallback 披露分步 PRD，按现状清理、Plan metadata 契约、后端 full fallback、前端 notice/history、partial fallback/Replanner 审计五阶段组织。
- `prd/MCP/user-scoped-on-demand/`：用户专属 MCP 配置与低常驻资源改造，按配置/Gateway、两级路由/授权、灰度/旧 Runtime 下线三阶段组织。

## Future Work

本栏记录已经成文、但尚未实施或尚未在 PRD 内标记为完成的后续工作。实施、拆分、废弃或标记完成时，必须同步更新本栏、对应 PRD 索引和 `CHANGELOG.md`。

| PRD | 状态 | 后续动作 |
|---|---|---|
| `个人桌面长任务Agent总体设计总纲.md` | 总纲已确认；实现尚未开始 | 基于该总纲生成分阶段实施计划；个人版以 Rust daemon 为唯一可信控制 runtime，必须支持受控子 Agent spawn 以及主 Agent 决策、Runtime 仲裁的权限/上下文/交接边界，一次性替换服务端架构，旧历史只读导入。 |
| `prd/backend/capability-missing-fallback/README.md`（父入口：`prd/backend/23-能力缺失LLMFallback披露PRD.md`） | Phase 0 至 Phase 4 代码实现已落地 | 后续仅在新增 fallback reason、artifact 政策或能力注册语义时同步更新 PRD、sanitizer、前端 notice 与测试矩阵。 |
| `prd/backend/conversation-file-history-selection/README.md`（父入口：`prd/backend/21-对话文件历史与智能选择PRD.md`） | 阶段零至阶段五已实施；后续仅保留 guarded multi-select 放量/观测增强 | 若后续放量 `enforce_guarded_multi` 或新增 file_upload public 字段，需同步发布指标、后端 sanitizer、memory 投影、前端安全卡片 allowlist、API 文档与 CHANGELOG。 |
| `prd/backend/skill-workbench/README.md`（父入口：`prd/backend/22-Skill运行闭环Workbench总纲PRD.md`） | 已拆分为阶段零至阶段四；待实施 | 按阶段实施 Workbench policy/runtime state/stage placement、内部 capability/executor、runtime loop/finalizer/Skill refinement、事件 graph prompt 脱敏、contract / health diagnostics。 |
| `prd/MCP/user-scoped-on-demand/` | 阶段一、阶段二业务闭环和阶段三 CP-0～CP-6 仓库实现已落地；CP7-A 开发候选待实施 | 项目负责人已批准在 `main` 取消定时灰度观察，并按 `superpowers/specs/2026-08-13-user-mcp-cp7-manual-retirement-design.md` 先交付 assembly-off 人工测试候选；只有明确回复“可以退役”后才在 `main` 执行 CP7-B 物理删除。`prod` 仍未修改或部署，现有 Phase 3 Runbook 在 CP7-A/CP7-B 实施时同步修订。 |
