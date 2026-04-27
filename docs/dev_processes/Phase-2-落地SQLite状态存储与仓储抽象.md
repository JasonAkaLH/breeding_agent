# Phase 2：落地 SQLite 状态存储与仓储抽象

> 状态：已完成（2026-04-23）

## 目标
在不绑定 PostgreSQL 细节的前提下，先落地一套能承载一期任务状态链路的 SQLite 状态存储实现，让状态机有真正的真相源，而不是只有内存对象。

## 推荐 Owner
- 主 Owner：存储负责人 / 数据模型负责人
- 协作 Owner：生命周期负责人、编排负责人

## 输入
- `docs/dev_processes/Phase-1-建立核心契约与共享模型.md`
- `docs/prd/backend/04-状态存储与迁移策略.md`
- `docs/prd/backend/05-API与核心数据模型.md`

## 输出
- `src/storage/interfaces.py`
- `src/storage/sqlite/` 实现
- SQLite ORM / repository / session 管理
- 一组 round-trip 与约束测试
- 会话延续型记忆最小字段集的落库说明
- PostgreSQL 同构迁移预留点说明
- 从 SQLite 切换到 PostgreSQL 时的代码/配置/测试修改清单
- 配套设计稿：`docs/dev_processes/Phase-2-SQLite状态存储表结构草案.md`

## 要做什么
- 定义 storage interface，保证上层只面向接口编程。
- 落地 SQLite 模型与仓储，覆盖：
  - 会话延续型记忆所依赖的 conversation / message / task / interrupt / 最小结果摘要等状态对象
  - conversation
  - message
  - task
  - task_node
  - task_edge
  - artifact
  - event_record
  - mailbox_message
  - mailbox_delivery
  - interrupt
  - interrupt_answer
  - checkpoint
- 对 JSON / 时间字段采用 SQLite 兼容落地方式。
- 为未来 PostgreSQL 增强字段保留清晰升级位点。
- 明确记录未来迁移到 PostgreSQL 时要改的地方：engine/session、repository、ORM 字段类型、JSON/时间序列化、migration 脚本、索引与回归测试。
- 按“一期会话记忆落库规则”实施：
  - 恢复判断必需的信息走独立字段 / 独立列
  - 补充上下文的信息走 JSON / refs / summary
  - 不单独建设 memory 专用表
- 补齐 repository 级测试，验证 round-trip、唯一约束与核心索引前提。
- 显式验证会话延续型记忆最小字段集：conversation / message / task / task_node / interrupt / artifact 的最小恢复字段都能被稳定读回。

## 不做什么
- 不直接实现 PostgreSQL 连接与生产部署。
- 不在本阶段把高级索引、分区、JSONB 特化做完。
- 不新增 memory 专用表或跨任务知识复用结构。
- 不让 orchestration / api 直接持有底层 session。

## 依赖
### 前置依赖
- Phase 1 已稳定共享模型与 storage contract。

### 外部依赖
- 当前依赖快照中的 SQLAlchemy 能力。

### 下游依赖
- Phase 3 的 mailbox / interrupt / cancel 运行逻辑依赖本阶段存储落地。
- Phase 4 的 registry / scheduler / DAG 状态也依赖本阶段仓储接口。

## 边界条件
### 进入条件
- 共享核心模型与字段语义已稳定。

### 退出条件
- 一期所需核心对象都可在 SQLite 中持久化。
- 会话延续型记忆最小字段集已可稳定落库并读回。
- mailbox / interrupt / checkpoint 结构已经能被上层直接使用。
- 存储接口与 SQLite 实现完成基本解耦。

## 风险
- **方言绑定风险**：为了快而把 SQLite 细节写死进上层。
- **字段语义漂移风险**：落库字段名与 PRD 不一致，后面迁移难。
- **表职责混乱风险**：mailbox 主表和 delivery 状态表职责混在一起。

## 缓解建议
- 严格按 PRD 字段语义落库。
- 应用层只通过 storage port 访问。
- mailbox 继续坚持“消息主表 + 投递状态表”模型。

## 建议验收命令
> 以下是目标命令形态，需在实现本阶段时同步落地为项目实际命令。

```bash
cd /Users/yinpeihai/Code_workspace/multi_agent_framework
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
```

## 验收清单
- [x] storage interface 已存在
- [x] SQLite 模型与仓储已存在
- [x] 核心对象可 round-trip
- [x] 恢复判断必需字段已采用独立字段 / 独立列
- [x] JSON / refs / summary 字段与恢复判断字段的边界已明确
- [x] 会话延续型记忆最小字段集可稳定读写
- [x] mailbox / interrupt / checkpoint 已可持久化
- [x] PostgreSQL 同构迁移预留已明确
