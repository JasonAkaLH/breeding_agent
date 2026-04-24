# SQLQuery LLM 版本改造方案

- 状态：Phase 5.5 首轮实现已落地，真实 provider 运行绑定与手工 smoke 待后续补齐
- 日期：2026-04-23
- 适用范围：在当前 Phase 5 已完成的 SQLQuery MVP 基础上，升级为真正使用 LLM 的版本

---

## 1. 背景

当前仓库里的 SQLQuery capability 已经形成一条**可运行的 MVP 闭环**：

- `intent_route`
- `schema_context_prepare`
- `sql_generate`
- `sql_guard`
- `sql_execute_readonly`
- `result_summarize`

其中：

- `schema_context_prepare` 已复用现有 `src/sql_query/schema_context_builder.py`
- `sql_guard` 已接入 `configs/sql_query/sql_guard_rules.yaml`
- `sql_execute_readonly` 已通过 `src/integrations/mysql_readonly.py` 形成只读执行适配层

Phase 5.5 首轮实现前，两处关键能力仍是**启发式 / stub-friendly**实现；当前已补齐可注入的 LLM 主路径，但默认自动化测试仍不访问真实 provider：

1. `src/capabilities/sql_query/sql_generate.py`
   - 支持注入 `llm_text_generator` 走结构化 LLM 输出；
   - 未显式注入 LLM 时仍使用规则拼接 SQL，保证默认回归不访问外部 provider。
2. `src/capabilities/sql_query/result_summarize.py`
   - 支持注入 `llm_text_generator` 走结构化 LLM 摘要；
   - 未显式注入 LLM 或 LLM 失败时仍使用确定性模板摘要。

同时，仓库里已经存在一个可复用的异步 LLM 客户端雏形：

- `src/integrations/llm_client.py`
- `src/capabilities/sql_query/prompt_builders.py`
- `src/capabilities/sql_query/llm_utils.py`

因此，当前最合理的后续升级路线不是推翻 Phase 5，而是：

> **保留现有 capability / orchestration / guard / readonly execution 结构，  
> 只把 `sql_generate` 与 `result_summarize` 升级为真正的 LLM 驱动版本，并保留稳定降级路径。**

---

## 2. 当前实现现状

### 2.1 已有可复用部分

- `src/capabilities/sql_query/workflow.py`
  - 已定义标准 6 节点 workflow
- `src/capabilities/sql_query/executor.py`
  - 已提供 `sql_generator` / `summarizer` / `llm_text_generator` 注入位
- `src/capabilities/sql_query/schema_context_prepare.py`
  - 已能产出 route / selected_tables / selected_columns / join_hints / context_summary
- `src/capabilities/sql_query/sql_guard.py`
  - 已能执行只读安全校验并发放 `guard_pass_token`
- `src/integrations/mysql_readonly.py`
  - 已能在 async 边界后执行只读 SQL
- `src/integrations/llm_client.py`
  - 已有异步 OpenAI-compatible client seam
- `src/capabilities/sql_query/prompt_builders.py`
  - 已承载 SQLQuery 专属 SQL 生成 prompt 与结果摘要 prompt 组装
- `src/capabilities/sql_query/llm_utils.py`
  - 已承载文本生成器兼容、JSON 提取与 JSON-safe preview 工具

### 2.2 当前能力短板

#### SQL 生成
未显式注入 `llm_text_generator` 时，`sql_generate` 的 fallback 行为是：
- 选择 1~2 张表
- 从裁剪后的字段里拼接 `SELECT`
- 用 join hint 拼接 `JOIN`
- 默认补 `LIMIT 50`

这能跑通 MVP，但明显不适合：
- 复杂条件理解
- 隐式时间范围推理
- 多维度聚合 / 分组问法
- 用户口语化问题

#### 结果总结
未显式注入 `llm_text_generator` 或 LLM 摘要失败时，`result_summarize` 的 fallback 行为是：
- 读取 rows / columns / row_count
- 输出固定模板摘要

它的优点是稳定，但不适合：
- 复杂结果解释
- 业务重点归纳
- 表格/聚合结果的自然语言分析

---

## 3. 改造目标

本方案的目标是：

1. 将 `sql_generate` 升级为**LLM 主路径 + 规则降级路径**
2. 将 `result_summarize` 升级为**LLM 主路径 + 结构化降级摘要**
3. 保持以下边界不变：
   - orchestration 不理解 SQL 细节
   - `sql_guard` 仍然是强制前置节点
   - `sql_execute_readonly` 仍然必须校验 `guard_pass_token`
   - DB 执行仍然走 async 边界
