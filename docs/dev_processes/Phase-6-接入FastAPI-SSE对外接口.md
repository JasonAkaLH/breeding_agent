# Phase 6：接入 FastAPI / SSE 对外接口

## 目标
在主框架内核与 NL2SQL capability 闭环已经打通后，接入对外 API、SSE 事件流、取消入口与审计输出，让系统形成真正可调用的后端服务面。

## 推荐 Owner
- 主 Owner：后端接口负责人 / API 负责人
- 协作 Owner：编排负责人、生命周期负责人、NL2SQL 负责人

## 输入
- `docs/dev_processes/Phase-5-接入NL2SQL-MVP能力链路.md`
- `docs/prd/05-API与核心数据模型.md`
- `docs/prd/03-协作协议与任务生命周期.md`
- `docs/prd/06-NL2SQL-MVP设计.md`

## 输出
- `src/api/` 目录
- FastAPI 路由与 DTO
- SSE 事件流实现
- cancel / graph / artifacts / capabilities 查询接口
- JSONL 审计输出
- 一组 API / SSE 集成测试

## 要做什么
- 落地以下 API：
  - `POST /api/v1/conversations/{conversation_id}/messages`
  - `GET /api/v1/tasks/{task_id}`
  - `GET /api/v1/tasks/{task_id}/events`
  - `POST /api/v1/tasks/{task_id}/cancel`
  - `GET /api/v1/tasks/{task_id}/graph`
  - `GET /api/v1/tasks/{task_id}/artifacts`
  - `GET /api/v1/capabilities`
- 将内部 event 转为 SSE 输出。
- 暴露 cancel 行为，并确保其真正驱动 Task Context Termination。
- 输出 JSONL 审计日志，至少覆盖：
  - blocked SQL
  - cancel 异常
  - 关键状态变化
- 补齐 API / SSE 层面的集成测试。

## 不做什么
- 不在本阶段完成一期 e2e 总验收结论。
- 不在本阶段直接做二期范围评估。
- 不在本阶段直接上线 PostgreSQL 生产库。
- 不在本阶段实现完整长期记忆系统。
- 不在本阶段实现跨任务知识沉淀 / 任务知识复用型记忆。

## 依赖
### 前置依赖
- Phase 5 capability 闭环已经成立。
- lifecycle 与 orchestration 规则已稳定。

### 外部依赖
- FastAPI / SSE 所需现有依赖。
- 若增加运行命令或脚本，需同步更新 README。

### 下游依赖
- Phase 7 的一期验收与二期评估依赖本阶段对外接口稳定。

## 边界条件
### 进入条件
- 内部最小闭环已跑通，API 不再只是壳子。

### 退出条件
- 外部可通过 API 提交任务、查询状态、订阅事件、发起取消。
- SSE 事件流与审计输出具备最小可观察性。
- API 层集成测试已经覆盖主路径与关键异常路径。

## 风险
- **入口先行风险**：API 提前暴露未稳定内部状态，导致接口很快返工。
- **协议漂移风险**：DTO / SSE 事件形状与 PRD 定义不一致。
- **取消假象风险**：表面有 cancel 接口，但底层没有真实取消语义。

## 缓解建议
- API 只建立在已稳定的 orchestration/lifecycle/capability 之上。
- 先做 API / SSE 集成测试，再开放更广使用面。
- cancel 接口必须连接到底层 Task Context Termination 语义，而不是只改响应文案。

## 建议验收命令
> 以下是目标命令形态，需在实现本阶段时同步落地为项目实际命令。

```bash
cd /Users/yinpeihai/Code_workspace/multi_agent_framework
python -m pytest tests/api -q
```

## 验收清单
- [ ] `src/api/` 已存在
- [ ] SSE 事件流已存在
- [ ] cancel 接口已能驱动真实取消语义
- [ ] API 集成测试已存在
- [ ] 审计输出已存在
