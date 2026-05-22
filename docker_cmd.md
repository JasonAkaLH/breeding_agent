# Docker 启动命令

按顺序逐行执行以下命令即可拉取并启动 `0.1.0` 版本镜像。

```bash
docker network create breeding-agent-net >/dev/null 2>&1 || true
```

```bash
docker volume create breeding-agent-runtime
```

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.0
```

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-frontend:0.1.0
```

```bash
docker rm -f breeding-agent-frontend breeding-agent-backend >/dev/null 2>&1 || true
```

```bash
docker run -d --name breeding-agent-backend --network breeding-agent-net --network-alias backend -p 51888:8000 -e PYTHONPATH=/app -e PYTHONUNBUFFERED=1 -e MAF_RUST_CORE_MODE=off -e MAF_RUST_LIFECYCLE_MODE=off -e MAF_RUST_ARTIFACT_STORE_MODE=off -e MAF_RUST_AUTH_CORE_MODE=off -e MAF_RUST_DATA_ACCESS_MODE=off -e MAF_RUST_AUDIT_SANITIZER_MODE=off -e MAF_RUST_SKILL_RUNTIME_MODE=off -v breeding-agent-runtime:/app/runtime registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.0
```

```bash
docker run -d --name breeding-agent-frontend --network breeding-agent-net -p 51999:80 registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-frontend:0.1.0
```


## 数据持久化说明

后端容器把 `/app/runtime` 挂载到 Docker named volume `breeding-agent-runtime`：

```bash
-v breeding-agent-runtime:/app/runtime
```

当前后端默认 SQLite 数据库、audit 日志和 artifact 文件都在 `/app/runtime` 下，因此不会写进镜像层，也不会因为更新镜像或删除/重建容器而丢失。

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
