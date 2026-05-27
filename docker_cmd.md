# Docker 启动命令

按顺序逐行执行以下命令即可拉取并启动 `0.1.2` 版本镜像。

```bash
docker network create breeding-agent-net >/dev/null 2>&1 || true
```

PostgreSQL 应按 `/Users/yinpeihai/Code_workspace/postgres-docker-demo/remote_command.md` 作为 `postgres-longrun` 容器运行。后端容器内访问 PostgreSQL 时不要使用 `127.0.0.1` / `localhost`，也不要使用宿主机映射端口 `15432`；镜像内 `config.yaml` 的 PostgreSQL DSN 应使用同一 Docker network 下的容器别名和容器内端口 `5432`：

```bash
postgresql://biobin_user:<password>@postgres:5432/biobin_db
```

只要镜像内 `config.yaml` 已包含上述 DSN 与 `state_platform.backend=postgresql`，后端启动命令不需要额外传 `MAF_STATE_STORE_BACKEND` / `MAF_POSTGRES_STATE_DSN` 环境变量。

排障约定：后续新增的远端 Docker / PostgreSQL / backend 排障命令，都应同步追加到本文档对应的“最小判别命令”小节，避免临时命令散落在聊天记录里。

涉及 `biobin_user` 密码的命令不要把真实密码写入本文档；在远端 shell 中先设置一次临时环境变量，再复制后续命令：

```bash
export BREEDING_AGENT_POSTGRES_PASSWORD='<biobin_user password>'
```

## 最小判别命令 A：确认 backend 镜像内置 PostgreSQL 配置

如果怀疑远端镜像内置的 `config.yaml` 不是最新版，先用一次性容器做脱敏检查。这条命令不连接数据库，只确认 backend 镜像准备用哪个用户 / host / port / db / 密码口径去连 PostgreSQL：

```bash
docker run --rm --entrypoint python \
  -e BREEDING_AGENT_POSTGRES_PASSWORD \
  registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.2 \
  -c "from pathlib import Path; import os; import yaml; from sqlalchemy.engine import make_url; d=yaml.safe_load(Path('/app/config.yaml').read_text()); pg=(d.get('state_platform') or {}).get('postgres') or {}; u=make_url(pg.get('dsn') or ''); expected=os.environ.get('BREEDING_AGENT_POSTGRES_PASSWORD') or ''; print('backend=', (d.get('state_platform') or {}).get('backend')); print('user=', u.username); print('host=', u.host); print('port=', u.port); print('db=', u.database); print('password_matches_expected=', bool(expected) and u.password == expected); print('password_len=', len(u.password or ''))"
```

期望输出应包含：

```text
backend= postgresql
user= biobin_user
host= postgres
port= 5432
db= biobin_db
password_matches_expected= True
password_len= 10
```

如果这里 `password_matches_expected` 不是 `True`，说明远端拉到的 backend 镜像不是当前期望版本，或远端 shell 中的 `BREEDING_AGENT_POSTGRES_PASSWORD` 没有设置为当前部署密码；先修正后再重新 `docker pull` / 检查。

## 最小判别命令 C：确认运行中的 backend 没有环境变量覆盖 DSN

如果最小判别命令 A 是 `True`，但 backend 启动仍然报 PostgreSQL 密码错误，再检查运行中的 backend 容器是否通过环境变量覆盖了镜像内 `config.yaml`：

```bash
docker inspect breeding-agent-backend --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'MAF_POSTGRES_STATE_DSN|MAF_STATE_STORE_BACKEND'
```

期望结果：没有输出 `MAF_POSTGRES_STATE_DSN=...`。如果存在 `MAF_POSTGRES_STATE_DSN=...`，backend 会优先使用这个环境变量，而不是镜像内 `config.yaml` 的 DSN；删除并按下面的标准 `docker run` 命令重建 backend 容器。


## 最小判别命令 D：确认运行中的 backend 容器是否来自当前镜像

如果镜像内配置正确，但运行中的 backend 仍然报旧密码错误，比较运行容器的 image id 和当前 tag 的 image id：

```bash
docker inspect breeding-agent-backend --format 'container_image_id={{.Image}}'
docker image inspect registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.2 --format 'tag_image_id={{.Id}}'
```

期望结果：两行 ID 一致。如果不一致，说明当前 `breeding-agent-backend` 容器不是用刚检查过的 `0.1.2` 镜像创建的，删除并按下面的标准 `docker run` 命令重建 backend 容器。

