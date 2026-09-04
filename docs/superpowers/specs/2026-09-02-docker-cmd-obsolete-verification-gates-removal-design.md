# docker_cmd 一次性部署校验清理设计

状态：已实施

目标环境：`main` 开发环境；不涉及 `prod`

## 目标

开发库切换、候选 backend 严格 PostgreSQL bootstrap 和 Skill revision v2 hard cut 已经
成功部署过多次。受保护的本地 `docker_cmd.md` 不再保留只服务于首次上线的校验逻辑。

此前批准删除的两个临时 Shell 人工确认标志在当前文件中已经不存在：

- `BIOBIN_DEV_AGENT_SCHEMA_CUTOVER_VERIFIED`
- `BIOBIN_DEV_BACKEND_0128_BOOTSTRAP_VERIFIED`

本次新增清理目标为：

- 完整删除 `skill_revision_v2_audit()` 函数；
- 删除该函数专用的 `MAF_SKILL_V2_AUDIT_DSN` 提取、计数、导出与清理；
- 删除 `pre` 和 `post` 两个调用点。

## 修改边界

只删除上述一次性 audit 函数及其专用 plumbing，不改变正常部署流程。

继续保留：

- master key、Skill root 和 Skill bundle digest 检查；
- PostgreSQL readiness、外部配置文件和严格配置 bootstrap 检查；
- 旧 backend 精确删除、删除结果确认及其他 writer 冲突检查；
- backend 启动后的健康等待；
- Runtime Sidecar health 检查；
- 所有容器、网络、端口、volume、镜像、MCP 和运行模式参数；
- 现有子 Shell 与 `set -e`，使其他预检失败时终止本次部署而不退出登录 Shell；
- `docker rm` 仍位于全部剩余预检之后。

不新增数据库查询、自动迁移、兼容逻辑、持久化确认文件或新环境变量。

## 验证

1. 部署命令块通过 `bash -n`。
2. `docker_cmd.md` 中不存在 `skill_revision_v2_audit` 和
   `MAF_SKILL_V2_AUDIT_DSN` 引用。
3. master key 路径和其他真实预检仍存在。
4. `docker_cmd.md` 仍为 `0600`、Git-ignored 且未被跟踪。
5. 修改前在仓库外建立 `0600` 备份；验证失败时从该备份恢复。

## 回滚

如需恢复本次清理，从仓库外备份恢复完整文件。不得修改数据库、远端服务或已经完成的
开发库与 Skill revision v2 切换。
