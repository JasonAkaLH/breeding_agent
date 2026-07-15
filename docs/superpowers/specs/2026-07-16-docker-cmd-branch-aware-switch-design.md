# `docker_cmd.md` 分支感知安全切换设计

## 背景与目标

`docker_cmd.md` 包含本地敏感部署信息，必须保持 Git-ignored 且永不进入 Git。单工作树切换 `main` / `prod` 时，Git 不会替换 ignored 文件，导致上一分支的环境命令残留在当前分支。

本设计在只保留一个工作树的前提下实现：

- `main` 始终激活开发环境 `docker_cmd.md`；
- `prod` 始终激活生产环境 `docker_cmd.md`；
- 手工修改、缺失、空文件、环境标记冲突或校验失败时 fail-closed；
- 切换前保留 `0600` 快照，任何流程都不得删除 profile、活动文件或快照；
- 敏感内容不得进入 Git、日志、测试 fixture、命令输出或异常信息。

## 方案比较

### 方案 A：每个环境保留独立 worktree

环境隔离最直接，但需要长期维护多个工作树，与当前“只保留主工作树”的要求冲突，因此不采用。

### 方案 B：仅安装 `post-checkout` hook

可覆盖 CLI 与 IDE checkout，但 hook 运行时分支切换已经发生，无法在覆盖风险出现前真正中止操作，不能独立满足 fail-closed，因此不采用。

### 方案 C：受控切换器 + `post-checkout` 守卫

受控切换器在执行 `git switch` 前完成快照、手工修改检测和目标 profile 校验；本地 hook 作为绕过受控切换器时的第二道守卫。该方案兼顾单工作树、自动恢复与无损失败，确定采用。

## 组件与边界

### 1. 仓库外本地存储

默认目录按平台选择：

