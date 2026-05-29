# Agent 基础设施大块重复整改设计

- 日期：2026-05-29
- 状态：整改设计稿；用于后续实施排期，不代表已经完成代码改造。
- 范围：后端系统代码、测试与文档；不改 `skill/**` 内的项目级 Skill bundle。
- 目标：减少大块复制粘贴，同时保持现有运行语义、失败边界、安全门禁与回归测试可验证。

## 1. 背景与目标

反屎山清理第一轮已经收敛了若干小型重复工具函数，但仓库里仍存在几类“大块重复”：

1. RuntimeSidecar 与 SkillSandbox 两套手写 gRPC/h2 + protobuf wire helper。
2. PostgreSQL bootstrap 与 schema reconciler 两套 SQL script splitter。
3. MCP Runtime 与 Skill Runtime 两套 revision bundle retain / release / evict 生命周期管理。
4. LLM planner 与 runtime replanner 两套 final answer 节点补齐逻辑。

整改目标不是为了“抽象而抽象”，而是把**稳定、低业务语义、重复程度高**的部分下沉到窄工具层；对差异明显的业务流程只抽组合对象或小 helper，避免继承层级膨胀。

## 2. 非目标

- 不引入新的 gRPC / HTTP2 / protobuf 依赖。
- 不移动、重写或格式化 `skill/**` 内的 Skill bundle。
- 不改变 RuntimeSidecar / SkillSandbox 的 endpoint 安全规则、artifact provenance 门禁、contract handshake 或 error code。
- 不把 MCP Runtime 与 Skill Runtime 强行合并为一个父类。
- 不改变当前 Prompt Envelope、Soft Skill binding、主代理 finalizer 的外部 API/SSE 契约。

## 3. 重复点与整改策略

| 优先级 | 重复块 | 当前证据 | 整改策略 | 风险 |
| --- | --- | --- | --- | --- |
| P0 | gRPC/h2 + protobuf wire helper | `src/storage/runtime_sidecar_grpc_client.py` 与 `src/integrations/agent_skills/skill_sandbox_client.py` 都有 `_frame`、`_read_frame`、`_recv_exact`、`_encode_headers`、`_field_*`、`_decode_message` 等重复实现。 | 新增共享窄模块，只承载 wire 级读写与 protobuf 基元；两个业务 client 保留 endpoint / handshake / response decode。 | 中 |
| P0 | PostgreSQL SQL splitter | `src/storage/postgres/bootstrap.py` 与 `src/state/postgres/schema_reconciler.py` 的 `_split_sql` 逻辑一致。 | 新增 `src/state/postgres/sql_script.py::split_sql_statements()`，两处复用。 | 低 |
| P1 | revision bundle 生命周期 | `MCPRuntimeState` 与 `SkillRuntimeState` 均维护 `_bundles`、`_active_revision`、`_retained_counts`、retain / release / evict。 | 新增组合式 `RevisionBundleStore[T]`，只负责 revision map 与 retain/release/evict；runtime 自身继续负责 refresh、client close 与 fingerprint/discovery。 | 中 |
| P2 | final answer 节点补齐 | `LLMWorkflowProvider` 与 `RuntimeReplanner` 都有 tail node、non-answering tail、rewire、append finalizer、unique id 逻辑。 | 抽 `ensure_final_answer_node()` 与 `unique_node_id()` 小 helper；保持调用方 metadata source 不变。 | 中低 |

## 4. 目标模块设计

### 4.1 `sidecar_wire`：共享 wire 基元

建议位置：`src/integrations/sidecar_wire.py`

职责：

- HTTP/2 frame 编码 / 解码：
  - `frame(frame_type, flags, stream_id, payload)`
  - `read_frame(sock, *, component_label)`
  - `recv_exact(sock, size, *, component_label)`
- unary gRPC response 读取：
  - `read_unary_grpc_response(sock, policy)`
- HPACK literal header 编码：
  - `encode_headers(headers)`
- protobuf wire 基元：
  - `field_varint`
  - `field_string`
  - `field_bytes`
  - `field_map_entry`
  - `decode_message`
  - `first_message`
  - `first_bytes`
  - `first_string`
  - `first_bool`
  - `first_int`
  - `first_int32`
  - `all_strings`

建议用显式 policy 承载两个 client 的差异：

```python
@dataclass(frozen=True, slots=True)
class GrpcUnaryReadPolicy:
    component_label: str
    max_response_bytes: int | None = None
    allow_empty_response: bool = False
    require_exact_message_end: bool = True
```

差异保留：

- SkillSandbox 当前要求：
  - 超过 `_MAX_GRPC_RESPONSE_BYTES` 必须失败。
  - 少于 5 字节必须失败。
  - payload 不完整必须失败。
  - trailing bytes 必须失败。
- RuntimeSidecar 当前更宽松：
  - 短响应返回 `b""`。
  - 没有显式响应大小上限。
  - 当前实现未校验 trailing bytes。

因此不能简单把 SkillSandbox 的严格读取语义套到 RuntimeSidecar；迁移时必须先用测试锁定两者差异，再通过 policy 显式表达。

