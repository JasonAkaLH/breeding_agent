# 前端 Slash Skill Command 设计

日期：2026-05-20  
状态：已通过设计评审，待实施计划  
范围：前端业务对话台 slash command MVP，用于显式强制调用 public Skill。

## 背景与参考

当前系统已有 public Skill capability、`GET /api/v1/capabilities`、消息提交 `capability_id`、`WorkflowRouter` 对 `skill.*` 的强制路由、Skill progress / artifact / upload / interrupt 展示链路。本设计只补齐前端 slash command 入口与后端强制路由语义，不重做 Skill runtime。

外部参考：

- Claude Code 支持用户通过 `/skill-name` 直接调用 skill；skills 可由 `SKILL.md` 定义，并按需加载完整内容。
- OpenAI Codex Agent Skills 支持显式调用和隐式匹配；CLI / IDE 中可用 `/skills` 或 `$` mention 选择 skill，Codex 也可在任务匹配 description 时隐式使用 skill。

参考链接：

- https://code.claude.com/docs/en/slash-commands
- https://developers.openai.com/codex/skills

## 目标

在前端业务对话台加入 slash command MVP：用户输入 `/` 可选择并强制调用一个 public Skill；未使用 slash 时保持现有 LLM 自动路由。该能力要像 Claude Code 的 `/skill-name` 直接调用，同时保留本系统现有 Skill capability、SSE、artifact、upload、interrupt 机制。

## 非目标

- 不做完整通用 command framework。
- 不做 `/clear`、`/new`、`/help` 等内置命令。
- 不做富文本 token composer。
- 不在前端实现 Skill 参数校验。
- 不把 slash command 文本作为 prompt prefix 传给 LLM。

## 用户体验

### Skill Picker

- 用户在空输入或行首输入 `/` 时，输入框上方打开内联 Skill picker。
- Picker 数据来自页面加载后调用的 `GET /api/v1/capabilities`。
- 前端只展示 public Skill：优先以 `capability_id` 以 `skill.` 开头过滤；若 API 响应带 `kind/source`，可作为辅助过滤和展示字段。
- 列表项展示：
  - `/skill-name`
  - Skill 描述
  - 可选小字：`source_path` 或 capability id。
- 支持鼠标点击、`↑/↓` 高亮、`Enter` 选择、`Esc` 关闭。

### 已选 Skill 显示

- 选择 Skill 后，composer 显示独立徽标，例如 `Skill: data-lookup ×`。
- TextArea 只保留用户真实问题正文，不保留 slash 命令文本。
- 点击徽标 `×` 只取消当前 composer 的选择，回到自动规划模式。

### 快捷提交

- 用户可直接输入 `/skill-name 问题` 后按 Enter。
- 若 `/skill-name` 精确匹配一个 Skill，前端提交时：
  - `content` 去掉 slash 前缀，只保留问题正文。
  - `capability_id` 设置为对应 `skill.*`。
  - `routing_mode` 设置为 `force_capability`。

### 无匹配处理

- 输入 `/abc` 且无匹配时，picker 显示“未找到 Skill”。
- 前端阻止提交，不把 `/abc` 当普通消息发送，避免用户以为强制调用了 Skill，实际却进入自动路由。

## 路由优先级合同

1. **Slash command = 强制路由**
   - 只要用户显式选择或输入 `/skill-name`，本轮请求必须提交 `capability_id=skill.xxx`。
   - 后端不得再让 LLM Planner 改路由到其他 Skill。
   - Slash command 的含义是“用户明确指定这个 Skill”，不是“给 LLM 一个建议”。

2. **无 slash command = LLM 自主路由**
   - 用户普通输入时，继续走 `routing_mode=auto`。
   - LLM Planner / main agent 可根据用户意图自动选择一个或多个 Skill。
   - 继续保留当前自动 Skill matching 能力。

3. **Slash command 失败/无匹配 = 不提交**
   - `/abc` 无匹配时前端阻止提交，并提示“未找到 Skill”。

4. **新 slash command 覆盖旧 pending Skill context**
   - 用户下一轮明确选择另一个 slash command 时，新 slash 优先。

## 请求契约

普通消息：

