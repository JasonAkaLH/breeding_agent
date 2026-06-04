# Docker 启动命令

按顺序逐行执行以下命令即可拉取并启动 backend `0.1.13` 与 frontend `0.1.13` 版本镜像。

```bash
docker network create breeding-agent-net >/dev/null 2>&1 || true
```

PostgreSQL 应按 `/Users/yinpeihai/Code_workspace/postgres-docker-demo/remote_command.md` 作为 `postgres-longrun` 容器运行。后端容器内访问 PostgreSQL 时不要使用 `127.0.0.1` / `localhost`，也不要使用宿主机映射端口 `15432`；镜像内 `config.yaml` 的 PostgreSQL DSN 应使用同一 Docker network 下的容器别名和容器内端口 `5432`：

```bash
postgresql://biobin_user:<password>@postgres:5432/biobin_db
```

只要镜像内 `config.yaml` 已包含上述 DSN 与 `state_platform.backend=postgresql`，后端启动命令不需要额外传 `MAF_STATE_STORE_BACKEND` / `MAF_POSTGRES_STATE_DSN` 环境变量。

```bash
docker volume create breeding-agent-runtime
```

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.13
```

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-frontend:0.1.13
```

```bash
docker rm -f breeding-agent-frontend breeding-agent-backend >/dev/null 2>&1 || true
```

每次启动后端前，都重建 `postgres-longrun` 在 `breeding-agent-net` 上的 membership，确保它一定带有 `postgres` 别名；这一步不会删除 PostgreSQL 容器或 volume 数据：

```bash
docker network disconnect breeding-agent-net postgres-longrun >/dev/null 2>&1 || true
docker network connect --alias postgres breeding-agent-net postgres-longrun
```

确认别名已经生效：

```bash
docker inspect postgres-longrun --format '{{json ((index .NetworkSettings.Networks "breeding-agent-net").Aliases)}}'
```

```bash
docker run -d --name breeding-agent-backend --network breeding-agent-net --network-alias backend -p 51888:8000 -e PYTHONPATH=/app -e PYTHONUNBUFFERED=1 -e MAF_RUST_CORE_MODE=off -e MAF_RUST_LIFECYCLE_MODE=off -e MAF_RUST_ARTIFACT_STORE_MODE=off -e MAF_RUST_AUTH_CORE_MODE=off -e MAF_RUST_DATA_ACCESS_MODE=off -e MAF_RUST_AUDIT_SANITIZER_MODE=off -e MAF_RUST_SKILL_RUNTIME_MODE=off -v breeding-agent-runtime:/app/runtime registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.13
```

```bash
docker run -d --name breeding-agent-frontend --network breeding-agent-net -p 51999:80 registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-frontend:0.1.13
```


## 数据持久化说明

后端容器把 `/app/runtime` 挂载到 Docker named volume `breeding-agent-runtime`：

```bash
-v breeding-agent-runtime:/app/runtime
```

当前镜像的主状态库使用 PostgreSQL；`/app/runtime` 仍用于 audit 日志和 artifact 文件，因此这些运行时文件不会写进镜像层，也不会因为更新镜像或删除/重建容器而丢失。

会导致数据丢失的操作：

```bash
docker volume rm breeding-agent-runtime
```

或者使用 `docker compose down -v` 删除对应 volume。升级镜像时只需要重新 `docker pull` 并 `docker rm -f` / `docker run`，不要删除 `breeding-agent-runtime` volume。

启动后访问：

- 前端：<http://127.0.0.1:51999/>
- 后端 API 文档：<http://127.0.0.1:51888/api-doc>

查看运行状态：

```bash
docker ps --filter "name=breeding-agent"
```

查看日志：

```bash
docker logs -f breeding-agent-backend
```

```bash
docker logs -f breeding-agent-frontend
```

停止并删除容器：

```bash
docker rm -f breeding-agent-frontend breeding-agent-backend
```
