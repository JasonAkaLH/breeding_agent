# Skill 一等 Capability 能力池 PRD

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- **项目**：breeding_agent
- **范围**：后端 capability registry、Planner / Replanner 能力发现、Skill runtime 接入边界
- **文档状态**：已实现并更新（2026-05-13 起 数据查询 Skill 也作为 `skill.data_lookup` platform-service 进入能力池）
- **日期**：2026-05-09
- **关联 PRD**：
  - `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`
  - 对应可移除 Skill bundle 自带的边界文档
  - `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`

## 1. 背景与问题

当前系统已经具备两套“能力”概念：

1. **系统 Capability**：通过 `CapabilityRegistry` 注册，供 API、LLM Planner、Runtime Replanner 与调度器发现和校验。
2. **Agent Skill**：通过 `SkillCatalog` 从 `SKILL.md` 扫描，并在 `main_agent.respond` 执行阶段通过 `match_skills()` 进行文本匹配。

这导致一个关键断层：

- 数据查询 Skill 已作为项目级 public Skill 注册在 `CapabilityRegistry`，因此 Planner / Replanner / `/api/v1/capabilities` 可发现 `skill.data_lookup`。
- 已注册 Skill 只在 `main_agent.respond` 内部匹配，Planner / Replanner 的 public capability 搜索上下文不可见。
- 用户开启深度思考后，如果 LLM Planner 或 Runtime Replanner 需要“搜索系统能力”，它只能看到内置 public capability，看不到项目已经扫描到的 Skill。

当前代码事实：

- `CapabilityRegistry` 是 public capability 的注册与发现入口，`list(public_only=True)` 返回 Planner / API 可见能力：`src/orchestration/registry.py:7-37`。
- API runtime 当前注册主代理、项目级 public Skill（含 `skill.data_lookup`）与 MCP tool capability；数据查询 Skill domain stage 不再作为 capability 注册。
- `SkillCatalog` 在 runtime 中较晚扫描并只注入 `MainAgentExecutor`：`src/api/runtime.py:930-933`、`src/api/runtime.py:1028-1037`。
- LLM Planner 使用 `capability_registry.list(public_only=True)` 构造可用能力上下文：`src/orchestration/llm_workflow_provider.py:55-65`。
- Runtime Replanner 同样只格式化 `capability_registry.list(public_only=True)`：`src/capabilities/main_agent/runtime_replanner.py:262-303`。
- Planner prompt 当前仍写死“普通问题只使用 `main_agent.respond`”：`src/orchestration/planner_contract.py:55-65`。
- `/api/v1/capabilities` 只返回 public capability registry：`src/api/routes/capabilities.py:15-28`。
- Skill 匹配只发生在主代理 executor 内部：`src/capabilities/main_agent/executor.py:69-104`。
- Skill 自动脚本执行、参数解析与 artifact 产出已经在主代理路径内实现：`src/capabilities/main_agent/executor.py:222-280`。

## 2. 目标

### 2.1 产品目标

把符合条件的项目 Skill 提升为系统一等 public capability；数据查询 Skill 当前也以 `skill.data_lookup` project Skill 身份出现在同一个能力池中：

```text
CapabilityRegistry public pool
├── main_agent.respond
├── skill.data_lookup
├── skill.mini_breedstat_rcbd
├── skill.<project_skill_a>
└── skill.<project_skill_b>
```

用户开启深度思考或触发动态重规划时，LLM 可以在 public capability 列表中看到 Skill，并主动选择最合适的 Skill，而不是只能依赖 `main_agent.respond` 内部的关键词匹配。

### 2.2 工程目标