4. 不引入新的 Agent 框架，不把 capability 再包装成 LangChain / LangGraph 风格流水线

---

## 4. 非目标

本次改造**不**包括：

- 重写 `src/sql_query/schema_context_builder.py`
- 取消或弱化 `sql_guard`
- 让 orchestration 直接理解 SQL prompt / schema 细节
- 做多 capability 路由竞争
- 做管理员豁免 / 写入型 SQL
- 接入向量检索、长期记忆、跨任务知识复用
- 引入 LangChain / LangGraph / AutoGen

---

## 5. 总体方案

## 5.1 架构原则

升级后的整体结构仍保持：

```text
intent_route
  -> schema_context_prepare
  -> sql_generate (LLM)
  -> sql_guard
  -> sql_execute_readonly
  -> result_summarize (LLM)
```

核心原则：

1. **LLM 只负责“生成”和“解释”**
2. **安全性不交给 LLM 保证**
3. **执行权仍由 guard + readonly executor 掌握**
4. **LLM 失败时必须可降级**

---

## 5.2 推荐改造方式

### 方案 A：在现有节点内增加可注入 LLM 主路径（已按此方向落地）

#### 做法

- `sql_generate.py`
  - 显式注入 `llm_text_generator` 时优先使用 LLM 生成
  - 保留启发式 fallback；未注入 LLM 时默认仍走 fallback，避免默认回归访问外部 provider
- `result_summarize.py`
  - 显式注入 `llm_text_generator` 时优先使用 LLM 总结
  - 保留结构化 fallback；未注入 LLM 或 LLM 失败时仍走确定性摘要
- `executor.py`
  - 提供 `llm_text_generator` 注入位
  - 真实运行时可注入 `src/integrations/llm_client.py` 的 `generate_text`，该接口默认使用非 streaming completion，适合 SQL / 摘要这类结构化 JSON 生成；如后续主代理需要“不启用 thinking 但流式回传用户输出”，可使用 `stream_text()`；默认测试不自动启用真实 provider

#### 优点

- 变更面最小
- 不破坏 Phase 5 的 capability 接口
- 不需要改 workflow 标准
- 风险集中在两个节点内部，便于测试和回滚

#### 缺点

- 单文件职责会变得更重
- `sql_generate.py` / `result_summarize.py` 需要同时管理 LLM 主路径和 fallback 路径

---

### 方案 B：新增 LLM adapter 层，再由 capability 调用（更清晰）

#### 做法

新增例如：

- `src/integrations/llm_text_generation.py`
- `src/capabilities/sql_query/prompt_builders.py`

由 capability 节点只负责：
- 组装输入
- 调用 LLM adapter
- 处理返回结构

#### 优点

- 结构更清晰
- LLM 逻辑与 capability 逻辑分离
- 后续可复用到其他 capability

#### 缺点

- 增加新模块
- 初次实现时工作量更大

---

### 推荐决策

**先做 A，再按需要演进到 B。**

原因：
- 当前仓库仍处于 Phase 5 之后、Phase 6 之前
- 最需要的是快速把 LLM 主路径做通
- 不宜在此时引入过多新抽象

---

## 6. 详细改造步骤

### Step 1：收口 LLM 接入边界

#### 建议改动

- 保留 `src/integrations/llm_client.py`
- 在 capability 层只通过注入方式使用它

#### 建议文件

- `src/integrations/llm_client.py`（可增强）
- `src/capabilities/sql_query/executor.py`

#### 具体目标

1. 明确 `sql_generator` 的调用签名
2. 明确 `summarizer` 的调用签名
3. 让 `SQLQueryExecutor` 能在“测试模式 / 真实 LLM 模式”之间切换

---

### Step 2：改造 `sql_generate`

#### 当前文件

- `src/capabilities/sql_query/sql_generate.py`

#### 改造目标

让 `sql_generate`：

1. 读取上游输出：
   - `route_id`
   - `schema_profile_id`
   - `selected_tables`
   - `selected_columns`
   - `join_hints`
   - `context_summary`
   - `sql_policy_profile`
   - `user_question`
2. 基于这些输入构造 LLM prompt
3. 让 LLM 生成 SQL 草案
4. 若 LLM 调用失败 / 超时 / 输出为空：
   - 回退到当前启发式 SQL 生成逻辑

#### 关键要求

- 生成结果不直接执行
- 必须继续经过 `sql_guard`
- prompt 中必须显式附带：
   - 只读限制
   - 单语句限制
   - LIMIT 约束
   - 表白名单
   - 禁止系统 schema

