# API Doc HTML UI Redesign 设计

- 日期：2026-05-20
- 状态：已通过设计讨论，待实现计划
- 范围：根目录 `api-doc.html` 的单文件 UI / 交互重设计
- 决策：采用「单页 API Console 文档」方案，保留目录导航，不拆成多个 HTML

## 背景

当前仓库根目录已有第一版 `api-doc.html`，它覆盖 FastAPI OpenAPI 中的 20 个 REST / SSE path，并已通过 HTML parser 与 OpenAPI path 覆盖校验。第一版以静态文档为主，适合作为 API 信息基线；本次目标是让开发同事更容易查找、理解、复制并接入 API。

项目前端已有暖米色、农业绿色、玻璃卡片等视觉基调（见 `frontend/src/styles.css`）。API 文档应沿用这套产品气质，但仍保持开发文档的高可读性、高对比代码块和清晰接口结构。

## 目标

1. 继续保持根目录单文件交付：`api-doc.html`。
2. 让文档「像文档站一样好找，像 API Console 一样好复制」。
3. 提供友好的目录导航、接口检索、分类过滤、展开/收起与复制代码体验。
4. 保持离线可读、无外部依赖、无构建步骤。
5. 不改变后端 API 行为、DTO、路由或认证语义。

## 非目标

- 不拆分为多个 HTML 文件。
- 不实现 Swagger-like 的真实 Try-It 请求执行器。
- 不引入外部 CDN、前端框架、构建工具或新依赖。
- 不修改后端 route、DTO、SSE 协议或前端 API client。
- 不把本次文档改造扩展为 OpenAPI 自动生成系统。

## 选定方案

采用方案 2：**单页 API Console 文档**。

桌面端呈现为三层信息结构：

1. **左侧目录导航**：快速跳转 Guides、Endpoint 分组、Schemas、Errors。
2. **中间主内容区**：Hero、Quickstart、接入指南、Endpoint cards、SSE 专区、Schema 与错误说明。
3. **右侧 / 卡片内工具区**：按接口提供 curl、fetch、EventSource 等代码示例与复制按钮。

实现时可将右侧工具区内嵌到 endpoint card 中，避免单文件结构过重；只要桌面体验保留清晰的导航与工具分区即可。

## 信息架构

### 顶部 Hero

- 产品名 / 文档名：Multi-Agent Framework REST API。
- 一句话说明：用于创建会话、提交任务、接收 SSE 事件、上传文件、读取 artifact。
- Base URL 示例：`http://localhost:8000`。
- 认证提示：`Authorization: Bearer <token>`；本地开发可根据运行配置选择是否启用。
- 快速入口：Quickstart、Endpoints、SSE、Schemas。

### 左侧导航

导航分为两类：

- Guides：Quickstart、Authentication、SSE Streaming、Artifacts、Errors。
- Endpoints：按 Auth、Conversations、Uploads、Tasks、SSE、Artifacts、Health / Runtime 分组。

要求：

- 点击导航使用 anchor 跳转。
- 当前分组名称应清晰，不依赖开发者记住 path。
- 移动端不强求固定侧栏，可降级为顶部横向 chips 或普通目录块。

### 主内容区

主内容按以下顺序组织：

1. Quickstart：最短接入步骤。
2. API lifecycle：从创建 conversation 到提交 message、监听 task events、读取结果。
3. Endpoint cards：REST / SSE 接口详情。
4. Schemas：核心请求、响应和事件字段。
5. Errors：HTTP status、错误体、排查建议。
6. Source map：说明文档依据的后端/前端文件位置。

## Endpoint Card 设计

每个 endpoint card 使用统一结构：

- Header：HTTP method badge、path、短标题、分类标签。
- When to use：该接口什么时候调用。
- Request：path/query/header/body 参数。
- Response：关键字段与示例 JSON。
- Errors：常见错误状态与原因。
- Next：下一步通常调用哪个接口或监听哪个 SSE。
- Code examples：curl、fetch；SSE 接口使用 EventSource 示例。
- Copy actions：复制 path、curl、fetch / EventSource。

方法 badge 色彩：

- `GET`：蓝色。
- `POST`：绿色。
- `PATCH`：紫色。
- `DELETE`：红色。

## 交互设计

