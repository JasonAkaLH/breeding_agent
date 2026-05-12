# Agent 基础设施优化建议

- 日期：2026-05-11
- 状态：基础设施评估与后续优化建议稿；不代表已经批准实施或已经完成改造。
- 范围：当前仓库内主代理、编排、状态存储、Capability / Skill、SQLQuery、API / SSE、前端业务对话台与配套测试 / 文档。
- 依据：本次只读检查当前代码与文档关系；未访问真实 LLM / MySQL provider，未运行全量回归。

## 1. 当前成熟度判断

当前项目已经不是“概念验证”或空框架，而是一个**单机服务型 Agent 平台内核 + SQLQuery / Skill 能力体系 + 前端业务对话台**的可运行基线。

已经形成的基础设施能力包括：

| 领域 | 当前建设情况 | 代表路径 |
| --- | --- | --- |
| API / Runtime 装配 | FastAPI app、DTO、SSE、runtime bootstrap 与配置注入已经形成主入口。 | `src/api/` |
| 编排内核 | Capability registry、workflow plan、router、scheduler、LLM planner、validator、expander 已分层。 | `src/orchestration/` |
| 生命周期 | task / node / mailbox / interrupt / cancel / conversation guard 规则独立沉淀。 | `src/lifecycle/` |
| 状态存储 | 已有状态存储抽象与 SQLite 实现，为后续 PostgreSQL 迁移留出接口。 | `src/storage/` |
| 主代理能力 | `main_agent.respond`、prompt 构造、streaming 输出与 Skill 兼容层已落地。 | `src/capabilities/main_agent/` |
| SQLQuery 能力 | public macro + 内部只读查询 workflow、SQL Guard、schema context 与候选表格筛选已落地。 | `src/capabilities/sql_query/`, `src/sql_query/`, `configs/sql_query/` |
| Skill 能力池 | 项目级 Skill 已可进入 public capability pool，并有 forced skill、安全 metadata 剥离与审计。 | `skill/`, `src/integrations/codex_skills/`, `src/orchestration/skill_workflow_provider.py` |
| 前端联调 | React + TypeScript + Vite + Ant Design 对话台、SSE client、状态 reducer 与结果卡片测试已存在。 | `frontend/` |
| 验证资产 | 后端按模块分层 `unittest`，前端有 Vitest / build 命令；当前文档基线已收口到 PRD 目录与少量专题说明。 | `tests/`, `docs/prd/`, `docs/` |

整体评价：**框架边界和能力抽象已经成型，下一阶段重点应从“能跑通能力链路”转向“可生产运行、可治理、可扩展、可观测”。**

## 2. 主要风险与短板

| 风险 | 当前表现 | 影响 |
| --- | --- | --- |
| 单机运行假设较强 | SQLite、进程内事件 broker、本地 runtime 装配与部分内存态能力仍是主路径。 | 横向扩展、重启恢复、生产可用性受限。 |
| 执行资源治理不足 | 已有 retry / timeout / scheduler 概念，但缺少统一的并发预算、LLM token 预算、能力级限流和资源池治理。 | 多用户 / 多任务并发时容易出现 provider 抖动、DB 压力或任务互相拖慢。 |
| 事件与任务恢复能力偏弱 | SSE 与任务事件已有实现，但还没有明确的持久化事件重放、断线恢复与 dispatcher 恢复模型。 | 前端断线、服务重启或长任务中断后，用户体验和排障难度上升。 |
| 运维观测仍偏测试驱动 | 有 audit log 与大量测试，但缺少统一 metrics、trace、health/readiness、告警字段与日志轮转策略。 | 线上定位慢，难以量化 LLM / SQLQuery / Skill 的成功率、耗时和 fallback 分布。 |
| Capability 扩展仍依赖中心装配 | Skill 已一等化，但 runtime 中央装配、前端能力模式与 public capability 发现仍有继续插件化空间。 | 后续新增能力时容易在 API runtime、前端展示、Planner prompt、审计和测试多处同步修改。 |
| Skill 信任边界待强化 | 当前已做 public root、manifest、forced metadata 与 artifact 安全约束，但若 Skill 来源变复杂，仍需要更强 sandbox。 | 第三方 / 半可信 Skill 执行时存在网络、文件、CPU / 内存与依赖供应链风险。 |
| 文档权威源有漂移 | 若干根目录历史文档已被 PRD / Phase 文档吸收，但仍被部分文档或 README 提及。 | 新协作者容易误读旧方案为当前事实。 |

## 3. 优先级建议

### P0：生产运行基座

