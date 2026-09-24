# main 前端 API 接入文档重写

日期：2026-09-24。用户已确认「前端接入手册 + 完整接口参考」方案并授权实施。代码基线为 main `101a9d2`，包含 27 个路径、33 个 HTTP 操作。

## 已确认内容结构

1. 快速开始：基础地址、username 登录、创建会话 ID、提交消息、SSE、最终消息与产物。
2. 通用约定：Bearer、身份隔离、ID、错误响应、202/204、CORS、重试与客户端状态恢复。
3. 聊天与任务：auto/hint/force_capability、模型配置、取消、历史恢复。
4. SSE 与续接：增量解析、历史去重、临时 reasoning、终态、补参及 MCP 审批。
5. 文件与 MCP：上传引用、表格 sheet 选择、下载、typed MCP 结果、Server/Grant 与调用控制。
6. 完整接口参考：逐接口参数、返回结构、示例、成功状态、业务错误和下一步。

## 表达与数据来源

- 中文任务导向指南，TypeScript/fetch 为主，curl 辅助。示例不自动访问业务 API。
- 单 HTML 页面可独立分发；嵌入由当前 app.openapi() 导出的合同快照，并提供显式的同源在线 OpenAPI 链接。
- 鉴权、业务错误、条件校验和 SSE 语义以路由、DTO validator、runtime 及官方前端客户端为准，不能从缺少 securitySchemes 的 OpenAPI 推断公开接口。
- 每个接口采用一致的用途、参数、返回、示例、错误和下一步顺序。字段结构由快照渲染，关键业务说明手写；维护脚本和测试检测合同漂移及接口遗漏。
- 支持当前文档入口识别基础地址、请求示例联动、复制、接口搜索、分类、目录深链接、折叠与窄屏显示。
- 提供独立 TypeScript 接入示例文件并嵌入文档，便于复制/下载与自动验证；不是独立发布的 SDK。

## 实施计划

1. 核对 33 个操作及 SSE/interrupt/MCP 来源，补充每个操作的说明、示例和状态码。
2. 编写并验证 TypeScript 请求及流式读取示例；重写 HTML 内容、样式和导航/搜索交互。
3. 加入合同快照同步脚本；更新文档回归为合同、示例和行为断言，去掉旧页面布局及内部实现文案的字符串绑定。
4. 运行文档后端回归、示例类型与流式解析测试、页面交互检查；浏览器验证直连/代理、桌面/窄屏。审查变更并保存本地提交，保留已有用户修改。

## 验收

33 个 HTTP 操作全部有接口参考；请求/响应示例符合实际 DTO；鉴权与业务错误被显式说明；SSE 示例覆盖跨 chunk 的 UTF-8、CRLF、多行 data、终态、断线及 abort；文档通过 /api-doc 和 /seedpilot/api-doc 访问时链接正确；不要求业务请求、数据库或外部模型服务参与页面验证。

## 写法参考

截至 2026-09-24 核对以下官方文档，仅借鉴组织方式，本项目协议由当前代码决定。

- GitHub REST 入门：认证、完整请求、响应和后续步骤。https://docs.github.com/en/rest/using-the-rest-api/getting-started-with-the-rest-api
- Stripe 接口参考：请求/响应示例、参数类型、条件及结果。https://docs.stripe.com/api/customers/create
- Claude Streaming：事件顺序、示例流、终态和错误恢复单独成章。https://platform.claude.com/docs/en/build-with-claude/streaming

## 自查

范围、来源、交付物与验证明确；无待定项。旧 compat 文档不作为 main 协议依据；不改变后端业务行为。主目录已有 AGENTS.md、CHANGELOG.md 和 docker/AGENTS.md 修改需原样保留。

## 实施与验证结果

- 已交付单文件 HTML、独立 TypeScript 示例、合同同步脚本及回归测试。33 个操作、60 个 schema 的合同快照与当前 app.openapi() 一致；JSON 请求/响应示例通过实际 DTO 校验。
- 15 项 Node/jsdom/示例执行测试、9 项后端文档/代理配置测试通过；TypeScript strict 类型检查、合同 `--check` 与 diff-check 通过。
- 浏览器检查 1280px 桌面与 390px 窄屏，无页面横向溢出；检索、深链接展开、代码语言切换及真实剪贴板内容验证通过。根路径、代理子路径和离线打开由页面回归覆盖。
- 无业务请求、数据库变更、镜像发布或服务器部署。用户原有三个文件的修改继续保留在工作区；仅提交本轮文档相关变更。
