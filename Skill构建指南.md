# Skill 构建指南（v2 Skill Contract）

- **适用对象**：为本项目后端创建可被 `main_agent.respond` 发现、答疑、缺参追问并在内部显式执行的项目级 Skill 开发者。
- **适配范围**：本系统的 Skill v2 contract 兼容层；不是通用本地 agent runtime。
- **当前实现入口**：`src/integrations/agent_skills/`、`src/capabilities/main_agent/`、`src/capabilities/skill_tool/`、`src/api/runtime.py`。
- **更新时间**：2026-06-05

## 1. 一句话结论

本项目已切换为 **v2-only Skill bundle**：

1. `SKILL.md` 是触发后加载的 agent-facing runbook：frontmatter 只保留 `name` / `description`，正文保留核心流程、边界、输出口径和资源导航。
2. `skill.contract.yaml` 是唯一平台契约，定义公开 capability、路由、运行时、入口、输出和资源策略。
3. `schemas/*.input.yaml` 定义可执行输入 schema、字段来源、校验和缺参问题。
4. `references/` 存放按需公开资料，由 `SkillResourceService` 在声明范围内读取。
5. 外部调用方不能直接硬提交 `skill.*`；slash / API 点名 Skill 固定走 `main_agent.respond + metadata.soft_skill_binding`，由主代理判断答疑、追问缺参或内部展开显式 Skill 节点。

无 `skill.contract.yaml` 的 Skill 不注册、不执行、不进入 `/api/v1/capabilities`。历史大 frontmatter 字段不会再作为公开注册或执行契约。

## 2. 目录结构

推荐目录：

```text
skill/my-skill/
  SKILL.md
  skill.contract.yaml
  schemas/
    default.input.yaml
  references/
    usage.md
    data-format.md
  scripts/
    run.py                  # 可选，仅由 contract entrypoint 引用
  runtime/
    my_skill_service/        # 可选，仅 platform_service handler 使用
      platform_handler.py
  native/                    # 可选，仅项目级 trusted runtime 源码/构建产物归属
```

说明：

- `SKILL.md` 和 `skill.contract.yaml` 是项目级公开 Skill 的必需文件。
- `schemas/` 在执行型 Skill 中通常必需；多种业务模式应拆成多个 input schema。
- `references/` 是答疑资料库，不会被主代理自动全量读取，只能按 contract resource policy 按需读取。
- `scripts/`、`runtime/`、`native/` 是实现细节；主代理 prompt-facing 资料不得读取这些目录原文。

## 3. `SKILL.md`：Agent runbook，不承载平台契约

系统 `skill-creator` 对 `SKILL.md` 的定义是：frontmatter 用于触发，body 是 Skill 触发后加载的操作指南。因此 v2 项目 Skill 不应把 `SKILL.md` 写成空壳；它应该是精简但足够可执行的 agent-facing runbook。

### 3.1 Frontmatter：只写触发字段

`SKILL.md` frontmatter 只写 `name` 和 `description`。其中 `description` 是触发和匹配的重要文本，应写清楚“做什么 + 何时用”，可以包含关键业务场景、常见叫法和用户会说的触发表达。

```markdown
---
name: field-design
description: >-
  基于用户上传或粘贴的材料清单生成田间试验设计、fieldbook、种植顺序和布局预览。适用于随机区组设计/RCBD、对角线增广设计、间比法/Interval、重复数/区组数设置、田块列数、CK 起始位置与间隔、对照材料布置、生成 CSV fieldbook 或 HTML 布局预览等请求；也适用于回答 ped_id、hyb_check、set、blocks、ncols、ck_spec 等字段和设计参数如何填写。
---
```

禁止在 frontmatter 中恢复平台契约字段；这些字段只属于 v1 manifest 或 v2 contract/schema。

### 3.2 Body：写触发后的操作指南

正文应保留 Agent 触发后真正需要的核心知识：欢迎语/启动协议、业务工作流、输入口径、缺参追问策略、输出格式、follow-up 处理、资源导航和安全边界。

```markdown
# 试验设计智能体

使用此 Skill 帮用户完成 RCBD、Diagonal 和 Interval 三类田间设计任务。
平台执行事实源由同目录 `skill.contract.yaml` 和当前 selected input schema 决定；本文只提供 agent-facing 使用流程和解释边界。

## 欢迎语
当用户裸调用本 Skill，或首次表达试验设计需求但材料清单/设计类型/关键参数不足时，先说明支持的设计类型和需要用户补充的最小信息。

## 选择设计类型
- 用户提到随机区组、RCBD、重复数、区组数时，选择 RCBD。
- 用户提到对角线增广、diagonal checks、田块列数时，选择 Diagonal。
- 用户提到间比法、CK 起始位置或 check intervals 时，选择 Interval。

## 输入与补参
解释用户可见字段：ped_id、hyb_check、set、blocks、ncols、ck_spec。缺少 selected schema 必填字段时，只问当前最小必要项。

## Resources
- `references/material-data.md`：材料表字段说明。
- `references/rcbd.md`：RCBD 规则。
- `references/diagonal.md`：对角线增广规则。
- `references/interval.md`：Interval 与 CK 参数规则。

## 输出策略
最终回复先说明设计模式和核心参数，再展示前 10 行预览，最后提供完整 CSV 和 HTML artifact 入口。

## Boundaries
不暴露脚本路径、handler、service、token、数据库/LLM 配置、本机绝对路径或内部运行目录。
```

