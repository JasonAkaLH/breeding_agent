# 首次空白对话 High 思考强度默认实施计划

依据：`2026-08-27-initial-high-reasoning-effort-default-design.md`
设计提交：`846045d`
状态：`ready_for_implementation`
目标分支：`main`

## 1. 完成声明

唯一目标是把全新 frontend App mount 的首次空白对话初始为：

```text
deepThinking = false
reasoningEffort = high
```

后续点击“新建对话”继续沿用当前 thinking/effort。完成时业务源码只修改
`frontend/src/App.tsx` 的一个 React state 初始值；豆包模型、`config.yaml`、backend、API、数据库、
Rust/Sidecar 与 `prod` 全部不变。

## 2. 范围保护

- 实施前确认工作树干净，当前分支为 `main`。
- 只读记录 `config.yaml` 的本地 digest 用于实施前后等价比较；不输出 digest 或文件内容。
- 不读取、修改、暂存或跟踪 `docker_cmd.md`。
- 不改 `resetConversationWorkspace`、`handleNewConversation`、`resolveEffectiveReasoningEffort`、
  API client 或任何 reasoning policy 配置。
- 无新增依赖、feature flag、持久化或迁移。

## 3. Checkpoint A：回归测试与单行行为修复

### 3.1 先锁定失败行为

修改 `frontend/src/App.test.tsx`：

1. 在普通聊天首次提交用例中，明确断言：
   - `deepThinking: false`；
   - `reasoningEffort: 'high'`。
2. 将现有“开启深度思考后提交”用例的 effort 预期从 `minimal` 改为 `high`；默认
   DeepSeek 的 enabled policy 支持 `high`，开关切换必须保留合法当前值。
3. 新增“新建对话沿用当前 thinking/effort”用例：
   - 进入首次空白对话；
   - 开启深度思考并选择一个非初始合法强度（例如 `max`）；
   - 点击“新建对话”；
   - 提交新消息，断言新 conversation ID 不同，但 `deepThinking=true` 与
     `reasoningEffort=max` 保持。
4. 保留豆包与 force-thinking fixture 用例，证明初始 `high` 仍会在不合法时回退，不移除或
   重写模型能力合同。

先运行精确用例，确认首次默认断言在旧代码上以 `minimal` 失败。

### 3.2 最小源码修改

修改 `frontend/src/App.tsx`：

```ts
const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>('high');
```

不改其他业务源码。

### 3.3 定向验证

```bash
cd frontend
npm test -- --run src/App.test.tsx -t "submits normal chat and renders transient Agent reasoning|submits deep thinking flag from the composer function menu|keeps current thinking and effort when starting a new conversation|falls back to the Doubao disabled default|forces deep thinking"
npm run typecheck
```

预期：定向用例、TypeScript 编译全部通过，没有生产文件额外 diff。

Checkpoint commit：`fix(frontend): default initial reasoning effort to high`

## 4. Checkpoint B：Frontend 全量与回归审计

执行：

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

全量用例后执行：

- `rg` 检查 `frontend/src/App.tsx` 只有一处初始 `reasoningEffort` 常量；
- `git diff --check`；
- 复查最终 diff，确认 `resetConversationWorkspace` 和 `handleNewConversation` 零变更；
- 比较实施前后 `config.yaml` digest 完全一致；
- 确认两个豆包 edition 仍在本地配置 API 中，但不输出配置内容或敏感字段。

若全量 App 测试默认 reporter 长时间无输出，使用 `--reporter=verbose` 等待完整结果；既有
`App.test.tsx` 约130项且用时约195秒，不得因暂无输出误判为死锁。

## 5. Checkpoint C：文档状态与本地真实验收

### 5.1 文档

- 把设计状态更新为 `implemented`；
- 把本计划状态更新为 `complete`，记录实际测试数、警告和浏览器验收；
- 同步 `docs/AGENTS.md` 与 `CHANGELOG.md`。

### 5.2 Frontend-only 重建

使用当前本地 Compose project 只重建/重建立 frontend：

- 不重建、重启或修改 backend/Runtime Sidecar；
- 保留当前数据卷和临时 Skill named-volume override；
- 记录新 frontend hashed asset，不输出敏感挂载或配置。

### 5.3 浏览器验收

在全新页面 mount 中：

1. 登录本地 SeedPilot，打开功能菜单；
2. 确认深度思考为关闭、思考强度为“高”；
3. 改为开启深度思考与另一个合法强度，点击“新建对话”；
4. 再次打开功能菜单，确认两个值仍为用户刚才的选择；
5. 确认前端模型下拉仍包含豆包 Seed 2.1 Pro/Turbo。

浏览器验收不提交真实 Provider Task；请求 metadata 由前端自动测试锁定，避免不必要的外部模型调用。

### 5.4 最终闭合

- frontend 容器 healthy，backend/Sidecar 容器身份与启动时间保持；
- 工作树干净；
- `config.yaml`、`docker_cmd.md` 存在且 Git-ignored/untracked；
- `prod` 未变更。

Final commit：`docs: close initial high reasoning effort rollout`

## 6. 回滚

1. 回退 Checkpoint A commit，把 React 初始 effort 恢复为 `minimal`；
2. 重建/重建立 frontend 容器；
3. 重跑定向 App 测试、typecheck 和 build；
4. backend、数卷、模型配置与历史 Task 无需回滚。