```json
{
  "content": "帮我分析这个数据",
  "routing_mode": "auto",
  "capability_id": null,
  "metadata": {}
}
```

Slash 强制调用：

```json
{
  "content": "帮我基于这个 CSV 生成 RCBD 设计",
  "routing_mode": "force_capability",
  "capability_id": "skill.mini_breedstat_rcbd",
  "metadata": {
    "forced_by_slash_command": true,
    "slash_command": "/mini-breedstat-rcbd",
    "upload_ids": ["upload_123"]
  }
}
```

说明：

- `capability_id` 表示强制使用哪个 Skill。
- `metadata.upload_ids` 表示本条消息绑定的已上传文件。
- 两者必须同时保留，不能因为 slash command 改造而覆盖现有上传文件上下文。

## 前端设计

### 新增模块

#### `frontend/src/domain/slashCommands.ts`

职责：

- 从 capability list 派生 Skill command 列表。
- 过滤 public `skill.*` capability。
- 支持按 command name / capability id / description 做轻量 prefix 或 fuzzy 匹配。
- 解析输入形态：
  - `/data-lookup 查询xxx`
  - `/mini-breedstat-rcbd`
- 返回提交意图或阻断原因：
  - matched capability
  - cleaned content
  - slash command metadata
  - no-match blocked reason

#### `frontend/src/components/SlashCommandMenu.tsx`

职责：

- 展示候选 Skill。
- 展示当前高亮项和空结果。
- 支持鼠标点击选择。
- 由父组件传入 candidates、activeIndex、onSelect；组件自身不做业务提交。

### 修改模块

#### `frontend/src/api/client.ts`

- 增加 `listCapabilities()`，调用 `GET /api/v1/capabilities`。
- `submitMessage()` 支持显式 `capabilityId`。
- 若传入 `capabilityId`：
  - body 使用 `routing_mode=force_capability`。
  - body 使用该 `capability_id`。
- 若未传入 `capabilityId`：
  - 保持 `routing_mode=auto`。
  - `capability_id=null`。

#### `frontend/src/App.tsx`

- 登录/页面加载后拉取 capabilities。
- Composer 状态增加：
  - `skillCommands`
  - `slashMenuState`
  - `selectedSkillCommand`
- TextArea `onChange` 更新 slash menu 状态。
- TextArea `onPressEnter` 行为：
  - slash menu 打开时，`Enter` 优先选择候选，不发送。
  - 直接 slash 命令时，解析并提交强制 Skill。
  - 无匹配 slash 命令时阻止提交。
  - IME composition 确认仍沿用现有保护，不误触发提交。
- `handleSubmit()` 合并 metadata：
  - pending upload ids
  - slash command metadata
- 提交成功后清空 `selectedSkillCommand`。

## 后端设计

已有基础可复用：

- `GET /api/v1/capabilities`
- `SubmitMessageRequest.capability_id`
- `ApiRuntime._ensure_supported_capability`
- `WorkflowRouter` 对 `skill.*` 的分流
- `SkillWorkflowProvider` 将 `skill.*` 映射到强制 Skill 执行

### 强制路由语义加固

当请求满足 `routing_mode=force_capability` 且 `capability_id=skill.*`：

- 要求 `capability_id` 非空且存在。
- 保存到 task `requested_capability_id`。
- metadata 保留 slash 来源：
  - `forced_by_slash_command`
  - `slash_command`
- WorkflowRouter 必须进入 `SkillWorkflowProvider`，不让自动 Planner 改路由。

### 信息不足与 pending Skill context

当用户通过 slash command 强制调用 Skill，但输入信息不足：

- 如果该 Skill / 执行链路支持 `waiting_for_input` / interrupt：
  - 正常进入 Skill 流程，由后端发起补充信息请求。
  - 前端沿用现有 interrupt 输入条提交补充答案。

- 如果该 Skill 没有多轮补全能力：
  - 后端返回明确 assistant 消息，说明缺少哪些必要信息。
  - 这次待补全调用必须进入对话历史，而不是只做前端 toast。
  - 历史/状态中保留 lightweight pending context：
    - `capability_id`
    - `skill_name`
    - `original_user_message`
    - `missing_requirements`
    - `created_at`
    - `status=pending_user_input`