- Skill 进入 `CapabilityRegistry` 后仍复用现有受控 Skill runtime，不引入任意 shell、任意本地文件读写或插件 runtime。
- Planner / Replanner 只能选择系统公开的 `skill.*` capability，不能自行指定脚本路径、Skill 根目录或任意参数。
- Skill capability 必须保持与 数据查询 Skill 同级：对 Planner 公开的是 public capability contract，不暴露内部脚本、handler 或 domain stage 细节。
- `/api/v1/capabilities` 返回统一能力池，便于前端或诊断工具展示内置能力与项目 Skill。
- 保持 deterministic fallback：当 LLM Planner 禁用或失败时，现有 `main_agent.respond` 内部 Skill 匹配仍可兜底，不阻塞普通对话。

## 3. 非目标

- 不复刻完整 本地 runtime、plugin runtime、任意 shell runtime。
- 不允许 Planner 直接输出 Skill 脚本节点、脚本路径、临时文件路径或运行时命令。
- 不把 数据查询 Skill 内部节点、Skill 内部脚本、参数解析器作为 public capability 暴露。
- 不把所有用户级 `~/用户级本地 Skills 目录` 默认公开给业务用户和 Planner。
- 不在本 PRD 中设计 Skill 市场、权限后台、人工审核、版本发布流程。
- 不新增前端 Skill 专属业务卡片；前端 v1 仍通过统一 capability / artifact / 附件契约展示。

## 4. 用户故事

1. **深度思考选择 Skill**
   - 作为业务用户，我上传材料表并要求“做随机区组设计”时，开启深度思考后系统应能在能力池里发现 `skill.mini_breedstat_rcbd`，而不是只看到 `main_agent.respond` / `skill.data_lookup`。

2. **能力目录统一展示**
   - 作为前端或调试工具，我调用 `/api/v1/capabilities` 时，应能看到系统内置能力和已公开项目 Skill，且能区分 capability 来源。

3. **安全执行**
   - 作为平台维护者，我希望 Planner 只能选择已注册 Skill capability，由系统注入可信 `forced_skill` 信息，不能让 LLM 自由构造脚本路径或任意参数。

4. **保留旧路径**
   - 作为维护者，我希望尚未公开为 capability 的 Skill 仍可被 `main_agent.respond` 的内部 matcher 命中，避免一次改造破坏现有 Skill 使用方式。

## 5. 术语

- **Built-in capability**：系统代码静态定义的 public capability，如 `main_agent.respond`。数据查询 Skill 的公开入口是 public Skill capability `skill.data_lookup`。
- **Skill capability**：从符合条件的 `SkillManifest` 映射得到的 public capability，命名空间为 `skill.*`。
- **Skill macro**：Planner 可见的高层能力；执行前由系统转换为受控主代理 Skill 执行路径。
- **Forced Skill**：系统根据 `skill.*` capability_id 派生的可信 Skill 选择信号，注入 `main_agent.respond`，绕过或优先于文本 matcher。

## 6. 功能需求

### 6.1 Skill Capability 注册

运行时扫描 `SkillCatalog` 后，应把符合条件的 Skill 注册到 `CapabilityRegistry`。

#### 6.1.1 注册范围

v1 默认只把**项目级 Skill**公开为 capability：

- 默认允许：仓库根目录 `skill/**/SKILL.md`。
- 默认不公开：用户级 `~/用户级本地 Skills 目录下的 SKILL.md`。
- 如需公开用户级或外部 Skill，必须通过显式 runtime 配置或 manifest allowlist 开启。

原因：当前 runtime 默认扫描 `Path.cwd() / "skill"` 与 `Path.home() / ".[c]odex" / "skills"`；如果全部公开，会把个人本地工具 Skill 暴露给业务 Planner 与 API。

#### 6.1.2 Capability ID 规则

每个公开 Skill 必须映射到稳定 capability_id：

```text
skill.<normalized_skill_name>
```

推荐规则：

- 优先读取 manifest metadata 中的 `capability_id`，但必须满足：
  - 以 `skill.` 开头；
  - 只包含小写字母、数字、下划线、短横线和点；
  - 不与已有 capability 冲突。
- 未声明时，从 `SkillManifest.name` 派生：
  - 小写；
  - 非 `[a-z0-9]` 字符替换为 `_`；
  - 连续 `_` 合并；
  - 首尾 `_` 去除。
