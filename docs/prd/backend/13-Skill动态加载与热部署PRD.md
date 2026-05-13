# Skill 动态加载与热部署 PRD

- **项目**：multi_agent_framework
- **范围**：后端 SkillCatalog、Skill capability 注册、Planner / Replanner 能力发现、Skill macro 展开、主代理 Skill 执行快照
- **文档状态**：Phase 1 已实现（开发 / 内网可用热加载闭环）；生产级 package snapshot 待后续 Phase 3
- **日期**：2026-05-11
- **关联 PRD**：
  - `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`
  - 对应可移除 Skill bundle 自带的边界文档
  - `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`
  - `docs/prd/backend/12-Skill一等Capability能力池PRD.md`

## 1. 背景与问题

当前系统已经把符合公开范围的项目 Skill 注册为 `skill.*` public capability，使 LLM Planner、Runtime Replanner 与 `/api/v1/capabilities` 能在统一能力池中发现 Skill。

但当前加载模型仍是**启动期一次性扫描**：

- API runtime 在 `build_api_runtime()` 中解析 `skill_roots`，调用 `SkillCatalog.from_roots(...)`，并用扫描结果构造 Skill capability registry。
- `SkillCatalog` 是不可变快照，内部保存 `SkillManifest` tuple，没有 refresh / reload 方法。
- `SkillWorkflowProvider`、`LLMWorkflowProvider`、`AutoWorkflowProvider`、`MainAgentRuntimeReplanner` 与 `WorkflowExpander` 初始化时都会复制当时的 skill macro provider 映射。
- `MainAgentExecutor` 内部持有启动期 `SkillCatalog`；forced skill 最终仍要从这个 catalog 找到 manifest。
- 前端“新建对话”目前只是生成本地 `conversation_id` 并清空页面；后端真正创建 conversation 通常发生在首次提交消息或首次上传时。

因此，当前如果新增、删除或修改 `SKILL.md`，不重启服务 / 不重建 runtime，新的 Skill 元数据和 `skill.*` capability 不会自动进入后端运行时。

## 2. 目标

### 2.1 产品目标

支持在不重启后端服务的情况下，让新开启的聊天使用最新的公开项目 Skill：

```text
用户部署 / 修改 skill/**/SKILL.md
        ↓
后端在新聊天首次任务前刷新 Skill runtime bundle
        ↓
LLM Planner / Replanner / capabilities API 看到最新 skill.* 能力
        ↓
新聊天可选择并执行最新 Skill
```

### 2.2 工程目标

- 新 conversation 的首次任务规划前，应能加载最新公开项目 Skill。
- 已经开始执行的任务不应被后续 Skill 热更新破坏。
- Planner、Replanner、显式 `skill.*` 路由、macro 展开、主代理 forced skill 执行必须看到同一份 Skill 状态。
- Skill 刷新必须是原子操作：成功则整体生效，失败则保留上一份可用状态。
- `/api/v1/capabilities` 应反映当前已激活的 public capability pool。
- 热部署不得扩大 Skill 的安全边界：不能公开未授权 root，不能让 LLM 指定脚本路径，不能绕过既有 Skill 参数解析、artifact 管理和审计。

## 3. 非目标

- 不设计 Skill 市场、权限审批后台、多人发布流程或版本回滚 UI。
- 不把用户级 `~/.codex/skills` 默认公开给业务 Planner。
- 不让 Planner 直接输出脚本路径、shell 命令、本地文件路径或 arbitrary tool call。
- 不在每一轮普通消息、每一个 token 或每一次 SSE 事件里重复扫描 Skill。
- 不要求前端“点击新建对话”立即调用后端创建 conversation；后端应以首次任务提交作为可靠边界。
- 不改变 数据查询 Skill、主代理 LLM runtime、对话记忆与上传文件的既有契约。

## 4. 术语

- **Skill runtime bundle**：一次 Skill 刷新产生的完整运行时包，至少包含：`SkillCatalog`、公开 Skill capability descriptors、`capability_id -> skill_name` 映射、macro provider 映射、诊断信息、revision 与激活时间。
- **Skill bundle revision**：Skill runtime bundle 的单调版本号或内容哈希，用于标记任务规划时使用的 Skill 快照。
- **Conversation start refresh**：后端在一个新 conversation 的首次任务规划前触发 Skill 刷新检查。
- **Active bundle**：当前新任务默认使用的 Skill runtime bundle。
- **Retained bundle**：仍被运行中任务引用、暂时不能释放的历史 Skill runtime bundle。

