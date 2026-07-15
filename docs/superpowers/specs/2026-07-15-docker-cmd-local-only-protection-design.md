# `docker_cmd.md` 本地专用保护设计

## 目标

根目录 `docker_cmd.md` 包含本地敏感部署信息。该文件必须在开发者工作区保留，但不得出现在任何 Git 分支、标签、提交、stash、reflog 或远端历史中。

## 保护边界

1. `docker_cmd.md` 由根目录 `.gitignore` 的 `/docker_cmd.md` 规则排除，文件内容不提供 tracked 模板或副本。
2. 根目录 `AGENTS.md` 禁止删除、移动、重命名、清空、输出敏感内容，以及通过强制 add、stash、历史或其他 Git 对象传输该文件。
3. `scripts/check_docker_cmd_policy.sh` 只检查仓库策略，不读取敏感文件内容：路径不得被 Git 跟踪，根目录忽略规则必须存在且有效。
4. GitHub Actions 在 `main`、`prod` 的 push 与所有 pull request 上执行门禁。Gitee 等不执行 GitHub Actions 的远端仍由相同脚本提供手工或外部 CI 入口。
5. 分支切换、仓库清理、工作树移除或历史重写前，必须先在仓库外保留权限不高于 `0600` 的备份；操作结束后验证本地文件仍存在、被忽略且未被跟踪。

## 已知边界

Git 无法保护一个未跟踪文件免受操作系统级删除。仓库规则负责约束 Agent 和阻止重新提交；真正的灾难恢复依赖仓库外的受限权限本地备份。已经暴露过的密码、token、DSN 或其他凭据仍必须轮换，历史重写不能撤回既有克隆、缓存或日志中的副本。
