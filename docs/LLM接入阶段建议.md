# LLM 接入阶段建议

- 状态：提案
- 日期：2026-04-23
- 适用范围：主代理、通用子代理、SQLQuery（原 SQLQuery）capability 的 LLM 接入时机建议

---

## 1. 结论摘要

当前项目的一期主线（Phase 0 ~ Phase 7）已经先后完成了：

- 范围冻结
- 核心契约
- 状态存储
- 生命周期
- 通用编排内核
- 首个真实 capability（SQLQuery，原 SQLQuery MVP）

在这个基础上，LLM 接入建议分成两类处理：

### A. SQLQuery 内部的 LLM 接入

建议放在：

> **Phase 5 的后续增强专题（可称为 Phase 5.5）**

也就是：
- 不并入当前已完成的 Phase 5 主交付
- 单独作为 SQLQuery capability 增强专题推进

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
- SQLQuery（原 SQLQuery）capability 闭环
- 可复用的 `src/integrations/llm_client.py`

Phase 5.5 首轮后，SQLQuery 内部已经具备可注入的 LLM seam；Phase 8 首轮后，主代理也已经接入非 thinking streaming LLM seam；Phase 8.2 进一步把主代理真实 provider 绑定收口到 runtime 显式装配与手工 smoke 验证；2026-04-27 后，真实 API runtime 也会默认把 `LLMClient.generate_text()` 绑定到 SQLQuery 内部 `sql_generate` / `result_filtering`；2026-04-28 后，`config.yaml` 只作为启动期 bootstrap 源写入 `MAF_CONFIG_*` 环境变量，运行期消费者从环境读取。默认自动化测试仍不会访问真实 provider：

- 主代理已通过 `main_agent.respond` 接入 LLM seam，普通消息默认进入主代理
- 主代理真实 provider 绑定在 Phase 8.2 通过 `build_api_runtime()` 的显式 config / config path / client factory seam 完成，并提供 `scripts/smoke_main_agent_llm.py` 作为手工 smoke 入口
- 通用子代理未接入 LLM
- SQLQuery 的 `sql_generate` 已支持注入 `llm_text_generator` 走结构化 LLM 输出；真实 API runtime 启动时会先把 `config.yaml` bootstrap 到环境变量，再从环境创建文本生成器，fake / unittest 路径可显式关闭或注入 fake
- SQLQuery 的 `result_filtering` 已支持注入同一个 `llm_text_generator` 走结构化 LLM 结果筛选；调用前会按 `trim_max_tokens` 从最新行开始裁剪候选数据 token 预算，未配置、禁用或失败时仍保守保留候选表格 / 后过滤结果
- `build_api_runtime()` 提供 `sql_query_llm_config`、`sql_query_llm_config_path`、`sql_query_llm_client_factory`、`sql_query_reasoning_effort` 与 `enable_sql_query_llm`，其中 `*_config_path` 是启动期 bootstrap seam，运行期不重复读取 YAML；同一个 runtime 的多个 `*_config_path` 必须解析到同一个文件，如需组件级不同 provider 应使用显式 `config` dict / factory；真实运行、手工验证与自动化测试路径可明确区分

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
| SQLQuery 内部 `sql_generate` / `result_filtering` 的 LLM 化 | Phase 5.5 / SQLQuery 增强专题 | 它仍然属于首个 capability 的深化，不需要等待整个主代理 LLM 化 |
| 主代理普通对话输出真实 provider 绑定 | Phase 8.2 / 主代理运行时收口专题 | Phase 8 已有 streaming seam，本阶段只补真实 provider 显式装配、safe metadata 与 smoke，不改变编排决策 |
| 主代理任务理解 / 路线选择的 LLM 化 | Phase 8.3 或后续新专题 | 属于主框架行为升级，影响编排内核，不应混入 Phase 8.2 的 provider 绑定收口 |
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

### Step 3：启动 SQLQuery LLM 增强专题

在一期验收已经稳定之后，优先升级：

- `sql_generate`
- `result_filtering`

让 SQLQuery 从“启发式 MVP”升级到“LLM 主路径版本”。

### Step 4：再启动主代理 / 通用子代理 LLM 专题

在 SQLQuery capability 内部 LLM 已跑稳之后，再升级：

- 主代理任务理解与普通对话输出
- 路由判断与后续多 capability 自动选择
- 更泛化的子代理执行能力

> 当前 Phase 8 首轮已经完成主代理普通对话输出的 LLM seam；Phase 8.2 补齐主代理真实 provider runtime 绑定与 smoke；通用子代理 LLM 化与更复杂的自动路由仍保留为后续专题。

---

## 6. 为什么要先做 SQLQuery 的 LLM，再做主代理 / 通用子代理的 LLM

原因很简单：

### 6.1 风险更可控

SQLQuery 的 LLM 只影响：

- SQL 生成
- 结果筛选

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

### 7.1 SQLQuery LLM 增强专题

建议输出：

- `docs/prd/backend/07-SQLQuery-LLM增强与真实库验证.md`（正式 PRD）
- `docs/SQLQuery-LLM版本改造方案.md`（已存在）
- `docs/dev_processes/backend/Phase-5.5-SQLQuery-LLM增强专题.md`（Phase 5.5 讨论与开发过程入口）
- 新增 LLM 版 capability 测试与回归测试

### 7.2 主代理 / 通用子代理 LLM 专题

建议后续单独新增：

- `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`（主代理 Skill / LLM runtime 正式 PRD）
- `docs/prd/backend/09-高层DAG规划与SQLQuery宏能力边界.md`（高层 Planner 前置契约正式 PRD）
- `docs/dev_processes/backend/Phase-8.2-主代理真实LLM运行时绑定与Smoke验证.md`（主代理真实 provider runtime 绑定与 smoke）
- 后续主代理 LLM Planner / 路由设计文档
- 通用子代理 / worker 执行范式设计文档
- 资源配额 / 延迟 / 成本 / 可观测性设计文档

---

## 8. 最终建议

如果按当前项目阶段来安排：

> **先做完 Phase 6 和 Phase 7，完成一期非 LLM 主导版本的收口；**  
> **然后优先启动 SQLQuery 的 LLM 增强专题；**
> **最后再启动主代理和通用子代理的 LLM 接入专题。**

换句话说：

- **SQLQuery 的 LLM 接入**：建议最先做，但放在一期收口之后
- **主代理 / 通用子代理的 LLM 接入**：主代理普通对话输出已在 Phase 8 首轮接入，主代理真实 provider runtime 绑定在 Phase 8.2 收口；通用子代理 LLM 化与复杂自动路由建议继续单独立项