## 5. 当前代码事实约束

本 PRD 的设计必须尊重以下当前实现事实：

1. `CapabilityRegistry` 是 public capability 发现入口，Planner prompt、API capability 目录和执行前校验都依赖它。
2. `skill.*` 当前由通用 Skill workflow/provider 展开；普通 instruction/script Skill 可走受控 Skill executor 或 forced skill 路径，`platform_service` Skill 由 runtime allowlisted handler 执行。
3. 调度器只选择支持真实执行 capability 的 instance；本地主代理 instance 支持的是 `main_agent.respond`，不是任意 `skill.*`。
4. `MainAgentExecutor` 最终要用 `SkillCatalog.get(forced_skill_name)` 找到 manifest，才能运行 forced skill。
5. `WorkflowExpander`、`LLMWorkflowProvider`、`AutoWorkflowProvider` 与 Runtime Replanner 内部都持有 macro provider 映射副本；刷新时不能只改一个外部 dict。
6. 前端新建空对话不等于后端 conversation 已创建；上传文件也可能先于第一条消息创建 conversation。

## 6. 功能需求

### 6.1 Skill runtime bundle 构建

系统应新增统一的 Skill runtime bundle 构建能力，负责从配置的 `skill_roots` 与 `public_skill_roots` 中扫描、校验并产出完整快照。

每个 bundle 至少包含：

- `revision`
- `created_at`
- `skill_roots`
- `public_skill_roots`
- `catalog`
- `skill_capabilities`
- `skill_name_by_capability_id`
- `source_path_by_capability_id`
- `diagnostics`
- 可用于 macro 展开的 provider / provider 映射

要求：

- revision 必须可审计，推荐使用单调序号 + 内容指纹。
- 构建失败不得污染当前 active bundle。
- 无公开 Skill 时也应形成合法 bundle，只是不包含 `skill.*` public capability。

### 6.2 刷新触发时机

v1 必须支持以下刷新触发：

1. **新聊天首次任务前刷新检查**
   - 当 `submit_message()` 发现当前 conversation 尚无历史任务时，必须在任务规划前执行一次 Skill 刷新检查。
   - 如果 conversation 已因上传文件提前创建，但尚未提交过任务，也仍视为新聊天首次任务。
   - 刷新检查可以基于文件指纹 / mtime / TTL 跳过实际重扫，但语义上必须保证“新聊天首次任务前有机会看到最新 Skill”。

2. **服务启动期初始化**
   - 保留当前启动期扫描能力，作为初始 active bundle。

3. **显式运维刷新入口（建议 v1.1）**
   - 可增加受保护的 `POST /api/v1/admin/skills/reload` 或内部 runtime 方法，用于手动触发刷新。
   - 该入口不是前端业务对话台 v1 的必要依赖。

不要求：

- 已存在 conversation 的每一轮消息都刷新。
- 前端仅点击“新建对话”但未提交任何任务时，后端立即刷新。

### 6.3 原子激活与失败回退

Skill 刷新必须以“构建新 bundle → 校验 → 原子激活”的顺序执行。

要求：

- 构建阶段在临时对象中完成，不修改 active registry / provider / executor。
- 校验通过后，以单个临界区切换 active bundle。
- 切换成功后，新任务统一使用新 bundle。
- 切换失败或扫描异常时，保留旧 active bundle，并记录审计事件。
- 如果系统尚无旧 active bundle，则至少保留内置 capability 可用，Skill capability 为空。

### 6.4 CapabilityRegistry 同步

刷新 Skill 时，`CapabilityRegistry` 必须与 active bundle 保持一致。

要求：