- 示例：
  - `mini-breedstat-rcbd` → `skill.mini_breedstat_rcbd`

冲突处理：

- 不允许覆盖内置 capability。
- 同一 capability_id 多个 Skill 冲突时，该 capability 不注册为 public，并记录启动诊断；原 Skill 可继续留在主代理内部 matcher 路径。

#### 6.1.3 Descriptor 内容

Skill capability descriptor 应至少包含：

- `capability_id`
- `name`：Skill manifest name 或展示名
- `description`：Skill manifest description，必要时附加短 triggers 摘要
- `version`：v1 可沿用 `"1"`；后续可从 manifest metadata 读取
- `public=True`
- 来源标识：建议扩展 descriptor 或 API DTO 增加 `kind="skill"` / `source="skill"`，便于前端与诊断区分

### 6.2 Planner / Replanner 可见性

LLM Planner 与 Runtime Replanner 必须从同一个 public capability pool 获取能力列表。

要求：

- `LLMWorkflowProvider` 的 `public_capabilities` 应包含已公开 Skill capability。
- `MainAgentRuntimeReplanner` 的 “可用 public capability” 区块应包含已公开 Skill capability。
- Planner prompt 文案必须从“普通问题只使用 `main_agent.respond`”改为：
  - 数据查询优先 `skill.data_lookup`；
  - 明确匹配公开 Skill 的任务优先选择对应 `skill.*`；
  - 兜底对话、解释、汇总使用 `main_agent.respond`。

### 6.3 Skill Capability 执行模型

v1 最初推荐采用 **Skill public capability → `main_agent.respond` forced skill** 模型；当前 generic `SkillExecutor` 已进一步支持直接执行与 platform-service。

高层 public plan 示例：

```json
{
  "nodes": [
    {
      "node_id": "design_rcbd",
      "capability_id": "skill.mini_breedstat_rcbd",
      "input_payload": {}
    }
  ]
}
```

系统展开 / 执行时应变成受控主代理节点：

```json
{
  "node_id": "design_rcbd:main_agent.respond",
  "capability_id": "main_agent.respond",
  "input_payload": {
    "user_message": "<effective_user_message>"
  },
  "metadata": {
    "forced_skill_capability_id": "skill.mini_breedstat_rcbd",
    "forced_skill_name": "mini-breedstat-rcbd"
  }
}
```

实现约束：

- `forced_skill_*` 只能由系统根据 registry 中的 descriptor / mapping 注入。
- Planner 的 `input_payload` v1 不允许携带任意 Skill 参数；业务参数仍由现有 Skill 参数 resolver 从用户问题、上传 artifact 与安全上下文中解析。
- `MainAgentRespondCapability` 收到 forced skill 后：
  - 必须先从 `SkillCatalog` 按可信 mapping 找到对应 manifest；
  - 必须将该 Skill 放入 `skill_matches`；
  - 可以保留 matcher 结果作为补充，但 forced skill 优先；
  - 未找到 forced skill 时应产生可审计失败或回退诊断，不得悄悄执行别的 Skill。

### 6.4 Answer-producing 能力尾节点规则

当前 LLM Planner 会在非 `main_agent.respond` 尾节点后自动补最终 `main_agent.respond`。Skill capability 本身也是面向用户的回答 / 文件生成能力，因此必须避免生成冗余二次主代理尾节点。

可接受实现路径二选一：

1. 在 `_ensure_final_main_agent` 中引入 answer-producing public capability 集合，包含 `main_agent.respond` 与公开 `skill.*`；
2. 或调整顺序：先展开 public capability，再基于展开后的 tail 判断是否需要最终 `main_agent.respond`。

验收要求：

- 单节点 `skill.mini_breedstat_rcbd` 不应自动产生两个连续 `main_agent.respond` 节点。
- `skill.data_lookup` 通过 `answer_mode=requires_finalizer` 保留“查询结果 → 主代理最终回答”的 finalizer 行为。

