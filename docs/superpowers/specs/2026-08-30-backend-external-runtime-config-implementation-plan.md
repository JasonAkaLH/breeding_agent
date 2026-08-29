# Backend 外部运行时配置最小实施计划

依据：`2026-08-30-backend-external-runtime-config-design.md`
状态：`planned`
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
   `/data/peihai/config.yaml`非symlink、普通文件、单link、mode `0600`且非空，再用待发布backend镜像执行
   无输出的严格`bootstrap_config_env()`；启动命令增加只读挂载到`/app/config.yaml`；
3. 运行`bash -n docker_cmd.md`和`scripts/check_docker_cmd_policy.sh`，确认文件仍存在、忽略且未跟踪；验证失败时
   从外部备份恢复；
4. 构建`registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend:0.1.25`，固定
   `--platform linux/amd64 --target backend`；
5. 验证未挂载镜像不存在`/app/config.yaml`；把本地配置复制到明确临时目录并设为`0600`，仅作为运行时只读
   mount执行严格bootstrap和SQLite `/api-doc` smoke，不输出配置正文；smoke后删除临时容器、主密钥和配置副本；
6. 记录本地OCI digest与未推送状态，更新设计/计划、`docs/AGENTS.md`和`CHANGELOG.md`，提交非敏感实施账本。

最终门禁：deployment目录相关测试、镜像`linux/amd64`检查、镜像内配置缺失检查、严格外部配置bootstrap、
backend `/api-doc` 200、`git diff --check`和干净工作树。

## 4. 已知边界

此前中止推送可能遗留未引用blob，按已批准设计记录为用户接受的私有registry风险；本计划不执行registry GC、
凭据轮换、远端tag删除、推送或部署。外部`/data/peihai/config.yaml`只由远端执行时预检，本地实施不连接服务器。

License Requirement：复用既有Docker、Compose、Python配置bootstrap与部署测试；无新增依赖或许可变化。
