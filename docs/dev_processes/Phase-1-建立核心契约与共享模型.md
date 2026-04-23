# Phase 1：建立核心契约与共享模型

> 状态：已完成（2026-04-23）

## 目标
先把系统的共享语言固定下来，建立后续 storage / lifecycle / orchestration / api / capabilities 共同依赖的一套核心模型与 contract，避免每层各写一套 Task、Node、Event 语义。

## 推荐 Owner
- 主 Owner：后端架构负责人 / 核心模型负责人
- 协作 Owner：存储负责人、编排负责人、API 负责人

## 输入
- `docs/dev_processes/Phase-0-冻结一期范围与验收边界.md`
- `docs/prd/02-编排模型与资源调度.md`
- `docs/prd/05-API与核心数据模型.md`
- 当前 `src/nl2sql/models.py`

## 输出
- `src/core/` 目录
- 一套共享 models / enums / errors / contracts
- 一份主框架与 capability 的目录边界约定
- 第一批核心模型失败测试与通过测试

## 要做什么
- 定义共享核心对象：
  - `Conversation`
  - `Message`
  - `Task`
  - `TaskNode`
  - `TaskEdge`
  - `Artifact`
  - `EventRecord`
  - `MailboxMessage`
  - `MailboxDelivery`
  - `Interrupt`
  - `Checkpoint`
- 定义共享状态枚举：
  - task status
  - node status
  - mailbox delivery status
  - interrupt status
- 定义 contract / port：
  - storage port
  - capability contract
  - executor port
  - event sink
  - audit sink
- 确定目录边界：
  - `src/core/`
  - `src/storage/`
  - `src/lifecycle/`
  - `src/orchestration/`
  - `src/api/`
  - `src/integrations/`
  - `src/capabilities/`

## 不做什么
- 不实现 SQLite 细节。
- 不写 mailbox / interrupt 运行逻辑。
- 不写 scheduler / registry。
- 不写 FastAPI route。
- 不写 NL2SQL 业务执行逻辑。

## 依赖
### 前置依赖
- Phase 0 边界已冻结。

### 外部依赖
- 无新增外部依赖，本阶段以模型与接口为主。

### 下游依赖
- Phase 2 ~ Phase 6 都依赖本阶段输出，后续不应再随意改共享模型语义。

## 边界条件
### 进入条件
- 已明确一期的模块边界和核心对象。

### 退出条件
- 后续所有层都可以直接依赖 `src/core/`。
- 关键对象与状态枚举不再分散定义。
- capability 不直接依赖 API 层或具体存储实现。

## 风险
- **模型散落风险**：不同模块重复定义 Task/Node/Event，后续状态对不齐。
- **过度抽象风险**：为了未来能力一次性设计过多抽象，拖慢一期推进。
- **边界模糊风险**：NL2SQL 现有代码与未来 capability 目录关系不清晰。

## 缓解建议
- 只定义一期必需 contract，不提前抽象二期。
- 使用现有 PRD 字段名作为第一来源，减少重新命名。
- 当前 `src/nl2sql/` 可先保留，等 Phase 5 再迁入 `src/capabilities/nl2sql/`。

## 建议验收命令
> 以下是目标命令形态，需在实现本阶段时同步落地为项目实际命令。

```bash
cd /Users/yinpeihai/Code_workspace/multi_agent_framework
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
```

## 验收清单
- [x] `src/core/` 已建立
- [x] 共享核心对象已集中定义
- [x] 共享状态枚举已集中定义
- [x] storage / capability / event 等基础 contract 已明确
- [x] 核心模型测试已就绪
