# Phase 5：接入 SQLQuery MVP 能力链路

> 状态：已完成（2026-04-23）

## 目标
把一期的首个真实 capability 做通：从意图识别、schema context、SQL 生成、guard、只读执行到结果汇总，形成主框架上的第一条业务闭环；同时明确 **SQLQuery 要适配 Phase 4 已定义的编排标准**，而不是反向修改主代理内核。

## 推荐 Owner
- 主 Owner：SQLQuery capability 负责人
- 协作 Owner：编排负责人、数据库接入负责人、测试负责人

## 输入
- `docs/dev_processes/backend/Phase-4-打通编排调度与最小运行闭环.md`
- `docs/prd/backend/06-SQLQuery-MVP设计.md`
- `configs/sql_query/routing_rules.yaml`
- `configs/sql_query/schema_metadata.yaml`
- `configs/sql_query/sql_guard_rules.yaml`
- 当前 `src/sql_query/` 资产

## 输出
- `src/capabilities/sql_query/` 目录
- 适配 Phase 4 orchestration 标准的 SQLQuery workflow definition / plan provider
- SQLQuery capability 级实现
- MySQL 只读执行适配层
- SQLQuery 专项测试
- 一条 capability 闭环验证结果

## 要做什么
- 将现有 `src/sql_query/` 资产按 capability 级边界收口。
- 提供符合 Phase 4 通用 workflow / task plan 标准的 SQLQuery workflow definition。
- 实现以下能力单元：
  - `intent_route`
  - `schema_context_prepare`
  - `sql_generate`
  - `sql_guard`
  - `sql_execute_readonly`
  - `result_summarize`
- 复用当前 routing/schema/guard 配置。
- 将数据库访问封装到明确 async 边界中，避免直接阻塞事件循环。
- 落地 guard pass token、表白名单、单语句限制、LIMIT 要求、危险 SQL 阻断与审计输出。
- 支持结果汇总失败时降级为结构化结果摘要。

## 不做什么
- 不让 orchestration 直接操作 SQL guard 细节。
- 不在本阶段实现多 capability 路由竞争。
- 不做写入型 SQL 或管理员豁免。
- 不做 SQLQuery 之外的第二个 capability。

## 依赖
### 前置依赖
- Phase 4 的 orchestration / registry / scheduler 已可用。

### 外部依赖
- 当前依赖快照中的 SQLAlchemy / FastAPI / Pydantic 等。
- 现有只读 MySQL 账号事实与 schema 配置事实。

### 下游依赖
- Phase 6 的 API / SSE / e2e 验收依赖本阶段 capability 闭环。

## 边界条件
### 进入条件
- 主框架已能接收 capability 并驱动最小 DAG 闭环。

### 退出条件
- SQLQuery 标准链路可以跑通。
- guard 能阻断写入、多语句、系统 schema、越权表访问和缺失 LIMIT。
- 执行器没有绕过 guard 的直接入口。

## 风险
- **边界回流风险**：为了方便，把 SQL 逻辑回写到 orchestration。
- **阻塞风险**：沿用同步 DB 调用直接卡住事件循环。
- **安全假象风险**：只有数据库只读账号，没有应用层 guard；或反过来只有 guard，没有执行 contract。

## 缓解建议
- SQLQuery 必须适配 Phase 4 的 orchestration 标准，不应要求主代理为其增加专用业务分支。
- capability 内部实现必须完全隐藏在 `src/capabilities/sql_query/` 与 `src/integrations/` 后面。
- DB 调用统一通过 async 边界封装。
- 维持至少四层只读防线：contract、guard、executor、数据库权限。

## 建议验收命令
> 以下是目标命令形态，需在实现本阶段时同步落地为项目实际命令。

```bash
cd /Users/yinpeihai/Code_workspace/multi_agent_framework
conda run -n multi_agent python -m unittest discover -s tests/capabilities/sql_query -p 'test_*.py'
```

## 验收清单
- [x] `src/capabilities/sql_query/` 已存在
- [x] SQLQuery workflow definition 已按 Phase 4 标准接入
- [x] 标准 SQLQuery 链路已存在
- [x] guard 阻断路径已存在
- [x] 只读执行器无绕过入口
- [x] SQLQuery 专项测试已存在
