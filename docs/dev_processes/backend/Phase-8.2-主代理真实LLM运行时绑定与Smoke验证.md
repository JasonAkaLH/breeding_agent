# Phase 8.2：主代理真实 LLM Runtime 绑定与 Smoke 验证

> 状态：已完成（2026-04-27）  
> 定位：承接 Phase 8 的主代理 streaming LLM seam 与 Phase 8.1 的高层 Planner 前置契约，补齐主代理真实 provider 的显式 runtime 绑定、可观测 metadata 与手工 smoke 验证；本阶段不实现完整 LLM Planner 自动规划。

## 1. 背景

Phase 8 首轮已经完成：

- `main_agent.respond` 默认主代理入口；
- Codex Skill parser / catalog / matcher；
- 受控 Python script runner；
- 主代理非 thinking streaming 输出事件；
- `capability_id=None` 的普通消息默认进入主代理，显式 `sql_query.query` 仍进入 SQLQuery 固定 workflow。

Phase 8.1 又补齐了 LLM Planner 的前置契约：

- public capability allowlist；
- `WorkflowPlanValidator`；
- `WorkflowExpander`；
- Planner 输出 JSON schema 与 fake LLM 输出解析测试；
- SQLQuery 内部节点默认不暴露给主代理 / Planner。

但当前仍存在一个运行时缺口：

> 项目已有 `LLMClient` provider 适配器，也已有主代理 / SQLQuery 的 LLM seam；但主代理真实 provider 的 runtime 绑定、safe metadata 与手工 smoke 验证尚未作为独立阶段收口。

## 2. 当前仓库事实

- `src/integrations/llm_client.py` 提供 OpenAI-compatible `LLMClient`，支持 YAML 配置、非 streaming `generate_text()` 与非 thinking streaming `stream_text()`。
- `src/capabilities/main_agent/executor.py` 已能消费 `StreamGenerator` 并产生 `main_agent.output_delta` / `main_agent.output_final` 前端事件。
- `src/api/runtime.py` 已能注入 `main_agent_stream_generator` 与 SQLQuery `llm_text_generator`，测试默认用 fake generator 或显式关闭真实 provider。
- SQLQuery 的 `sql_generate` / `result_filtering` 支持 `llm_text_generator` 注入；真实 API runtime 默认可从 `config.yaml` 创建 SQLQuery 文本生成器，fake / 测试路径可显式关闭，失败时分别回退确定性 SQL 生成与候选表格保守筛选。
- 本地 `config.yaml` 已被 `.gitignore` 忽略，真实 provider smoke 必须由开发者显式触发，不进入默认 unittest。

## 3. 目标

1. 将主代理真实 LLM 绑定从能力内部隐式行为，补强为 API runtime 层可显式装配的 seam。
2. 保留 fake stream 注入优先级，确保默认自动化测试不访问真实 provider。
3. 为主代理 LLM 成功 / 失败事件补充 safe metadata，例如 `model`、`reasoning_effort`、`config_source`，但不记录 API key、完整 prompt、上传文件全文或真实服务端路径。
4. 提供显式手工 smoke 入口，验证本地 `config.yaml` 可驱动普通主代理消息走真实 provider。
5. 继续保证 SQLQuery 宏能力边界与 Phase 8.1 Planner 前置契约不回归。

## 4. 非目标

Phase 8.2 不做：

- 不实现完整 LLM Planner runtime 自动规划；
- 不让 LLM Planner 生成或执行 `sql_query.sql_generate`、`sql_query.sql_guard`、`sql_query.sql_execute_readonly` 等内部节点；
- 不改变 SQL Guard 与只读执行安全边界；
- 不把 SQLQuery 默认改成自动访问真实 provider；
- 不引入 LangChain、LangGraph、AutoGen 等现成 Agent 框架；
- 不把真实 provider 调用放进默认 unittest / 常规 e2e。

## 5. 实施切面

### 5.1 LLMClient safe metadata

`LLMClient` 继续作为 `src/integrations/` 下的外部系统适配器，但需要暴露不含 secret 的 metadata：

- `provider=openai_compatible`；
- `model`；
- `temperature`；
- `base_url_configured=true/false`，不记录实际 `base_url`；
- `config_source`；
- `reasoning_effort`。

### 5.2 Runtime 显式装配

`build_api_runtime()` 新增主代理真实 LLM 绑定参数：

- `main_agent_llm_config`：测试或上层运行时显式注入配置；
- `main_agent_llm_config_path`：显式指定本地配置文件路径；
- `main_agent_llm_client_factory`：测试 seam，可传入 fake client factory；
- `main_agent_reasoning_effort`：主代理 streaming 输出默认 `minimal`，可显式覆盖。

优先级：

