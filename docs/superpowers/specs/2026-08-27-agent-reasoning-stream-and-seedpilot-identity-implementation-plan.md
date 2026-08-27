# Agent Reasoning 实时展示与 SeedPilot 身份恢复实施计划

依据：`2026-08-27-agent-reasoning-stream-and-seedpilot-identity-design.md`

设计提交：`361430d`；两轮 perfectization 提交：`2cd4b02`

状态：`checkpoint_d_validation`
目标分支：`main`

## 1. 完成声明

唯一目标是在现有统一 Agent Tool Calling 链路中补齐 Provider `reasoning_content` 的真正逐段 transient SSE
展示，并恢复 SeedPilot 用户身份。完成时必须同时满足：

- thinking enabled 时 reasoning 按 Provider 顺序进入 ReasoningBox；
- 失败 attempt 通过最小 `agent.reasoning_reset` 清除当前 sample 内容；
- reasoning 不进入 Message、AgentItem、Conversation Memory、durable EventRecord、artifact、audit 或最终回答；
- Runner/Frontend 双 524,288 UTF-8 byte 上限、唯一截断状态与 sink fail-open 生效；
- SeedPilot 是唯一用户可见身份，“统一 Agent”只作为内部机制名；
- 同 conversation 历史与新 conversation 隔离语义不变；
- 本地真实 DeepSeek Task、身份问答、历史无 reasoning 和完整回归闭合。

## 2. 范围约束

- 不新增依赖、数据库 schema、Rust/proto、持久化 reasoning、跨 conversation memory 或 `prod` 部署。
- 不读取、输出、暂存或提交 `docker_cmd.md`、`config.yaml`、master-key 路径、Provider request ID 或 reasoning 原文证据。
- 不改写现有 Tool Call、usage、finish reason、protocol retry、最终回答和历史 API 合同。
- 只创建新的本地测试 conversation/Task，不复活或重放旧终态 Task。

## 3. Checkpoint A：模型请求与 Provider Adapter

### 3.1 Contract

修改 `src/orchestration/agent_loop/models.py`：

- 为 `AgentModelRequest` 增加可选、`compare=False/repr=False` 的 `reasoning_reset_sink`；
- 保持既有 `reasoning_delta_sink: Callable[[str], Awaitable[None]]` 不变；
- reset sink 无 payload，由 Runner 闭包绑定 logical sample ID。

### 3.2 Adapter

修改 `src/integrations/openai_agent_model_adapter.py`：

- stream chunk 同时读取 `reasoning_content`、`content`、`tool_calls`；
- thinking enabled 且 delta sink 可用时立即发布非空/非纯空白 reasoning；
- sink 首次失败后仅关闭当前 model sample 的 reasoning delivery，记录无正文安全诊断，答案/Tool Call 继续；
- 每个 attempt 记录是否成功发布过 reasoning；protocol violation、cancellation 或 stream exception 时 best-effort reset；
- reset 失败不覆盖原异常、不改变 retry/终态；
- non-stream 先形成合法 `AgentSample`，再单次发布有界 reasoning；
- reasoning 永不进入 visible text 或 tool buffer。

### 3.3 Tests

扩展 `tests/integrations/test_agent_model_adapter.py`：

- 多 reasoning chunk 顺序、同 chunk reasoning+answer+tool call；
- thinking off、无 sink、纯空白、无 Provider reasoning；
- protocol retry/cancel/incomplete/transport reset 与无意义 reset 拒绝；
- delta/reset sink fault fail-open；
- non-stream 合法后单次 reasoning、非法 sample 零 reasoning。

门禁：

```bash
conda run -n multi_agent python -m unittest tests.integrations.test_agent_model_adapter tests.integrations.test_llm_client tests.integrations.test_llm_runtime
conda run -n multi_agent ruff check src/orchestration/agent_loop/models.py src/integrations/openai_agent_model_adapter.py tests/integrations/test_agent_model_adapter.py
```

Checkpoint commit：`feat(agent): stream transient reasoning from provider`

## 4. Checkpoint B：Runner、SSE 与 SeedPilot Prompt

### 4.1 Runner byte budget 与 reset

修改 `src/orchestration/agent_loop/runner.py`：

- 每次 `run_claimed()` 建立 524,288-byte reasoning budget，覆盖该次执行内全部 model samples/attempts；
- 使用 UTF-8 安全截断 helper，为固定提示“思考内容过长，已截断”预留空间；
- 只发布一次截断提示，之后继续 sample 但停止 reasoning fanout；
- 失败 attempt 消耗不返还；reset 不清除 Runner truncation state；
- logical sample ID 绑定 run ID/revision；delta ordinal 跨 retry 单调；reset event ID 使用独立单调 reset ordinal；
- Runner 同时向 request 注入 delta/reset sink。

### 4.2 Transient event contract

修改 `src/api/agent_projection.py`、`src/api/sse.py`、`src/api/runtime.py`：

