# MCP Dispatch v2 引用式恢复信封设计

## 状态

- 日期：2026-08-18
- 结论：通过复审，按本设计实施
- 适用范围：user-scoped MCP dispatch 的 SQL authority 恢复路径

## 问题

当前恢复信封复制了 `input_payload`、`dependency_outputs` 和 `metadata`。当
`metadata` 间接包含附件正文或 Base64 时，信封会超过 64 KiB，导致 intent 尚未建立
就失败；Task 随后可能失败，而正在执行的 Node 没有同步收敛。

恢复信封是控制面证据，不是业务数据容器。MCP 实际输入输出、Tool 参数和结果、附件
正文及认证信息必须继续由各自的持久化 authority 管理，恢复时按引用重新加载。

## v2 合同

新 writer 只生成 `maf.user_mcp.dispatch_resume.v2`：

```json
{
  "schema": "maf.user_mcp.dispatch_resume.v2",
  "capability_id": "mcp.dispatch",
  "conversation_id": "...",
  "task_id": "...",
  "root_message_id": "...",
  "node_id": "...",
  "server_id": "...",
  "task_assignment": {
    "mcp_execution_mode": "user_scoped",
    "mcp_shadow_enabled": false,
    "mcp_rollout_config_version": "...",
    "mcp_route_reason_code": "enforce_selected",
    "mcp_rollout_mode": "enforce"
  },
  "node_snapshot": {
    "capability_id": "mcp.dispatch",
    "criticality": "...",
    "dependency_type": "...",
    "input_refs": [],
    "resource_class": null,
    "retry_policy": {},
    "timeout_policy": {}
  },
  "edge_snapshot": [],
  "input_attachment_ids": [],
  "dependency_output_refs": []
}
```

顶层和固定结构对象采用 exact keys，未知字段拒绝。任何层级不得出现
`metadata`、`input_payload`、`dependency_outputs`、`uploaded_artifacts`、
`skill_artifacts`、`content_base64`、Tool arguments/results、Endpoint、credential 或
auth metadata。

Assignment 从当前 Task row 读取。除 rollout config version 外，必须精确满足
`user_scoped / false / enforce_selected / enforce`；builder 和 Repository 都不得以常量
覆盖不一致的持久化值。

约束如下：

- ID 数组排序且唯一；dependency 按 node ID 排序。
- Attachment 最多 20 个，Edge 最多 256 条，dependency 最多 64 个，Artifact ref
  总数最多 256。
- Server ID 最多 128 UTF-8 字节；其他 ID 和 config version 最多 512 字节。
- canonical JSON 最大 64 KiB；48 KiB 仅触发容量审查，不自动提高上限。

## 写入边界

`src/integrations/mcp/resume_envelope.py` 集中实现 builder、parser、validator 和限制
常量，并复用 CP7 canonical JSON/SHA。

Coordinator 只从已经持久化的 Task、Node、Edge、TaskInputAttachment，以及 dependency
Node 的 `output_refs` 生成信封。`arm_user_mcp_target_intent` 接口保持不变；Repository
在同一事务中区分 legacy v1 与 exact v2，并在写 intent 前验证 v2 closed schema、
Task/Node/Server 身份、assignment、canonical size 和 digest。

无顶层 `schema` 的历史信封继续由 legacy reader 处理；exact v2 走新 reader；其他
schema 拒绝。旧 intent 不迁移、不改写。

## 恢复重建

v2 恢复不读取实际 I/O：

```json
{"server_id": "intent.requested_server_id"}
```

Task assignment 来自当前 Task row并与快照比较；用户任务文字和 explicit binding 来自
root Message 的持久化 context；没有 persisted binding context 时按 automatic 处理；
Interrupt、Answer、Grant 和 sealed state 仍是 Tool approval/MRTR response 的 authority。

每个入边 dependency 必须在 `dependency_output_refs` 出现。每个引用 Artifact 必须存在、
属于当前 Task、由对应 dependency Node 产生、已经完成并含有非空 summary。恢复只构造：

```json
{
  "safe_summary": "按 artifact ID 排序合并并截断至 2000 字符",
  "artifact_refs": ["artifact-id"]
}
```

当所有 dependency 都有持久化引用时，初次执行也使用同一 projection。初次执行可以在
缺少 refs 时继续使用当前内存输出；跨进程恢复则以
`mcp_dispatch_resume_dependency_unrecoverable` 失败关闭，不丢上下文继续，也不重放
上游 Node。

附件仅以 TaskInputAttachment ID 进入信封。恢复时重新加载并要求集合一致、仍属于同一
Task/Conversation 且有效；正文、Base64 和 `skill_artifacts` 不进入信封或恢复 metadata。

## 状态与失败语义

恢复依次验证 digest、schema、Task/Node/Server/owner、Task assignment、Node/Edge/
Attachment 快照、dependency Artifact refs，以及 intent/outbox revision 和 Server
config/security version。

- `waiting_for_input` 且存在 open Interrupt 时保持等待，不 claim、不执行。
- Task 非 running、已取消或 Node 已终态时不得发起网络调用。
- malformed v2、digest/identity/snapshot 冲突和 unknown active schema 是 authority
  corruption，阻断 Ready。
- dependency refs 缺失或附件正常删除/失活，且没有 `may_have_dispatched` 证据时，使用
  现有 no-call finalize 收敛 intent、outbox、Node 和 Task。
- `may_have_dispatched=true` 时只接受 terminal candidate/receipt；没有可信终态则进入
  现有 unknown/no-replay，绝不重放。
- live builder/Repository 的预期校验失败转换为不可重试 capability error，由现有
  completion policy 同时收敛 Node 与 Task。

## 可观测性与验收

新增 audit-only 记录只允许 schema、canonical size、attachment/dependency/artifact ref
计数、accepted/rejected 和 reason code；不得记录业务 ID、SHA、文件名、summary、用户
文字或 payload。

验收覆盖：2.3 MB 图片不进入信封且 v2 小于 4 KiB；嵌套 forbidden key、assignment
损坏、Artifact refs 完整或缺失、explicit/automatic binding、open Interrupt、附件删除、
legacy v1、unknown/损坏 v2、`may_have_dispatched` no-replay，以及 live 校验失败时
Node/Task 终态一致。

## 非目标与部署约束

本轮不实现附件到 MCP Tool 的传输、OCR 参数桥接、生产部署、旧失败任务自动修复、
全局输入冻结、通用 Artifact 不可变改造、取消/Tool admission 生命周期重写，或 Sidecar
enforce 下的跨 authority 原子快照。

后续`2026-08-18-mcp-dispatch-aggregate-recovery-hardening-design.md`在不改变本文件v2信封
字段、64 KiB上限、legacy v1 reader和实际I/O禁入规则的前提下，专门取代上述“本轮不重写
取消/Tool admission生命周期”的范围限制，统一approval、普通多Call、MRTR、remote Task和
startup recovery；两份设计不存在两套信封合同。

回滚到旧版本前必须停止新提交，并证明不存在 `armed/available/dispatched` 的 v2 intent
及 `pending/claimed` outbox。
