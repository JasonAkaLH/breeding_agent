# 临时 Skill 状态行设计

日期：2026-05-18  
状态：已确认设计，待 implementation plan  
对应讨论：多 Skill 并行执行时，页面按 Skill 分行展示调用状态；Skill 进度不进入聊天气泡、不进入长期记忆，刷新/重新登录/历史会话不还原。

## 1. 背景与目标

当前系统已完成多 Skill DAG final-only 回答收口：多 Skill 执行后只由一个 final `main_agent.respond` 生成最终回答。下一步 UI 需要把 Skill 执行过程从聊天气泡正文中进一步剥离出来，以轻量状态行展示。

目标：

1. 多个 Skill 并行执行时，前端显示多行状态，每个 Skill 一行。
2. 单个 Skill 执行时也用同一套状态行，不特殊化为聊天气泡。
3. Skill 进度信息不进入 assistant 聊天气泡正文。
4. Skill 进度信息不进入 assistant history、conversation memory 或长期语义记忆。
5. 当前页面任务完成后保留这些灰色轻量记录。
6. 刷新页面、重新登录、重新打开历史会话时不还原这些记录。
7. 保持后端 schema、message 持久化、artifact 持久化不变。

## 2. 已确认产品决策

- 采用方案 A：前端临时状态层。
- 展示风格：气泡外轻量行，灰色、小字号、非 Markdown、非气泡背景。
- 展示粒度：只展示每个 Skill 的当前状态，不展示阶段日志。
- 生命周期：仅当前页面实时任务态保留；任务完成后本页仍可见；刷新/重登/历史会话不恢复。
- 主代理不作为 Skill 行展示；`main_agent.respond` 的生成状态继续用现有全局状态或最终回答流式内容体现。

## 3. 非目标

- 不新增后端持久化表或字段。
- 不把 Skill 状态写入 `Message.content`、assistant history 或 conversation memory。
- 不做历史任务的 Skill 进度 replay。
- 不展示每个 Skill 的多条阶段日志。
- 不把 Skill 状态做成 artifact。
- 不改变 DAG 调度语义；并行/串行仍由后端依赖关系决定。

## 4. 信息架构

当前页面中，一个实时 assistant 占位消息由两层组成：

```text
灰色 Skill 状态区（可选，多行）
SQLQuery：正在检索数据
RCBD：正在读取材料清单
文件生成：等待执行

最终回答气泡
已完成品种查询，并基于上传材料清单生成随机区组设计……
```

Skill 状态区是 UI 辅助信息，不属于聊天内容。最终回答气泡仍只来自 final assistant answer。

## 5. 前端临时状态模型

建议在 `frontend/src/domain/taskEvents.ts` 的 `TaskEventState` 中新增临时字段：

```ts
interface SkillStatusLine {
  key: string;
  nodeId: string | null;
  capabilityId: string;
  label: string;
  statusText: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
}

interface TaskEventState {
  // existing fields...
  skillStatuses: SkillStatusLine[];
}
```

说明：

- `key` 优先使用 `node_id`。
- 如果事件没有 `node_id`，用 `capability_id + skill_name` 兜底。
- `label` 复用当前 `capabilityLabel()` 规则，优先显示 `skill_name`。
- `skillStatuses` 只存在于前端运行态，不从历史 API 恢复。

## 6. 事件映射规则

### 6.1 `node.started`

当 `payload.capability_id` 是 `skill.*` 时：

```text
<SkillLabel>：正在处理
```

状态设为 `running`。

如果 `capability_id` 是 `main_agent.respond`，不新增 Skill 行。

### 6.2 `skill.progress`

按同一个 `node_id` / fallback key 更新对应 Skill 行：

```text
<SkillLabel>：<progress label>
```

例如：

```text
SQLQuery：正在检索数据
RCBD：正在读取材料清单
```

状态保持 `running`。

### 6.3 `node.completed`

当对应节点是 `skill.*` 时：

```text
<SkillLabel>：已完成
```

状态设为 `completed`。

### 6.4 `node.failed`

当对应节点是 `skill.*` 时：

```text
<SkillLabel>：失败
```

状态设为 `failed`。视觉上可使用灰红色或 muted red，但仍不使用气泡。

### 6.5 `task.completed`

任务完成时：

- 已完成行保持 `已完成`。
- 仍处于 `running` 的 Skill 行可保留最后状态，或统一收尾为 `已完成`；建议仅对仍 running 的行改为 `已完成`，避免停留在“正在……”造成误解。
- 不创建新的历史记录。

### 6.6 `main_agent.output_delta` / `main_agent.output_final`

不影响 Skill 状态行。它们只负责最终回答气泡内容与回答完成状态。

