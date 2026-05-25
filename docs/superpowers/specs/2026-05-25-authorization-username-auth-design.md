# Authorization API Token 与 username 统一认证设计

日期：2026-05-25
范围：后端认证入口、API 参数契约、前端登录态、SSE 订阅鉴权、历史数据字段迁移、API 文档与回归测试

## 1. 目标

本设计将系统认证从 Cookie / 密码 / 验证码 / 多 API Token scope 模型一次性切换为内部系统使用的 username + 当前 API Token 映射模型。

核心目标：

1. `Authorization` header 只承载 API Token，格式为 `Bearer <api-token>`。
2. 除登录接口外，所有接口都不接收、不信任、不使用请求里的 `username` 或旧 `account_id` 来声明身份。
3. 后端每次通过 `Authorization` 查表定位当前请求的 `username`。
4. 同一个 `username` 只允许一个当前有效 token；新登录或刷新 token 会让旧 token 立即失效。
5. 登出只清空该 `username` 当前 token，不删除用户名和历史业务数据。
6. 系统中代表用户归属的字段统一命名为 `username`，不再用 `account_id` 表示用户名。
7. 不改变业务行为：conversation、task、upload、artifact、长期记忆、SSE 结果分发与资源隔离语义保持不变。

## 2. 非目标

本设计不引入外部账号体系、密码校验、验证码、SSO、OAuth、RBAC、scope 权限、refresh token 家族或多设备并存登录。

本设计也不重做长期记忆算法、不改变 LLM 编排、不改变 Skill 执行、不改变 artifact 文件存储策略。

## 3. 安全模型

这是内部用户识别模型。登录接口只接收 `username`，请求方只要提交一个合法用户名即可获得该用户名的新 API Token。

安全边界是：

- 网络和系统部署环境默认是内部可信边界。
- token 是后续请求的唯一认证凭证。
- token 不放 URL query，不放 Cookie，不放业务 body。
- 数据库不保存明文 token，只保存 token hash。
- 资源访问仍由 `token -> username` 与 `resource.username` 的服务端校验保证隔离。

## 4. 数据模型

新增或改造认证映射表，语义如下：

```text
auth_user_token
- username: string, primary key
- api_token_hash: string | null, unique when not null
- token_issued_at: datetime | null
- token_last_used_at: datetime | null
- created_at: datetime
- updated_at: datetime
```

约束：

- `api_token_hash = NULL` 表示该用户当前没有有效登录 token。
- 同一 `username` 只允许一个有效 token。
- 登录和刷新都会覆盖旧 token。
- 登出只把当前 token 置为 `NULL`，保留 username。
- 明文 token 只在登录或刷新响应中返回一次。

历史认证表如 `auth_session`、旧 `auth_api_token` scope/TTL/token 列表管理，可在本次切换中下线或迁移为上述单 token 模型，最终运行时不再依赖 Cookie session 或多 token scope 体系。

## 5. API 契约

### 5.1 登录

`POST /api/v1/auth/login`

请求：

```json
{ "username": "alice" }
```

行为：

- 如果 username 不存在，创建 username 记录。
- 如果 username 已存在，复用记录。
- 生成新的 API Token，覆盖该 username 的旧 token。
- 返回新 token。
- 不写 `Set-Cookie`。

响应：

```json
{
  "user": { "username": "alice" },
  "access_token": "maf_tok_xxx"
}
```

### 5.2 当前用户

`GET /api/v1/auth/me`

请求：

```http
Authorization: Bearer maf_tok_xxx
```

行为：

- 通过 token hash 查找 username。
- 找到当前有效 token 则返回用户。
- 找不到则返回 401。

响应：

```json
{ "user": { "username": "alice" } }
```

### 5.3 登出

`POST /api/v1/auth/logout`

请求：

```http
Authorization: Bearer maf_tok_xxx
```

行为：

- 用 Authorization 中的 token 定位 username。
- 将该 username 的当前 token 清空为 `NULL`。
- 不删除 username。
- 不删除 conversation、message、task、upload、artifact 或长期记忆。

响应：

```json
{ "logged_out": true }
```

### 5.4 刷新 token

`POST /api/v1/auth/refresh-token`

请求：

```http
Authorization: Bearer maf_tok_old
```

行为：