### 3.3 写作原则

- **保留核心流程**：不要只写一句话；另一个 Agent 触发后应能知道如何和用户交互、何时追问、如何组织最终回答。
- **渐进披露**：把长字段表、完整示例、设计细节、报告结构等放入 `references/`；但必须在 `SKILL.md` 中列出何时读取哪个 reference。
- **避免重复事实源**：正文可以解释用户可见业务口径，但不得复制 `skill.contract.yaml` 的 entrypoint、runtime、handler、service allowlist，也不得复制 `schemas/*.input.yaml` 的机器校验细节。
- **保留安全边界**：明确不暴露脚本路径、handler、配置、secret、token、数据库连接串、内网地址、本机绝对路径或内部运行目录。
- **控制长度**：一般保持在 50-200 行；复杂 Skill 可更长，但应低于 500 行，接近上限时拆到 `references/`。
- **不要放辅助项目文档**：不要在 Skill bundle 内新增 README、安装指南、变更日志等与 Agent 执行无关的文件。

## 4. `skill.contract.yaml`：唯一平台契约

最小模板：

```yaml
contract_version: '2'
capability:
  id: skill.my_skill
  display_name: My Skill
  description: 一句话说明这个 Skill 什么时候可用
  version: 1.0.0
routing:
  triggers:
    - 生成试验设计
    - 查询材料
  intent_aliases:
    - 用户点名 /my-skill
  examples:
    - /my-skill 用上传 CSV 生成 RCBD，3 个重复
runtime:
  mode: python_subprocess
  answer_mode: requires_finalizer
  timeout_seconds: 120
entrypoints:
  - id: run_default
    kind: python_subprocess
    script: scripts/run.py
    input_schema: schemas/default.input.yaml
    output: outputs/default
input_schemas:
  - id: default
    path: schemas/default.input.yaml
    title: 默认输入
    description: 使用上传材料和自然语言参数执行默认流程
output_contracts:
  - id: outputs/default
    required_keys:
      - summary
resources:
  - id: usage
    path: references/usage.md
    audience:
      - main_agent
resource_policy:
  default_audience:
    - main_agent
  max_bytes_per_read: 12000
  redaction: true
```

关键规则：

- `capability.id` 必须是稳定公开 ID，项目 Skill 使用 `skill.*`。
- capability registry 只从 contract 注册公开 Skill；不会从 `SKILL.md` 名称或历史 metadata 派生。
- `runtime.mode` 目前支持 `python_subprocess`、`platform_service`、`delegated_main_agent`。
- `answer_mode: requires_finalizer` 表示执行结果先进入受控 dependency context，再由主代理汇总。
- 所有路径必须在 Skill bundle 内，禁止绝对路径和 `..` 逃逸。
- `entrypoints[].script`、platform handler 和 output contract 都是内部执行契约，不进入公开 profile。

## 5. `schemas/*.input.yaml`：输入 schema 与缺参口径

示例：

```yaml
id: rcbd
version: 1.0.0
title: RCBD 设计输入
description: 使用上传材料表和重复次数生成随机完全区组设计。
activation:
  aliases:
    - rcbd
    - 随机完全区组
fields:
  material_data:
    type: artifact
    required: true
    title: 材料数据
    question: 请上传或指定材料表。
    sources:
      - upload
  blocks:
    type: integer
    required: true
    title: 重复次数
    question: 请提供重复次数，例如 3 个重复。
    sources:
      - user_text
    validation:
      min: 1
      max: 20
```

规则：

- 每个 schema 只覆盖一个业务模式；不要让一个字段表同时承载多个互斥模式。
- 字段来源必须显式声明，例如 `user_text`、`upload`、`metadata`、`const`。
- artifact 字段只引用平台上传/产物摘要；不得让 LLM 生成 artifact 内容或本地路径。
- 缺参 interrupt 使用 schema 里的 `question`、`title`、`required` 和校验规则生成，恢复时会保留 `selected_schema_id`。

## 6. ResourceService 按需读取边界

`SkillResourceService` 只允许读取 contract 声明或 bundle 内允许的资源，并按 audience 执行策略：