### 4.2 `sql_script`：PostgreSQL script 分割

建议位置：`src/state/postgres/sql_script.py`

接口：

```python
def split_sql_statements(script: str) -> list[str]:
    ...
```

初始语义保持当前实现：

- 按行累积 statement。
- 支持 `DO $$` 到 `$$;` 的 block。
- 非 DO block 下以行尾 `;` 结束 statement。
- 保留 strip 后的 statement。

后续如要支持更复杂的 dollar quote tag、字符串内分号或注释解析，应另开专门 SQL parser PRD；本次只做等价迁移。

### 4.3 `RevisionBundleStore[T]`：组合式 revision 生命周期

建议位置：`src/integrations/runtime_revision_store.py`

职责只限于：

- 保存 `revision -> bundle`。
- 读取 active revision / active bundle。
- `bundle_for_revision()`。
- `activate(bundle)` 或 `activate_revision(revision)`。
- `retain_revision(revision)`。
- `release_revision(revision)`。
- evict 未保留的 inactive bundle。
- 输出 known revision / bundle 列表供 runtime 自己派生能力 ID。

不放进 store 的逻辑：

- Skill catalog fingerprint。
- MCP server discovery。
- MCP client lifecycle / close。
- refresh result dataclass。
- sidecar gate。
- diagnostic 语义。

这样能删除重复 retain/release/evict，但不会把 MCP 和 Skill 的 refresh 流程绑成一个抽象层。

### 4.4 final answer 节点 helper

建议位置：`src/orchestration/final_answer_nodes.py`

接口草案：

```python
def is_answer_producing_capability(capability_id: str) -> bool:
    return capability_id == "main_agent.respond" or capability_id.startswith("skill.")


def unique_node_id(preferred: str, existing: set[str]) -> str:
    ...


def ensure_final_answer_node(
    nodes: tuple[WorkflowNodePlan, ...],
    *,
    request: OrchestrationRequest,
    payload_policy: PlannerPayloadPolicy,
    final_node_id_base: str = "answer_user",
) -> tuple[tuple[WorkflowNodePlan, ...], bool, bool]:
    ...
```

`WorkflowExpander` 的 `task_id:global_final_answer` 命名与 metadata 规则不同，先只复用 `unique_node_id` 或保留原逻辑；不要为了统一命名破坏现有图节点 ID 契约。

## 5. 实施顺序

### Phase A：先锁行为

在改代码前补或确认以下回归：

- SkillSandbox h2c response 严格失败测试继续覆盖 oversized / truncated / short header / trailing bytes。
- RuntimeSidecar endpoint / mTLS / artifact provenance / binary happy path继续覆盖。
- PostgreSQL reconciler empty schema、additive column、forbidden SQL guard 继续覆盖。
- SkillRuntimeState 与 MCPRuntimeState retain/release/refresh 继续覆盖。
- LLM planner 与 runtime replanner finalizer added / rewired 继续覆盖。

### Phase B：低风险 SQL splitter

1. 新增 `src/state/postgres/sql_script.py`。
2. 迁移两个 `_split_sql` 调用。
3. 补一条 DO block splitter 单元测试。
4. 跑 storage 层相关回归。

### Phase C：sidecar wire 抽取

1. 新增 `src/integrations/sidecar_wire.py` 与单元测试。
2. 先迁移 SkillSandbox client，因为它已有更细的 malformed response 测试。
3. 再迁移 RuntimeSidecar client，并用 policy 保留当前宽松差异。
4. 跑 sidecar / sandbox / API runtime assembly 相关回归。

### Phase D：revision store 组合对象

1. 新增泛型 store。
2. 先让 SkillRuntimeState 使用 store；因为它同步、边界更小。
3. 再让 MCPRuntimeState 使用 store；保留 prepare / commit / discard activation 与 client close 逻辑在原类。
4. 跑 `tests/integrations/agent_skills/test_skill_runtime_state.py`、MCP runtime state 与 API reload 相关回归。

### Phase E：finalizer helper 小拆

1. 抽 answer-producing 判断与 unique node id helper。
2. 如差异足够小，再抽 finalizer append / rewire helper。
3. 跑 orchestration、main-agent runtime replanner 与 workflow expander 回归。

## 6. 验收命令

建议每个 phase 至少运行：

```bash
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_skill_sandbox_client
conda run -n multi_agent python -m unittest tests.integrations.test_runtime_sidecar_grpc_client
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/agent_skills -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/mcp -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
```

如果涉及 `native/`、Rust 依赖、`Cargo.lock` 或 license 策略，额外执行：

```bash
cd native && cargo deny check
```

## 7. 完成标准

- `skill/**` 无修改。
- wire / SQL / revision / finalizer 重复块有明确 owner module。
- RuntimeSidecar 与 SkillSandbox 行为差异由 policy 或测试显式记录，而不是隐式散落。
- 不新增第三方依赖。
- 相关测试全部通过；失败语义、error code、audit 脱敏与外部 API/SSE 契约不漂移。
