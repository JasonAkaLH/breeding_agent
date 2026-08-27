# 四角色 LLM 消息合同设计

状态：用户已批准；第二轮信心审查`100/100 Pass`；代码与本地真实冒烟已完成

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

受影响对象仅包括使用 Agent 对话的用户、维护模型配置的开发者，以及
Agent Context、PromptEnvelope、LLM Client/provider adapter 四个现有模块。
不新增用户流程或运维角色。

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
Agent 请求构造，不再因 `messages.role=developer` 返回 400；默认模型完成
一次真实 Agent 对话冒烟。

## 3. 范围

### 3.1 生产代码与配置

- `AgentMessageRole` 和运行时校验移除 `developer`。
- `LLMMessage`、直接 Mapping 消息输入和 provider payload 使用同一四角色
  allowlist；四角色之外的消息在发网前明确失败。
- `AgentContextBuilder` 将安全工具规则、上下文摘要、可信事实和 Skill
  激活/摘要直接构造成 `system` 消息。
- PromptEnvelope 的 active note 直接构造成 `system`；`PromptSegment`只允许
  四个模型角色和内部`context`分类，不再接受或生成 developer。删除不再
  需要的 developer provider fallback 分支及其专用审计语义。
- LLM provider payload 在发送前维持四角色闭合校验，未知角色明确失败，
  不做请求级试错或静默降级。
- 当前五个模型的 `agent_capabilities.roles` 全部改为四角色集合；配置解析
  拒绝包含 developer 或其他额外角色的 Agent role 声明，不因未来模型能力
  变化而放宽。

### 3.2 精确迁移语义

每条现有 developer 消息执行一对一角色替换：

- `role`从 developer 改为 system；
- 消息数量、相对顺序、正文、name、工具关联和其他字段保持不变；
- 不与相邻 system 消息拼接；
- 不添加`role_fallback:developer`包装文本或新的提示词；
- 原 developer 消息仍位于原顺序位置，因此安全工具规则、可信事实、历史
  摘要和 Skill 上下文不会降级到 user。

该规则同时适用于 Agent Context 与 PromptEnvelope/LLM Client 的现有静态
生产点。切换后传入新的 developer 配置或 DTO 是合同错误，不再进行运行时
兼容转换。

### 3.3 测试与当前文档

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

## 6. 持久化与恢复

Agent message role 不持久化在 Conversation、Message、Task、AgentRun 或
AgentItem 中；恢复时由 Agent Context 根据持久化 Item kind 重新构造。因此：

- active/waiting AgentRun 在恢复时直接按四角色规则重建上下文；
- completed、failed 和 cancelled Task 保持已有终态；
- 已失败的`task-8a90f5f40143`不自动重试，用户重新提交才产生新Task；
- 不执行数据库、Sidecar或历史消息数据迁移。

## 7. 错误处理

- 配置声明包含四角色之外的值时，启动配置校验失败。
- 内部代码构造未知角色时，模型 DTO 校验失败。
- provider 返回的其他 BadRequest 继续沿现有安全失败路径处理，不在本次
  修改中新增猜测性兜底。

## 8. 非功能要求

- 安全：原 developer 内容只可提升/保持为 system，不得降级到 user。
- 兼容：provider-visible role 集合严格等于四角色 allowlist；内部 context
  必须在发网前映射为四角色之一。
- 性能：不得新增外部请求、启动探测、请求级探测或网络重试；角色转换只做
  本地确定性构造，不引入新的 I/O。
- 可观察性：删除 developer 专用 fallback audit 后，不伪造替代事件；未知
  角色继续以现有配置/DTO错误暴露。

## 9. 验收与验证

| 要求 | 验收证据 |
| --- | --- |
| Agent DTO只有四角色 | `AgentMessage`逐角色正例及developer/unknown反例 |
| Agent Context一对一迁移 | 覆盖safe tool rules、summary、trusted facts、Skill activation，断言角色、数量、顺序和正文 |
| PromptEnvelope不产生developer | active note、context、tool result与provider role capability矩阵测试 |
| 直接LLM消息边界闭合 | `LLMMessage`和Mapping输入四角色正例、developer/unknown反例 |
| 配置闭合 | 五个模型配置均为四角色；缺少必需角色和包含额外角色均拒绝 |
| provider payload闭合 | fake completions遍历五个model edition，断言发网payload角色集合是四角色子集且无developer |
| 历史恢复保持 | active/waiting Run从既有AgentItem重建为四角色；终态Task不变化 |
| 真实故障闭合 | 默认模型重新提交“请问你叫什么？”，新Task完成且无developer role 400 |

按以下顺序验证：

1. Agent model/context 与 PromptEnvelope 定向单元测试。
2. LLM client/runtime、model edition、API fixture 相关回归。
3. Integrations、Orchestration 与 API 相关完整测试。
4. 重建本地 backend，使用当前默认模型提交“请问你叫什么？”真实冒烟；
   Task 应完成且请求日志不再出现 developer role 400。
5. 最终 diff 检查确认无数据库、Sidecar、Frontend 或无关重构。

## 10. 实施依赖与追踪

| 合同 | 现有owner/主要验证入口 |
| --- | --- |
| Agent消息与上下文 | `src/orchestration/agent_loop/models.py`、`context.py`、`tests/orchestration/test_agent_context_builder.py` |
| PromptEnvelope角色映射 | `src/orchestration/prompt_envelope.py`、`tests/orchestration/test_prompt_envelope.py` |
| 直接LLM消息/provider边界 | `src/integrations/llm_client.py`、`openai_agent_model_adapter.py`、`tests/integrations/test_llm_client.py`、`test_agent_model_adapter.py` |
| 模型能力配置 | `src/integrations/model_editions.py`、`agent_model_gate.py`、`config.yaml`及相关fixture/API测试 |

实现不得引入新依赖或新抽象层；优先复用现有DTO校验和消息构造边界。

## 11. 发布、风险与回滚

本地验证必须重建backend镜像并重启服务后再执行真实冒烟；仅重启旧镜像不
构成验证。主要风险是角色替换时改变消息顺序/正文，或遗漏非Agent的直接
LLM消息路径；上述一对一断言和provider payload矩阵为对应门禁。

本变更不含数据迁移。回滚只需恢复代码、配置、fixture 与文档提交；已有
Conversation、Message、Task 和 AgentRun 数据不需要转换。
