# 主代理框架 PRD（后端总览）

- **项目**：multi_agent_framework
- **范围**：后端主代理框架
- **文档状态**：正式版（已补齐至对话上下文记忆与压缩 PRD）
- **日期**：2026-05-07
- **说明**：本文件为后端 PRD 总览入口。后端专题 PRD 统一放在 `docs/prd/backend/`；前端 PRD 后续放在 `docs/prd/frontend/`。

## 0. 目录定位

本 PRD 只覆盖后端：主代理框架、SQLQuery 能力链路、状态存储、API、LLM runtime 与后端可观测性。

前端产品体验、页面结构、交互与视觉设计不在本文件展开；后续前端设计应以 `docs/prd/frontend/` 为入口，并引用本目录中的后端 API / 事件 / 能力契约。

## 1. 项目背景

本项目面向内部付费用户，目标是构建一个办公助手后端。当前优先建设的是主代理框架，而不是具体功能 Agent 本身；后续文档 RAG、SQLQuery、数据分析、农业生物信息分析等能力将在该框架之上接入。

本框架不依赖 LangChain、LangGraph、AutoGen 等现成 Agent 框架，采用 Python 为主、异步优先的服务端架构；性能热点未来可下沉到 C++，但不作为一期前提。

## 2. 一期目标与非目标摘要

### 2.1 一期目标

一期需交付一个可支撑业务扩展的主代理内核，至少覆盖：
- 任务拆分
- Agent 注册与发现
- 资源调度与执行
- 上下文传递
- 会话状态
- 任务队列
- 观测日志
- 记忆系统（以会话延续型记忆为主）
- 实时事件流
- 用户主动中断任务
- 首个可验收业务样例绑定为 **SQLQuery 只读查询链路**

### 2.2 一期不做但必须预留接口

- 人工审批
- 权限控制
- 通用工具调用平台
- 多实例生产化部署能力
- 完整长期记忆系统
- 跨任务知识沉淀 / 任务知识复用型记忆

## 3. 总体架构摘要

### 3.1 产品与部署形态

- 产品形态：前后端分离
- 对外形态：HTTP / API 服务
- 服务模型：异步任务驱动的对话式 Agent 服务
- 业务查询数据源：公司现有 MySQL 数据库
- 主框架状态库目标：PostgreSQL
- 本地测试状态库：SQLite

### 3.2 架构核心原则

- 主代理优先面向 **capability** 编排，而不是直接面向 tool
- 任务采用**混合型 DAG**，允许受控动态扩展
- 系统内部采用：**状态机主干 + 结构化 mailbox + Interrupt/Resume**
- 硬停止的正式语义为：**Task Context Termination**
- 背压策略：**严格拒绝型**
- 配额策略：**系统级 + capability 级**，并预留未来用户级配额兼容

## 4. 后端专题 PRD 索引

| 专题 | 文件 | 适合阅读场景 |
|---|---|---|
| 产品目标与范围 | `docs/prd/backend/01-产品目标与范围.md` | 了解项目背景、后端边界、术语 |
| 编排模型与资源调度 | `docs/prd/backend/02-编排模型与资源调度.md` | 主代理拆分、DAG、调度、背压、配额 |
| 协作协议与任务生命周期 | `docs/prd/backend/03-协作协议与任务生命周期.md` | mailbox、interrupt/resume、取消、状态机 |
| 状态存储与迁移策略 | `docs/prd/backend/04-状态存储与迁移策略.md` | SQLite / PostgreSQL、mailbox DDL、迁移 |
| API 与核心数据模型 | `docs/prd/backend/05-API与核心数据模型.md` | API、Conversation/Task/Node 等对象模型 |
| SQLQuery MVP 设计 | `docs/prd/backend/06-SQLQuery-MVP设计.md` | SQLQuery 路由、SQL Guard、Schema Context Builder、MVP 验收 |
| SQLQuery LLM 增强与真实库验证 | `docs/prd/backend/07-SQLQuery-LLM增强与真实库验证.md` | Phase 5.5、prompt schema、LLM fallback、MySQL 只读适配器 |
| 主代理 Skill 兼容与真实 LLM Runtime | `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md` | Phase 8 / 8.2、普通主代理消息、Skill 上下文、真实 provider smoke |
| 高层 DAG 规划与 SQLQuery 宏能力边界 | `docs/prd/backend/09-高层DAG规划与SQLQuery宏能力边界.md` | Phase 8.1、public capability、planner validator、macro expander |
| 对话上下文记忆与压缩 | `docs/prd/backend/10-对话上下文记忆与压缩PRD.md` | 多轮对话记忆、Planner / 主代理上下文注入、两级压缩策略 |

## 5. 当前已定的关键决策摘要

### 5.1 主框架共性决策

- 当前只写后端主代理框架 PRD，不先展开前端与具体功能 Agent 产品实现。
- capability 是稳定能力契约；agent 是执行实体；tool 是底层操作接口。
- 子代理执行采用混合模式：优先专用任务型 Agent，必要时允许受限 ReAct Worker。
- 同一 `conversation_id` 内任务串行执行。
- 主代理采用受规则、状态机与完成判定约束的编排型闭环，而不是自由试错式纯 ReAct。
- 任务优先级采用“两层模型”：控制类动作独立最高优先级；普通任务按来源驱动排序，并允许少量结构化权重作为同类内排序依据。
- 主框架与 capability 是明确上下级关系：主框架只管拆解、编排、分发；SQLQuery 等 capability 只管各自执行。

