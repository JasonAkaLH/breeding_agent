# 一期开发流程总览

## 目的

本目录用于把一期开发计划拆成可执行的 Phase 文档，写法参考外部 `PRD/dev_processes/` 样式，但内容完全基于当前仓库自己的 PRD 与约束。

与 `docs/主代理框架PRD.md`、`docs/prd/*.md` 的关系如下：
- `docs/主代理框架PRD.md`：总览与决策入口；
- `docs/prd/*.md`：专题 PRD；
- `docs/dev_processes/*.md`：开发实施文档，回答“先做什么、后做什么、每个阶段做到什么程度”。

---

## Phase 总表

| Phase | 主题 | 主要输入 PRD | 主要输出 |
|---|---|---|---|
| Phase 0 | 冻结一期范围与验收边界 | `docs/主代理框架PRD.md`、`docs/prd/01-产品目标与范围.md` | 一期范围红线、非目标清单、验收口径 |
| Phase 1 | 建立核心契约与共享模型 | `docs/prd/02-编排模型与资源调度.md`、`docs/prd/05-API与核心数据模型.md` | `core/` 层共享模型与 contract |
| Phase 2 | 落地 SQLite 状态存储与仓储抽象 | `docs/prd/04-状态存储与迁移策略.md`、`docs/prd/05-API与核心数据模型.md` | `storage/` 抽象与 SQLite 实现 |
| Phase 3 | 实现生命周期与协作协议 | `docs/prd/03-协作协议与任务生命周期.md` | mailbox / interrupt / cancel / serial guard |
| Phase 4 | 打通编排调度与最小运行闭环 | `docs/prd/02-编排模型与资源调度.md` | 通用 orchestration 标准、registry、scheduler、workflow 执行闭环 |
| Phase 5 | 接入 SQLQuery MVP 能力链路 | `docs/prd/06-SQLQuery-MVP设计.md` | 按 Phase 4 标准适配的 SQLQuery capability 与 workflow definition |
| Phase 6 | 接入 FastAPI / SSE 对外接口 | `docs/prd/05-API与核心数据模型.md`、`docs/prd/03-协作协议与任务生命周期.md` | API、事件流、cancel、审计输出 |
| Phase 7 | 完成一期验收并评估第二阶段 | `docs/prd/05-API与核心数据模型.md`、`docs/prd/06-SQLQuery-MVP设计.md` | e2e 验收、observability 结果、二期评估输入 |
| Phase 5.5 | SQLQuery LLM 增强专题 | `docs/SQLQuery-LLM版本改造方案.md`、`docs/LLM接入阶段建议.md`、`docs/SQLQuery提示词输入模板.md` | SQLQuery 内部 LLM 主路径、fallback、可观测与测试口径 |
| Phase 8 | Codex Skill 兼容层与上传文件上下文驱动的主代理技能选择机制 | Phase 5.5 结论、Codex Skill `SKILL.md` 格式、Web 上传 artifact 约束 | `main_agent.respond`、Skill parser/catalog/matcher、IO contract、受控脚本 runner、ArtifactRef prompt context、主代理 streaming runtime |
| Phase 8.1 | SQLQuery 宏能力与 LLM 动态 DAG 规划 | Phase 8 主代理 LLM / Skill runtime 结论、SQL 查询整体能力边界 | SQLQuery public capability、macro/subworkflow capability 规则、LLM Planner 高层 DAG 规划边界 |

---

## 执行顺序

必须按顺序推进：

1. `Phase-0-冻结一期范围与验收边界.md`
2. `Phase-1-建立核心契约与共享模型.md`
3. `Phase-2-落地SQLite状态存储与仓储抽象.md`
4. `Phase-3-实现生命周期与协作协议.md`
5. `Phase-4-打通编排调度与最小运行闭环.md`
6. `Phase-5-接入SQLQuery-MVP能力链路.md`
7. `Phase-6-接入FastAPI-SSE对外接口.md`
8. `Phase-7-完成一期验收并评估第二阶段.md`

补充专题：

- `Phase-5.5-SQLQuery-LLM增强专题.md`：编号上承接 Phase 5 的 SQLQuery capability，但启动时机位于一期 Phase 7 验收通过之后；用于沉淀 SQLQuery 内部 LLM 化讨论与后续实施口径。
- `Phase-8-Codex-Skill兼容层与上传文件上下文驱动的主代理技能选择机制.md`：二期主代理能力专题，已完成首轮主代理 LLM 接入与 Codex Skill 兼容层实现；用于沉淀 Codex Skill 格式兼容、输入输出契约识别、受控执行 skill 包内声明脚本、上传文件 `ArtifactRef` 边界与主代理技能选择机制；首版不复刻 Codex 本地文件 / 任意 shell / plugin runtime。
- `Phase-8.1-SQLQuery宏能力与LLM动态DAG规划.md`：二期主代理规划专题设计稿，明确 SQL 查询公开命名改为 SQLQuery、对外只暴露 `sql_query.query` 宏能力，并把 `sql_query.*` 限定为内部固定子工作流实现细节。

---

## 使用约束

- Phase 文档是**开发过程文档**，不是 PRD 替代品。
- Phase 5.5 是一期验收后的增强专题，不修改 Phase 0 ~ Phase 7 的已完成验收结论。
- Phase 8 是二期主代理能力专题，不修改 Phase 0 ~ Phase 7 的已完成验收结论，也不默认包含完整 Codex runtime 复刻。
- 后续若某个 Phase 范围扩大，必须先更新对应 Phase 文档，而不是直接在代码里扩范围。
- 每个 Phase 结束时，都要能明确回答：
  - 这一阶段做了什么；
  - 没做什么；
  - 下阶段依赖什么；
  - 如何证明本阶段已经完成。


---

## 关键边界约束

- **Phase 4 负责定义主代理的通用编排标准**，包括 workflow/node 执行协议、capability 调用契约、调度与收敛语义。
- **Phase 4 必须兼容后续新增 capability**，不能为了 SQLQuery 这个首个 capability 而反向定制 orchestration 内核。
- **Phase 5 负责让 SQLQuery 适配 Phase 4 的标准**，而不是让 Phase 4 去适配 Phase 5 的业务细节。
- 因此，Phase 4 的验证应优先使用 mock/fake capability flow；SQLQuery 的真实业务闭环留到 Phase 5。
- **一期记忆系统只做会话延续型记忆**：服务同一账户下的会话连续性与上下文恢复，不在一期内沉淀跨任务知识。


## 配套设计稿

- `docs/dev_processes/Phase-2-SQLite状态存储表结构草案.md`：将 Phase 2 的状态存储规则翻译成 SQLite 表结构、索引、独立列/JSON 字段映射与迁移关注点。
