# docker/AGENTS.md

本文件用于快速定位 Docker 辅助配置。除非本文件另有说明，继续遵守仓库根目录 `AGENTS.md`。

## 目录速览

- `nginx.conf`：前端容器 nginx 配置与 API / API 文档反代规则。

## 相关根目录文件

- `Dockerfile`：backend / frontend / runtime-sidecar 镜像构建入口；backend 仅额外交付 MCP 首次初始化 CLI 和两份指定 PostgreSQL SQL，不打包整个 scripts 目录。
- `docker-compose.yml`：本地 compose 编排。
- `.dockerignore`：Docker build context 排除规则。
- `docker_cmd/docker_cmd_dev.md`、`docker_cmd/docker_cmd_prod.md`：Git-ignored 的本地开发 / 生产部署命令记录，整个目录不进入 Docker build context。
