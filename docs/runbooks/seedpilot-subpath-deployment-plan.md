# SeedPilot 子路径部署改造 Runbook

- **日期**：2026-06-24
- **目标 URL**：`http://175.6.25.109:51999/seedpilot/`
- **目标**：让 SeedPilot 前端、业务 API 和 API 文档都挂载在同一域名/端口的 `/seedpilot` 外部上下文下，避免 SeedPilot 占用根路径 `/`、`/assets`、`/api`，减少与同域名下其他应用冲突。
- **状态**：代码与配置已落地；本地单元测试、typecheck、生产 base 构建、Compose 配置和 nginx 语法检查已通过，仍需部署后浏览器 smoke。
- **部署策略**：生产环境采用完全子路径部署；不保留 SeedPilot 老路径。

## 1. 背景

当前前端 nginx 配置服务于根路径：

- 静态前端：`/`
- API 代理：`/api/`
- API 文档：`/api-doc`、`/docs`、`/redoc`、`/openapi.json`

当前 Vite 未配置 `base`，前端构建产物默认引用 `/assets/...`；前端 API client 默认请求 `/api/v1/...`。如果直接把页面放到 `/seedpilot/`，浏览器仍可能请求根路径 `/assets/...` 和 `/api/...`，会影响同域名下其他应用。

## 2. 目标行为

生产环境 SeedPilot 只响应以下外部路径：

```text
/seedpilot/
/seedpilot/assets/...
/seedpilot/api/v1/...
/seedpilot/api-doc
/seedpilot/api-doc/API更新日志.md
/seedpilot/docs
/seedpilot/redoc
/seedpilot/openapi.json
```

生产环境 SeedPilot 不再占用以下老路径：

```text
/
/assets/...
/api/...
/api-doc
/docs
/redoc
/openapi.json
```

根路径 `/` 不跳转到 `/seedpilot/`，应返回 `404`，避免影响同域名同端口未来可能承载的其他应用。

内部后端 FastAPI 路由保持不变：

```text
/api/v1/...
/api-doc
/api-doc/API更新日志.md
/openapi.json
/docs
/redoc
```

外部 `/seedpilot/api/...` 由前端 nginx 反向代理到内部后端 `/api/...`。外部 `/seedpilot/api-doc`、`/seedpilot/docs`、`/seedpilot/redoc`、`/seedpilot/openapi.json` 由前端 nginx 转发到后端对应文档路径。

## 3. 非目标

- 不修改后端 FastAPI 业务路由前缀。
- 不把其他应用迁移到 SeedPilot nginx 内。
- 不保留 SeedPilot 老路径 `/`、`/assets`、`/api`。
- 不要求根路径 `/` 跳转到 `/seedpilot/`。
- 不改变认证 token、SSE、上传、artifact 下载等 API 契约。
- 不改变本地 Vite dev 默认访问方式；本地开发仍可访问 `http://127.0.0.1:5173/`。

## 4. 已确认部署决策

| 决策 | 结论 |
|---|---|
| 老路径是否保留 | 不保留；SeedPilot 生产只挂 `/seedpilot/`。 |
| 根路径 `/` 行为 | 返回 `404`，不跳转。 |
| API 文档是否在子路径暴露 | 暴露到 `/seedpilot/api-doc`、`/seedpilot/docs`、`/seedpilot/redoc`、`/seedpilot/openapi.json`。 |
| API 文档内部链接 | 修改 `docs/api/api-doc.html`，使用相对或可配置路径；不依赖根路径兜底。 |
| Docker 构建默认路径 | Docker/生产默认 `VITE_APP_BASE_PATH=/seedpilot/`、`VITE_API_BASE_URL=/seedpilot`。 |
| Vite 本地开发 | 保持根路径开发体验；仅 Docker/生产默认子路径。 |
| 前端产物目录 | 构建产物放入 `/usr/share/nginx/html/seedpilot/`。 |
| nginx 业务 API 代理 | 使用 `rewrite + $breeding_agent_backend`，沿用现有 Docker resolver/变量风格。 |
| frontend healthcheck | 检查 `/seedpilot/`。 |
| backend healthcheck | 保持检查后端 `/api-doc`。 |