### 6.5 API 能力目录

`GET /api/v1/capabilities` 应返回统一 public capability pool。

建议响应字段扩展：

```json
{
  "capability_id": "skill.mini_breedstat_rcbd",
  "name": "mini-breedstat-rcbd",
  "description": "生成 RCBD 随机区组设计与 fieldbook。",
  "version": "1",
  "status": "active",
  "kind": "skill",
  "source": "skill"
}
```

兼容要求：

- 旧字段保持不变。
- 新字段应可选或默认，避免破坏旧前端。

### 6.6 Fallback 与兼容

- LLM Planner 禁用或失败时，`AutoWorkflowProvider` 仍可走现有 deterministic 路由：
  - SQL 查询走 数据查询 Skill；
  - 其他问题走 `main_agent.respond`；
  - `main_agent.respond` 内部继续执行 Skill matcher。
- 未公开为 public capability 的 Skill 仍可被内部 matcher 使用。
- 如果 forced skill 缺少必填参数，应复用现有 `skill.input_missing` 路径，不应退回空口承诺。

### 6.7 审计与可观测性

新增或复用以下审计事件：

- `skill.capability_registered`
  - `capability_id`
  - `skill_name`
  - `source_path` 的安全摘要或相对路径
- `skill.capability_registration_skipped`
  - `skill_name`
  - `reason`：invalid_id / duplicate / not_public_scope / disabled
- `skill.forced_selected`
  - `capability_id`
  - `skill_name`
  - `source`：planner / replanner / explicit_request
- 继续保留现有：
  - `skill.matched`
  - `skill.match_fallback`
  - `skill.input_resolved`
  - `skill.input_missing`
  - `skill.script_started`
  - `skill.script_completed`
  - `skill.output_file_collected`

审计中不得记录完整 prompt、完整上传文件内容、API key、base_url、数据库连接串或服务器真实文件路径。

## 7. 安全与权限边界

### 7.1 Skill 公开边界

公开为 capability 的 Skill 必须满足：

- manifest 可解析；
- name / capability_id 合法；
- 位于允许公开的 Skill root 或通过配置 allowlist；
- 不声明不受支持 runtime；
- 不要求任意 shell 或未受控外部执行。

### 7.2 LLM 权限边界

LLM 只能选择 `skill.*` capability_id，不能：

- 指定本地脚本路径；
- 指定 Skill 根目录；
- 修改 runtime；
- 传入未校验参数；
- 指定输出文件真实路径；
- 覆盖 `forced_skill_name`。

### 7.3 用户与文件边界

Skill 执行继续遵守现有上传与 artifact 规则：

- 上传原文只进入受控脚本 payload，不直接进入 LLM prompt。
- 输出文件由 managed artifact store 收集。
- 下载鉴权复用 task / conversation owner 边界。

## 8. 方案选型

### 8.1 方案 A：仅把 Skill 描述注入 Planner prompt

**做法**：Planner prompt 额外列出 Skill 描述，但不注册到 `CapabilityRegistry`。

**优点**：

- 改动小。

**缺点**：

- 不是同一个能力池；
- `/api/v1/capabilities` 仍不可见；
- validator / payload policy 无法统一约束；
- Replanner 与 Planner 容易再次分叉。

**结论**：不选。

### 8.2 方案 B：Skill 作为 direct executor capability

**做法**：`MainAgentExecutor` 或新增 executor 直接支持 `skill.*` capability_id。

**优点**：

- DAG 中执行节点与 public capability_id 一致；
- 不需要 macro expansion。

**缺点**：

- 需要动态扩展 execution instance supported capabilities；
- 需要让 executor 处理动态 capability_id；
- 容易把 Skill runtime 与主代理回答链路拆散，重复参数解析与 prompt 构造。

**结论**：可作为后续优化，不作为 v1 推荐。

### 8.3 方案 C：Skill public capability → main_agent forced skill（历史推荐）