- 内置 capability 不受 Skill 刷新影响，包括 `main_agent.respond`；数据查询 Skill 当前通过项目级 `skill.data_lookup` 公开，随 Skill bundle 进入能力池，平台 handler 由 runtime allowlist 绑定。
- 新增公开 Skill 时，新 `skill.*` descriptor 应进入 public registry。
- 删除或取消公开 Skill 时，对应 `skill.*` descriptor 应从 public registry 中移除，不能继续出现在 Planner prompt 或 `/api/v1/capabilities`。
- 修改 `SKILL.md` description / capability_id / version 后，新 descriptor 应在下一次 bundle 激活后生效。
- registry 更新必须避免“Planner 看得到，但 macro provider / executor 看不到”的半刷新状态。

### 6.5 Planner / Replanner / Macro Provider 同步

刷新后，以下组件必须读取同一份 active bundle 或同一份 revision：

- `LLMWorkflowProvider`
- `MainAgentRuntimeReplanner`
- `AutoWorkflowProvider`
- `WorkflowRouter` 的 skill provider
- `WorkflowExpander` 使用的 macro providers
- `MainAgentExecutor` / `MainAgentRespondCapability`

允许的实现方式：

1. **动态 resolver 模型**：各组件不再复制静态 skill map，而是在构建 plan / expand / execute 时从 `SkillRuntimeState` 读取当前或指定 revision。
2. **整体替换模型**：刷新成功后重建相关 provider / replanner / executor，并在 runtime 临界区内整体替换引用。

禁止：

- 只更新 `CapabilityRegistry`，不更新 skill provider / executor。
- 只更新 `SkillCatalog`，不更新 Planner 可见 capability。
- 让不同组件在同一任务中混用不同 Skill 状态。

### 6.6 任务级 Skill 快照与运行中任务保护

每个任务在规划时必须记录使用的 `skill_bundle_revision`。

要求：

- Planner prompt、macro 展开和 forced skill 执行应使用同一 revision。
- 运行中任务引用的 bundle 必须被保留，直到任务结束或取消。
- 新 bundle 激活后，不应改变已建 DAG 中 forced skill 的 manifest 解析结果。
- 如果历史 bundle 已被清理，而任务恢复需要它，系统必须产生明确失败诊断，不能静默改用最新 bundle。

脚本文件一致性要求分两级：

- **v1 最低要求**：Skill 元数据、capability 映射与 forced skill manifest 必须按 revision 稳定。
- **生产热部署要求**：公开 Skill 的可执行脚本与必要资源也应进入 revision package snapshot，避免 `SKILL.md` 已锁定但脚本文件被覆盖导致同一任务前后执行不同代码。

若 v1 暂不实现 package snapshot，必须在审计中标记 `script_package_snapshot=false`，不得宣称具备严格可复现执行。

### 6.7 Skill package snapshot（生产热部署口径）

为了把“热加载”升级为“生产可解释的热部署”，系统应支持 public Skill package snapshot。

要求：

- 激活 bundle 前，将公开 Skill 的 `SKILL.md`、声明脚本和必要包内资源复制到 runtime 管理目录，例如 `runtime/skill_bundles/<revision>/...`。
- bundle 中的 `SkillManifest.source_path` 指向 snapshot 内的 `SKILL.md`。
- 审计中保留原始 root 相对路径摘要与 snapshot revision，不记录敏感绝对路径。
- 不跟随 symlink，不允许 `..` 逃逸 package root。
- snapshot 清理必须等待无运行中任务引用该 revision。

### 6.8 `/api/v1/capabilities` 行为

`GET /api/v1/capabilities` 应返回当前 active bundle 对应的 public capability pool。

建议扩展响应 metadata：

```json
{
  "capability_id": "skill.mini_breedstat_rcbd",
  "name": "mini-breedstat-rcbd",
  "description": "生成 RCBD 随机区组设计与 fieldbook。",
  "version": "1",
  "status": "active",
  "kind": "skill",
  "source": "skill",
  "skill_bundle_revision": "skillrev-000012"
}
```

兼容要求：

- 旧字段保持不变。
- `skill_bundle_revision` 可以作为可选字段或整体响应 metadata，不应破坏现有前端。

### 6.9 安全边界

Skill 热部署不得改变既有安全边界：

- 默认只公开仓库项目级 `skill/` 下的 Skill。
- 用户级 `~/.codex/skills` 默认只可被主代理内部 matcher 使用，不进入业务 public capability pool。
- Planner 只能选择已注册 `skill.*` capability，不能指定脚本路径、运行命令、root 路径或 forced skill metadata。
- 用户请求 metadata 中伪造的 `forced_skill_*` 字段仍必须被执行层剥离。
- 公开 Skill 脚本 runtime 仍只允许当前受支持 runtime；不因热部署扩大为任意 shell。
- 刷新审计不得记录 API key、数据库连接串、完整本地绝对路径或完整 prompt。