- 用旧 token 定位 username。
- 如果旧 token 无效，返回 401。
- 如果有效，生成新 token，替换旧 token。
- 旧 token 立即失效。

响应：

```json
{
  "user": { "username": "alice" },
  "access_token": "maf_tok_new"
}
```

### 5.5 业务接口

所有受保护业务接口只通过 `Authorization: Bearer <api-token>` 定位用户。除登录接口外，外部请求不得传 `username` 或 `account_id` 来声明身份。

请求参数位置规则：

- Header：只放认证和协议字段，例如 `Authorization`、`Accept`、`Content-Type`。
- JSON body：放 POST、PATCH、DELETE 的业务参数。
- Path：放 GET、SSE、download 的单一资源 ID。
- Query：放 GET 的筛选、分页、排序、状态等参数。
- Multipart：上传接口保留 `multipart/form-data`，文件放 file part，业务字段放 form field，Authorization 仍只放 token。

## 6. username 命名统一

系统中代表用户归属的字段统一为 `username`。

需要统一的范围：

- `Conversation.account_id` -> `Conversation.username`
- `ConversationMemorySummary.account_id` -> `ConversationMemorySummary.username`
- `UploadedFileRecord.account_id` -> `UploadedFileRecord.username`
- `ConversationSummaryResponse.account_id` -> `ConversationSummaryResponse.username`
- `SubmitMessageRequest.account_id` 删除
- `list_conversations_for_account(account_id)` -> `list_conversations_for_username(username)`
- API 文档和前端类型不再暴露 `account_id`

迁移约束：

- 旧数据库列 `account_id` 中的值迁移到新 `username` 列。
- 历史 conversation、memory summary、upload 归属不变，只改字段名。
- 迁移后同一 username 仍能看到原来的历史会话、消息、长期记忆、上传和 artifact。

## 7. 业务行为保持

本次切换只替换身份来源和字段命名，不改变业务行为。

### 7.1 conversation 与消息

- 创建消息时，conversation owner 来自 `Authorization -> username`。
- 请求 body 不再包含 `account_id`。
- 客户端恶意提交 `username` 或 `account_id` 不得影响 owner。
- 历史 conversation 仍按 username 过滤。
- 不属于当前 username 的 conversation 继续隐藏式 404。

### 7.2 task 与 SSE

- task 仍通过 `task -> conversation -> username` 判断归属。
- 订阅 SSE 前必须校验 `token -> username` 与 `task -> conversation -> username` 一致。
- SSE 事件流仍按 `task_id` 隔离，不做全局广播。
- 前端收到事件后可防御性校验 `event.task_id === subscribedTaskId`，不匹配则丢弃。
- token 被刷新、覆盖或登出后，旧 SSE 连接应停止，或在下一次读取/heartbeat 时失效。

### 7.3 upload 与 artifact

- 上传、列表、删除仍绑定当前 username 的 conversation。
- multipart 参数位置不为命名统一而额外改变。
- artifact download 仍先查 artifact，再通过 task/conversation 校验 username。
- A 用户不能下载或列出 B 用户的 artifact。

### 7.4 长期记忆

长期记忆核心逻辑不改。

关联链路保持为：

```text
Authorization token -> username -> conversation.username -> messages/tasks/memory_summary
```

token 刷新、登出、新设备登录只改变当前 token，不删除 username、conversation、messages 或 memory summary。

读取或构建长期记忆前必须校验 conversation 属于当前 username。

## 8. 前端设计

### 8.1 登录态保存

前端使用浏览器 `localStorage` 保存当前 token 和必要的当前用户展示信息。

- localStorage 是浏览器按 origin 隔离的本地存储。
- 它不会自动随请求发送。
- 前端每次请求都必须显式读取 token 并放入 `Authorization` header。
- 内部系统接受 localStorage 的 XSS 风险权衡。

### 8.2 登录流程

- 登录页只输入 username。
- 调用 `POST /api/v1/auth/login`。
- 成功后保存 `access_token`。
- 后续 API 请求全部加 `Authorization: Bearer <token>`。

### 8.3 刷新页面

- 从 localStorage 读取 token。
- 调用 `/api/v1/auth/me`。
- 200：恢复用户和历史会话。
- 401：清理 localStorage，回到登录页。

### 8.4 登出