## 7. UI 渲染设计

### 7.1 位置

Skill 状态区渲染在当前 assistant 占位消息的气泡外、气泡上方或紧邻气泡区域：

- 如果 final answer 还没开始流式输出，只显示状态行和现有“等待/执行中”提示。
- 如果 final answer 已开始，状态行保留在最终回答气泡上方。
- 任务完成后，当前页面仍保留状态行。

### 7.2 样式

建议 class：

```css
.skill-status-lines {
  margin: 4px 0 8px;
  color: var(--muted-text);
  font-size: 12px;
  line-height: 1.7;
}

.skill-status-line {
  display: flex;
  gap: 4px;
  align-items: baseline;
}

.skill-status-line-failed {
  color: #a36b6b;
}
```

视觉约束：

- 灰色小字。
- 不使用聊天气泡背景。
- 不做 Markdown 渲染。
- 不抢最终回答视觉层级。

## 8. App 状态流

当前 `App.tsx` 在 `handleTaskEvent()` 中：

1. 调用 `applyTaskEvent(previous, event)` 得到 `next`。
2. 如果 `assistantText` 变化，更新 assistant message content。
3. 如果没有 answer text，用 `activityText` 更新 assistant message activity。

新增后：

- `applyTaskEvent()` 同时维护 `skillStatuses`。
- `ConversationMessage` 增加可选 `skillStatuses?: SkillStatusLine[]`。
- `handleTaskEvent()` 在 `skillStatuses` 变化时 patch 当前 assistant message。
- 历史消息加载不设置 `skillStatuses`，因此不会还原。

建议保持 `activityText` 作为全局任务提示，例如“正在执行能力 / 正在生成答案”。Skill 细节交给 `skillStatuses`。

## 9. 测试设计

### 9.1 Domain reducer 测试

文件：`frontend/src/domain/taskEvents.test.ts`

覆盖：

1. `node.started` with `skill.*` creates one status line.
2. two parallel `node.started` events create two independent lines.
3. `skill.progress` updates only matching Skill line.
4. `node.completed` marks the matching line completed.
5. `node.failed` marks the matching line failed.
6. `main_agent.respond` node does not create Skill line.
7. `task.completed` closes running Skill lines as completed.
8. duplicate events remain idempotent through existing `seenEventIds`.

### 9.2 App rendering 测试

文件：`frontend/src/App.test.tsx`

覆盖：

1. Skill progress renders as grey/light status text outside assistant bubble.
2. Skill progress does not become assistant message content.
3. Multiple parallel Skill lines render together.
4. Final answer still renders in assistant bubble.
5. Loaded historical conversation messages do not render old Skill status lines.

### 9.3 回归命令

```bash
cd frontend
npm test -- --run src/domain/taskEvents.test.ts src/App.test.tsx
npm run build
```

可选全量：

```bash
cd frontend
npm test -- --run
```

## 10. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| Skill 行与现有 `activityText` 重复 | 页面显示两套类似进度 | `activityText` 保持全局状态，Skill 行只显示每个 Skill 的当前状态。 |
| 事件缺少稳定 `node_id` | 多个同类 Skill 可能合并 | 优先使用 `node_id`；fallback key 仅用于缺失 `node_id` 的兼容场景。 |
| 完成后状态仍显示“正在……” | 用户误解任务未结束 | `task.completed` 将 running Skill 行收尾为 `已完成`。 |
| 历史会话不还原导致用户看不到执行过程 | 可追溯性降低 | 这是已确认产品决策；后端 task events/audit 仍保留执行记录。 |
| UI 占用空间过大 | 影响阅读最终回答 | 使用 A 方案轻量文本行，不使用卡片/时间线。 |

## 11. 验收标准

1. 并行 Skill 时页面显示多行 Skill 状态，每个 Skill 独立更新。
2. 单 Skill 时也显示同一风格的一行状态。
3. Skill 状态行不进入最终回答气泡正文。
4. Skill 状态行在当前页面任务完成后保留。
5. 刷新、重新登录或打开历史会话时不还原 Skill 状态行。
6. 最终回答仍只显示 final assistant answer。
7. 后端持久化 schema、assistant history、conversation memory 均不变。
8. 前端 reducer、App 渲染测试和 build 通过。

## 12. Spec 自检

- Placeholder scan：无 TBD / TODO / 未定项。
- Internal consistency：状态生命周期、持久化边界与 UI 行为一致。
- Scope check：仅前端临时展示层，不扩大到后端 replay 或持久化。
- Ambiguity check：已明确完成后保留当前页、刷新/历史不还原；只显示当前状态，不显示日志。