所有交互都用原生 HTML / CSS / JavaScript 实现，且作为 progressive enhancement：禁用 JS 时文档主体仍可阅读。

### 搜索

- 搜索范围：path、method、标题、分类、关键描述。
- 无结果时显示明确的 empty state。
- 搜索不应删除 DOM，只控制 card 显示状态，保证锚点结构稳定。

### 分类过滤

提供 All / Auth / Conversations / Uploads / Tasks / SSE / Artifacts / Schemas 等 filters。

- 默认 All。
- Filter 与搜索可组合。
- 被隐藏的 card 不影响其他章节阅读。

### 展开 / 收起

- Endpoint card 默认展示摘要与关键示例，详细 schema 可放在可展开区域。
- 提供 Expand all / Collapse all。
- 使用原生 `<details>` / `<summary>` 优先，降低 JS 复杂度。

### 复制

- 每个代码块或代码示例提供 Copy 按钮。
- 复制成功显示短暂反馈，例如 `Copied`。
- 优先使用 `navigator.clipboard.writeText`，必要时用隐藏 textarea fallback。

## 视觉设计

整体使用「农科产品感 + 开发者控制台」混合风格：

- 背景：暖米色 / 浅绿色渐变。
- 重点色：农业绿色、深墨绿。
- 卡片：柔和阴影、圆角、轻玻璃质感。
- 代码块：深色高对比背景，等宽字体，支持横向滚动。
- 信息密度：接口卡片需要紧凑但不拥挤，优先让 path、method、示例可被快速扫读。

设计不得牺牲开发文档的可读性；装饰元素必须服务于导航、分组和复制效率。

## 响应式与可访问性

- 桌面端：左侧 sticky nav + 中央内容 + 工具区 / 卡片内工具区。
- 平板端：导航可缩窄或转为顶部 sticky 区。
- 移动端：单列布局，目录在顶部，代码块横向滚动。
- 所有按钮应有可见 focus 状态。
- 颜色对比应满足普通阅读需求，不能只靠颜色表达 method / 状态。
- Anchor 跳转需考虑 sticky header 偏移，避免标题被遮挡。

## 数据与来源约束

实现必须基于当前仓库事实，不得编造接口。

主要来源：

- `src/api/routes/*.py`
- `src/api/dto.py`
- `frontend/src/api/client.ts`
- `frontend/src/api/types.ts`
- `api-doc.html` 第一版中已整理的接口内容

必须保留当前 OpenAPI 的 20 个 path 覆盖。若实现时发现文档与 OpenAPI 不一致，应以代码和 OpenAPI 输出为准修正文档。

## 验证计划

实现后至少执行以下验证：

1. HTML parser 校验：确认 `api-doc.html` 可被标准 parser 解析。
2. OpenAPI path 覆盖校验：从 `create_app().openapi()` 读取 paths，确认文档包含全部 path。
3. 静态检查：确认无外部 CDN / 网络资源引用。
4. 手工浏览器 smoke：打开本地 HTML，验证：
   - 左侧导航可跳转。
   - 搜索可过滤 path / keyword。
   - 分类 filter 可用。
   - Expand all / Collapse all 可用。
   - Copy 按钮有反馈。
   - SSE EventSource 示例存在。
   - 移动宽度下仍可阅读。

## 风险与权衡

- 单文件会变长：通过清晰章节、复用 CSS class、少量 JS 函数控制复杂度。
- 交互过多会增加维护成本：仅实现搜索、过滤、展开/收起、复制这些开发者高频动作。
- 不实现真实 Try-It：牺牲在线调试能力，换取安全、离线、无配置、无 token 泄露风险。
- 当前工作区存在多处既有未提交变更：实现阶段应只触碰 `api-doc.html`，必要时追加 `CHANGELOG.md`，避免改动无关文件。

## 验收标准

- 根目录存在单文件 `api-doc.html`，不新增多 HTML 文档集。
- UI 呈现为带目录导航的单页 API Console 文档。
- Endpoint card 具备统一结构，并提供复制友好的 curl / fetch / EventSource 示例。
- 搜索、分类过滤、展开/收起、复制反馈可用。
- 覆盖当前 OpenAPI 的 20 个 path。
- 不修改后端 API 行为和前端业务代码。
- 文档可离线打开，无外部资源依赖。
- 验证证据记录在最终实现报告中。