## 最小判别命令 E：使用同一 backend 镜像和同一 Docker 网络实际连接 PostgreSQL

如果最小判别命令 A / C / D 都正常，再用同一个 backend 镜像、同一个 `breeding-agent-net` 网络实际连接 PostgreSQL。该命令读取镜像内 `config.yaml` 的 DSN，但只输出当前数据库用户和数据库名，不打印完整 DSN 或密码：

```bash
docker run --rm --network breeding-agent-net --entrypoint python \
  registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.2 \
  -c "from pathlib import Path; import yaml; from sqlalchemy import create_engine, text; d=yaml.safe_load(Path('/app/config.yaml').read_text()); dsn=d['state_platform']['postgres']['dsn']; e=create_engine(dsn, future=True, hide_parameters=True); c=e.connect(); print(c.execute(text('SELECT current_user, current_database()')).one()); c.close(); print('connect ok')"
```

期望输出应包含：

```text
connect ok
```

如果这条成功，说明 backend 镜像、Docker 网络、PostgreSQL 用户密码都没问题；剩余重点检查运行中的 `breeding-agent-backend` 容器是否旧 image / 旧 env / 未按标准命令重建。如果这条失败，错误就是当前 backend 镜像按内置 DSN 连接 PostgreSQL 时的真实错误。


## 最小判别命令 F：确认当前 backend 容器状态与最新日志

当 image id、镜像内配置都正常，但 `docker logs -f breeding-agent-backend` 仍然看到旧错误时，先确认当前容器状态，并只看最近日志，避免把旧容器启动失败日志误判为本次新错误：

```bash
docker ps -a --filter name=breeding-agent-backend --format 'name={{.Names}} status={{.Status}} image={{.Image}}'
docker logs --tail 200 breeding-agent-backend
```

如果容器状态是 `Exited` 且最新日志仍是 PostgreSQL authentication failed，继续按最小判别命令 C / E 排查 env 覆盖或真实连库失败；如果容器已经 `Up`，以最新日志和 `/api-doc` / health 访问结果为准。


## 最小判别命令 G：跨容器认证失败时重置 `biobin_user` 密码

如果最小判别命令 E 在同一 backend 镜像、同一 Docker 网络下仍然报 `password authentication failed for user "biobin_user"`，说明问题已经不在 backend app 或 DSN 解析，而是在 PostgreSQL 对来自 Docker network 的真实密码认证。先可选查看 `pg_hba.conf`，确认 loopback 与 Docker network 可能命中不同认证规则：

```bash
docker exec postgres-longrun bash -lc 'grep -nE "^(local|host)" "${PGDATA:-/var/lib/postgresql/data}/pg_hba.conf"'
```

然后用 PostgreSQL 管理员账号在 `postgres-longrun` 内重置 `biobin_user` 密码，并重新执行最小判别命令 E：

```bash
docker exec -i postgres-longrun psql -U postgres -d biobin_db \
  -v biobin_user_password="$BREEDING_AGENT_POSTGRES_PASSWORD" <<'SQL'
ALTER ROLE biobin_user WITH LOGIN PASSWORD :'biobin_user_password';
SQL
```

如果这里提示 `role "biobin_user" does not exist`，回到 `/Users/yinpeihai/Code_workspace/postgres-docker-demo/user_command.md` 的“创建可建表/改表但不能删表/删库的用户”章节完整创建用户和授权。

```bash
docker volume create breeding-agent-runtime
```

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.2
```

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-frontend:0.1.2
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
docker inspect postgres-longrun --format '{{json .NetworkSettings.Networks.breeding-agent-net.Aliases}}'
```

```bash
docker run -d --name breeding-agent-backend --network breeding-agent-net --network-alias backend -p 51888:8000 -e PYTHONPATH=/app -e PYTHONUNBUFFERED=1 -e MAF_RUST_CORE_MODE=off -e MAF_RUST_LIFECYCLE_MODE=off -e MAF_RUST_ARTIFACT_STORE_MODE=off -e MAF_RUST_AUTH_CORE_MODE=off -e MAF_RUST_DATA_ACCESS_MODE=off -e MAF_RUST_AUDIT_SANITIZER_MODE=off -e MAF_RUST_SKILL_RUNTIME_MODE=off -v breeding-agent-runtime:/app/runtime registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.2
```

```bash
docker run -d --name breeding-agent-frontend --network breeding-agent-net -p 51999:80 registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-frontend:0.1.2
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
