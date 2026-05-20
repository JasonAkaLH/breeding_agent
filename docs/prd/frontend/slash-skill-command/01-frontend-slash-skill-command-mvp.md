# PRD 01: Frontend Slash Skill Command MVP

日期：2026-05-20  
状态：待实施  
范围：前端业务对话台 slash command MVP + 后端 force capability 基础语义加固。

## 1. 背景

当前系统已有 public Skill capability、`GET /api/v1/capabilities`、消息提交 `capability_id`、`WorkflowRouter` 对 `skill.*` 的强制路由、Skill progress / artifact / upload / interrupt 展示链路。本 PRD 只补齐用户显式 slash 调用 Skill 的入口和提交契约。

代码锚点：

- Composer state：`frontend/src/App.tsx:121-150`
- 当前提交逻辑：`frontend/src/App.tsx:472-502`
- TextArea / Enter handler：`frontend/src/App.tsx:969-990`
- Send button：`frontend/src/App.tsx:1027-1034`
- API client submit：`frontend/src/api/client.ts:31-40`、`frontend/src/api/client.ts:151-171`
- API request types：`frontend/src/api/types.ts:22-29`
- Backend capabilities API：`src/api/routes/capabilities.py:15-31`
- Backend submit DTO：`src/api/dto.py:9-15`
- Runtime capability validation / task save：`src/api/runtime.py:291-340`
- Runtime metadata merge / schedule：`src/api/runtime.py:351-377`
- Skill route：`src/orchestration/workflow_router.py:14-21`
- Skill provider：`src/orchestration/skill_workflow_provider.py:31-78`

## 2. 目标

用户可以通过 `/` picker 或 `/skill-name args` 显式强制调用一个 public Skill。无 slash 的普通输入保持现有 LLM 自动路由。

## 3. 非目标

- 不做 pending Skill context 跨轮续接；该能力由 PRD 02 负责。
- 不做通用 command framework。
- 不做 `/clear`、`/new`、`/help` 等内置命令。
- 不做富文本 token composer。
- 不在前端验证 Skill 业务参数是否完整。

## 4. 用户体验

### 4.1 Skill picker

- 用户在空输入或行首输入 `/` 时，输入框上方打开内联 Skill picker。
- Picker 数据来自 `GET /api/v1/capabilities`。
- 仅展示 active public `skill.*` capability。
- 列表项展示：
  - `/skill-name`
  - Skill 描述
  - 可选小字：`source_path` 或 capability id。
- 支持鼠标点击、`↑/↓` 高亮、`Enter` 选择、`Esc` 关闭。

### 4.2 已选 Skill badge

- 选择 Skill 后，composer 显示独立 badge，例如 `Skill: sql-query ×`。
- TextArea 只保留用户问题正文，不保留 slash 命令文本。
- 点击 `×` 只取消当前 composer 选择，下一次提交回到自动规划。

### 4.3 直接 slash 提交

- 用户可直接输入 `/skill-name args` 后按 Enter。
- 若 `/skill-name` 精确匹配一个 Skill，提交时：
  - `content=args`
  - `routing_mode=force_capability`
  - `capability_id=<matched skill.*>`
  - metadata 带 slash 来源。

### 4.4 无匹配

- `/unknown args` 不提交。
- UI 显示“未找到 Skill”。

## 5. 请求契约

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

关键约束：

- `capability_id` 表示强制使用哪个 Skill。
- `metadata.upload_ids` 表示本条消息绑定的已上传文件。
- slash metadata 与 upload metadata 必须 merge，不能互相覆盖。

## 6. 前端实现要求

### 6.1 API client

- `frontend/src/api/types.ts` 增加：
  - `CapabilityResponse`
  - `CapabilityListResponse`
- `frontend/src/api/client.ts` 增加：
  - `listCapabilities()`
  - `SubmitMessageInput.capabilityId?: string | null`
- `submitMessage()` 行为：
  - `capabilityId` 非空：发送 `routing_mode=force_capability` 与该 `capability_id`。
  - `capabilityId` 为空：保持 `routing_mode=auto` 与 `capability_id=null`。

### 6.2 Slash command domain module

新增 `frontend/src/domain/slashCommands.ts`：

- 从 capability list 派生 Skill command 列表。
- 过滤 active `skill.*` capability。
- command name 由 capability id 或 name 规范化而来，例如 `skill.mini_breedstat_rcbd` -> `/mini-breedstat-rcbd`。
- 解析 `/skill-name args`。
- 返回 matched submit intent 或 no-match blocked result。

### 6.3 UI component

