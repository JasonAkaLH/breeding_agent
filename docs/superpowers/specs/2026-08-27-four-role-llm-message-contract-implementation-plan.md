# 四角色 LLM 消息合同实施计划

依据：`2026-08-27-four-role-llm-message-contract-design.md`（`100/100 Pass`）

状态：`complete`

## 1. 范围与完成声明

唯一目标是让所有模型消息合同固定为`system/assistant/user/tool`，并把现有
developer生产点逐条一对一改为system。不得加入模型探测、请求重试、
`StopAsyncIteration`修复、数据库/Sidecar/Frontend或其他重构。

完成必须同时满足：生产源码不再生成/接受developer模型角色；五个模型配置
只声明四角色；定向与相关完整回归通过；重建后的默认模型真实Agent Task完成。

## 2. Checkpoint

### A. 基线与红测

- 保留既有66项Agent context、PromptEnvelope、LLM client/model gate/adapter
  通过基线。
- 先新增四角色DTO、配置extra-role拒绝、Agent Context一对一替换、
  PromptEnvelope active note、直接LLM消息和五模型payload矩阵红测。

验证：新增断言在旧实现上因developer存在而失败，既有无关断言不变。

### B. Agent与配置合同

- `src/orchestration/agent_loop/models.py`：AgentMessageRole及运行时allowlist
  固定为四角色。
- `src/orchestration/agent_loop/context.py`：四个developer生产点逐条改为system。
- `src/integrations/model_editions.py`：Agent roles要求恰为四角色，缺少或额外
  角色均进入既有gate拒绝原因。
- `config.yaml`及受影响fixture：五个模型只声明四角色。

验证：Agent model/context、model edition/gate与相关API配置测试。

### C. PromptEnvelope与LLM边界

- `src/orchestration/prompt_envelope.py`：active note直接映射system；
  PromptSegment角色闭合为四角色加内部context；删除developer fallback。
- `src/integrations/llm_client.py`：LLMMessage/Mapping输入只允许四角色，删除
  developer直通和fallback；保留既有tool/context等非developer兼容行为。
- `src/integrations/openai_agent_model_adapter.py`：在现有payload构造边界复验
  四角色，不新增抽象层或I/O。

验证：PromptEnvelope、LLM client/runtime与Agent adapter测试，断言消息数量、
顺序、正文和name不变。

### D. 恢复、fixture与当前合同文档

- 用既有AgentItem fixture证明active/waiting上下文按新规则重建，终态不变。
- 更新API support、clean archive和仅与模型role声明相关的fixture。
- 更新当前PromptEnvelope/Agent Model合同的角色描述；历史设计稿不批量改写。
- 同步`docs/AGENTS.md`和`CHANGELOG.md`。

验证：静态扫描生产源码/model config无developer模型角色；`developer-docs`
路由名及历史文档引用不计入模型角色扫描。

### E. 完整验证与真实冒烟

依次运行：

1. 定向Agent/Prompt/LLM/model/API测试。
2. `tests/integrations`、`tests/orchestration`、`tests/api`完整回归。
3. compileall、Ruff和`git diff --check`。
4. 重建并重启当前本地backend。
5. 通过公开API重新提交“请问你叫什么？”，确认新Task完成、生成assistant
   消息，且日志没有developer role 400。

## 3. 回滚

本计划不迁移数据。代码检查点可整体回滚；旧终态Task不复活，active/waiting
Run仍由当前代码从既有AgentItem重建。

## 4. 实施结果

- AgentMessage、LLMMessage、PromptSegment、模型能力配置和provider payload
  已闭合为四角色；Agent Context四个developer生产点均逐条改为system。
- 当前五个本地模型配置和所有受影响fixture只声明四角色；developer仅保留
  在明确的拒绝测试、历史文档和非模型角色的`developer-docs`路由名中。
- 定向84项、受影响API 50项、Orchestration 125项、Integrations 734项
  （2项既有平台skip）及API 590项通过；compileall、Ruff（本次修改文件）和
  diff-check通过。
- 本地backend已重建；真实默认模型Task`task-de61bb288177`完成并生成唯一
  complete assistant消息，日志没有developer role 400。
- 未修改数据库、Sidecar/proto、Frontend或部署合同，未新增依赖。
