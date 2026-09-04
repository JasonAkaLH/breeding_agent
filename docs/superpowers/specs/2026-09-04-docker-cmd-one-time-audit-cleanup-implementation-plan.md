# docker_cmd 一次性部署校验清理实施计划

状态：complete

目标环境：`main` 开发环境；不涉及 `prod`

## 完成声明

受保护的本地 `docker_cmd.md` 不再包含 `skill_revision_v2_audit()` 及其专用 DSN
plumbing，同时所有长期部署门禁、启动参数和容器替换顺序逐字保持。

## Checkpoint A：备份与基线

1. 确认文件存在、权限为 `0600`、被 Git ignore 且未被跟踪。
2. 在仓库外建立唯一 `0600` 备份并复验内容摘要相同。
3. 记录目标函数、专用变量和两个调用点的非零引用计数。

## Checkpoint B：精确删除

1. 以 `skill_revision_v2_audit() {` 和其 heredoc/函数闭合边界删除完整函数。
2. 删除 `MAF_SKILL_V2_AUDIT_DSN_COUNT`、`MAF_SKILL_V2_AUDIT_DSN`、`export`、
   `pre`/`post` 调用和专用 `unset`。
3. 不修改相邻 master key、Skill digest、PostgreSQL readiness、配置 bootstrap、
   writer 冲突、Sidecar/backend health 或容器启动命令。

## Checkpoint C：验证与失败恢复

1. 从 Markdown 提取唯一 Bash fenced block并运行 `bash -n`。
2. 断言所有目标引用归零，所有保留门禁标记仍存在。
3. 断言除目标行外，修改前后的非目标行序列完全一致。
4. 复验文件仍为 `0600`、存在、Git-ignored 且未被跟踪。
5. 任一检查失败即从仓库外备份恢复，并重新验证保护状态。

## Checkpoint D：文档与交付

1. 把设计、计划和 CHANGELOG 状态更新为完成，但不记录敏感内容。
2. 检查最终 Git diff；`docker_cmd.md` 不进入 Git 对象。
3. 提交并推送仅含非敏感文档状态的 Git 检查点。

## 回滚

回滚只使用本次仓库外 `0600` 备份恢复完整 `docker_cmd.md`。不修改数据库、远端服务、
容器或已完成的开发环境切换。