## 5. 实施步骤

### 5.1 更新 Vite base path

在 `frontend/vite.config.ts` 中增加可配置 `base`：

```ts
export default defineConfig({
  base: process.env.VITE_APP_BASE_PATH ?? '/',
  plugins: [react()],
  // ...existing config
});
```

预期效果：

- Docker/生产构建时静态资源引用变为 `/seedpilot/assets/...`。
- 本地未设置 `VITE_APP_BASE_PATH` 时仍使用 `/`，保持 `npm run dev` 体验不变。

### 5.2 保持前端 API base 使用 `VITE_API_BASE_URL`

当前 `frontend/src/api/client.ts` 已通过 `VITE_API_BASE_URL` 生成业务 API 请求前缀：

```ts
const baseUrl = normalizeBaseUrl(options.baseUrl ?? import.meta.env.VITE_API_BASE_URL ?? '');
```

当前 `frontend/src/api/taskEvents.ts` 已通过同一变量生成 SSE URL：

```ts
export function taskEventsUrl(taskId: string, baseUrl = import.meta.env.VITE_API_BASE_URL ?? ''): string {
  return `${normalizeBaseUrl(baseUrl)}/api/v1/tasks/${encodeURIComponent(taskId)}/events`;
}
```

生产构建必须设置：

```bash
VITE_API_BASE_URL=/seedpilot
```

预期效果：

- 普通 API 请求：`/seedpilot/api/v1/...`
- SSE 请求：`/seedpilot/api/v1/tasks/{task_id}/events`
- 上传请求：`/seedpilot/api/v1/conversations/uploads`
- artifact 下载请求：`/seedpilot/api/v1/artifacts/{artifact_id}/download`

### 5.3 修改 API 文档的内部绝对路径

因为生产不再占用根路径，所有 API 文档页面都不能继续请求根路径 `/api-doc/...` 或 `/openapi.json`。

#### 5.3.1 项目自维护文档页 `docs/api/api-doc.html`

`docs/api/api-doc.html` 中面向同站资源的根路径引用必须改为相对或可配置路径。

当前需要重点处理的路径包括：

```text
/api-doc/API更新日志.md
/openapi.json
```

推荐改法：

- `/api-doc/API更新日志.md` 改为相对当前文档目录的 `api-doc/API更新日志.md`，或由脚本按当前 `location.pathname` 推导。
- `/openapi.json` 改为相对部署上下文的 `openapi.json`，或由脚本按当前 `location.pathname` 推导。

必须满足：

- 从 `/seedpilot/api-doc` 打开时，请求 `/seedpilot/api-doc/API更新日志.md` 和 `/seedpilot/openapi.json`。
- 不请求根路径 `/api-doc/API更新日志.md` 或 `/openapi.json`。
- 从内部后端 `/api-doc` 打开时仍可读取后端 `/api-doc/API更新日志.md` 和 `/openapi.json`，除非另行决定不再支持内部后端文档页直接浏览。

#### 5.3.2 FastAPI 生成文档 `/docs` 与 `/redoc`

FastAPI 生成的 Swagger UI 和 ReDoc 页面通常会在 HTML 中引用后端 `openapi_url`，当前后端是 `/openapi.json`。生产从 `/seedpilot/docs` 或 `/seedpilot/redoc` 访问时，不能让浏览器请求根路径 `/openapi.json`。

本次 runbook 不修改后端 FastAPI 路由前缀，因此推荐在 nginx 的 `/seedpilot/docs` 与 `/seedpilot/redoc` 代理中做 HTML 响应替换：

```nginx
proxy_set_header Accept-Encoding "";
sub_filter_once off;
sub_filter '/openapi.json' '/seedpilot/openapi.json';
```

