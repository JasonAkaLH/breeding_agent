# 主代理框架PRD

- **项目**：multi_agent_framework
- **范围**：后端主代理框架
- **文档状态**：正式版（总览）
- **日期**：2026-04-22
- **说明**：本文件为总览入口，具体设计已按专题拆分到 `docs/prd/` 目录。

## 1. 项目背景
本项目面向内部付费用户，目标是构建一个办公助手后端。当前优先建设的是主代理框架，而不是具体功能 Agent 本身；后续文档 RAG、NL2SQL、数据分析、农业生物信息分析等能力将在该框架之上接入。

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
- 首个可验收业务样例绑定为 **NL2SQL 只读查询链路**

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

## 4. 专题文档索引
| 专题 | 文件 | 适合阅读场景 |
|---|---|---|
| 产品目标与范围 | `docs/prd/01-产品目标与范围.md` | 了解项目背景、边界、术语 |
| 编排模型与资源调度 | `docs/prd/02-编排模型与资源调度.md` | 主代理拆分、DAG、调度、背压、配额 |
| 协作协议与任务生命周期 | `docs/prd/03-协作协议与任务生命周期.md` | mailbox、interrupt/resume、取消、状态机 |
| 状态存储与迁移策略 | `docs/prd/04-状态存储与迁移策略.md` | SQLite / PostgreSQL、mailbox DDL、迁移 |
| API与核心数据模型 | `docs/prd/05-API与核心数据模型.md` | API、Conversation/Task/Node 等对象模型 |
| NL2SQL-MVP设计 | `docs/prd/06-NL2SQL-MVP设计.md` | NL2SQL 路由、SQL Guard、Schema Context Builder、MVP 验收 |

## 5. 当前已定的关键决策摘要
### 5.1 主框架共性决策
- 当前只写后端主代理框架 PRD，不先展开功能 Agent 产品实现
- capability 是稳定能力契约；agent 是执行实体；tool 是底层操作接口
- 子代理执行采用混合模式：优先专用任务型 Agent，必要时允许受限 ReAct Worker
- 同一 `conversation_id` 内任务串行执行
- 主代理采用受规则、状态机与完成判定约束的编排型闭环，而不是自由试错式纯 ReAct
- 任务优先级采用“两层模型”：控制类动作独立最高优先级；普通任务按来源驱动排序，并允许少量结构化权重作为同类内排序依据
- 主框架与 capability 是明确上下级关系：主框架只管拆解、编排、分发；NL2SQL 等 capability 只管各自执行

### 5.2 协作与生命周期决策
- 结构化 mailbox 采用统一信封 + channel + typed payload 模型
- mailbox 生命周期采用 **分级 ACK**
- 强 ACK 用于控制类 / interrupt 类消息；轻 ACK 用于普通协作类消息
- 停止处理的正式语义是终止 task context，而不是直接定义为“杀线程”

### 5.3 状态存储决策
- 主框架状态不落公司业务 MySQL
- 本地先 SQLite，同构迁移到 PostgreSQL
- PostgreSQL 存结构化状态与索引，不直接存大对象正文
- PostgreSQL DDL 采用 ORM Model + migration 生成；一期索引策略为基础索引 + 少量关键增强索引

### 5.4 NL2SQL 决策
- NL2SQL 是一期首个 MVP 样例
- 只允许只读查询
- 当前 MySQL 执行账号为 `chatu:chatu123`，已确认只读
- 外部 LLM 迟到结果不回写主任务，仅审计可见

## 6. 相关配套文档
- 数据库结构说明：`docs/MySQL数据库表结构说明.md`
- NL2SQL prompt 输入模板：`docs/NL2SQL提示词输入模板.md`
- PRD 解耦方案：`docs/PRD解耦重构方案.md`

## 7. 使用建议
- 做全局规划时先读本文件
- 做局部设计或开发计划时优先读取对应专题文档
- 需要做 NL2SQL 实现或提示词设计时，配合 `docs/prd/06-NL2SQL-MVP设计.md` 与 `docs/NL2SQL提示词输入模板.md` 一起阅读

## 8. 后续专题设计与演进项
以下事项不阻碍当前 PRD 作为正式基线，但建议在后续专题设计中继续细化：
- PostgreSQL 最终 DDL 文件生成方式与索引增强细节
- PostgreSQL 部署完成后的索引优化、JSONB 查询策略与正式 DDL 生成流程
- 任务优先级权重的更细粒度策略
- Schema Context Builder 的更强评估样例与调优工具