**做法**：Skill 以 `skill.*` 注册 public capability；执行前由系统转换为带 forced skill metadata 的 `main_agent.respond`。

**优点**：

- 与 数据查询 Skill public Skill contract 的边界一致；
- Planner / Replanner / API 统一从 `CapabilityRegistry` 发现能力；
- 复用现有主代理 Skill matcher、参数解析、脚本 runner、artifact manager；
- 安全边界集中，LLM 只选择 capability_id。

**缺点**：

- 需要处理 answer-producing tail，避免补冗余主代理节点；
- 需要维护 skill capability_id ↔ manifest 的系统 mapping。

**结论**：v1 采用。

## 9. 验收标准

1. **能力池可见**
   - 给 runtime 注入包含 `mini-breedstat-rcbd` 的 `SkillCatalog` 后，`capability_registry.list(public_only=True)` 包含 `skill.mini_breedstat_rcbd`。
   - `/api/v1/capabilities` 返回 `main_agent.respond`、`skill.data_lookup` 和公开 Skill capability。

2. **Planner 可选择**
   - LLM Planner prompt 的 capability block 包含公开 Skill。
   - fake planner 输出 `skill.mini_breedstat_rcbd` 时，计划校验通过。

3. **安全展开**
   - `skill.mini_breedstat_rcbd` 被系统展开 / 转换为 `main_agent.respond` forced skill 路径。
   - Planner 不能通过 `input_payload` 传入 `forced_skill_name`、脚本路径或未允许字段。

4. **执行正确**
   - forced skill 命中对应 `SkillManifest`，并触发现有参数解析与脚本执行。
   - 缺少必填参数时返回结构化缺参结果并产生 `skill.input_missing` 审计。

5. **不破坏现有能力**
   - 数据查询 Skill 通过 `skill.data_lookup` platform-service handler 执行领域链路；能力池只展示该 Skill public capability。
   - 普通对话仍可使用 `main_agent.respond`。
   - 未公开 Skill 仍可通过主代理内部 matcher 兜底。

6. **防冲突**
   - 重名 / 非法 capability_id 的 Skill 不覆盖内置 capability，且有可诊断记录。

## 10. 测试计划

### 10.1 单元测试

- `tests/integrations/agent_skills`
  - Skill capability_id 派生规则。
  - manifest 显式 `capability_id` 校验。
  - duplicate / invalid id 跳过逻辑。

- `tests/orchestration`
  - `CapabilityRegistry` 注册 Skill descriptor。
  - Planner prompt 包含 Skill public capability。
  - Planner payload policy 拒绝 LLM 注入 forbidden Skill 字段。
  - Workflow expander / finalizer 不为 answer-producing Skill 添加重复主代理尾节点。

- `tests/capabilities/main_agent`
  - forced skill 优先于 matcher。
  - forced skill 未找到时安全失败或诊断 fallback。
  - forced skill 复用参数解析、缺参、脚本执行、artifact 收集。

### 10.2 API 测试

- `tests/api`
  - `/api/v1/capabilities` 包含公开 Skill。
  - 返回字段兼容旧 schema，并可区分 `kind=skill`。
  - user-level skill 未配置公开时不出现在 API。

### 10.3 e2e 测试

- 上传 CSV 后提交“做随机区组设计”，fake planner 选择 `skill.mini_breedstat_rcbd`，最终产出 Skill 脚本结果或缺参提示。
- 深度思考开启时，planner prompt 中可观察到 Skill capability。
- planner 禁用时，旧 `main_agent.respond` matcher 路径仍可命中同一 Skill。