### 6.10 性能与并发

- 新聊天首次任务前的刷新检查必须有轻量跳过路径，避免每次都全量扫描大型 Skill 目录。
- 推荐维护 Skill roots 文件指纹，至少包含 `SKILL.md` mtime / size / hash；生产可扩展到声明脚本与资源文件。
- 同一时间只能有一个 Skill refresh 激活流程；并发新聊天应共享同一次刷新结果。
- 刷新不应阻塞已有任务继续执行。
- 如果刷新耗时过长，应让新任务等待刷新完成或按配置使用旧 bundle，并产生可观测诊断。

### 6.11 审计与可观测性

新增审计事件：

- `skill.bundle_refresh_started`
  - `reason`: `startup` / `conversation_start` / `manual`
  - `previous_revision`
- `skill.bundle_refresh_skipped`
  - `reason`: `fingerprint_unchanged` / `refresh_in_progress` / `disabled`
  - `active_revision`
- `skill.bundle_refresh_completed`
  - `previous_revision`
  - `active_revision`
  - `registered_count`
  - `skipped_count`
  - `duration_ms`
  - `script_package_snapshot`
- `skill.bundle_refresh_failed`
  - `previous_revision`
  - `error_type`
  - `fallback_revision`
- `skill.bundle_retained`
  - `revision`
  - `active_task_count`
- `skill.bundle_released`
  - `revision`

继续保留并复用：

- `skill.capability_registered`
- `skill.capability_registration_skipped`
- `skill.forced_selected`
- `skill.forced_missing`

## 7. 验收标准

### 7.1 新增 Skill 热加载

前置：后端 runtime 已启动，初始 public capability pool 不包含 `skill.demo_hot_reload`。

步骤：

1. 在项目 `skill/` 下新增一个合法 `SKILL.md`。
2. 不重启服务。
3. 新建 conversation 并提交第一条消息。
4. Planner prompt / `/api/v1/capabilities` 能看到 `skill.demo_hot_reload`。
5. fake planner 选择该 capability 时，任务可展开为 `main_agent.respond` forced skill，并产生 `skill.forced_selected`。

验收：任务完成或进入既有 Skill 参数缺失流程，但不得因为找不到 capability / provider / manifest 而失败。

### 7.2 修改 Skill 元数据

1. 修改已公开 Skill 的 description 或 version。
2. 新 conversation 首次任务前触发刷新。
3. `/api/v1/capabilities` 返回新 description / version。
4. Planner prompt 使用新 descriptor。

### 7.3 删除 Skill

1. 删除或移出某个公开 Skill。
2. 新 conversation 首次任务前触发刷新。
3. `/api/v1/capabilities` 不再返回该 `skill.*` capability。
4. Planner 如果输出已删除 capability，应被 validator 或 provider 拒绝，并产生可诊断失败，而不是落到错误 Skill。

### 7.4 运行中任务保护

1. 任务 A 使用 revision N 的 Skill 开始执行。
2. 任务 A 未结束时，部署 revision N+1。
3. 新任务 B 使用 revision N+1。
4. 任务 A 继续使用 revision N 对应 manifest / snapshot。
5. revision N 在任务 A 结束后才允许释放。

### 7.5 刷新失败回退

1. 写入非法 `SKILL.md` 或制造解析错误。
2. 新 conversation 触发刷新。
3. 系统记录 `skill.bundle_refresh_failed` 或 registration skipped 诊断。
4. 旧 active bundle 仍可用于新任务，内置 capability 不受影响。

## 8. 测试计划

### 8.1 单元测试

- `SkillCatalog` / Skill bundle builder：新增、修改、删除、非法 YAML、重复 capability_id、public root 过滤。
- Capability registry 同步：新增 skill 注册、删除 skill 注销、内置 capability 保留。
- Skill provider 同步：刷新后显式 `skill.*` 路由能找到新 mapping。
- Revision 选择：任务 metadata 中的 `skill_bundle_revision` 能定位对应 retained bundle。

