# 主代理 Skill 兼容与真实 LLM 运行时

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- **范围**：后端 / 主代理 capability / LLM runtime
- **文档状态**：正式版（补齐主代理 Skill 兼容与真实 LLM runtime 实现事实）
- **日期**：2026-04-27

## 1. 背景

原后端 PRD 主要覆盖主代理编排内核与 数据查询 Skill MVP。当前实现已经新增普通主代理消息入口、Skill 兼容层、上传文件上下文注入、主代理 streaming LLM 输出、真实 provider runtime 绑定和手工 smoke 脚本，因此需要补齐正式 PRD。

## 2. 目标

主代理二期能力的目标是：
- 普通用户消息在未指定 `capability_id` 时进入 `main_agent.respond`；
- 主代理可以基于 Skill 描述、上传 artifact metadata 与受控脚本结果构造 prompt；
- 主代理通过 streaming LLM 输出前端可见的增量与最终事件；
- runtime 支持 fake / injected stream、显式 LLM config、config path 与 client factory，保证测试与真实 provider 验证分离；
- 真实 provider 调用只记录安全 metadata，不记录 prompt、API key、base_url 等敏感数据；
- 保持 数据查询 Skill 显式请求走 `skill.data_lookup` platform-service workflow，由 project Skill bundle handler 在 Skill 内部执行 domain stages，不被普通主代理路径吞掉。

## 3. 非目标

- 不复刻 本地 workspace runtime。
- 不支持任意 shell 执行。
- 不实现 plugin / MCP runtime。
- 不实现通用工具调用平台。
- 不引入 LangChain、LangGraph、AutoGen 等现成 Agent 框架。
- 不把 Skill 脚本能力扩大为不受控的系统级代码执行。

## 4. Capability 与路由契约

### 4.1 Public capability

主代理普通回复能力为：
- `main_agent.respond`

默认路由规则：
- `capability_id=None` → `main_agent.respond`；
- `capability_id="skill.data_lookup"` → 数据查询 Skill platform-service workflow；
- 数据查询 Skill domain stage 不允许作为外部普通请求入口。

### 4.2 输出事件

主代理成功调用时输出：
- `main_agent.output_delta`：前端可见，按 streaming chunk 输出；
- `main_agent.output_final`：前端可见，标记最终输出完成；
- `main_agent.llm_call`：audit-only，记录安全 provider metadata、耗时、命中 skill 数与上传 artifact 数。

provider 失败时输出：
- `main_agent.llm_fallback`：audit-only，记录 fallback 原因与安全 diagnostic；
- capability result 返回 retriable error，供上层调度 / API 处理。

## 5. Skill 兼容层契约

### 5.1 Skill 解析

后端可从 Skill 根目录读取 `SKILL.md`，解析：
- `name`
- `description`
- `triggers`
- `inputs`
- `outputs`
- `scripts`
- 正文说明

解析目标是让后端主代理理解 skill 能力与输入输出边界，而不是复刻 完整本地运行时。

### 5.2 Skill 匹配

主代理根据用户消息与 Skill catalog 做轻量匹配，并把匹配结果以结构化上下文注入 prompt。

匹配事件：
- 命中 skill：`skill.matched` audit-only；
- 未命中 skill：`skill.match_fallback` audit-only。

### 5.3 受控脚本执行

Skill 中声明的脚本仅允许按后端约束执行：
- 当前只支持 Python 脚本；
- 只运行声明为可自动执行的脚本；
- 输入 payload 包含用户问题、上传 artifact metadata 与请求 metadata；
- 脚本输出必须能被结构化收集；
- 脚本失败只记录受限原因并继续主代理路径，不应导致任意系统命令执行。

脚本相关事件：
- `skill.script_started`
- `skill.script_completed`
- `skill.script_failed`

均为 audit-only。

## 6. Artifact 上下文契约

主代理 prompt 可以注入上传文件 / 中间产物的脱敏 metadata，例如：
- artifact id / name / type；
- summary；
- storage ref 或安全引用；
- 经过裁剪的结构化 metadata。

不得默认把完整文件内容、secret、API key 或大对象正文直接注入 prompt。

## 7. LLM Runtime 绑定契约

主代理 streaming LLM 绑定优先级：
1. 测试或调用方注入 `main_agent_stream_generator`；
2. `main_agent_llm_config` 注入配置；
3. `main_agent_llm_config_path` 指向启动期 bootstrap 配置文件，并写入 `MAF_CONFIG_*` 环境变量；
4. `main_agent_llm_client_factory` 注入 client factory；
5. capability 内部按默认 `LLMClient` 从环境变量解析 provider。

服务化部署建议显式传入 config / config path / factory，避免依赖本机隐式配置；`config_path` 只允许在 runtime 启动阶段读取，业务节点执行阶段不得重复读取 YAML。同一 runtime 若同时配置多个 `*_config_path`，它们必须指向同一个启动配置文件；组件级差异 provider 配置应使用显式 config dict / factory。自动化测试必须使用 fake / injected stream，不访问真实 provider。

支持的 runtime 参数包括：
- `main_agent_stream_generator`
- `main_agent_llm_config`
- `main_agent_llm_config_path`
- `main_agent_llm_client_factory`
- `main_agent_reasoning_effort`

## 8. 安全 metadata 与审计

主代理调用 provider 时只允许记录安全 metadata：
- provider；
- model；
- config_source；
- reasoning_effort；
- status；
- duration_ms；
- matched_skill_count；
- uploaded_artifact_count；
- `prompt_recorded=false`。

禁止记录：
- API key；
- authorization / token / secret / password；
- 完整 prompt；
- messages；
- base_url / url；
- provider 原始敏感异常文本。

fallback diagnostic 只记录异常类型或受限摘要。

## 9. 手工 Smoke 契约

主代理真实 provider 验证通过显式脚本执行：

```bash
conda run -n multi_agent python scripts/smoke_main_agent_llm.py --config config.yaml --timeout 60
```

Smoke 验收重点：
- 普通主代理消息可通过真实 provider 产生回复；
- 事件流包含 `main_agent.output_delta` 与 `main_agent.output_final`；
- audit 事件包含 `main_agent.llm_call`；
- `prompt_recorded=false`；
- 不泄漏 API key、完整 prompt、base_url 等敏感信息；
- 数据查询 Skill 显式请求应走 `skill.data_lookup` platform-service workflow，尾阶段为 Skill handler 内部的 `filter_results`。

## 10. 对 API 与前端的影响

前端后续可依据事件流实现：
- 普通主代理 streaming 输出展示；
- 最终回复完成状态；
- Skill 命中状态的内部调试 / 审计视图；
- provider 失败时的可恢复错误提示；
- 上传文件上下文已被主代理使用的状态提示。

前端不应依赖 audit-only 事件作为用户主界面必需数据；正式用户可见信息应以后端明确标记为 `FRONTEND` 的事件和最终 artifact 为准。

## 11. Skill 构建指南

面向 OMX `skill-creator` 与后端可加载 Skill 的具体构建约束，见 `git@gitee.com:biobin/breeding-skill-builder.git` 的 `references/Skill构建指南.md`。该指南以当前实现为准，明确支持的 frontmatter 字段、触发匹配、prompt-only Skill、受控 Python auto-run 脚本、JSON IO、路径限制与本系统不支持的完整 本地 runtime 能力。
