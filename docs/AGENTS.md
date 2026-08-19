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
- `superpowers/specs/2026-08-14-maf-master-key-domain-derivation-design.md`：首次部署使用单一固定在线根密钥，通过五个闭合 HKDF 领域标签隔离 MCP credential/recovery、Auth token、audit reference 与 sentinel；不包含根密钥轮换或旧密文迁移。
- `superpowers/specs/2026-08-17-public-http-user-mcp-endpoint-policy-design.md`：已在 `main` 仓库实现并通过自动回归与真实 OCR MCP 隔离 smoke；用户级 MCP 允许任意公网 HTTP/HTTPS、自定义端口与 HTTP 凭据，公网 HTTP 由前端一次确认，私网/特殊地址及 DNS/重定向防护继续强制，并覆盖 Phase 3 evidence、legacy migration、错误可见性、历史兼容与回滚边界；`prod` 未变更。
- `superpowers/specs/2026-08-17-dollar-mcp-server-soft-binding-design.md`：`$显示名称` 主动选择用户级 MCP Server，强制当前 Server discovery，由 Selector决定 Tool调用并保留逐 Tool授权；首轮审查补齐持久化前 owner/status验证、后端生成安全历史badge、App候选刷新、显式Selector模式、discovery副作用、最小附件投影和迁移/回滚边界。
- `superpowers/specs/2026-08-17-dollar-mcp-server-soft-binding-implementation-plan.md`：上述 `$` MCP Server Soft Binding 已按 API副作用门禁、固定工作流、Selector/附件隐私、历史审计和前端命令闭环完成仓库实现；真实OCR discover-only smoke仍待受控环境引用，限定 `main`且未触碰 `prod`。
- `superpowers/specs/2026-08-18-planner-node-identity-v1-design.md`：已完成仓库实现；LLM Planner/Runtime Replanner 仅提供局部语义键，Runtime 使用 task、持久化 replan epoch 与 key 生成全局 v1 node ID；覆盖 existing/new 引用、SQLite/PostgreSQL/Rust Sidecar claim、legacy/system兼容、前端 opaque ID、回滚和原故障验收。
- `superpowers/specs/2026-08-18-mcp-dispatch-reference-resume-envelope-design.md`：MCP dispatch v2 引用式恢复信封设计；只保存控制面快照、附件 ID 和 dependency Artifact refs，保持 64 KiB 上限并保留 legacy v1 reader，不保存实际 I/O、Tool 参数、附件正文或 Base64。
- `superpowers/specs/2026-08-18-mcp-dispatch-aggregate-recovery-hardening-design.md`：通过95%信心门且已在`main`完成Phase 0～4仓库实现的 MCP dispatch 聚合状态机与恢复加固设计；保持v2引用式信封和no-replay，统一Tool approval、普通多Call、MRTR、remote Task、durable result、terminal candidate、claim、取消线性化与startup recovery的SQL原子状态合同；真实PostgreSQL与OCR人工smoke仍是外部证据。
- `superpowers/specs/2026-08-18-mcp-dispatch-aggregate-recovery-hardening-implementation-plan.md`：以96%置信度通过95%信心门并已执行的开发计划；记录5个Phase green checkpoint、本地SQLite report/apply/retry、candidate/result lifecycle、18项FR/8项NFR、17个故障注入边界、完整回归例外与真实PostgreSQL缺口。
- `superpowers/specs/2026-08-18-mcp-approval-event-stream-resubscribe-design.md`：连续 MCP Tool 审批的前端事件流恢复设计；审批成功后沿用普通 Interrupt 合同重新订阅当前 Task SSE，使不同 Tool 的后续审批无需刷新即可出现，同时保持 `always_allow` 的 per-Tool 边界。
- `superpowers/specs/2026-08-19-ocr-mcp-trusted-attachment-workflow-design.md`：以旧OCR Skill为行为基准，为显式用户MCP绑定增加execution-only单附件Base64物化与单一逻辑start/poll/ack workflow；同时修复标准`isError=true`误记completed、短Call不续claim及异常后aggregate终态残留，保持64 KiB引用式信封不含实际I/O。
- `superpowers/specs/2026-08-19-ocr-mcp-trusted-attachment-workflow-implementation-plan.md`：上述设计的分阶段开发计划，按纯materializer与job runner、Gateway、Coordinator、SQLite/PostgreSQL finalizer、外部ocr_mcp严格schema、回归checkpoint和用户报纸PNG真实smoke顺序实施。
- `superpowers/specs/2026-08-19-mcp-auto-explicit-route-equivalence-design.md`：已完成仓库实现、自动回归和本地真实auto OCR smoke的MCP路由等价性设计；只在Orchestration向Executor交接的唯一route handoff以可信固定ID或当前Server allowlist验证authority，再把auto/explicit归一化为同一selected-server执行合同；未修改API Runtime、恢复Provider、执行链、v2信封、Storage或多MCP DAG。
- `superpowers/specs/2026-08-19-mcp-auto-explicit-route-equivalence-implementation-plan.md`：上述route-only设计的已执行开发计划；记录纯handoff红绿测试、Orchestration唯一接入点、恢复/多MCP/prompt-injection回归、完整验证和用户PNG真实auto OCR smoke证据，业务源码严格限定为两个Orchestration文件。
- `superpowers/specs/2026-08-19-mcp-tool-result-shared-artifact-standard-design.md`：经十轮一致性复审以99%置信度通过95%信心门、尚未实施的MCP Tool原始返回Artifact设计；每个completed业务Call正常路径生成唯一公共Artifact，到期异常闭合permanent failure；业务result只可在本轮投影后精确CAS删除，bulk GC永久排除；覆盖ordinary/approval/remote/60秒补投、目标盘容量、精确事件幂等、Task与Message历史提醒，以及源`artifact_owned → deleted`生命周期。
- `superpowers/specs/2026-08-19-mcp-tool-result-shared-artifact-standard-implementation-plan.md`：经九轮自主审计/修订以99%通过信心门的待执行计划；按红测、未装配projector authority、安全reconciler/GC、一次性runtime激活、Task/Message API与MessageBubble提醒、完整回归和真实OCR smoke实施，设置1个非生产合同checkpoint、1个完整代码checkpoint和最终文档checkpoint。
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
| `prd/MCP/user-scoped-on-demand/` | 阶段一、阶段二业务闭环和阶段三 CP-0～CP-6 仓库实现已落地；CP7-A 开发候选待人工验收 | `main` 首次部署的单一在线根密钥与五领域派生已按 `superpowers/specs/2026-08-14-maf-master-key-domain-derivation-design.md` 实施并通过自动验收；只有明确回复“可以退役”后才执行 CP7-B 物理删除。`prod`、根密钥轮换和旧密文迁移仍不在当前范围。 |
| `superpowers/specs/2026-08-18-mcp-dispatch-aggregate-recovery-hardening-design.md`及对应implementation plan | Phase 0～4仓库实现、本地SQLite cutover与17边界自动proof已完成 | 重启本地新backend后由用户创建新OCR Task做approval/恢复smoke；补充真实PostgreSQL validation DSN证据。不得自动复活旧失败Task或把本地证据当作`prod`部署。 |
| `superpowers/specs/2026-08-19-ocr-mcp-trusted-attachment-workflow-design.md` | 主仓实现、外部ocr_mcp严格source schema、自动回归和用户报纸PNG本地真实smoke已完成 | 大于10 MiB companion upload和远端OCR严格schema源码部署保持独立后续工作；不得把本地源码测试记为远端发布。 |
| `superpowers/specs/2026-08-19-mcp-tool-result-shared-artifact-standard-design.md` | 99%通过信心门；尚未实施 | 复用现有promotion与公共Artifact链；按Call ready/permanent闭合、业务result精确删除CAS、bulk排除、四路径projector、Task/Message history提醒、无网络重放和OCR下载验收。 |
| `superpowers/specs/2026-08-19-mcp-tool-result-shared-artifact-standard-implementation-plan.md` | 99%通过信心门；待实施 | 依次完成未装配authority、安全reconciler、一次性runtime激活、Task/Message API、MessageBubble notice、自动追踪矩阵与真实OCR smoke；Checkpoint B前禁止重启或部署。 |
