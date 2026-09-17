# 全仓业务代码渐进式架构清理 P2 实施计划

## 1. 状态与边界

- 日期：2026-08-25
- 分支：`main`
- 状态：`complete`
- P2 start commit：`92a8a605984f65264f6f5b192eb8b3108a7c93d7`
- P2 start tree：`d7760d620112c745303ac082a5c9ad978547f780`
- P2 start tracked set：1047

P2只处理`src/orchestration/**`与`src/capabilities/**`拥有的边界：采用P1窄persistence ports、消除已证实的Prompt文本复制，并复验Agent waiting/continuation/Interrupt/MCP functional seams。P2不修改P3 Integrations、P4 API、P5 Storage/Lifecycle adapter，不能改变Tool/LLM/Storage调用次数、顺序、错误、public import或prompt输出，也不进入P3～P8。

## 2. ai-slop-cleaner审计

| Finding | 分类 | 当前证据 | P2处置 |
|---|---|---|---|
| `P2-PORT-TASK-PROJECTION-001` | boundary violation | `AgentTaskInvocationCommitPort`依赖259-method aggregate，实际只用Task/Interrupt/Conversation/MCP Remote Task/Slot五域12方法 | 定义P2本地组合Protocol并收窄annotation |
| `P2-PORT-MEMORY-001` | boundary violation | `ConversationMemoryBuilder.storage`无annotation，实际只用Conversation/Message/Task/Artifact六方法 | 定义P2本地组合Protocol并收窄annotation |
| `P2-PORT-SKILL-ARTIFACT-001` | boundary violation | `SkillOutputArtifactManager`依赖aggregate，实际只用Task/Artifact三方法 | 定义P2本地组合Protocol并收窄annotation |
| `P2-PORT-VISIBLE-MESSAGE-001` | boundary violation | 可见Interrupt消息helper依赖aggregate，实际只用Message两方法 | 直接改用`MessageStoragePort` |
| `P2-PROMPT-GAP-DUP-001` | duplication | legacy prompt已有`_format_capability_gap_context`；Envelope builder逐字复制同一披露正文 | Envelope复用现有formatter并保持无前导换行的segment内容 |
| `P2-CONTINUATION-OWNER-001` | reviewed_no_change | locator cache只在`AgentTaskInvocationCommitPort`一处；restart/cache miss已有durable carrier重建 | 保持P0 logical IDs、cache/durable authority区别与所有时序 |
| `P2-INTERRUPT-SEAM-001` | reviewed_no_change | P2只有`bind_interrupt`/`persist_interrupt_authority`两个可选薄callback，生产装配不注入第二authority | 不发明未采用Protocol或P4 adapter；将统一边界交给P4 wiring计划 |
| `P2-COMPLEXITY-001` | reviewed_no_change | P2树有8个C901；均位于Invocation、memory/prompt或Skill/MCP executor高风险时序；AST三语句以上exact duplicate为0 | 不为降复杂度拆分；未来只有对应owner行为锁支持时处理 |
| `P2-P3-BOUNDED-IMPORTS-001` | grounded compatibility | Prompt/Skills/MCP capability需读取P3 public contracts，P3 Coordinator反向读取P2 MCP Dispatch contracts | import/call identity delta=0；不复制parser、Gateway或Coordinator状态 |

未发现P2可安全删除的dead code或masking fallback。Skill/MCP executor的错误/fallback策略跨P3 authority，本阶段不动。

## 3. Checkpoints

### Checkpoint A：计划与行为基线

