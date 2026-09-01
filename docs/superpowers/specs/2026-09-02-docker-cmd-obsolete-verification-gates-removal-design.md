# docker_cmd 过期人工验证门禁删除设计

状态：已批准，待书面复核

目标环境：`main` 开发环境；不涉及 `prod`

## 目标

开发库切换和候选 backend 严格 PostgreSQL bootstrap 已由用户确认完成。受保护的本地
`docker_cmd.md` 不再检查以下两个临时 Shell 人工确认标志：

- `BIOBIN_DEV_AGENT_SCHEMA_CUTOVER_VERIFIED`
- `BIOBIN_DEV_BACKEND_0128_BOOTSTRAP_VERIFIED`

## 修改边界

只删除上述两个变量对应的 `test ... || { ...; exit 1; }` 门禁行。

继续保留：

- master key、Skill root 和 Skill bundle digest 检查；
- PostgreSQL readiness、外部配置文件和严格配置 bootstrap 检查；
- Runtime Sidecar health 检查；
- 所有容器、网络、端口、volume、镜像、MCP 和运行模式参数；
- 现有子 Shell 与 `set -e`，使其他预检失败时终止本次部署而不退出登录 Shell；
- `docker rm` 仍位于全部剩余预检之后。

不新增数据库查询、自动迁移、兼容逻辑、持久化确认文件或新环境变量。

## 验证

1. 部署命令块通过 `bash -n`。
2. `docker_cmd.md` 中不存在上述两个变量引用。
3. master key 路径和其他真实预检仍存在。
4. `docker_cmd.md` 仍为 `0600`、Git-ignored 且未被跟踪。

## 回滚

如需恢复人工确认门禁，只恢复删除的两条检查；不得修改数据库或回滚已经完成的开发库切换。