- 新增 closed `agent.reasoning_reset`，payload 精确 `{sample_id}`；
- reset/delta 只允许 `EventVisibility.FRONTEND` 与 transient broker；
- durable projector 明确拒绝 reset/delta；
- runtime 发布唯一、可去重 event ID，不写 storage/audit。

### 4.3 SeedPilot identity

修改 `src/api/runtime.py`：

- 复用 `MAIN_AGENT_SYSTEM_CONTRACT_LINES` 生成 stable rules；
- 追加 catalog Tool 工作规则，但不复制产品身份；
- 保留 safe tool rules 与 final guard；
- 生产路径删除“你是统一同模型Agent”身份文本。

### 4.4 Tests

- `tests/orchestration/test_agent_loop.py`：byte boundary、跨 sample budget、ordinal/reset、sink fault；
- `tests/api/test_agent_task_projection.py`、SSE/streaming tests：reset transient、durable rejection、audit bypass；
- runtime/prompt tests：SeedPilot identity、Tool rules、旧身份零引用；
- Conversation Memory 回归锁定同会话/跨会话边界。

门禁：

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_loop \
  tests.api.test_agent_task_projection \
  tests.api.test_streaming_write_after_completion \
  tests.orchestration.test_conversation_memory
conda run -n multi_agent ruff check \
  src/orchestration/agent_loop/runner.py src/api/agent_projection.py src/api/sse.py src/api/runtime.py \
  tests/orchestration/test_agent_loop.py tests/api/test_agent_task_projection.py
```

Checkpoint commit：`feat(agent): reset bounded transient reasoning`

## 5. Checkpoint C：Frontend

修改 `frontend/src/domain/taskEvents.ts`：

- closed event allowlist 接受 `agent.reasoning_reset` 精确 `{sample_id}`；
- Task state 增加当前 answer sample ID、sample 起点与独立 `reasoningTruncated`；
- 新 sample 首个 delta 记录字符串边界；匹配 reset 回退当前 sample，重复/陈旧 reset 幂等忽略；
- 用 `TextEncoder`/安全 UTF-8 截断实现 524,288-byte 防御上限；
- 固定截断提示独立于 sample 文本，reset 后仍保留且只显示一次；
- thinking off/history 继续不显示或恢复 reasoning。

仅在需要展示独立截断状态时最小修改 `frontend/src/App.tsx`；不重做 ReasoningBox UI。

测试：

- `frontend/src/domain/taskEvents.test.ts`：live delta、sample switch、reset、stale/duplicate reset、UTF-8 三边界、
  failed attempt truncation + reset + zero remaining budget；
- `frontend/src/api/taskEvents.test.ts`：reset closed schema；
- `frontend/src/App.test.tsx`：ReasoningBox、完成后折叠、历史不恢复、SeedPilot label/a11y。

门禁：

```bash
cd frontend
npm test -- --run src/domain/taskEvents.test.ts src/api/taskEvents.test.ts src/App.test.tsx
npm run typecheck
npm run build
```

Checkpoint commit：`feat(frontend): reset bounded reasoning display`

## 6. Checkpoint D：文档与完整门禁

同步：

- `docs/api/api-doc.html` 与 `docs/api/API更新日志.md` 的 transient Agent event 合同；
- 设计/实施计划状态、`docs/AGENTS.md`、`CHANGELOG.md`；
- `tests/api/test_developer_docs.py`。

完整验证：

```bash
conda run -n multi_agent python -m compileall -q src tests scripts
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

对所有修改 Python 文件运行精确 Ruff；运行 `git diff --check` 与旧身份/持久化 reasoning leak scan。既有平台 skip
或环境缺口必须记录精确测试名和原因。

Checkpoint commit：`docs: document transient reasoning reset contract`

## 7. Final Gate：本地真实验收

1. 用当前本地配置重建 backend/frontend；保留 sidecar/数据卷，不输出敏感挂载路径。
2. backend `/api-doc`、frontend `/seedpilot/`、认证模型配置 API healthy。
3. 强制刷新浏览器并确认新 hashed asset。
4. 新建 thinking-enabled DeepSeek conversation：页面在最终答案前逐段显示非空 reasoning，Task 完成。
5. 新建身份 conversation：回答“育种助手”或“SeedPilot”，不出现“统一同模型Agent”。
6. 只读检查 SQLite/history：最终答案存在，reasoning/reset 不存在于 Message、AgentItem、EventRecord、memory/audit。
7. reset/截断主要由 deterministic 自动化验收；不得为触发 Provider 异常而篡改真实请求或重放旧 Task。
8. 仓库干净，`config.yaml`、`docker_cmd.md` 仍 ignored/untracked；`prod` 未更新。

## 8. 最终闭合

Final Gate 全部通过后：

- 设计状态改为 `implemented`，计划改为 `complete`；
- 写入实际测试数量、skip、真实 smoke 和本地 Skill mount 缺口；
- 更新索引与 CHANGELOG；
- 最终提交：`docs: close reasoning stream and SeedPilot identity rollout`。

任何 Final Gate 失败或未运行时不得标记 complete。
