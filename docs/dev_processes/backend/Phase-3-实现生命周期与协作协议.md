# Phase 3：实现生命周期与协作协议

> 状态：已完成（2026-04-23）

## 目标
优先把运行时最难返工的正确性问题做实：mailbox、interrupt/resume、cancel、conversation 串行约束，以及任务上下文终止语义。

## 推荐 Owner
- 主 Owner：运行时负责人 / 生命周期负责人
- 协作 Owner：存储负责人、编排负责人、API 负责人

## 输入
- `docs/dev_processes/backend/Phase-2-落地SQLite状态存储与仓储抽象.md`
- `docs/prd/backend/03-协作协议与任务生命周期.md`
- `docs/prd/backend/04-状态存储与迁移策略.md`

## 输出
- `src/lifecycle/` 目录
- mailbox service
- interrupt service
- cancellation service
- conversation serial guard
- 一组生命周期核心测试

## 要做什么
- 落地 mailbox channel 和 typed payload 基础设施。
- 实现强 ACK / 轻 ACK 状态流转。
- 实现 TTL、有限重试、过期与补偿策略。
- 实现 interrupt / resume：
  - `waiting_for_input`
  - `ready_to_resume`
  - `resuming`
- 实现 Task Context Termination：
  - 停止新节点调度
  - 标记协作链路结束
  - 忽略迟到结果回写
- 实现同一 `conversation_id` 的串行保护。

## 不做什么
- 不写具体 capability 业务逻辑。
- 不在本阶段实现最终 API 返回形状。
- 不把 cancel 扩展成二期统一跨 capability 语义。

## 依赖
### 前置依赖
- Phase 2 的状态持久化能力可用。

### 外部依赖
- 无新增基础设施依赖，本阶段主要依赖 storage 与 core。

### 下游依赖
- Phase 4 的编排调度必须建立在本阶段状态机和取消语义之上。
- Phase 6 的 API/cancel/SSE 行为依赖本阶段运行规则。

## 边界条件
### 进入条件
- mailbox、interrupt、task/node 状态已可持久化。

### 退出条件
- 强 ACK / 轻 ACK 差异可被测试证明。
- interrupt answer 能驱动节点恢复。
- cancel 能真正阻断当前 task context，而不是只停输出。
- conversation 串行约束已经形成统一规则。

## 风险
- **状态回写竞争风险**：取消后迟到结果仍被写回 completed。
- **ACK 幻觉风险**：把 ACK 当业务完成，导致状态失真。
- **恢复语义回归风险**：interrupt / resume 状态迁移不完整，后续刷新恢复失败。

## 缓解建议
- 所有业务完成仍以状态机为准，不以 mailbox ACK 代替。
- 对 cancel-late-result-ignore 建专项测试。
- 对 `waiting_for_input` / `interrupt_payload` 相关状态做显式契约测试。

## 建议验收命令
> 以下是目标命令形态，需在实现本阶段时同步落地为项目实际命令。

```bash
cd /Users/yinpeihai/Code_workspace/multi_agent_framework
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
```

## 验收清单
- [x] mailbox service 已存在
- [x] interrupt / resume 已存在
- [x] cancel 已具备 Task Context Termination 语义
- [x] conversation 串行保护已存在
- [x] 生命周期测试已覆盖关键边界