### 8.2 编排测试

- LLM Planner 在新 bundle 激活后 prompt 包含新 Skill。
- Runtime Replanner 在新 bundle 激活后 prompt 包含新 Skill。
- `skill.*` macro 展开后 forced skill metadata 使用同一 revision。
- 已删除 Skill 不再能通过新 Planner 路径进入 DAG。

### 8.3 API 测试

- runtime 启动后新增 Skill，不重启，提交新 conversation 首条消息后能力生效。
- `/api/v1/capabilities` 返回 active bundle 能力列表。
- 上传先创建 conversation、再提交第一条消息时，仍按“无历史任务”触发刷新检查。
- 刷新失败时 API 仍能提交普通 `main_agent.respond` 消息。

### 8.4 回归测试

至少运行：

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
```

若改动前端 capability 展示，再补充：

```bash
cd frontend
npm test -- --run
npm run build
```

## 9. 分阶段交付建议

### Phase 1：开发 / 内网可用热加载闭环

- 新增 Skill runtime bundle builder。
- 新聊天首次任务前刷新检查。
- 原子更新 registry、skill provider、macro providers 与主代理 catalog。
- 任务规划 metadata 记录 `skill_bundle_revision`。
- retained bundle 生命周期与任务状态绑定，至少保证运行中任务继续使用规划时的 manifest / capability 映射。
- forced skill 执行按 revision 解析 manifest。
- 支持新增 / 修改 / 删除 `SKILL.md` 后对新 conversation 生效。
- 记录 refresh 审计事件。
- 覆盖运行中任务跨热加载测试。

### Phase 2：运维与诊断增强

- 增加受保护的显式 reload 入口。
- `/api/v1/capabilities` 暴露 active skill bundle revision 或整体响应 metadata。
- 增加 refresh 指纹、跳过原因、耗时与 retained bundle 数量的诊断查询。
- 完善 refresh in progress 的并发等待 / 复用策略。

### Phase 3：生产级 package snapshot

- 将 public Skill package 复制到 revision snapshot。
- 脚本执行改用 snapshot 路径。
- 增加 snapshot 清理与审计。
- 明确 `script_package_snapshot=true` 后才宣称生产级热部署。

## 10. PRD 自检结论

本 PRD 对当前系统的判断基于以下已核对事实：

- 当前 Skill 扫描发生在 API runtime 构建阶段。
- 当前 `SkillCatalog` 是不可变扫描快照。
- 当前 `skill.*` 通过 macro provider 展开为 `main_agent.respond` forced skill。
- 当前 Planner 虽然按请求动态读取 `CapabilityRegistry`，但 macro providers 与主代理 SkillCatalog 仍存在启动期快照。
- 当前前端新建对话不是可靠后端刷新触发点，后端首次任务提交才是可靠边界。

因此，本 PRD 不把热部署简化为“重扫目录”，而是要求以 Skill runtime bundle 为单位同步刷新发现、规划、展开和执行链路。该设计覆盖新增、修改、删除、并发任务、刷新失败和安全边界，作为后续实现输入具备完整闭环。

## 11. Phase 1 实现记录（2026-05-11）

- 已新增 `SkillRuntimeState` / `SkillRuntimeBundle`，统一管理 active / retained Skill bundle、内容指纹、revision、catalog 与 public `skill.*` capability 映射。
- API runtime 已在新 conversation 首次任务提交前执行刷新检查；上传先创建 conversation 但尚无任务时，也按首次任务边界刷新。
- 刷新成功后同步更新 `CapabilityRegistry`；显式 `skill.*` 路由校验发生在刷新之后。
- `WorkflowExpander`、LLM Planner、AutoWorkflow、Runtime Replanner、`SkillWorkflowProvider` 与 `MainAgentExecutor` 已通过动态 resolver / `skill_bundle_revision` 使用同一 Skill 快照。
- 运行中任务会 retain 规划时的 revision，任务终态后释放；刷新失败时保留上一份 active bundle 并记录失败诊断。
- 当前 Phase 1 明确标记 `script_package_snapshot=false`，只保证 Skill 元数据 / capability 映射 / manifest 按 revision 稳定；脚本与资源 package snapshot 仍按 Phase 3 处理。
