# 四角色 LLM 消息合同设计

状态：待用户书面复核后实施

## 1. 决策

breeding_agent 的模型消息合同固定只支持以下四种角色：

- `system`
- `assistant`
- `user`
- `tool`

系统不再建模、生成、声明或向供应商发送 `developer` 角色。现有
`developer` 内容全部并入 `system`，即使未来某个模型原生支持
`developer`，本系统也不启用该能力。

`context` 可以继续作为 PromptEnvelope 内部内容分类，但它不是模型消息
角色；渲染后的供应商请求仍必须只包含上述四种角色。

## 2. 目标与行为锁

本次修改只修复消息角色合同与供应商接口不一致导致的 Agent 首次采样
HTTP 400。以下行为保持不变：

- system 规则仍具有最高约束力；
- 原 developer 承载的安全工具规则、可信事实、历史摘要和 Skill 上下文
  仍位于用户输入之前，并作为 system 内容发送；
- user、assistant 与 tool 的含义、顺序和关联 ID 不变；
- 模型选择、thinking、reasoning effort、工具目录、Task/AgentRun 生命周期、
  持久化 schema 与公开 API 均不改变。

成功标准：所有模型请求只能出现四种角色；当前五个模型均可完成最小
Agent 对话，不再因 `messages.role=developer` 返回 400。

## 3. 范围

### 3.1 生产代码与配置

- `AgentMessageRole` 和运行时校验移除 `developer`。
- `AgentContextBuilder` 将安全工具规则、上下文摘要、可信事实和 Skill
  激活/摘要直接构造成 `system` 消息。
- PromptEnvelope 的 developer 生产路径改为 system；删除不再需要的
  developer provider fallback 分支及其专用审计语义。
- LLM provider payload 在发送前维持四角色闭合校验，未知角色明确失败，
  不做请求级试错或静默降级。
- 当前五个模型的 `agent_capabilities.roles` 全部改为四角色集合。

### 3.2 测试与当前文档

- 更新 Agent context、模型能力门禁、LLM client/runtime、PromptEnvelope、
  API fixture 与 clean archive fixture。
- 新增回归，证明 Agent 请求不产生 developer，且原 developer 约束被
  system 保留。
- 更新当前有效的 PromptEnvelope/Agent Model 合同说明；历史设计稿只作为
  当时决策记录保留，不进行批量改写。

## 4. 不在范围

- 不增加模型能力探测脚本、启动期外部探测或请求级重试。
- 不按供应商或模型保留 developer 分支。
- 不修改 tool schema、消息正文、Prompt 内容策略或优先级。
- 不修改数据库、Sidecar/proto、Frontend、部署端口或生产环境。
- 不处理本次失败后出现的 `StopAsyncIteration` 清理告警；它不是任务失败
  根因，应作为独立问题评估。

## 5. 数据流

1. 稳定系统规则、安全工具规则、可信摘要和内部可信上下文统一进入
   `system` 消息。
2. 用户输入进入 `user`。
3. 模型输出与工具调用进入 `assistant`，工具结果进入 `tool`。
4. provider adapter 只序列化四角色消息；其他角色在发网前失败。

该流程没有模型特例，也没有外部能力探测结果参与业务请求。

## 6. 错误处理

- 配置声明包含四角色之外的值时，启动配置校验失败。
- 内部代码构造未知角色时，模型 DTO 校验失败。
- provider 返回的其他 BadRequest 继续沿现有安全失败路径处理，不在本次
  修改中新增猜测性兜底。

## 7. 验证

按以下顺序验证：

1. Agent model/context 与 PromptEnvelope 定向单元测试。
2. LLM client/runtime、model edition、API fixture 相关回归。
3. Integrations、Orchestration 与 API 相关完整测试。
4. 重建本地 backend，使用当前默认模型提交“请问你叫什么？”真实冒烟；
   Task 应完成且请求日志不再出现 developer role 400。
5. 最终 diff 检查确认无数据库、Sidecar、Frontend 或无关重构。

## 8. 回滚

本变更不含数据迁移。回滚只需恢复代码、配置、fixture 与文档提交；已有
Conversation、Message、Task 和 AgentRun 数据不需要转换。
