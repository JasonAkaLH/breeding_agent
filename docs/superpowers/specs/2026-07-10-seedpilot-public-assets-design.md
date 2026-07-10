# SeedPilot public 静态资源子路径修复设计

## 背景

生产前端部署在 `/seedpilot/`，Vite 构建产物复制到 nginx 的
`/usr/share/nginx/html/seedpilot/`。当前 `main` 在 `frontend/src/App.tsx`
中把三个按钮图片写成根路径 `/pics/*`，浏览器因此绕过 `/seedpilot/`
请求资源，并被 nginx 的根路径 404 规则拒绝。

`prod` 已在提交 `b380366` 中验证了基于 Vite `BASE_URL` 的处理方式。

## 目标

- 生产构建下，public 图片地址为 `/seedpilot/pics/*`。
- 本地 Vite 根路径开发下，图片地址仍为 `/pics/*`。
- 保持 nginx 根路径隔离，不新增 `/pics/` 根路由。
- 不改变按钮结构、图标内容或其他前端行为。

## 方案

在 `frontend/src/App.tsx` 增加一个局部 `publicAssetPath` helper：

1. 读取 Vite 提供的 `import.meta.env.BASE_URL`。
2. 规范化 base 尾部斜杠和资源路径开头斜杠。
3. 三个 public SVG 常量统一通过该 helper 生成地址。

不硬编码 `/seedpilot/`，避免破坏本地根路径开发；不开放 nginx 根路径
`/pics/`，避免与同域名其他应用发生静态资源冲突。

## 验证

- 运行现有 `App` 定向回归，确认本地根路径仍生成 `/pics/*`。
- 运行前端 typecheck。
- 使用 `VITE_APP_BASE_PATH=/seedpilot/` 执行生产构建，检查构建产物包含
  `/seedpilot/pics/*` 且图片文件存在于 `dist/pics/`。
- 运行 nginx 配置回归，确认根路径仍返回 404、SPA 仍由 `/seedpilot/` 服务。
- 运行 `git diff --check`。

## 文档影响

实现完成后更新根 `CHANGELOG.md`。本次修改不改变目录职责或入口索引，
无需调整根目录、`frontend/` 或 `docs/` 下的 `AGENTS.md`。
