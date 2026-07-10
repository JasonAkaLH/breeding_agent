# SeedPilot public 静态资源子路径修复设计

状态：已实施并验证

## 背景

生产前端部署在 `/seedpilot/`，Vite 构建产物复制到 nginx 的
`/usr/share/nginx/html/seedpilot/`。修复前，`main` 在 `frontend/src/App.tsx`
中把三个按钮图片写成根路径 `/pics/*`，浏览器因此绕过 `/seedpilot/`
请求资源，并被 nginx 的根路径 404 规则拒绝。

`prod` 已在提交 `b380366` 中验证了基于 Vite `BASE_URL` 的处理方式。

## 目标

- 生产构建下，public 图片地址为 `/seedpilot/pics/*`。
- 本地 Vite 根路径开发下，图片地址仍为 `/pics/*`。
- 保持 nginx 根路径隔离，不新增 `/pics/` 根路由。
- 不改变按钮结构、图标内容或其他前端行为。

## 范围

本次修改范围仅包括：

- `frontend/src/App.tsx` 中三个 public SVG 地址的生成方式。
- 覆盖本地根路径和生产 `/seedpilot/` 子路径行为的回归验证。
- 根 `CHANGELOG.md` 中的实现记录。

本次明确不修改：

- `frontend/public/pics/` 下的 SVG 文件。
- `docker/nginx.conf` 的路由或根路径隔离规则。
- `VITE_API_BASE_URL`、API client 或 SSE 地址生成逻辑。
- 页面结构、按钮样式、可访问性属性和交互行为。

## 方案

在 `frontend/src/App.tsx` 增加一个局部 `publicAssetPath` helper：

1. 读取 Vite 提供的 `import.meta.env.BASE_URL`。
2. 规范化 base 尾部斜杠和资源路径开头斜杠。
3. 三个 public SVG 常量统一通过该 helper 生成地址。

不硬编码 `/seedpilot/`，避免破坏本地根路径开发；不开放 nginx 根路径
`/pics/`，避免与同域名其他应用发生静态资源冲突。

两个带查询参数的 SVG 地址必须原样保留查询参数。生成结果不得出现
`/seedpilot//pics/` 等重复斜杠。

## 验收标准

| 场景 | 必须满足的结果 |
|---|---|
| 本地默认 Vite 环境 | 三个图片地址继续以 `/pics/` 开头，现有 `App` 回归通过。 |
| `VITE_APP_BASE_PATH=/seedpilot/` 生产构建 | 三个图片地址均以 `/seedpilot/pics/` 开头，不再请求根路径 `/pics/`。 |
| 查询参数 | 发送按钮和账户设置按钮的既有 `?v=...` 参数保持不变。 |
| 静态文件 | 三个 SVG 均存在于生产构建的 `dist/pics/`。 |
| nginx 部署 | `/seedpilot/pics/*.svg` 返回 HTTP 200；对应根路径 `/pics/*.svg` 继续返回 HTTP 404。 |
| 回归边界 | nginx、API/SSE 路径、按钮 DOM 与交互行为没有变化。 |

## 验证

1. 运行 `cd frontend && npm test -- --run src/App.test.tsx`，确认默认 base 下
   三个 `<img>` 仍使用 `/pics/*`。
2. 运行 `cd frontend && npm run typecheck`。
3. 运行
   `cd frontend && VITE_APP_BASE_PATH=/seedpilot/ VITE_API_BASE_URL=/seedpilot npm run build`，
   确认三个 SVG 均存在于 `dist/pics/`。
4. 通过生产 nginx 镜像或等价容器 smoke 请求三个
   `/seedpilot/pics/*.svg` 地址，确认 HTTP 200，并确认对应 `/pics/*.svg`
   仍为 HTTP 404。最终 URL 检查必须同时覆盖查询参数保留和无重复斜杠。
5. 运行
   `PYTHONPATH=. pytest tests/api/test_frontend_nginx_proxy_config.py -q`，确认根路径隔离
   和 `/seedpilot/` SPA 路由未改变。
6. 运行 `git diff --check`。

## 风险与回滚

- 主要交付风险是服务器仍运行旧前端镜像。部署时必须核对镜像版本或 digest，
  并以实际 HTTP smoke 结果作为成功依据，不能只依据容器健康状态。
- 如果生产 smoke 未通过，停止发布并回滚到已知可用的前端镜像；不得通过开放
  nginx 根路径 `/pics/` 绕过失败。
- 本次不包含数据、配置或 API 迁移，代码回滚不需要额外的数据处理。

## 文档影响

根 `CHANGELOG.md` 已更新。本次修改不改变目录职责或入口索引，
无需调整根目录、`frontend/` 或 `docs/` 下的 `AGENTS.md`。
