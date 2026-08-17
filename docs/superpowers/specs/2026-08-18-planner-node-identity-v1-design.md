# Planner Node Identity v1 设计

日期：2026-08-18

状态：设计与实施计划已批准；待书面 spec 复核后实施

适用范围：`main` 分支中由 LLM 初始 Planner 和 Main Agent Runtime Replanner 生成的工作流节点身份、依赖引用、持久化、恢复、API/SSE 关联和前端 artifact 分类。

不适用范围：`prod` 部署、历史节点 ID 回填、系统 provider 的既有节点身份重写、改变 Task/TaskNode 公共关联语义。

## 1. 问题与证据

`task_node.node_id` 是全局主键，但 LLM Planner 当前直接提供 `n1`、`answer_user` 等任务内名称。自动 finalizer 也固定使用 `answer_user`。当后续任务保存同名节点时，repository 只按 `node_id` 命中旧行，并因 `task_id` 或 `capability_id` 不同拒绝写入：

```text
task_node_identity_immutable: task_id and capability_id cannot be changed
```

已确认的代码证据：

- `src/orchestration/planner_contract.py` 把模型 `node_id` 直接转换为 `WorkflowNodePlan.node_id`。
- `src/orchestration/llm_workflow_provider.py` 在当前 plan 内生成固定本地键 `answer_user`。
- `src/capabilities/main_agent/runtime_replanner.py` 使用相同本地键策略生成完整修订 DAG。
- `src/storage/sqlite/models.py` 把 `task_node.node_id` 定义为全局主键。
- `src/storage/sqlite/repositories.py` 只按 `node_id` 查询并保护 TaskNode 身份不可变。
- `frontend/src/domain/artifacts.ts` 仍通过 `producer_node_id.includes(...)` 推断部分 artifact 语义。

故障与 MCP 登录名、Server ID 或 MCP Soft Binding 无关；执行在任何 MCP discovery/call 之前就已失败。

## 2. 目标

1. 模型只能提供规划内局部语义键，不能决定持久化主键。
2. Runtime 必须为每个模型节点生成全局唯一、确定性、可审计的 canonical `node_id`。
3. 初始规划、同一任务多轮 replan、同一次 replan 重试和多实例竞争必须保持正确身份语义。
4. `depends_on`、入口/终点引用、TaskNode、TaskEdge、artifact、interrupt 和事件必须使用同一 canonical ID。
5. 旧任务继续按原 ID 恢复；不回填、不重写历史节点。
6. API/SSE/前端把 `node_id` 当作不透明关联键，不再从字符串格式推断产品语义。

## 3. 非目标

- 不把 TaskNode 主键改成 `(task_id, node_id)` 复合键。
- 不允许模型提供数据库 ID、planning epoch 或 identity origin。
- 不重写 Skill、MCP、interrupt、file-selection 等系统 provider 已生成的受信节点 ID。
- 不为历史任务生成 planner key 或 identity metadata。
- 不修改 Task/TaskNode 对外关联字段名称。
- 不在本轮发布或部署 `prod`。

## 4. 方案比较与裁决

### 4.1 采用：Runtime canonical ID + 持久化 replan claim

模型提供局部 key，Runtime 使用任务 ID、规划 epoch 和 key 生成 canonical ID。每次模型 replan 通过 durable claim 获得递增 epoch；相同 decision digest 重试复用同一 epoch。

优点：保持现有全局主键和公共关联模型；可确定性重试；旧 ID 无需迁移；能显式区分初始规划和多轮 replan。

### 4.2 不采用：TaskNode 复合主键

把数据库主键改为 `(task_id, local_node_id)` 可以保留模型 ID，但会扩散到 TaskEdge、artifact、interrupt、mailbox、API/SSE、Rust Sidecar 和前端关联，迁移与回滚成本远高于本问题所需。

### 4.3 不采用：随机 UUID

随机 ID 能避免碰撞，但同一次规划或 replan 重试会生成不同 ID，难以做幂等恢复、审计和 crash recovery。

## 5. 身份模型

### 5.1 三类来源

| `identity_origin` | 来源 | 行为 |
|---|---|---|
| `model` | LLM 初始 Planner 或 Main Agent Runtime Replanner | 必须经过 v1 canonicalization |
| `system` | Skill/MCP/interrupt/file-selection 等确定性 provider | 保留既有受信 ID 合同 |
| `legacy` | 已持久化且没有 v1 metadata 的节点 | 原样读取；不得回填或改写 |

来源必须由受信调用路径或结构化 metadata 决定，禁止根据 `node_id` 前缀猜测。

### 5.2 Planner 局部键

- 初始 Planner 继续在模型 JSON 中使用 `node_id` 字段，但解析后其语义是 `planner_node_key`，不是持久化 ID。
- key 去除首尾空白后必须非空，UTF-8 长度不得超过 256 bytes，不得包含 Unicode control characters。
- 同一规划 epoch 内 key 必须唯一。
- 原始 key 只作为 hash 输入；日志和审计只允许记录清洗后的 `safe_key`。