必须满足：

- 从 `/seedpilot/docs` 打开 Swagger UI 时，schema 请求落到 `/seedpilot/openapi.json`。
- 从 `/seedpilot/redoc` 打开 ReDoc 时，schema 请求落到 `/seedpilot/openapi.json`。
- 不通过继续暴露根路径 `/openapi.json` 来兜底。

### 5.4 更新 Dockerfile build args 与前端产物目录

当前 Dockerfile frontend build stage 直接执行 `npm run build`，需要增加 build args，并默认使用生产子路径：

```dockerfile
FROM node:25-bookworm AS frontend-build

WORKDIR /workspace/frontend

ARG VITE_APP_BASE_PATH=/seedpilot/
ARG VITE_API_BASE_URL=/seedpilot
ENV VITE_APP_BASE_PATH=${VITE_APP_BASE_PATH}
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build
```

frontend nginx stage 将 dist 放入子目录：

```dockerfile
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /workspace/frontend/dist/ /usr/share/nginx/html/seedpilot/
```

frontend healthcheck 改查子路径：

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1/seedpilot/ >/dev/null || exit 1
```

### 5.5 更新 docker-compose build args 与 healthcheck

`docker-compose.yml` 的 frontend service 应显式声明生产 build args，避免不同 Docker 构建环境默认值漂移：

```yaml
frontend:
  platform: linux/amd64
  build:
    context: .
    dockerfile: Dockerfile
    target: frontend
    args:
      VITE_APP_BASE_PATH: /seedpilot/
      VITE_API_BASE_URL: /seedpilot
  image: breeding-agent-frontend:local
  depends_on:
    backend:
      condition: service_healthy
  ports:
    - "51999:80"
  healthcheck:
    test: ["CMD", "curl", "-fsS", "http://127.0.0.1/seedpilot/"]
    interval: 30s
    timeout: 5s
    start_period: 10s
    retries: 3
```

backend healthcheck 继续检查内部后端文档：

```yaml
healthcheck:
  test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/api-doc"]
