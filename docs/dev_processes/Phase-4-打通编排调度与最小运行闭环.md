# Phase 4：打通编排调度与最小运行闭环

> 状态：已完成（2026-04-23）

## 目标
在 core / storage / lifecycle 已稳定后，建立主代理的**通用编排内核**，让任务能够完成“理解 → 编排 → 分发 → 检查 → 收敛/重编排”的闭环，并且这套能力可以兼容后续新增 capability，而不是只服务于 SQLQuery。

## 推荐 Owner
- 主 Owner：编排负责人 / 调度负责人
- 协作 Owner：生命周期负责人、能力负责人

## 输入
- `docs/dev_processes/Phase-3-实现生命周期与协作协议.md`
- `docs/prd/02-编排模型与资源调度.md`
- `docs/prd/05-API与核心数据模型.md`

## 输出
- `src/orchestration/` 目录
- capability registry / instance registry
- scheduler
- 通用 workflow / node plan 执行协议
- completion policy
- 一组基于 mock/fake capability flow 的 orchestration 测试

## 要做什么
- 实现任务 intake 到内部编排请求的转换。
- 实现 capability registry 与 instance registry 的最小版本（先支持本地实例）。
- 定义并实现**通用 workflow / node plan 标准**，让编排层可以消费“外部提供的 capability workflow 定义”。
- 实现 required / optional / fallback 节点策略绑定。
- 实现 completion policy、失败收敛与受控重编排入口。
- 加入严格拒绝型背压与最小资源配额骨架。
- 使用 mock / fake capability flow 验证编排闭环，而不是提前用真实 SQLQuery 业务链路证明本阶段。
- 明确 Phase 4 只定义 orchestration 标准，不负责为 Phase 5 的 SQLQuery 业务细节做反向适配。

## 不做什么
- 不在本阶段把 SQLQuery 细节写死进 orchestration。
- 不在本阶段生成“只针对 SQLQuery”的专用 DAG 逻辑。
- 不在本阶段为了首个 capability 反向修改主代理编排标准。
- 不在本阶段暴露最终 FastAPI 路由。
- 不提前实现多实例生产化调度。

## 依赖
### 前置依赖
- Phase 3 的生命周期与取消语义已经可用。

### 外部依赖
- 无新增外部依赖，本阶段以内核逻辑为主。

### 下游依赖
- Phase 5 的 SQLQuery capability 与 workflow definition 必须通过本阶段定义的 orchestration 标准接入。
- Phase 6 的 API / SSE 只应调用本阶段公开的 orchestration 服务。

## 边界条件
### 进入条件
- 共享模型、存储、生命周期都已形成稳定基础。

### 退出条件
- 能消费一个外部提供的 workflow / task plan，并驱动其执行完成。
- 调度器只选择 capability 匹配且可用的执行实例。
- 编排器不会直接理解 SQL、schema、guard、route 等 SQLQuery 内部细节。
- 本阶段能力可兼容后续新增 capability，而不是只为 SQLQuery 定制。

## 风险
- **业务耦合风险**：为了赶进度，把 SQLQuery 特殊逻辑塞进 orchestration。
- **标准反向适配风险**：让主代理编排能力去迁就首个 capability，导致后续新增 capability 时需要继续改内核。
- **循环失控风险**：重编排没有上限，导致任务无限扩展。
- **调度假象风险**：看似有 scheduler，实则只是硬编码调用单一路径。

## 缓解建议
- orchestration 只面向 capability contract 与 workflow plan 标准编程。
- 明确最大重编排轮次、最大动态扩展节点数。
- 即便先只支持本地实例，也要保留 registry/scheduler 接口边界。
- 本阶段验证优先使用 mock/fake capability flow，避免把框架问题和 SQLQuery 业务问题混在一起。

## 建议验收命令
> 以下是目标命令形态，需在实现本阶段时同步落地为项目实际命令。

```bash
cd /Users/yinpeihai/Code_workspace/multi_agent_framework
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
```

## 验收清单
- [x] orchestration 目录已存在
- [x] registry / scheduler 已存在
- [x] 通用 workflow / task plan 标准已存在
- [x] completion policy 已存在
- [x] 编排层未耦合 SQLQuery 业务细节
- [x] mock/fake capability flow 已能证明主代理编排闭环可运行
