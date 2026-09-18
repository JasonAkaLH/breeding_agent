# 新用户 MCP 功能首次生产启用

适用于**已有业务历史、但用户 MCP 尚未启用**的 PostgreSQL 数据库。先在恢复库演练；正式库须完成停写、最终备份和结构迁移后再执行。入口不会删除旧数据、执行 DDL、迁移旧 MCP 配置或伪造灰度观察记录。

## 工件和前置准备

- 使用已经核验 Linux/amd64、源码 revision、镜像 digest 的 Backend 工件。
- 结构迁移完成后 public 应有 66 张表，schema manifest 为 `maf.postgresql_fresh_runtime_schema.v11`。旧 66 表库需显式执行镜像内 `/app/scripts/postgres/user_mcp_first_enablement_constraints.sql`；该 SQL 事务性更新五项 evidence CHECK，保留已有证据，支持重复执行。新建结构已包含这些约束。
- 管理员另行安装 `/app/scripts/postgres/user_mcp_rollout_permissions.sql`，并准备独立 LOGIN，授予 `maf_rollout_app_writer`，成员选项为 `INHERIT TRUE, SET FALSE`。该脚本涉及集群 NOLOGIN 角色，演练前也应核对现有角色；不把 state 表所有者或 postgres 当作 rollout app 身份。
- 保持 MCP 写入方停止。初始化检查所有 MCP 业务/发布表为空；已有会话、Task、Artifact、登录记录和 schema 元数据允许存在。
- 提供真实生产 config、主密钥和现有 Sidecar manifest/allowlist 的只读挂载。预检不发送 LLM/MCP 外部请求，不能替代上线后的真实调用验证。

## 一次性管理容器

以下变量使用本次环境的实际值，管理员 DSN 只留在临时管理进程，不能加入日常 Backend 命令。管理员必须是实际登录的 superuser，且 `session_user=current_user`。生产数据库为 `biobin_db`；恢复演练应明确指定独立数据库名。

```bash
# 这些变量须先按验收工件、目标数据库和独立身份设置；不要把密码写入命令历史。
: "${BACKEND_IMAGE:?使用 registry/repository@sha256:...}"
: "${BACKEND_GIT_SHA:?填写该工件的 40 位源码 revision}"
: "${BACKEND_MANIFEST_DIGEST:?填写该工件的 linux/amd64 sha256:...}"
: "${TARGET_DATABASE:?明确指定本次恢复库或正式库}"
: "${MAF_MCP_INITIALIZATION_ADMIN_DSN:?临时管理员连接，禁止用于常驻 Backend}"
: "${MAF_MCP_ROLLOUT_APP_DSN:?同一数据库的独立 rollout app 连接}"
: "${MCP_ENFORCE_HASH_SALT:?与实际 Backend 保持一致}"
: "${MCP_ROLLOUT_ENVIRONMENT_ID:?真实生产环境标识}"
: "${MCP_ROLLOUT_DEPLOYMENT_ID:?本次发布标识}"
: "${MCP_ROLLOUT_ACTIVATION_ID:?本次 activation 标识}"
export MAF_MCP_INITIALIZATION_ADMIN_DSN MAF_MCP_ROLLOUT_APP_DSN
export MCP_ENFORCE_HASH_SALT MCP_ROLLOUT_ENVIRONMENT_ID
export MCP_ROLLOUT_DEPLOYMENT_ID MCP_ROLLOUT_ACTIVATION_ID

first_mcp() {
  docker run --rm --platform linux/amd64 --network breeding-agent-net \
    --mount type=bind,source=/data/peihai/seedpilot_config_prod.yaml,target=/app/config.yaml,readonly \
    --mount type=bind,source=/data/peihai/seedpilot-prod-maf-master.key,target=/run/secrets/maf-master.key,readonly \
    --mount type=bind,source=/data/peihai/seedpilot-prod-sidecar-trust,target=/run/maf-sidecar-trust,readonly \
    -e MAF_API_ENV=prod -e MAF_ENV=prod \
    -e MAF_MASTER_KEY_FILE=/run/secrets/maf-master.key \
    -e MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH=/run/maf-sidecar-trust/manifest.json \
    -e MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH=/run/maf-sidecar-trust/allowlist.json \
    -e MCP_USER_SCOPED_GATEWAY_ENABLED=true -e MCP_ROUTING_MODE=enforce \
    -e MCP_ENFORCE_PERCENT=100 -e MCP_ENFORCE_COHORTS= \
    -e MCP_ENFORCE_COHORT_CONFIG_FILE= -e MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=false \
    -e MCP_ROLLOUT_STAGE=legacy_assembly_off \
    -e MCP_ENFORCE_HASH_SALT -e MCP_ROLLOUT_ENVIRONMENT_ID \
    -e MCP_ROLLOUT_DEPLOYMENT_ID -e MCP_ROLLOUT_ACTIVATION_ID \
    -e MAF_MCP_INITIALIZATION_ADMIN_DSN -e MAF_MCP_ROLLOUT_APP_DSN \
    --entrypoint python "$BACKEND_IMAGE" \
    /app/scripts/initialize_mcp_first_production.py "$@" \
    --database "$TARGET_DATABASE" --git-sha "$BACKEND_GIT_SHA" \
    --backend-image-digest "$BACKEND_MANIFEST_DIGEST"
}

# 只读：应返回 status=ready 和 check_digest，不创建任何记录。
first_mcp check
```

核对只读报告后，显式执行以下一步。`check_digest` 必须来自同一数据库、发布、源码、镜像、管理员、app 身份、schema 与 MCP 配置。`apply` 会取得数据库级事务锁和表锁，再重新核验实际状态。

```bash
: "${MCP_FIRST_CHECK_DIGEST:?填写刚才报告中的 check_digest}"
first_mcp apply --expected-check-digest "$MCP_FIRST_CHECK_DIGEST"
unset MAF_MCP_INITIALIZATION_ADMIN_DSN
```

成功返回 `status=initialized`，同时产生一组 evidence/approval/activation。首次 evidence 为 `source=deployment`、`producer=deployment_initializer`、`kind=first_enablement`，记录当前空状态，不包含伪造的 CI 成功或观察时间窗。目标 activation 为 `legacy_assembly_off`。

同一操作精确重试返回 `already_initialized`，即使之后已添加用户 MCP 配置也不重复写入。不同发布/配置、部分记录、后续 activation 或安全 blocker 会拒绝，不进行覆盖修复。任何事务写入失败会完整回滚；对连接中断等不确定结果，先使用相同参数重新 `check`，不要换一组 ID 再试。

## 正常启动与保留证据

Backend 继续使用 state 连接、独立 rollout app 连接、相同环境/发布/activation/配置和稳定实例 ID；管理员变量必须移除。常驻入口不自动初始化，仍执行角色、activation、实例租约、安全阻断、用户隔离、Tool 授权、主密钥和 Sidecar 信任校验。

归档只读报告、apply 结果、镜像/revision、结构迁移与保留数据摘要，放入本次服务器备份记录目录。不得归档明文 DSN、密码、API Key 或主密钥到 Git。开发库下一次使用 v11 代码前也需显式升级相应约束，不自动修改或重启开发服务。

License Requirement：复用现有依赖，无新增依赖或许可例外。