| 优化项 | 建议交付物 | 验收口径 |
| --- | --- | --- |
| PostgreSQL 状态存储后端 | 在现有 `src/storage/` 抽象下补 PostgreSQL 实现、迁移脚本、连接配置、SQLite / PostgreSQL 同构测试。 | 同一生命周期测试可在 SQLite 与 PostgreSQL 后端复用；状态模型无业务语义分叉。 |
| Durable task dispatcher | 明确任务领取、节点执行、恢复、重复执行幂等与失败补偿规则，避免只依赖单进程内存状态。 | 服务重启后可恢复未终止任务或明确标记为可解释失败；任务状态无悬空节点。 |
| 持久化事件与 SSE replay | 将关键任务事件持久化，定义 event sequence / cursor / schema version。 | 前端断线重连后可按 cursor 补齐关键状态，不依赖仅内存事件流。 |
| 上传与 artifact 存储外置化 | 将当前本地 / 内存态文件能力抽象成可替换 store，保留本地开发实现。 | artifact 元数据与正文生命周期一致；替换旧文件、下载 404/gone 等语义在不同 store 下一致。 |
| 文档权威源收口 | 明确 PRD / README / 专题说明的入口关系，持续清理过期历史文档。 | README 与索引只指向当前权威源；旧文档不再被误认为执行入口。 |

### P1：资源治理与可观测性

| 优化项 | 建议交付物 | 验收口径 |
| --- | --- | --- |
| 统一执行预算 | 为 task、capability、LLM、SQLQuery、Skill 定义 timeout、retry、并发、队列长度和 token / cost 预算。 | 超限行为可预测：排队、拒绝、取消或失败事件均可审计。 |
| 并行 ready-node 调度 | 在 DAG 语义允许时支持 bounded parallel execution，并按 capability 类型隔离并发池。 | 独立节点可并行，依赖节点顺序稳定；失败传播与 cancel 语义保持一致。 |
| LLM provider 观测 | 记录 provider、model、prompt / completion token、耗时、失败类型、自修复次数、fallback / fail-closed 原因。 | 可按任务和 capability 汇总 LLM 成本、延迟和失败率。 |
| SQLQuery 观测 | 记录 schema context 构建、SQL Guard、DB 查询、LIKE 召回、LLM 表格筛选、降级路径耗时与结果规模。 | 可定位慢查询、召回失败、Guard 拒绝和 provider 降级。 |
| Skill 执行观测 | 记录 selected / forced / missing、脚本耗时、artifact 产出、manifest 约束命中与 sandbox 拒绝。 | Skill 问题可从审计和 metrics 直接复盘。 |
| Health / readiness | 增加 runtime 依赖健康检查：数据库、LLM 配置、MySQL readonly、artifact store、Skill catalog。 | 启动期与运行期可区分“服务存活”和“关键依赖可用”。 |

### P2：扩展治理与长期演进

| 优化项 | 建议交付物 | 验收口径 |
| --- | --- | --- |
| Capability 插件化注册 | 从中心 runtime 装配逐步转向能力自描述注册：descriptor、macro provider、executor、前端展示 metadata、测试契约成套出现。 | 新增能力无需在多个中心文件散落特判。 |
| 前端能力发现协议 | `/capabilities` 返回足够 UI metadata，让前端减少静态 capability 模式假设。 | 新能力进入 public pool 后，前端可展示基础入口或结果占位，无需立即硬编码。 |
| Skill sandbox 强化 | 对半可信 Skill 增加进程隔离、网络策略、文件系统白名单、CPU / 内存限制和依赖校验。 | 不可信或半可信 Skill 的失败不会污染主进程、泄露文件或绕过 artifact 管理。 |
| 配置与密钥治理 | 保持 `config.yaml` 本地化，同时完善部署环境变量、secret source、配置快照与启动审计。 | 同一 runtime 中组件配置来源一致，可审计且不会在节点执行阶段重复读配置文件。 |
| 文档漂移检查 | 增加轻量脚本检查旧文档引用、废弃文档标记、PRD / Phase 索引一致性。 | 文档改动可在 PR 中发现 stale reference。 |

## 4. 建议执行顺序

1. **先做状态与恢复**：PostgreSQL 后端、迁移、durable dispatcher、事件 replay。
   这是从本地联调走向生产环境的前置条件。
2. **再做资源治理**：capability 并发池、LLM / SQLQuery / Skill 预算、timeout / retry 统一语义。
   这能避免多用户压力下把 provider、DB 或主进程拖垮。
3. **同步补可观测性**：metrics、trace、health、审计字段标准化。
   没有观测就无法判断第 1、2 步是否有效。
4. **最后推进插件化与 sandbox**：在现有 Skill 一等 Capability 能力池基础上继续降低中心装配成本，并根据 Skill 信任边界决定 sandbox 深度。