```

### 5.6 更新 nginx 子路径配置

目标：只让 SeedPilot nginx 响应 `/seedpilot/...`，根路径返回 `404`。

推荐 `docker/nginx.conf` 结构如下，保留现有 Docker resolver 与 `$breeding_agent_backend` 变量风格：

```nginx
server {
    listen 80 default_server;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;
    client_max_body_size 50m;
    resolver 127.0.0.11 valid=30s ipv6=off;
    set $breeding_agent_backend http://backend:8000;

    location = / {
        return 404;
    }

    location = /seedpilot {
        return 301 /seedpilot/;
    }

    location /seedpilot/api/ {
        rewrite ^/seedpilot/api/(.*)$ /api/$1 break;
        proxy_pass $breeding_agent_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3900s;
    }

    location = /seedpilot/api-doc {
        rewrite ^/seedpilot/api-doc$ /api-doc break;
        proxy_pass $breeding_agent_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /seedpilot/api-doc/ {
        rewrite ^/seedpilot/api-doc/(.*)$ /api-doc/$1 break;
        proxy_pass $breeding_agent_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /seedpilot/openapi.json {
        rewrite ^/seedpilot/openapi\.json$ /openapi.json break;
        proxy_pass $breeding_agent_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /seedpilot/docs {
        rewrite ^/seedpilot/docs$ /docs break;
        proxy_pass $breeding_agent_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Accept-Encoding "";
        sub_filter_once off;
        sub_filter '/openapi.json' '/seedpilot/openapi.json';
    }

    location = /seedpilot/redoc {
        rewrite ^/seedpilot/redoc$ /redoc break;
        proxy_pass $breeding_agent_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Accept-Encoding "";
        sub_filter_once off;
        sub_filter '/openapi.json' '/seedpilot/openapi.json';
    }

    location /seedpilot/ {
        try_files $uri $uri/ /seedpilot/index.html;
    }

    location / {
        return 404;
    }
}
```

注意事项：

- `/seedpilot/api/` 必须排在 `/seedpilot/` SPA fallback 前面。
- `/seedpilot/api-doc/` 必须支持 `API更新日志.md` 等文档子资源。
- 当前方案使用 `rewrite + proxy_pass $breeding_agent_backend`，不要改成 `proxy_pass $breeding_agent_backend/api/`，避免 nginx 变量 `proxy_pass` URI 拼接行为导致路径错位。
- 如果未来不用变量而直接写 literal upstream，才可以考虑 `proxy_pass http://backend:8000/api/;`。
- `/seedpilot/docs` 与 `/seedpilot/redoc` 需要替换响应 HTML 中的 `/openapi.json`，否则浏览器会回到根路径请求 schema。

### 5.7 补充前端单元测试

建议补充或更新以下测试：

- `frontend/src/api/client.test.ts`
  - `createApiClient({ baseUrl: '/seedpilot' }).listCapabilities()` 请求 `/seedpilot/api/v1/capabilities`。
  - `createApiClient({ baseUrl: '/seedpilot/' })` 会去掉末尾 `/`，避免生成 `/seedpilot//api/...`。
  - `uploadConversationFile()` 在 `baseUrl='/seedpilot'` 时请求 `/seedpilot/api/v1/conversations/uploads`。
  - `downloadArtifact()` 在 `baseUrl='/seedpilot'` 时请求 `/seedpilot/api/v1/artifacts/{id}/download`。
- `frontend/src/api/taskEvents.test.ts`
  - `taskEventsUrl('task-1', '/seedpilot')` 返回 `/seedpilot/api/v1/tasks/task-1/events`。

### 5.8 构建与 smoke 验证

执行构建：

```bash
docker compose build frontend
```

启动服务：

```bash
docker compose up -d backend frontend
```

验证前端子路径：

```bash
curl -fsSI http://127.0.0.1:51999/seedpilot/
curl -fsS http://127.0.0.1:51999/seedpilot/ | grep '/seedpilot/assets/'
```

验证根路径不再由 SeedPilot 占用：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:51999/
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:51999/api/v1/capabilities
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:51999/assets/
```

预期：上述老路径不返回 SeedPilot 页面或业务 API；根路径 `/` 应返回 `404`。

验证业务 API：

```bash
curl -fsS http://127.0.0.1:51999/seedpilot/api/v1/capabilities
```

验证 API 文档：

```bash
curl -fsSI http://127.0.0.1:51999/seedpilot/api-doc
curl -fsSI http://127.0.0.1:51999/seedpilot/api-doc/API更新日志.md
curl -fsSI http://127.0.0.1:51999/seedpilot/openapi.json
curl -fsSI http://127.0.0.1:51999/seedpilot/docs
curl -fsSI http://127.0.0.1:51999/seedpilot/redoc
```

浏览器手动 smoke：

1. 打开 `http://127.0.0.1:51999/seedpilot/`。
2. DevTools Network 确认没有请求根路径 `/assets/...` 或 `/api/...`。
3. 登录。
4. 加载 capabilities。
5. 提交消息。
6. 确认 SSE 连接 `/seedpilot/api/v1/tasks/{task_id}/events` 正常收到事件。
7. 上传文件。
8. 下载 artifact。
9. 打开 `/seedpilot/api-doc`，确认 API 更新日志和 OpenAPI schema 能加载，且不请求根路径 `/api-doc/...` 或 `/openapi.json`。

## 6. 验收标准

- `/seedpilot/` 返回前端页面。
- `/seedpilot/assets/...` 返回前端静态资源。
- 前端所有业务 API 请求均以 `/seedpilot/api/` 开头。
- `/seedpilot/api/v1/capabilities` 返回能力列表。
- SSE 连接 `/seedpilot/api/v1/tasks/{task_id}/events` 正常接收事件。
- 文件上传走 `/seedpilot/api/v1/conversations/uploads` 并成功。
- artifact 下载走 `/seedpilot/api/v1/artifacts/{artifact_id}/download` 并成功。
- `/seedpilot/api-doc` 可打开。
- `/seedpilot/api-doc/API更新日志.md` 可访问。
- `/seedpilot/openapi.json` 可访问。
- `/seedpilot/docs` 和 `/seedpilot/redoc` 可访问。
- API 文档页面不请求根路径 `/api-doc/...` 或 `/openapi.json`。
- 根路径 `/` 返回 `404`，不返回 SeedPilot 页面。
- 根路径 `/api/` 不再代理 SeedPilot 后端。
- 根路径 `/assets/` 不再服务 SeedPilot 静态资源。
- Docker/Compose frontend healthcheck 检查 `/seedpilot/` 并通过。
- 本地 Vite dev 未设置环境变量时仍可用根路径开发。

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Vite base 未设置 | 页面 HTML 能打开但 JS/CSS 从 `/assets` 加载失败或冲突 | Dockerfile 和 Compose 默认设置 `VITE_APP_BASE_PATH=/seedpilot/`；构建后检查 HTML 资源路径 |
| API base 未设置 | 前端请求仍打到根路径 `/api` | Dockerfile 和 Compose 默认设置 `VITE_API_BASE_URL=/seedpilot`；补充 API client 单元测试 |
| nginx 业务 API 路径改写错误 | `/seedpilot/api/...` 到后端路径错位 | 使用 `rewrite ^/seedpilot/api/(.*)$ /api/$1 break; proxy_pass $breeding_agent_backend;`，并用 curl 验证 |
| API 文档 HTML 仍有根路径引用 | `/seedpilot/api-doc` 页面会请求 `/api-doc/...` 或 `/openapi.json`，与不占用根路径目标冲突 | 修改 `docs/api/api-doc.html` 为相对或可配置路径；用浏览器 Network 验证 |
| FastAPI `/docs` 或 `/redoc` 仍引用根 `/openapi.json` | 子路径 Swagger/ReDoc 页面可能无法加载 schema | 在 `/seedpilot/docs`、`/seedpilot/redoc` 的 nginx 代理中使用 `sub_filter '/openapi.json' '/seedpilot/openapi.json'`；用浏览器 Network 验证 |
| frontend healthcheck 仍检查 `/` | 容器健康但实际子路径不可用，或根路径 404 导致健康检查失败 | Dockerfile 和 Compose healthcheck 改为 `/seedpilot/` |
| artifact 返回裸 `/api/...` 链接 | 未来组件若直接使用 `download_url` 会越过 `/seedpilot` | 当前 UI 使用 `api.downloadArtifact()` 按 artifact_id 下载；补充 baseUrl 下载测试，避免直接使用裸 `download_url` 发起请求 |
| 同域名其他应用已有 `/seedpilot` | 路径冲突 | 部署前确认 `/seedpilot` 唯一；如冲突需重新确认上下文名并同步所有 build args/nginx/验收命令 |
| 本地开发误以为也默认子路径 | 开发访问路径混淆 | 文档明确本地 Vite dev 保持根路径；子路径验证走 Docker/生产构建 |

## 8. 回滚方案

如果需要回滚到老的根路径部署：

1. 将 Docker/Compose 构建参数改回：

   ```text
   VITE_APP_BASE_PATH=/
   VITE_API_BASE_URL=
   ```

2. 将前端构建产物恢复复制到：

   ```text
   /usr/share/nginx/html
   ```

3. 恢复 nginx 根路径配置：

   ```nginx
   location /api/ { ... }
   location / { try_files $uri $uri/ /index.html; }
   ```

4. 恢复 frontend healthcheck 检查 `/`。
5. 使用上一版前端镜像回滚。

回滚后 SeedPilot 会重新占用 `/`、`/assets`、`/api`，必须确认同域名下没有其他应用依赖这些路径。