### 5.3 Canonical ID

格式固定为：

```text
{task_id}:plan:v1:{epoch}:{safe_key}:{digest20}
```

规则：

- 初始 LLM 规划 epoch 固定为 `p0`。
- Runtime replan epoch 为 `r1`、`r2`……，由 durable claim 分配。
- `safe_key`：NFKC 规范化并小写；仅保留 ASCII `a-z0-9._-`；其他连续字符折叠为 `-`；去除首尾分隔符；空结果使用 `node`；最多 48 字符。
- `digest20`：对 canonical JSON `{version, task_id, epoch, planner_node_key}` 计算 SHA-256，取前 20 个十六进制字符。
- digest 是身份依据，`safe_key` 只用于诊断。
- 映射器必须检测一次转换内重复输出；发现碰撞时 fail closed，不自动加随机后缀。

`task_id` 继续使用系统生成的受信 Task ID。canonical ID 存储在 Text 字段，不增加模型可控路径或文件名。

### 5.4 Identity metadata

模型节点的 `WorkflowNodePlan.metadata` 必须包含 Runtime 生成的：

```json
{
  "identity_origin": "model",
  "identity_version": "v1",
  "planning_epoch": "p0",
  "planner_node_key": "answer_user"
}
```

其中 `planner_node_key` 保存的是安全、长度受限的诊断值。持久化审计事件记录 version、epoch、safe key 与 canonical ID，不记录未清洗原始 key。

## 6. 初始规划数据流

执行顺序固定为：

```text
parse model JSON
→ add/reconnect answer_user finalizer
→ canonicalize all model-authored nodes with p0
→ rewrite every depends_on/reference through the same map
→ public validator
→ macro expansion
→ internal validator
→ persist TaskNode/TaskEdge
```

要求：

- 自动生成的 `answer_user` 与模型节点属于同一 model identity domain。
- canonicalization 必须一次性建立完整 map，再重写节点与依赖；不得边遍历边猜测前缀。
- 未找到的 dependency、重复 key、非法 key 或 collision 必须在持久化前失败。
- macro provider 生成的内部节点属于 system origin；其 ID 继续从 canonical parent 派生或遵守 provider 自身合同。

## 7. Runtime Replanner 合同

Runtime Replanner 输出完整修订 DAG，但必须显式区分已有节点与新节点：

```json
{
  "action": "replan",
  "reason": "...",
  "nodes": [
    {"existing_node_id": "task-...:plan:v1:p0:..."},
    {
      "node_key": "retry_query",
      "capability_id": "...",
      "depends_on": [
        {"existing_node_id": "task-...:plan:v1:p0:..."},
        {"node_key": "other_new_node"}
      ],
      "input_payload": {}
    }
  ]
}
```

约束：

- `existing_node_id` 必须精确命中当前允许集合；模型不能改变其 capability、dependencies 或 identity metadata。
- `node_key` 只在本次 replan epoch 内命名新节点。
- existing/new 字段互斥；未知字段拒绝。
- 新节点依赖可以引用 allowlisted existing ID 或当前 epoch 的新 key。
- 被完整修订 DAG 移除的未终态旧节点继续按现有规则 orphan。
- deterministic Soft Skill Replanner 等非 LLM replanner 不进入该模型合同，保持 system origin。

## 8. Durable replan claim

新增 durable replan identity claim，至少包含：

- `task_id`
- `decision_digest`
- `planning_revision`
- `planning_epoch`
- `status`：`claimed | applied | rejected`
- `created_at`、`updated_at`

唯一性要求：

- `(task_id, decision_digest)` 唯一，保证同一决定重试复用 epoch。
- `(task_id, planning_revision)` 唯一，保证同一任务 epoch 不重复。

`decision_digest` 由 canonical replan JSON 计算，不包含模型解释性空白。claim 必须通过 StoragePort 的原子操作完成：

1. 已存在相同 digest：返回原 claim。
2. 不存在：在事务内分配 `max(revision)+1` 并插入；并发唯一冲突时重新读取/有界重试。
3. 节点和边全部成功持久化后标记 `applied`。
4. 验证失败标记 `rejected`；同 digest 不得换 epoch 绕过失败。
5. 进程在 `claimed` 后崩溃时，相同 decision retry 必须复用 claim 并幂等完成。

SQLite、PostgreSQL 和启用的 Rust Runtime Sidecar 权威路径必须提供等价语义；不得只修本地 SQLite。

## 9. 持久化与兼容

- `save_task_node` 继续执行现有全局 ID 和身份不可变校验。
- 新增的 planner identity guard 只适用于受信标记为 `model` 的新节点。
- v1 model 节点必须具有相互一致的 canonical ID、version、epoch 和 safe key。
- system 节点不要求 `:plan:v1:` 格式。
- legacy 节点没有 identity metadata 时原样读取；不得依据格式自动升级。
- 新旧节点可以存在于不同任务以及同一恢复图中，但新 model 节点只能使用 v1。
- 已失败历史任务保持失败状态；用户重试创建新 Task，不能原地篡改旧节点身份。