---

### Step 3：改造 `result_summarize`

#### 当前文件

- `src/capabilities/sql_query/result_summarize.py`

#### 改造目标

让 `result_summarize`：

1. 读取：
   - rows
   - columns
   - row_count
   - 用户原问题
   - 可能的 SQL / route 上下文
2. 调用 LLM 输出更自然的中文总结
3. 若 LLM 失败，则保留当前结构化 fallback

#### 关键要求

- fallback 路径绝不能删
- 即使 LLM 失败，整个 capability 链路仍应返回可用摘要

---

### Step 4：补齐 Prompt 资产

建议新增文档或模板文件，例如：

- `docs/SQLQuery-LLM提示词草案.md`

内容至少包括：

1. SQL 生成 prompt 模板
2. 结果总结 prompt 模板
3. 系统约束说明
4. 示例输入输出

如果暂时不想新增配置文件，也可以先把模板内嵌在 capability 内部，再等稳定后抽文档或配置。

---

### Step 5：增加 LLM 版测试

#### 需要新增/扩展的测试

在现有：
- `tests/capabilities/sql_query/test_sql_generate.py`
- `tests/capabilities/sql_query/test_result_summarize.py`

基础上补：

1. **LLM 正常返回时**
   - `sql_generate` 会产出 LLM 结果
   - `result_summarize` 会产出自然语言总结

2. **LLM 超时/异常时**
   - `sql_generate` 会回退到启发式 SQL
   - `result_summarize` 会回退到结构化摘要

3. **Guard 仍然有效**
   - 即使 LLM 生成了危险 SQL，也必须被 `sql_guard` 阻断

4. **闭环测试**
   - 一条基于 orchestration 的 SQLQuery LLM 版闭环测试

---

## 7. 风险与缓解

### 风险 1：LLM 生成 SQL 不稳定

#### 影响
- 生成错误 SQL
- 生成无 LIMIT SQL
- 生成越权表访问

#### 缓解
- 绝不跳过 `sql_guard`
- prompt 强化约束
- 保留启发式 fallback

---

### 风险 2：LLM 总结过度发挥

#### 影响
- 编造不存在的业务结论
- 把行级结果总结成未经验证的推断

#### 缓解
- summary prompt 要求“只基于结果表述”
- fallback 保留结构化摘要
- 对关键业务场景优先输出“数据事实 + 少量解释”

---

### 风险 3：外部依赖导致链路不稳定

#### 影响
- 网络问题
- provider 超时
- 速率限制

#### 缓解
- 在 `src/integrations/llm_client.py` 层做 timeout / retry
- capability 层保留 fallback
- 测试环境使用 fake/stub generator

---

## 8. 推荐实施顺序

### 第 1 步
先把 `sql_generate` 改成：

> **LLM 主路径 + 当前启发式 fallback**

因为 SQL 生成是能力提升最大的部分。

### 第 2 步
再把 `result_summarize` 改成：

> **LLM 主路径 + 当前结构化 fallback**

### 第 3 步
最后补文档、回归测试和闭环测试。

---

## 9. 验收标准

完成改造后，至少应满足：

1. `sql_generate` 在显式注入 LLM 文本生成器时走 LLM 主路径
2. `sql_generate` 在 LLM 失败时能降级
3. `result_summarize` 在显式注入 LLM 文本生成器时走 LLM 主路径
4. `result_summarize` 在 LLM 失败时能降级
5. `sql_guard` 仍然对 LLM 生成 SQL 生效
6. `sql_execute_readonly` 仍然必须要求 `guard_pass_token`
7. orchestration 层无需理解任何 SQL prompt 细节
8. SQLQuery capability 闭环测试仍然通过

---

## 10. 最小建议落点

如果只做第一轮最小改造，建议先改这些文件：

- `src/capabilities/sql_query/sql_generate.py`
- `src/capabilities/sql_query/result_summarize.py`
- `src/capabilities/sql_query/executor.py`
- `src/integrations/llm_client.py`
- `tests/capabilities/sql_query/test_sql_generate.py`
- `tests/capabilities/sql_query/test_result_summarize.py`
- `tests/capabilities/sql_query/test_orchestration_flow.py`

---

## 11. 一句话总结

当前 SQLQuery 已经具备：

> **可运行的 capability 闭环**

下一步 LLM 版改造的正确路线不是推翻现有结构，而是：

> **在保留 orchestration / guard / readonly execution 边界不变的前提下，  
> 把 `sql_generate` 与 `result_summarize` 升级为 LLM 主路径，并保留稳定降级能力。**