1. `main_agent_stream_generator`：最高优先级，测试 / fake stream 继续使用；
2. 显式 LLM config / config path / client factory：构造真实或 fake LLM client，并将 `stream_text()` 包装为主代理 stream generator；
3. 兼容旧路径：未显式注入时仍允许能力层懒加载默认 `LLMClient()`，但后续生产部署应优先使用 runtime 显式装配。

### 5.3 主代理事件可观测性

`main_agent.llm_call` 成功事件保留：

- `status=succeeded`；
- `prompt_recorded=false`；
- `duration_ms`；
- `matched_skill_count`；
- `uploaded_artifact_count`。

并补充 safe metadata。

`main_agent.llm_fallback` 失败事件保留：

- `fallback_reason=provider_failed`；
- `prompt_recorded=false`；
- 只记录异常类型作为 diagnostic，不记录 provider 原始异常文本；
- safe metadata。

### 5.4 手工 smoke 入口

新增脚本：

```bash
conda run -n multi_agent python scripts/smoke_main_agent_llm.py --config config.yaml
```

该脚本会：

1. 使用 `main_agent_llm_config_path` 显式绑定主代理真实 LLM；
2. 提交一条 `capability_id=None` 的普通主代理消息；
3. 等待 task 进入 terminal 状态；
4. 输出 event types、LLM audit payload 与 audit log 路径；
5. 要求出现 `main_agent.output_delta`、`main_agent.output_final` 与 `main_agent.llm_call` 才返回成功。

## 6. 测试矩阵

| 层级 | 测试位置 | 覆盖内容 | 是否访问真实 provider |
|---|---|---|---:|
| integration | `tests/integrations/test_llm_client.py` | `stream_text()`、`generate_text()`、safe metadata | 否 |
| capability | `tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py` | 主代理 streaming、safe metadata、fallback audit | 否 |
| API runtime | `tests/api/test_main_agent_llm.py` | runtime fake LLM client factory 显式绑定、事件落库 | 否 |
| manual smoke | `scripts/smoke_main_agent_llm.py` | 本地 `config.yaml` + 真实 provider + 主代理普通消息 | 是，显式手工 |

## 7. 验收标准

- [x] `LLMClient.safe_metadata()` 不包含 `api_key`、原始 `base_url` 或完整 prompt。
- [x] `build_api_runtime()` 支持显式主代理 LLM config / config path / fake client factory。
- [x] fake stream 仍是测试最高优先级，默认 unittest 不访问真实 provider。
- [x] 主代理成功事件包含 safe provider metadata。
- [x] 主代理 provider 失败事件可解释，且不记录 prompt 正文或 secret。
- [x] `scripts/smoke_main_agent_llm.py` 可用本地 `config.yaml` 做显式真实 provider smoke。
- [x] SQLQuery 显式请求仍走 `sql_query.query` 固定六节点 workflow，尾节点为 `sql_query.result_filtering`。
- [x] Phase 8.1 Planner 前置契约测试继续通过。

## 8. 本阶段完成记录

2026-04-27 已完成 Phase 8.2：

- `LLMClient` 新增 `safe_metadata()`，只暴露 provider、model、temperature、`base_url_configured`、`config_source`、`reasoning_effort` 等安全字段，不暴露 API key 或原始 endpoint。
- `build_api_runtime()` 新增 `main_agent_llm_config`、`main_agent_llm_config_path`、`main_agent_llm_client_factory`、`main_agent_reasoning_effort`，可以在 runtime 层显式装配主代理真实 LLM；`main_agent_stream_generator` 继续保持最高优先级，默认测试仍走 fake。
- `MainAgentExecutor` / `MainAgentRespondCapability` 支持注入 stream metadata，并在 `main_agent.llm_call` / `main_agent.llm_fallback` 中记录 safe metadata；敏感 key 会被过滤，fallback diagnostic 只记录异常类型。
- 新增 `scripts/smoke_main_agent_llm.py`，显式使用本地 `config.yaml` 跑通真实 provider smoke；脚本要求 task completed，并验证 `main_agent.output_delta`、`main_agent.output_final`、`main_agent.llm_call` 出现。
- 已更新 `README.md`、`AGENTS.md`、`docs/LLM接入阶段建议.md` 与本目录索引。

验证结果：

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
# Ran 8 tests ... OK

conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
# Ran 6 tests ... OK

conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
# Ran 10 tests ... OK

conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
# Ran 21 tests ... OK

conda run -n multi_agent python scripts/smoke_main_agent_llm.py --config config.yaml --timeout 60
# task completed; 14 frontend main-agent output events; main_agent.llm_call recorded with model=deepseek-v3-2-251201, prompt_recorded=false
```

后续事项：

- Phase 8.3 可在此基础上接入高层 LLM Planner runtime，但仍必须先经过 public-only validator 与 macro expander。
- SQLQuery 若要默认绑定真实 provider，应作为独立阶段处理；当前仍保持显式 `llm_text_generator` seam。