- 调用 `/api/v1/auth/logout`。
- 成功后清理 localStorage、关闭 SSE、回到登录页。

### 8.5 token 刷新

- 调用 `/api/v1/auth/refresh-token`。
- 成功后用新 token 覆盖 localStorage。
- 401 则视为登录过期。

### 8.6 多设备

同一 username 在其他设备或窗口重新登录后，旧 token 立即失效。旧设备下一次 API 请求或 SSE 检查收到 401 后，应提示登录已过期或账号已在其他位置重新登录，并返回登录页。

## 9. 错误处理

认证错误：

- 缺少 Authorization：401 `Authentication required`
- Authorization 不是 Bearer：401 `Authentication required`
- token 查不到映射：401 `Authentication expired`
- token 已被刷新、覆盖或登出：401 `Authentication expired`
- refresh-token 使用无效 token：401 `Authentication expired`

资源归属错误：

- 当前 username 访问其他用户的 conversation/task/upload/artifact：404 `Unknown resource`
- 保持隐藏式 404，避免泄露资源是否存在。

## 10. 兼容边界

一次性硬切换后不保留：

- Cookie 登录态
- `__Host-maf_session` / `maf_session`
- 密码登录
- 验证码登录
- 独立注册流程
- 旧 API Token 多 token、scope、TTL、列表、创建、撤销体系
- 请求体里的 `account_id`
- API 响应里的 `account_id`

保留：

- 登录后进入业务对话台
- 历史会话恢复
- 多轮对话和长期记忆
- 文件上传、删除、使用
- task SSE 流式返回
- task cancel / interrupt answer
- artifact 展示和下载
- 同一 username 重新登录后旧 token 立即失效
- 登出后 username 和历史数据保留

## 11. 验收测试

### 11.1 后端

1. 登录接口：
   - 新 username 第一次登录创建记录并返回 token。
   - 已存在 username 登录覆盖旧 token。
   - 旧 token 立即失效。
   - 响应不包含 Set-Cookie。

2. 登出接口：
   - 有效 token 登出后 token 置空。
   - username 记录保留。
   - 旧 token 访问业务接口返回 401。
   - 再次登录同 username 能看到历史 conversation。

3. refresh-token：
   - 有效 token 可刷新。
   - 新 token 可用。
   - 旧 token 立即 401。
   - 无效 token 不能刷新。

4. 身份来源：
   - 除 login 外，任何接口不接收 username/account_id 作为身份。
   - submitMessage 不再需要或发送 account_id。
   - 恶意 body 里的 username/account_id 不改变 owner。

5. 资源隔离：
   - A 不能读 B conversation。
   - A 不能订阅 B task SSE。
   - A 不能下载 B artifact。
   - A 不能读取或删除 B upload。
   - A 不能触发 B conversation memory 构建。

6. 字段迁移：
   - 旧 `account_id` 数据迁移到 `username` 后历史 conversation 可见。
   - memory summary 迁移后仍参与长期记忆构建。
   - API 响应不再包含 `account_id`。

7. SSE 安全：
   - 建连前校验 token -> username 和 task -> conversation -> username。
   - 事件 task_id 与订阅 task 匹配。
   - token 被刷新/登出后，旧 SSE 连接停止或下一次读取失败。

### 11.2 前端

1. 登录页只输入 username。
2. 登录成功保存 token 到 localStorage。
3. 所有 API 请求加 Authorization。
4. 刷新页面通过 `/auth/me` 恢复登录。
5. 401 后清 token 并回登录页。
6. 登出清 token、关闭 SSE、回登录页。
7. refresh-token 成功后替换 localStorage token。
8. submitMessage 不再发送 account_id。
9. conversation 类型使用 username。
10. SSE event 防御性校验当前 task_id。

## 12. 停止条件

实现完成必须满足：

- 运行时代码不再读取 Cookie 作为认证入口。
- API 文档不再把 Cookie 作为认证方式。
- 外部业务请求中 `Authorization` 只承载 API Token。
- 除登录外，外部请求不再通过 username/account_id 声明身份。
- 内部和 API 中代表用户归属的字段统一为 `username`。
- 历史数据迁移不丢 conversation、message、memory summary、upload 和 artifact 归属。
- 后端、前端、API 文档相关回归全部通过。