### 10.4 回归命令

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
```

如涉及前端 capability 展示字段同步：

```bash
cd frontend
npm test -- --run
npm run build
```

## 11. 实施建议

### 11.1 推荐步骤

1. 新增 Skill capability 映射模块。
   - 输入：`SkillCatalog`、公开 root allowlist。
   - 输出：descriptor 列表、capability_id ↔ skill_name mapping、诊断。

2. 调整 API runtime 装配顺序。
   - 先 resolve `SkillCatalog`；
   - 注册内置 capability；
   - 注册公开 Skill capability；
   - 构建 macro provider / planner / replanner。

3. 新增 Skill macro provider 或等价转换层。
   - 将 `skill.*` public node 转为 `main_agent.respond` forced skill。
   - 设置安全 metadata。

4. 调整 Planner finalizer 规则。
   - 把公开 Skill capability 视为 answer-producing，避免重复 finalizer。
   - 保持 数据查询 Skill finalizer 行为。

5. 调整 `MainAgentRespondCapability`。
   - 支持 `forced_skill_capability_id` / `forced_skill_name`。
   - forced skill 优先，matcher 作为补充。
   - 审计 forced selection。

6. 调整 Planner / Replanner prompt。
   - 移除“普通问题只使用 main_agent.respond”的硬编码限制。
   - 增加“明确匹配公开 Skill 时选择对应 skill capability”的规则。

7. 扩展 `/api/v1/capabilities` DTO。
   - 增加可选 `kind` / `source`。
   - 保留旧字段。

8. 补齐分层测试与 e2e。

### 11.2 迁移策略

- 第一阶段：仅项目级 Skill 默认公开；用户级 Skill 保持内部 matcher。
- 第二阶段：为 manifest 增加显式 `public_capability` / `capability_id` 元数据。
- 第三阶段：增加后台或配置化 allowlist，用于生产环境发布控制。

## 12. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 用户级本地 Skill 被误公开 | 业务 Planner 看到个人工具，安全和体验不可控 | v1 只公开项目级 Skill；用户级需显式 allowlist |
| capability_id 冲突 | 覆盖内置能力或错误路由 | 冲突 Skill 不注册，记录诊断 |
| Planner 注入未校验参数 | 绕过 Skill 参数 resolver | v1 禁止 planner payload 传 Skill 参数；系统只注入 forced skill |
| 重复主代理尾节点 | 生成冗余回答或重复执行 Skill | 增加 answer-producing capability 规则 |
| Skill 描述过长污染 Planner prompt | token 增长，选择不稳定 | descriptor 描述裁剪，triggers 只保留短摘要 |
| Skill matcher 与 forced skill 冲突 | 可能执行多个 Skill | forced skill 优先；是否允许附加 matcher 需明确限制并测试 |

## 13. ADR

### Decision

采用 **Skill public capability** 方案，将符合条件的项目 Skill 注册为 `skill.*` public capability，并纳入 `CapabilityRegistry` 统一能力池；当前实现已扩展为 generic `SkillExecutor`，数据查询 Skill 走 `platform_service`。

### Drivers

1. Planner / Replanner 必须能发现已注册 Skill。
2. Skill 执行必须继续复用现有受控 runtime 与 artifact 安全边界。
3. 系统能力发现来源必须统一，避免 Planner、API、主代理 matcher 三套视图长期分叉。

### Alternatives considered

- 仅把 Skill 描述注入 prompt：改动小但不是真正同池，validator / API / Replanner 仍分叉。
- Skill direct executor capability：模型更直接，但 v1 改动更大，需要动态 executor / instance 支持。

### Why chosen

推荐方案与 数据查询 Skill public Skill contract 的边界一致，同时最大化复用已有 Skill 匹配、参数解析、执行与 artifact 管理能力。

### Consequences

- 需要扩展 runtime 装配顺序和 capability descriptor 映射。
- 需要调整 Planner finalizer 规则，避免 answer-producing Skill 被二次 finalizer。
- 需要增加 forced skill 安全 metadata 与审计事件。

### Follow-ups

- 后续可评估 Skill direct executor capability，以减少 macro 转换层。
- 后续可为生产环境增加 Skill 发布审核、版本治理和租户级可见性。
- 后续可在前端 capability 列表中区分内置能力与 Skill 能力。

