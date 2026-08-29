# Backend 外部运行时配置最小实施计划

依据：`2026-08-30-backend-external-runtime-config-design.md`
状态：`published_main_not_deployed`
目标分支：`main`
镜像版本：`0.1.25`

## 1. 完成边界

只完成：根`config.yaml`退出Docker context和backend镜像；Compose与main启动命令改为外部只读挂载；重新构建并
验证本地`linux/amd64` backend `0.1.25`。不修改配置加载业务代码、Frontend、Runtime Sidecar、schema、依赖、
外部服务器文件或`prod`，本计划不推送backend。

## 2. Checkpoint A：tracked Docker配置合同

先在`tests/deployment/test_user_mcp_cp7a_lean_compose.py`增加聚焦红测：

1. `.dockerignore`包含根精确规则`/config.yaml`；
2. Dockerfile不含`COPY config.yaml`；
3. Compose backend包含必填`MAF_CONFIG_FILE_HOST`到`/app/config.yaml`的long bind，`read_only: true`且
   `bind.create_host_path: false`；
4. 缺配置路径时`docker compose config --quiet`失败，同时提供主密钥与配置fixture路径时成功。

最小生产/文档修改仅为：

- `.dockerignore`
- `Dockerfile`
- `docker-compose.yml`
- `README.md`
- 上述部署测试

门禁：聚焦deployment测试、`docker compose config --quiet`、`git diff --check`。独立提交：
`fix(docker): externalize backend runtime config`。

## 3. Checkpoint B：main命令与backend重建

1. 在仓库外创建权限不宽于`0600`的`docker_cmd.md`备份；不得输出文件内容；
2. 只对main backend相关命令做结构化修改：在任何停止/替换旧backend前验证
   `/data/peihai/seedpilot_config_dev.yaml`非symlink、普通文件、单link、mode `0600`且非空，再用待发布backend镜像执行
   无输出的严格`bootstrap_config_env()`；启动命令增加只读挂载到`/app/config.yaml`；
3. 对`docker_cmd.md`实际命令区运行`bash -n`并运行`scripts/check_docker_cmd_policy.sh`，确认文件仍存在、忽略且
   未跟踪；验证失败时从外部备份恢复；
4. 构建`registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.25`，固定
   `--platform linux/amd64 --target backend`；
5. 验证未挂载镜像不存在`/app/config.yaml`；把本地配置复制到明确临时目录并设为`0600`，仅作为运行时只读
   mount执行严格bootstrap和SQLite `/api-doc` smoke，不输出配置正文；smoke后删除临时容器、主密钥和配置副本；
6. 记录本地OCI digest与未推送状态，更新设计/计划、`docs/AGENTS.md`和`CHANGELOG.md`，提交非敏感实施账本。

最终门禁：deployment目录相关测试、镜像`linux/amd64`检查、镜像内配置缺失检查、严格外部配置bootstrap、
backend `/api-doc` 200、`git diff --check`和干净工作树。

## 4. 实施证据（2026-08-30）

- `4259464a fix(docker): externalize backend runtime config`闭合`.dockerignore`、Dockerfile、Compose、README和
  部署合同；聚焦红测先以2 failure/1 error证明三个缺口，修改后deployment 4项及Ruff通过；
- 受保护`docker_cmd.md`在仓库外建立`0600`备份，只增加首次容器删除前的配置identity/0600/严格bootstrap预检
  和backend只读挂载；实际命令区语法、local-only policy、忽略/未跟踪状态通过；文件未加入Git；
- 重新构建本地`linux/amd64` backend `0.1.25`，OCI digest为
  `sha256:c1664088e23d5879fb1dc85c898e3e2d0a9f4cde5ffe4f1640d99944982e8e34`；
- 未挂载镜像确认不存在`/app/config.yaml`；本地配置的临时`0600`副本只读挂载后严格bootstrap无输出通过，
  隔离SQLite backend健康且`/api-doc`返回200；临时容器、配置副本和主密钥已删除；
- 后续用户明确授权发布三个main镜像；runtime-sidecar-dev、backend-dev、frontend-dev `0.1.25`远端digest分别为
  `sha256:346622b598649553936b5453afca8d1c1f69b5a4b3a3d6fa17cc3d525c632162`、
  `sha256:c1664088e23d5879fb1dc85c898e3e2d0a9f4cde5ffe4f1640d99944982e8e34`和
  `sha256:6f80c176ce8462fb7059bec6e6e4328a8cfa947d41bc4e191d0bd72d3193c721`，均含`linux/amd64`；
  `docker_cmd.md`三镜像tag已更新为`0.1.25`且外部配置路径更正为
  `/data/peihai/seedpilot_config_dev.yaml`；尚未部署，配置加载业务代码、schema、依赖、外部服务器和`prod`未改。

## 5. 已知边界

此前中止推送可能遗留未引用blob，按已批准设计记录为用户接受的私有registry风险；后续发布授权只增加三个新
main tag，不执行registry GC、凭据轮换、远端tag删除或部署。外部`/data/peihai/seedpilot_config_dev.yaml`只由
远端执行时预检，本地实施不连接服务器。

License Requirement：复用既有Docker、Compose、Python配置bootstrap与部署测试；无新增依赖或许可变化。