复用P0/P1合同与以下focused suite：

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_agent_loop
conda run -n multi_agent python -m unittest tests.orchestration.test_agent_invocation
conda run -n multi_agent python -m unittest tests.orchestration.test_agent_continuation
conda run -n multi_agent python -m unittest tests.lifecycle.test_agent_run_recovery
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt
conda run -n multi_agent python -m unittest tests.orchestration.test_conversation_memory
```

提交：`docs(cleanup): plan P2 orchestration boundaries`

### Checkpoint B：采用P1窄Ports

只改四个P2 production consumers：

- `visible_message_history.py`：`MessageStoragePort`；
- `skill_output_artifacts.py`：本地`SkillOutputArtifactStoragePort(ArtifactStoragePort, TaskStoragePort, Protocol)`；
- `conversation_memory.py`：本地`ConversationMemoryStoragePort(ConversationStoragePort, MessageStoragePort, TaskStoragePort, ArtifactStoragePort, Protocol)`；
- `agent_loop/task_projection.py`：本地`AgentTaskProjectionStoragePort(TaskStoragePort, InterruptStoragePort, ConversationStoragePort, MCPRemoteTaskStoragePort, SlotStoragePort, Protocol)`，slot helper单独用`SlotStoragePort`。

新增一份直接contract test，证明组合Protocol的direct method为0、继承surface恰好等于所列P1窄域、四个文件不再import/use aggregate，且构造签名只发生annotation变化。不得修改调用、consumer装配、repository或Protocol method。

提交：`refactor(orchestration): adopt narrow persistence ports`

### Checkpoint C：复用Prompt capability-gap formatter

`prompt_envelope_builder.py`导入并调用现有`_format_capability_gap_context`，只去除formatter为legacy拼接保留的单个前导换行。新增行为断言：同一context的Envelope `capability_gap_disclosure` segment正文精确等于legacy formatter的无前导换行结果；公开builder签名、segment metadata、rendered string/messages与audit不变。

提交：`refactor(main-agent): reuse capability gap prompt formatter`

### Checkpoint D：全量门禁与终态handoff

Backend canonical逐域运行。Frontend/Rust/schema/data/依赖未触及时记为`N/A: production path not touched`。最终只允许上述5个P2生产文件变化；P3/P4/P5代码必须零diff。同步本计划、`docs/AGENTS.md`、对应源码`AGENTS.md`和`CHANGELOG.md`。

提交：`docs(cleanup): close P2 orchestration boundaries`

## 4. 必须保持的行为锁

- waiting链：lease→model/sample→TaskNode start→capability→waiting/Interrupt/event/outcome→release顺序与counts不变；
- multi-waiting只移除被回答call，remaining非空时model/Tool调用为0；
- atomic outcome后ack，response loss/restart沿同一durable identity；
- locator cache不是authority，cache miss从Interrupt/Agent result/remote binding重建；
- MCP Dispatch与Lifecycle recovery imports、functional call sites/kinds/counts/order相对P0 delta=0；
- Main Agent公开imports、函数签名、legacy/envelope输出与敏感字段过滤不变；
- 10项P0 deferred behavior只锁定、不修复。

## 5. 停止条件与回滚

每个checkpoint独立commit，逆序revert即可；无schema/data回滚。若需要修改P3/P4/P5实现、改变任何functional trace、创建第二Interrupt/continuation authority、复制P1 port methods、改变prompt正文或为降C901引入新collaborator，立即停止该改动并保持已独立验证的安全检查点。

## 6. 实施终态

### 6.1 Checkpoints

| Checkpoint | Commit | 结果 |
|---|---|---|
| Plan/audit | `fd6f19f` | 4个persistence boundary、1处Prompt复制、8个C901与continuation/Interrupt seam分类闭合 |
| Narrow port adoption | `cd0db43` | 4个P2 consumer采用Message或3个本地组合Protocol；调用代码零变化 |
| Prompt reuse | `2760b47` | Envelope复用legacy capability-gap formatter；正文exact-equal测试 |
| Final ledger | 本文终态提交 | Backend canonical、目录索引与P3/P4/P5 handoff闭合 |

### 6.2 Gate record

| Scope | ran/fail/skip | 结果 |
|---|---:|---|
| Python compileall `src scripts tests` | completed/0/0 | PASS |
| Core / Storage / Lifecycle | 48+410+42 / 0 / 7 | PASS；7项真实PostgreSQL profile沿用P0 N/A |
| Integrations / Agent Skills | 707+209 / 0 / 2 | PASS；2项Linux Result Parser gate在macOS N/A |
| Orchestration | 112/0/0 | PASS；waiting/continuation/Prompt及新增3项port合同包含在内 |
| Capabilities | 50/0/0 | PASS（Main Agent 17、MCP Dispatch 15、MCP Tool 15、Skill Tool 3） |
| API / E2E / Observability / Scripts / Deployment | 446+7+39+63+3 / 0 / 0 | PASS |
| Backend合计 | 2136/0/9 | PASS；9项均为未触及平台N/A，P2新增skip=0 |
| Changed Python compile/Ruff | completed/0/0 | PASS |
| P2 C901 audit | 8 signals | 与start相同；全部`reviewed_no_change`，不把复杂度当完成指标 |
| 全仓Ruff审计 | 162 C901 + 7 F401 + 3 F841 | 与P0/P1相同172信号；未运行`--fix` |
| Frontend / Rust | N/A | P2未触及对应生产或测试路径；不重复冒充新证据 |

### 6.3 Final invariants与handoff

- P2 final tracked set为1049，排序path SHA-256为`b45b16df7fc4a61e1952c5d44fd564a0300b62511c659aa5ea0c9efe1b6a78a3`；相对start只新增本计划与一份port adoption直接测试。
- `src/orchestration/**`与`src/capabilities/**`已无aggregate `StoragePort` import；4个consumer的组合surface分别精确等于实际使用的P1窄域，生产调用和装配零变化。
- capability-gap disclosure只有`prompt_builder._format_capability_gap_context`一个文本owner；legacy保留前导换行，Envelope segment去除该单个换行，正文exact-equal。
- continuation locator cache、waiting/resume、Interrupt callbacks、MCP Dispatch和Lifecycle recovery functional seams相对P0调用点/kind/count/order delta=0；没有第二authority。
- P3接管Integrations/Skills/MCP具体实现及其窄port consumer；P4接管API wiring、HTTP recovery和统一Interrupt adapter选择；P5接管Storage/Lifecycle adapter及其窄port consumer。P2不得代替这些计划改线。
- P2的8个C901、P0的10项deferred behavior、schema/data、依赖、`prod`、Frontend、Rust与`docker_cmd.md`正文均未触及。
