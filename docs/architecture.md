# Architecture Notes

## 1. Why this scaffold exists

这个项目不是为了做一个“可试玩”的 CLI Agent Demo，而是为了逐步演进成一个可在云端服务中稳定运行的业务级 Multi-Agent 框架。

因此第一阶段先固定这些边界：

- 服务入口
- 配置体系
- Agent 协议
- 注册与发现
- 编排入口
- 后续高性能扩展位置

## 2. Layered design

### API layer

职责：
- 接收外部请求
- 参数校验
- 返回标准响应
- 暴露 health / meta / execution 接口

当前实现：`src/multi_agent_framework/api/`

### Orchestration layer

职责：
- 根据请求选择 Agent
- 控制调用顺序
- 未来扩展为 DAG、状态机、并发调度、重试和超时治理

当前实现：`src/multi_agent_framework/orchestration/`

### Agent layer

职责：
- 约束每个 Agent 的统一输入输出
- 把具体业务推理逻辑和编排逻辑隔离
- 支持后续加入 planner / router / worker / evaluator 等不同 Agent 类型

当前实现：`src/multi_agent_framework/core/` + `src/multi_agent_framework/agents/`

### Infrastructure layer

职责：
- 配置
- 日志
- 未来的缓存、数据库、消息队列、追踪、指标

当前实现：`src/multi_agent_framework/config.py` + `src/multi_agent_framework/infra/`

### Native extension layer

职责：
- 为性能敏感模块预留 C++ 下沉路径
- 只让高热点逻辑原生化，不污染主控制面

当前目录：`native/`

## 3. What is intentionally not included yet

当前没有引入这些内容，是刻意为之：

- 没有引入 LangChain / LangGraph / AutoGen
- 没有引入复杂状态机框架
- 没有引入数据库层
- 没有引入分布式任务队列
- 没有引入模型厂商 SDK 适配器
- 没有引入记忆 / 向量库 / RAG

第一步先把“框架边界”立住，再逐个补能力。

## 4. Recommended near-term milestones

### Milestone A: model adapter

新增 `adapters/models/`：
- 定义统一 LLMClient 协议
- 对接 OpenAI / 内部推理网关
- 支持超时、重试、熔断、限流

### Milestone B: tool runtime

新增 `tools/`：
- 工具注册
- 工具权限边界
- 工具执行回包协议
- 超时与审计日志

### Milestone C: session + memory

新增 `memory/`：
- 会话上下文
- 短期记忆
- 长期记忆
- 存储后端抽象

### Milestone D: workflow engine

新增 `workflow/`：
- 顺序编排
- 条件分支
- 并行节点
- 审批节点
- 补偿与重试

### Milestone E: observability

新增：
- trace id / span id
- 指标埋点
- 关键决策日志
- Agent 执行审计

## 5. C++ usage guideline

只有在以下条件同时成立时，再考虑把逻辑下沉到 C++：

1. 已有 Python 版本可稳定运行
2. 已完成 profiling，确认瓶颈真实存在
3. 瓶颈位于高频、高耗时、可隔离的纯计算模块
4. Python/C++ 边界清晰，数据结构稳定

优先下沉候选：
- 高性能检索 / 路由评分
- 图计算 / 路径规划
- 大规模规则匹配
- 批量向量后处理

不建议优先下沉：
- 业务编排层
- 配置层
- Web API 层
- 与模型供应商交互的 I/O 层