新增 `frontend/src/components/SlashCommandMenu.tsx`：

- 展示候选 Skill。
- 展示 active item 与空结果。
- 由父组件控制 candidates、activeIndex、onSelect。

### 6.4 App integration

修改 `frontend/src/App.tsx`：

- 登录/页面加载后拉取 capabilities；失败不阻塞普通聊天。
- 增加状态：
  - `skillCommands`
  - `slashMenuState`
  - `selectedSkillCommand`
- TextArea `onChange` 同步 slash menu。
- TextArea `onPressEnter`：
  - 先保留 IME guard。
  - slash menu 打开时 `Enter` 选择候选，不发送。
  - unknown slash 阻止提交。
  - 普通输入保持现状。
- `handleSubmit()`：
  - selected badge 优先。
  - direct slash exact match 可直接提交。
  - unknown slash 阻止提交。
  - cleaned content 同时用于用户气泡和 API body。
  - metadata merge upload ids + slash fields。
- Send button disabled：
  - `/skill-name` 空 args 且匹配 Skill 时允许发送。
  - `/unknown` 禁止发送。

## 7. 后端实现要求

后端不新增 `/api/v1/skills`，复用 `GET /api/v1/capabilities`。

在 `src/api/runtime.py` 中加固 force capability：

- 当 `routing_mode=force_capability` 且 `capability_id` 为空时返回 400。
- 继续用 `_ensure_supported_capability()` 做 capability allowlist。
- `Task.routing_mode` 应保存 request routing mode，而不是总是默认 `AUTO`。
- 顶层 `capability_id=skill.*` 必须继续进入 `SkillWorkflowProvider`。
- 用户 metadata 不能单独伪造强制 Skill；必须有顶层 `capability_id`。

## 8. 验收标准

1. `/` 打开 picker，候选来自 `GET /api/v1/capabilities`。
2. picker 只展示 active `skill.*`。
3. 鼠标、`↑/↓`、`Enter`、`Esc` 均可用。
4. 选择后出现 Skill badge，正文不包含 slash 命令。
5. 点击 badge `×` 取消选择。
6. `/skill args` 快捷提交为 force capability。
7. `/unknown args` 不提交。
8. 上传文件 + slash 强制调用同时包含 `capability_id` 与 `metadata.upload_ids`。
9. 无 slash 普通对话仍为 auto routing。
10. IME composition Enter 不误提交。
11. 后端 `force_capability` 缺 capability 时 fail closed。

## 9. 测试计划

### 9.1 Frontend unit tests

- `frontend/src/domain/slashCommands.test.ts`
  - capability 过滤。
  - command name 规范化。
  - `/skill args` parse。
  - `/unknown args` blocked。
  - cleaned content 不含 slash。

- `frontend/src/api/client.test.ts`
  - `listCapabilities()` 调用正确 endpoint。
  - normal submit 保持 `routing_mode=auto`。
  - forced submit 发送 `routing_mode=force_capability` 与 `capability_id`。

- `frontend/src/App.test.tsx`
  - `/` 展示 picker。
  - keyboard select。
  - badge render / remove。
  - direct slash submit。
  - unknown slash blocked。
  - upload + slash metadata merge。
  - IME safety。

### 9.2 Backend tests

- `tests/api/test_skill_capability_pool.py` 或新增 `tests/api/test_slash_force_capability.py`
  - valid `skill.*` force request stores `requested_capability_id`。
  - missing capability returns 400。
  - unsupported capability returns 400。
  - metadata slash source survives into orchestration request where observable。

- `tests/orchestration/test_workflow_router.py`
  - existing top-level `skill.*` routes remain valid。

## 10. 验证命令

```bash
cd frontend
npm test -- --run
npm run build
```

```bash
conda run -n multi_agent python -m unittest tests.api.test_skill_capability_pool
conda run -n multi_agent python -m unittest tests.orchestration.test_workflow_router
```

如果新增 `tests/api/test_slash_force_capability.py`：

```bash
conda run -n multi_agent python -m unittest tests.api.test_slash_force_capability
```

## 11. 风险与缓解

- Slash metadata 覆盖 upload ids：用 App 测试强制断言两者同时存在。
- Enter 逻辑破坏 IME：先执行现有 IME guard，再处理 slash menu。
- 前端承担 Skill 参数校验：domain module 只做命令解析，不做业务校验。
- metadata 伪造强制 Skill：保留后端顶层 capability route 才能强制 Skill 的安全边界。

## 12. 后续

信息不足后的 pending context 续接由 `02-pending-skill-context-continuation.md` 实施。
