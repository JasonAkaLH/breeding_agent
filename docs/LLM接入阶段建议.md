# LLM 接入阶段建议

- 状态：提案
- 日期：2026-04-23
- 适用范围：主代理、通用子代理、NL2SQL capability 的 LLM 接入时机建议

---

## 1. 结论摘要

当前项目的一期主线（Phase 0 ~ Phase 7）已经先后完成了：

- 范围冻结
- 核心契约
- 状态存储
- 生命周期
- 通用编排内核
- 首个真实 capability（NL2SQL MVP）

在这个基础上，LLM 接入建议分成两类处理：

### A. NL2SQL 内部的 LLM 接入

建议放在：

> **Phase 5 的后续增强专题（可称为 Phase 5.5）**

也就是：
- 不并入当前已完成的 Phase 5 主交付
- 单独作为 NL2SQL capability 增强专题推进

### B. 主代理 / 通用子代理的 LLM 接入

建议放在：

> **Phase 7 之后，单独开一个新阶段 / 新专题**

也就是：
- 不塞进 Phase 6
- 不塞进 Phase 7
- 作为一期验收完成后的下一阶段能力升级来做

---

## 2. 当前现状

### 2.1 已经具备的基础

当前仓库已经具备：

- 通用 orchestration 标准
- 生命周期与取消语义
- SQLite 状态真相源
- NL2SQL capability 闭环
- 可复用的 `llm_client.py`

但目前真正接入 LLM 的部分还没有落地：

- 主代理未接入 LLM
- 通用子代理未接入 LLM
- NL2SQL 的 `sql_generate` 默认仍是启发式实现
- NL2SQL 的 `result_summarize` 默认仍是模板/规则式实现

---

## 3. 为什么不建议现在把 LLM 混进剩余的 Phase 6 / Phase 7

## 3.1 不建议放进 Phase 6

Phase 6 的任务是：

- FastAPI
- SSE
- 对外接口

它本质上解决的是：

> **服务面和交互面问题**

如果把 LLM 接入放到 Phase 6，会把：

- API 接口问题
- SSE 事件问题
- 模型输出稳定性问题
- prompt 质量问题
- provider 超时 / retry 问题

全部混在一起，导致 Phase 6 失焦。

## 3.2 不建议放进 Phase 7

Phase 7 的任务是：

- 一期验收
- 证据收集
- 第二阶段评估

它本质上解决的是：

> **一期收口与是否进入下一阶段的判断**

如果把 LLM 接入混进 Phase 7，会把“一期已收口的验收口径”重新打开，导致：

- 验收标准漂移
- 测试结论失真
- 一期边界失焦

---

## 4. 推荐阶段安排

| 能力类型 | 推荐接入阶段 | 原因 |
|---|---|---|
| NL2SQL 内部 `sql_generate` / `result_summarize` 的 LLM 化 | Phase 5.5 / NL2SQL 增强专题 | 它仍然属于首个 capability 的深化，不需要等待整个主代理 LLM 化 |
| 主代理任务理解 / 路线选择的 LLM 化 | Phase 7 之后的新专题 | 属于主框架行为升级，影响编排内核，不应混入一期收口阶段 |
| 通用子代理 / worker 型能力的 LLM 化 | Phase 7 之后的新专题 | 会影响 capability 执行范式与整体资源调度，不适合一期尾声混入 |

---

## 5. 推荐执行顺序

建议按以下顺序推进：

### Step 1：先完成 Phase 6

把：
- FastAPI
- SSE
- cancel / query / graph / artifacts 接口

全部接稳。

### Step 2：完成 Phase 7

把：
- 一期 capability 闭环
- 生命周期
- 编排调度
- API / SSE

都作为**非 LLM 主导版本**先验收收口。

### Step 3：启动 NL2SQL LLM 增强专题

在一期验收已经稳定之后，优先升级：

- `sql_generate`
- `result_summarize`

让 NL2SQL 从“启发式 MVP”升级到“LLM 主路径版本”。

### Step 4：再启动主代理 / 通用子代理 LLM 专题

在 capability 内部 LLM 已跑稳之后，再考虑升级：

- 主代理任务理解
- 路由判断
- 更泛化的子代理执行能力

---

## 6. 为什么要先做 NL2SQL 的 LLM，再做主代理 / 通用子代理的 LLM

原因很简单：

### 6.1 风险更可控

NL2SQL 的 LLM 只影响：

- SQL 生成
- 结果总结

但：
- `sql_guard` 仍然在
- `sql_execute_readonly` 仍然在
- orchestration 边界不变

所以它是一个：

> **被硬约束包裹住的 LLM 接入点**

### 6.2 主代理 LLM 的影响面更大

主代理一旦 LLM 化，会影响：

- 任务理解
- 路线选择
- DAG 生成
- 重编排决策
- 中断/澄清策略

这会直接改变主框架行为，风险远高于 capability 内部的 LLM 升级。

---

## 7. 推荐输出物

### 7.1 NL2SQL LLM 增强专题

建议输出：

- `docs/NL2SQL-LLM版本改造方案.md`（已存在）
- Phase 5.5 对应的开发过程文档
- 新增 LLM 版 capability 测试与回归测试

### 7.2 主代理 / 通用子代理 LLM 专题

建议后续单独新增：

- 主代理 LLM 化设计文档
- 通用子代理 / worker 执行范式设计文档
- 资源配额 / 延迟 / 成本 / 可观测性设计文档

---

## 8. 最终建议

如果按当前项目阶段来安排：

> **先做完 Phase 6 和 Phase 7，完成一期非 LLM 主导版本的收口；**  
> **然后优先启动 NL2SQL 的 LLM 增强专题；**  
> **最后再启动主代理和通用子代理的 LLM 接入专题。**

换句话说：

- **NL2SQL 的 LLM 接入**：建议最先做，但放在一期收口之后
- **主代理 / 通用子代理的 LLM 接入**：建议更晚做，单独立项