- `main_agent` audience：仅用于答疑和公开 profile 扩展；可读 `references/` 等公开资料。
- `runtime` audience：供执行层读取实现所需 bundle 文件，但仍受路径、大小、secret 黑名单和审计限制。
- prompt-facing 路径禁止读取 `scripts/`、`runtime/`、`schemas/`、`native/`、`configs/`、`config.yaml`、`.env`、`.git` 以及包含 secret/token/credential 的路径。
- 所有读取都会做 bundle 边界校验、大小裁剪、可选脱敏，并发出不含原文内容的审计事件。

因此，写 Skill 时应把“用户可见说明、字段口径、示例”放在 `references/`，把实现代码和内部配置留在执行目录。

## 7. 外部 API 调用方式

### 7.1 自然语言规划

普通用户消息不需要指定 Skill：

```json
{
  "capability_id": null,
  "routing_mode": "auto",
  "content": "用我刚上传的材料表生成 3 个重复的 RCBD 设计"
}
```

后端 planner / replanner 会基于 public capability 和 contract registry 选择是否展开 `skill.*` 节点。

### 7.2 Slash / API 点名 Skill

点名 Skill 时仍提交主代理 capability，并把目标放在 soft binding metadata：

```json
{
  "capability_id": "main_agent.respond",
  "content": "/field-design hyb_check 怎么填？",
  "metadata": {
    "soft_skill_binding": {
      "capability_id": "skill.field_design",
      "command": "/field-design"
    }
  }
}
```

主代理先判断用户是在问用法、缺执行参数，还是明确执行。直接提交 `capability_id=skill.field_design` 会 fail closed，错误码为 `direct_skill_execution_disabled`。

### 7.3 缺参与恢复

执行型 Skill 缺少 schema 必需字段时会创建 open interrupt。v2 缺参 payload 使用 `schema_version=2`，并包含 `selected_schema_id`、`selected_entrypoint`、`invalid`、`resource_hints` 等执行恢复上下文。客户端只提交用户答案和上传选择；不得伪造内部 resume 字段、节点 ID 或用户身份。

## 8. 平台服务型 Skill

`platform_service` 用于把项目内受控业务服务包装成公开 `skill.*` capability。handler 与 service allowlist 由 API runtime 注册；外部调用方仍只能看到 Skill capability。

约束：

- handler key、handler module、服务列表和信任边界都写在 contract 中。
- 服务型 Skill 必须 fail closed：未注册 handler、越权 service、输出缺 required key 都应失败并产生诊断/审计。
- `python_subprocess` 不得绑定 DB、内部 LLM、secret、完整环境变量或任意平台服务。

## 9. Rust / native 接入限制

Rust 只能作为 Skill-owned runtime 的内部实现，不能成为新的公开 capability 类型，也不能绕过 service allowlist、artifact-event-audit contract 或 output contract。

允许形态：

- CI/部署阶段预构建的 PyO3 wheel，由 platform handler import。
- 固定 allowlist native binary，由 platform handler 以 JSON stdin/stdout 或 typed protocol 调用。
- 平台托管 sidecar service，由 allowlist client 访问。

禁止在 Skill 执行时运行 `cargo build`、`cargo run`、`rustc`、下载 crates 或执行任意未注册 binary。

## 10. 测试矩阵

推荐本地回归命令：

```bash
python -m pytest \
  tests/integrations/agent_skills/test_skill_contract_parser.py \
  tests/integrations/agent_skills/test_skill_capabilities.py \
  tests/integrations/agent_skills/test_skill_runtime_state.py \
  tests/integrations/agent_skills/test_input_schema_parser.py \
  tests/integrations/agent_skills/test_input_schema_selector.py \
  tests/integrations/agent_skills/test_input_schema_validation.py \
  tests/integrations/agent_skills/test_skill_resource_service.py \
  tests/integrations/agent_skills/test_public_skill_profile.py \
  tests/integrations/agent_skills/test_input_resolution_v2.py \
  tests/integrations/agent_skills/test_output_contract.py \
  tests/api/test_capabilities_list.py \
  tests/api/test_skill_capability_pool.py \
  tests/api/test_soft_skill_binding.py \
  tests/api/test_skill_slot_collection_v2.py
```

项目级 Skill 迁移回归：

```bash
python -m pytest \
  tests/integrations/agent_skills/test_project_skill_manifest_contract.py \
  tests/integrations/agent_skills/test_field_design_skill.py \
  tests/integrations/agent_skills/test_ocr_skill_script.py \
  tests/integrations/agent_skills/test_field_analysis_skill.py \
  tests/integrations/agent_skills/test_rice_genie_skill.py \
  tests/api/test_capabilities_list.py \
  tests/integrations/agent_skills/test_public_skill_profile.py
```

文档/API 回归：

```bash
python -m pytest tests/api/test_developer_docs.py
```
