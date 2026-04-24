# Phase 7：完成一期验收并评估第二阶段

> 状态：已完成（2026-04-23）

## 目标
在对外 API 已稳定后，完成一期端到端验收，收集运行证据，并输出是否进入第二阶段的评估结论。

## 推荐 Owner
- 主 Owner：测试负责人 / 验收负责人
- 协作 Owner：后端接口负责人、SQLQuery 负责人、架构负责人

## 输入
- `docs/dev_processes/Phase-6-接入FastAPI-SSE对外接口.md`
- `docs/prd/05-API与核心数据模型.md`
- `docs/prd/06-SQLQuery-MVP设计.md`
- Phase 0 的一期范围与验收口径

## 输出
- e2e / observability 验收结果
- 一期完成度结论
- 已知风险与遗留问题清单
- 第二阶段是否启动的评估输入

## 要做什么
- 补齐 e2e 与 observability 验收：
  - happy path
  - guard blocked
  - interrupt/resume
  - cancel late result ignored
- 收集一期验收证据：
  - API 可调用
  - 状态可查询
  - 事件可订阅
  - 任务可取消
  - SQLQuery 只读链路可运行
  - 危险 SQL 可阻断
- 整理剩余风险与未做项：
  - PostgreSQL 正式化
  - 更完整长期记忆能力（如需启动，需另立专题）
  - 跨任务知识沉淀 / 任务知识复用型记忆（如需启动，需另立专题）
  - 第二个 capability
  - 生产化调度增强
- 若评估进入 PostgreSQL 正式化阶段，应同时复核 SQLite → PostgreSQL 修改清单，而不是只改连接串或建表脚本。
- 形成第二阶段评估结论，但不默认直接启动实现。

## 不做什么
- 不在本阶段直接进入第二阶段开发。
- 不因为一期已跑通就自动扩大范围。
- 不把二期需求回填进一期代码实现。

## 依赖
### 前置依赖
- Phase 6 的 API / SSE / cancel / audit 行为已经稳定。

### 外部依赖
- 无新增基础设施依赖，本阶段以验证、证据和评估为主。

### 下游依赖
- 若进入第二阶段，应基于本阶段结论重新立项与写 PRD/dev process 文档。

## 边界条件
### 进入条件
- 对外接口已经可调用，且具备基本测试基础。

### 退出条件
- 一期 happy path 与关键失败路径已被验证。
- 已形成清晰的一期完成度结论。
- 已形成第二阶段是否启动的评估输入。
- 未越界进入第二阶段实现。

## 风险
- **观察不足风险**：只有 happy path，没有 blocked/cancel/interrupt 的证据。
- **验收走样风险**：把“功能可跑”误当成“一期完成”。
- **二期串线风险**：验收时顺手开始做 PostgreSQL 正式化、长期记忆或跨任务知识沉淀，破坏一期收口。

## 缓解建议
- e2e 验收至少覆盖 1 条 happy path + 3 条关键失败路径。
- 以 Phase 0 的验收口径作为唯一完成标准来源。
- 第二阶段只输出评估结论，不直接并入实现。

## 建议验收命令
> 以下是目标命令形态，需在实现本阶段时同步落地为项目实际命令。

```bash
cd /Users/yinpeihai/Code_workspace/multi_agent_framework
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
```

## 验收清单
- [x] e2e 验收已覆盖主链路与关键失败路径
- [x] observability 验收已存在
- [x] 已形成一期完成度结论
- [x] 已形成二期评估输入
- [x] 未越界实现二期