## 10. API、SSE 与前端

- `node_id` 继续作为不透明关联键，用于节点、边、artifact、interrupt 和事件匹配。
- 本轮不公开原始 planner key，不要求客户端解析 v1 格式。
- 前端 `isDataQueryDisplayArtifact` 使用现有安全 metadata（`domain_kind`、`artifact_family`、`artifact_role`）、artifact ID 和 preview role，不再读取 `producer_node_id` 子串。
- `isMainAgentTextArtifact` 使用稳定 artifact ID/role，不再读取 `producer_node_id` 子串。
- 现有 TaskNodeResponse、SSE node events、answer selection 和 interrupt matching 必须用完整 opaque ID 做精确匹配。

## 11. 错误处理与可观测性

下列情况必须在写入 TaskNode 前 fail closed：

- key 非法、重复或过长；
- dependency/ref 悬空；
- existing reference 不在 allowlist；
- existing/new 合同混用；
- canonical collision；
- claim 状态或 epoch 与 plan metadata 不一致。

审计事件使用闭合 reason code，并记录 task ID、identity version、epoch、safe key、canonical node ID、decision digest 的安全摘要。不得记录模型原始 key、完整 prompt 或未经清洗的 replan JSON。

上线观测至少包含：

- identity validation/rejection 数量；
- canonical collision 数量；
- replan claim retry/冲突/恢复数量；
- legacy/v1 节点比例；
- `task_node_identity_immutable` 数量。

## 12. 迁移、发布与回滚

1. 只对新增 claim 存储执行 additive migration；旧 TaskNode 不回填。
2. 先在 `main` 开发环境启用并运行完整回归与原故障 smoke。
3. 新代码必须同时读取 legacy 和 v1；写入模型节点只允许 v1。
4. 回滚前必须确认旧代码能把 v1 ID 当作普通字符串读取；若 claim schema 被旧代码忽略则可保留，不做 destructive rollback。
5. 回滚不得删除 v1 节点或 claim 数据。
6. `prod` 发布需要独立批准，不属于本实施。

## 13. 测试与验收

### 13.1 单元测试

- key validation、safe slug、canonical digest、dependency rewriting、collision detection。
- 相同输入/epoch确定性；不同 task、epoch 或 key 得到不同 ID。
- initial `answer_user` 和 model-provided key 一起转换。
- Runtime Replanner existing/new closed contract。

### 13.2 存储与并发测试

- 相同 decision digest 重试返回相同 claim/epoch。
- 不同 digest 并发获得不同递增 revision。
- claim 后崩溃并重试可恢复。
- SQLite、PostgreSQL 和 Rust Sidecar contract 等价。
- legacy/v1 混合读取和系统节点不受 model guard 误伤。

### 13.3 集成与前端测试

- 同一 `n1 + answer_user` plan 在两个不同任务中顺序执行成功。
- 上述形状并发执行成功，不再出现 `task_node_identity_immutable`。
- 同一任务初始 plan 与多轮 replan 重复使用 `n1`、`answer_user` 时身份不冲突。
- 同一次 replan 重试不创建第二组节点。
- orphan、interrupt、cancel、artifact、answer selection、SSE 图更新使用正确 canonical ID。
- 前端 artifact 分类在 opaque v1 ID 下行为与原来一致。

### 13.4 质量门禁

先运行 planner/replanner/storage/lifecycle/API/frontend targeted tests，再运行相关后端分层回归、前端测试/类型检查和仓库质量门禁。最终复核 diff、`AGENTS.md`、`CHANGELOG.md` 和无新增依赖/许可变化。

## 14. 完成标准

只有以下条件全部满足，实施才算完成：

1. 模型局部键不再直接进入 TaskNode 主键。
2. 初始规划和 LLM replan 都使用 v1 identity map。
3. replan epoch 在所有权威存储路径上可恢复且并发安全。
4. system/legacy 节点兼容测试通过。
5. 前端不再从 `producer_node_id` 推断语义。
6. 原故障形状顺序、并发、replan 和重启 smoke 通过。
7. 相关自动测试、类型检查和质量门禁通过。
8. 没有修改或部署 `prod`，没有读取或改动 `docker_cmd.md`。

## 15. 风险与已记录假设

- **风险：存储范围扩大。** Durable claim 必须覆盖 SQLite、PostgreSQL 和 Rust Sidecar，实施量高于仅修 Planner；这是多实例幂等语义的必要成本。
- **风险：replan prompt 合同变化。** 旧测试 fixture 和模型输出需要同步更新；非法旧格式必须 fail closed，而不是猜测 existing/new。
- **风险：ID 变长。** 当前相关字段为 Text；实施仍需检查日志、UI 和第三方导出是否存在隐藏长度限制。
- **假设：普通旧代码把 node ID 当作字符串。** 实施时必须通过兼容测试验证，不能只依赖静态观察。
- **假设：无需展示 planner key。** planner key 仅用于受控审计；若未来需要公开展示，应新增显式 DTO 字段，而不是解析 canonical ID。