### 5.2 协作与生命周期决策

- 结构化 mailbox 采用统一信封 + channel + typed payload 模型。
- mailbox 生命周期采用 **分级 ACK**。
- 强 ACK 用于控制类 / interrupt 类消息；轻 ACK 用于普通协作类消息。
- 停止处理的正式语义是终止 task context，而不是直接定义为“杀线程”。

### 5.3 状态存储决策

- 主框架状态不落公司业务 MySQL。
- 本地先 SQLite，同构迁移到 PostgreSQL。
- PostgreSQL 存结构化状态与索引，不直接存大对象正文。
- PostgreSQL DDL 采用 ORM Model + migration 生成；一期索引策略为基础索引 + 少量关键增强索引。

### 5.4 SQLQuery 决策

- SQLQuery 是一期首个 MVP 样例，外部只暴露 `sql_query.query` 宏能力；`sql_query.*` 内部节点不作为外部请求入口。
- SQLQuery 只允许只读查询；MySQL 只读执行必须通过 SQL Guard 通过令牌后才能执行。
- MySQL 连接串与只读账号只允许通过本地 `config.yaml` 或部署环境变量注入，不得在仓库内硬编码；仍保留 SQL Guard 作为数据库权限之外的第二层保护。
- SQLQuery 的 SQL 生成与结果筛选默认可接入 LLM；当前默认 workflow 尾节点为 `sql_query.result_filtering`，负责从 `LIKE` 召回候选中筛掉不符合用户真实需求的行，并把筛选后的表格交给主代理整合。

### 5.5 主代理与 LLM Runtime 决策

- `capability_id=None` 的普通消息默认进入 `main_agent.respond`；显式 `sql_query` / `sql_query.query` 进入 SQLQuery 固定 workflow。
- 主代理可读取 Codex Skill 兼容的 `SKILL.md` 元数据、上传 artifact 脱敏上下文与受控脚本输出，用于构造提示词。
- 主代理真实 LLM provider 必须通过可测试 seam 绑定；自动化测试默认使用 fake / injected stream，真实 provider 只在显式配置或手工 smoke 中验证。
- 主代理与 SQLQuery 的 LLM 审计事件不得记录 API key、完整 prompt、完整 rows、base_url 等敏感信息。


### 5.6 对话记忆与上下文压缩决策

- v1 记忆系统定位为 conversation 内会话延续型记忆，不做跨会话长期用户画像或知识沉淀。
- 对话记忆上下文注入 LLM Planner / 自动规划阶段与 `main_agent.respond` 最终回答阶段，保证追问、省略主语和纠错能正确影响路由与回答。
- SQLQuery 内部 LLM 节点暂不直接消费完整对话记忆；如需上下文补全，应先在 public 规划层把当前轮问题合成为明确问题。
- 记忆压缩采用两级策略：Level 1 删除 capability 业务中间产物；Level 2 对较早对话历史做摘要压缩并保留最近若干轮原文。
- 记忆上下文必须按 account / conversation 隔离，并禁止注入 SQL、guard token、schema DDL、完整 rows、完整 prompt、API key、base_url 等敏感或高成本内容。

## 6. 相关配套文档

- PRD 总目录：`docs/prd/README.md`
- 前端 PRD 预留入口：`docs/prd/frontend/README.md`
- 数据库结构说明：`docs/MySQL数据库表结构说明.md`
- SQLQuery prompt 输入模板：`docs/SQLQuery提示词输入模板.md`
- 开发流程索引：`docs/dev_processes/backend/README.md`
- 对话上下文记忆与压缩 PRD：`docs/prd/backend/10-对话上下文记忆与压缩PRD.md`

## 7. 使用建议

- 做全局规划时先读本文件。
- 做局部设计或开发计划时优先读取对应专题文档。
- 做 SQLQuery 实现或提示词设计时，配合 `docs/prd/backend/06-SQLQuery-MVP设计.md`、`docs/prd/backend/07-SQLQuery-LLM增强与真实库验证.md` 与 `docs/SQLQuery提示词输入模板.md` 一起阅读。
- 做前端设计时，不要把前端范围追加到本文件；应在 `docs/prd/frontend/` 新建独立 PRD，并引用本目录中的后端接口和事件契约。

## 8. 后续专题设计与演进项

以下事项不阻碍当前 PRD 作为正式基线，但建议在后续专题设计中继续细化：
- PostgreSQL 最终 DDL 文件生成方式与索引增强细节。
- PostgreSQL 部署完成后的索引优化、JSONB 查询策略与正式 DDL 生成流程。
- 任务优先级权重的更细粒度策略。
- Schema Context Builder 的更强评估样例与调优工具。
- 前端 PRD：对话界面、任务流、事件流、SQLQuery 结果与主代理 Skill 命中状态展示。