- macOS / Linux：`${XDG_CONFIG_HOME:-~/.config}/breeding-agent/docker-cmd/`
- Windows：`%LOCALAPPDATA%\breeding-agent\docker-cmd\`
- 可通过 `BREEDING_AGENT_DOCKER_CMD_HOME` 显式覆盖。

目录权限在支持 POSIX mode 的平台上固定为 `0700`，文件固定为 `0600`。目录结构：

```text
docker-cmd/
├── profiles/
│   ├── main.md
│   └── prod.md
├── snapshots/
│   └── <UTC时间>-<分支>-<原因>.md
├── state.json
└── switch.lock
```

profile 和 snapshot 不自动清理。`state.json` 只记录活动分支、活动 profile 哈希、最近成功切换时间和阻断状态，不保存文件内容、DSN 或其他敏感字段。

### 2. 受控切换器

新增 tracked、无敏感内容的 Python CLI `scripts/manage_docker_cmd.py`：

- `init`：从明确提供的 main/prod 本地文件初始化仓库外 profile；拒绝缺失、空文件、同一文件或环境标记不匹配。
- `status`：只报告分支、profile 是否存在、活动文件是否匹配、权限和阻断状态，不输出内容或完整哈希。
- `sync-current`：显式把当前活动文件同步回当前分支 profile；同步前先创建快照。
- `switch main|prod`：唯一推荐的分支切换入口。
- `post-checkout <old-ref> <new-ref> <flag>`：供本地 Git hook 调用。
- `install-hook`：安装或更新本仓库 `.git/hooks/post-checkout`；如已有非本工具 hook，则拒绝覆盖并报告阻断。

`switch` 顺序固定为：

1. 获取本地互斥锁；
2. 确认当前分支为 `main` 或 `prod`；
3. 验证根 `docker_cmd.md` 存在、非空、`0600`、ignored、untracked；
4. 对根文件创建 `0600` 快照；
5. 将根文件哈希与 `state.json` 中最近成功激活哈希比较；不一致则中止，不执行 Git 分支切换；
6. 验证目标 profile；
7. 执行非交互 `git switch <target>`；
8. 通过同目录临时文件、flush/fsync、`0600` chmod 和原子替换激活目标 profile；
9. 再次验证环境标记、ignored/untracked、文件权限和内容哈希；
10. 原子更新 `state.json`，释放锁。

任何失败均不得删除或清空根文件、profile 或 snapshot。

### 3. `post-checkout` 守卫

直接使用 `git switch`、`git checkout` 或 IDE 切换时：

- 当前根文件仍匹配最近成功激活哈希：验证目标 profile 后原子激活；
- 当前根文件存在未同步修改：先创建快照，写入阻断状态，拒绝覆盖根文件并返回非零；
- 目标不是 `main` / `prod`：不切换文件，只报告该分支不受管理；
- 目标 profile 缺失、为空或校验失败：保持根文件原样，写入阻断状态并返回非零。

由于 Git 的 `post-checkout` 无法撤销已经完成的 checkout，阻断状态必须清楚标记“当前分支与活动 docker 命令不匹配”；后续 `switch`、`sync-current` 和 `status` 必须先处理该状态。正常操作应使用 `switch`，而不是直接执行 Git checkout。

### 4. 环境校验

校验器只判断固定非敏感标记，不解析或打印密码：

- `main`：必须包含开发环境标记、`-dev` 镜像、开发网络和开发数据库名；不得包含生产数据库或生产镜像引用。
- `prod`：必须包含 `MAF_ENV=production`、无 `-dev` 的生产镜像、生产网络、生产数据库名和 `/data/peihai/vibe-breeding-main/skills`；不得包含开发数据库、开发网络或 `MAF_ENV=development`。
- 两者都必须含 backend/frontend 镜像、Auth secret 注入、runtime volume 和 Skill 只读挂载，并通过 Bash 语法检查。

错误只报告缺失或冲突的标记名称，不回显匹配行。

## 原子性与恢复

- 所有敏感文件写入使用同目录临时文件，权限先设为 `0600`，完整写入并 fsync 后才执行 `os.replace`。
- 替换前必须已有快照；替换后哈希不一致则进入阻断状态，但不得删除当前文件。
- 互斥锁使用原子创建目录实现，避免依赖平台专用锁 API；锁元数据不含敏感内容。
- 初始化和切换不会自动删除旧 snapshot，避免留存策略误删唯一副本。
- 当前已有 main/prod 受限备份仅作为一次性迁移源；成功迁移并验证后仍保留，不自动清理。

## Git 与安全边界

- `docker_cmd.md`、仓库外 profile、snapshot、state 和 hook 均不提交。
- tracked 内容仅包括管理脚本、无敏感测试 fixture、设计文档和规则说明。
- 现有 `scripts/check_docker_cmd_policy.sh` 继续阻止 `docker_cmd.md` 被重新跟踪。
- CLI 不接受密码参数，不打印文件正文、DSN、环境变量值或完整哈希。
- Git hook 使用 `git rev-parse --git-path hooks/post-checkout` 安装，兼容普通仓库和 worktree 元数据布局。

## 测试与验收

自动测试使用临时 Git 仓库和虚构占位值，不接触真实文件：

1. 初始化 main/prod profile，验证权限、状态和 ignored/untracked 门禁；
2. main → prod → main 往返后，根文件分别与目标 profile 完整一致；
3. 手工修改根文件后 `switch` 在 checkout 前中止，快照存在且内容未丢失；
4. 直接 checkout 触发 hook 时，未修改文件自动切换，已修改文件快照并阻断覆盖；
5. profile 缺失、空文件、环境标记冲突、权限错误和 Bash 语法错误均 fail-closed；
6. 模拟原子替换失败时，原根文件和快照仍存在；
7. stdout/stderr 不包含测试密码、DSN 或文件正文；
8. 现有 repository policy 门禁继续通过。

真实工作区验收只比较哈希和非敏感状态，不输出内容：

1. 从现有 `0600` main/prod 备份初始化仓库外 profile；
2. 安装本地 hook；
3. 执行 main → prod → main 受控往返；
4. 每次确认分支、环境 profile、权限、ignored/untracked 与预期一致；
5. 最终停留在用户开始实施时所在分支，并保持该分支对应文件激活。

## 非目标

- 不把任何形式的真实或加密凭据提交到 Git；
- 不修改生产服务器、数据库、镜像或容器；
- 不自动清理快照；
- 不支持 `main` / `prod` 之外的环境 profile；
- 不替代组织级 secret manager。