### 续接策略

MVP 采用后端历史感知续接：

1. 第一轮：用户 `/data-lookup` 但信息不足。
2. 后端保存任务/消息，并让 assistant 回复需要补充的数据库范围、查询对象或时间条件。
3. 第二轮：用户普通输入补充信息。
4. 后端检查最近 pending Skill context，优先把第二轮合并为同一 Skill 意图，继续调用原 Skill。
5. 成功、新 slash 覆盖、或明确取消后关闭 pending context。

前端不承担业务状态判断，只负责提交显式 slash intent 和展示后端回复。

## 错误处理

- `/abc` 无匹配：前端不提交，picker 显示“未找到 Skill”。
- `/skill-name` 有匹配但正文为空：允许提交；信息是否足够由后端/Skill 判断。
- capability list 加载失败：slash 功能降级不可用，普通聊天仍可用，显示轻量提示“Skill 列表加载失败，请刷新重试”。
- Skill 下线或 capability 不再存在：后端返回 400；前端显示错误并清空已选徽标，提示刷新列表。
- 当前会话 busy：沿用现有 409 / active task 禁用逻辑。
- 用户第二轮补充时又输入另一个 slash：新 slash 优先，覆盖 pending context。
- 用户明确取消：MVP 可先支持新 slash 覆盖和成功后清理；自然语言取消可后续增强。

## 测试设计

### 前端单元测试

`frontend/src/domain/slashCommands.test.ts`：

- 从 `/api/v1/capabilities` 响应中过滤出 Skill commands。
- `/data-lookup 查询xxx` 解析为 `{ capabilityId, content }`。
- `/unknown 查询xxx` 返回 blocked 状态。
- 已选 Skill 提交时正文不含 slash。

`frontend/src/App.test.tsx`：

- 输入 `/` 展示 Skill picker。
- 键盘上下/Enter 选择 Skill。
- 选择后展示 Skill 徽标。
- 点击 `×` 取消 Skill。
- 直接 `/skill args` Enter 提交 `capability_id=skill.xxx`。
- `/unknown args` 不调用 `submitMessage`。
- 上传文件 + slash 强制调用时，请求同时包含：
  - `capability_id=skill.xxx`
  - `routing_mode=force_capability`
  - `metadata.upload_ids`

### 前端 API 测试

`frontend/src/api/client.test.ts`：

- `submitMessage` 支持显式 `capabilityId`。
- slash 强制调用发送 `routing_mode=force_capability`。
- 无 capability 时仍发送 `routing_mode=auto`。

### 后端测试

- `POST /messages` 收到 `routing_mode=force_capability + capability_id=skill.*` 时保存 `requested_capability_id`。
- `WorkflowRouter` 强制进入 `SkillWorkflowProvider`。
- metadata 保存 `forced_by_slash_command` / `slash_command`。
- pending Skill context：
  - 信息不足时写入 assistant 缺失提示。
  - 下一轮普通输入复用 pending `capability_id`。
  - 新 slash 覆盖旧 pending。
  - 成功后清理 pending。

### 回归测试

- 无 slash 普通对话仍自动规划。
- 上传文件 + Skill 强制调用同时传递 `capability_id` 与 `metadata.upload_ids`。
- interrupt / waiting_for_input 现有流程不被破坏。
- Skill progress line / artifact rendering 不变。

## 实施切分建议

1. 前端 slash command 纯解析与 API client 测试。
2. 前端 UI picker + Skill 徽标 + submit contract。
3. 后端 force capability 语义加固。
4. 后端 pending Skill context。
5. 全链路 e2e / 回归。

## 设计自检

- Placeholder scan：无 TBD / TODO / 占位章节。
- Internal consistency：UX、请求契约、路由优先级与前后端职责一致；slash command 始终是结构化 `capability_id`，不是 prompt prefix。
- Scope check：MVP 聚焦 Skill 强制调用；pending Skill context 可作为独立后端实施切片，但仍服务同一用户流程。
- Ambiguity check：无匹配 slash 不提交；上传文件与 `capability_id` 必须同时传递；新 slash 覆盖旧 pending context。
