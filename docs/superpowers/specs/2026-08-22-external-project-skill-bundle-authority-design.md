# External Project Skill Bundle Authority Design

- 日期：2026-08-22
- 状态：方案 A 已批准；书面设计待用户复核
- 适用分支：`breeding_agent/main`与`vibe-breeding/dev`
- 上游门禁：统一 Agent Loop P6-A required no-skip pre-cutover proof
- 目标：恢复一份可版本化、可独立回滚、可由 clean archive 复验的权威 Project Skill bundle，不把 Skill 重新 vendoring 进 `breeding_agent`。

## 1. 问题与决策

`breeding_agent` 已明确不跟踪根目录 `skill/`；开发部署从外部仓库的 `skills/` 目录只读挂载到
`/app/skill`。当前 P6-A 环境无该权威挂载，Agent Skill canonical suite 因此有 43 项 skip。已找到独立
`vibe-breeding` 仓库的 `dev` 分支；它包含大部分目标 Skill，但与 2026-08-22 主仓合同有 8 项失配，且缺少
Mini BreedStat RCBD Skill。

批准决策：

- `vibe-breeding/dev:skills/` 是 Project Skill 唯一源码 authority；
- `breeding_agent/main` 是 runtime/parser/API 与 P6 required contract authority；
- 两仓通过精确 commit、tree 和 bundle digest 绑定，不通过未版本化的本地副本绑定；
- 主仓继续忽略 `skill/`，不复制、追踪或打包外部 Skill 源码。

## 2. 备选方案与取舍

### A. 独立权威 Skill 仓库（采用）

在 `vibe-breeding/dev` 修复 bundle，将双仓精确版本写入 P6 证据。优点是保留当前部署和安全边界，Skill 可单独
审阅、发布和回滚。代价是 clean archive 和回滚必须成对管理两个仓库。

### B. 将 Skill 重新纳入主仓（拒绝）

可以简化单仓测试，但违反根 `AGENTS.md`的外部只读挂载边界，还会让 backend 发布和 Skill 发布重新耦合。

### C. 修改主仓测试接受当前漂移 bundle（拒绝）

能减少红测，但会放宽已批准的 P6 required contract，且无法解决缺失 Mini BreedStat 和不可重现的部署来源。

## 3. 权威、版本与文件边界

### 3.1 双仓 authority

| 对象 | Authority | 证据 |
|---|---|---|
| Agent Skill parser/runtime/API 行为 | `breeding_agent/main` | commit + tree + canonical tests |
| Project Skill 内容、schema、script、asset | `vibe-breeding/dev:skills/` | commit + tree + normalized bundle digest |
| 开发部署挂载 | `/data/peihai/vibe-breeding-dev/skills:/app/skill:ro` | Docker read-only mount smoke |
| P6-A 本地/clean archive 复验 | 两仓 archive 的临时成对 checkout | 两个 archive digest + 零 skip 命令 |

bundle digest 对 `skills/` 下所有受 Git 跟踪的文件计算：按 POSIX 相对路径字节序排序，每行编码
`path NUL size NUL sha256 LF`，再对全部行计算 SHA-256。不包含 `.git`、`__pycache__`、`.pyc` 和未跟踪文件。

### 3.2 本地接入

`breeding_agent/skill` 只可作为 Git-ignored 的本地符号链接，指向已绑定的外部 bundle checkout。它不进入 index、archive、
stash 或任何 Git object。clean archive 验证在仓库外临时目录解压两个 archive，然后为代码 archive 建立指向
Skill archive `skills/` 的临时链接。

## 4. Skill 修复边界

### 4.1 Mini BreedStat RCBD

从 `breeding_agent` 已审计历史提交 `d38952c` 恢复完整 R/Python 算法、HTML renderer 和示例数据，不重写统计实现。
恢复后将旧 manifest 适配为当前 v2 `skill.contract.yaml`，保留 `mini-breedstat-rcbd`、`skill.mini_breedstat_rcbd`、
`blocks`/`material_data` 缺输入、原始上传 artifact 和 HTML output contract。不改 RCBD 算法与样例期望。

### 4.2 Field Design

以主仓 v2 合同为准：Diagonal/Interval 的 `ncols` 恢复为 required，question 使用主仓安全、闭合文案；材料文件问题保留
`ped_id`/`hyb_check`/`set` 推荐列。补齐已批准的对角线/间比法 aliases，不降低 schema validation。

### 4.3 Field Analysis

修复 wrapper 在被 `importlib` 直接加载时的 script-local import，不依赖当前工作目录或全局 `PYTHONPATH`。补齐 RCBD/LSD 查询
tokens，保留现有计算后端边界和 UTF-8 Rscript 环境。

### 4.4 Rice Genie

补齐“统计优良变异并解读”等已批准查询 tokens，不修改 VCF/QTN 解析、SQLite asset 或 HTML report contract。

### 4.5 Bundle-wide 合同

全部 Project Skill 必须具有唯一 capability ID、display name、闭合 runtime mode、至少一个 input schema 或明确
`platform_service` 边界、只允许 public resource 投影。任何无效 bundle 必须在 startup/canonical test fail closed，不能令无关 API 测试
在 submit 时产生 400。

## 5. 实施与数据流

1. 从两仓 clean HEAD 开始，记录原 commit/tree。
2. 在 `vibe-breeding/dev` 恢复 Mini BreedStat 受审历史文件，再执行必要的 v2 manifest 适配。
3. 手术式修复 Field Design、Field Analysis 和 Rice Genie，运行每个 Skill 自有测试和结构校验。
4. 在主仓的 Git-ignored `skill` 链接和 clean archive 中分别运行完整 Agent Skill/API 回归。
5. 将外部 commit/tree、bundle digest、主仓 commit/tree 和命令结果写入 `cutover-readiness.md`。
6. 使用只读 bind mount 启动候选 backend，证明 `/app/skill` 可发现且宿主 Skill 文件不可由容器写入。
7. P6-A 所有 no-skip 门禁通过后才冻结双 archive rollback checkpoint，然后进入 P6-B。

## 6. 错误处理与回滚

- 外部仓有未归属修改：原地保留并停止，不 stash/reset。
- 历史 Mini BreedStat 恢复后行为漂移：只允许修正 manifest/runner 适配，统计核心不重写；仍不通过则 P6-A 保持 blocked。
- 两仓版本不匹配或 digest 漂移：候选 backend fail closed，不加载未绑定 bundle。
- 任一 required test skip/non-zero：不冻结 archive，不进入 P6-B。
- 回滚必须成对 `git revert` 外部 Skill commit 和主仓 evidence/integration commit；不移动分支指针，不使用 reset。
- 本设计不授权部署 `prod`、push 远端、变更外部服务或读取 `docker_cmd.md`。

## 7. 验证与验收

必须全部满足：

1. `vibe-breeding` 受影响 Skill 自有测试通过，无未跟踪 runtime cache进入 Git。
2. 所有 Skill 通过 `skill-creator` `quick_validate.py`及主仓 v2 manifest/catalog validation。
3. `tests/integrations/agent_skills` 完整 discover 实际发现每个测试，零 skip、零失败。
4. 聚焦 API output-contract/Skill execution 回归及 API canonical suite 通过。
5. 主仓 `git ls-files skill` 为空，`skill` 仍被忽略，不进入 code archive。
6. 双 clean archive 环境中重复第 2～4 项，结果与工作树一致。
7. Docker backend 以精确 Skill archive 只读挂载后可发现全部 Skill，写入尝试失败。
8. `cutover-readiness.md` 绑定两仓 commit/tree、bundle digest、命令结果和 rollback 方法，P6-A 才可从 blocked 转 green。

## 8. 非目标

- 不改写 RCBD、Field Analysis 或 Rice Genie 业务算法；
- 不新增 Skill marketplace、网络下载、热更新或远程发布流程；
- 不修改 P6 Agent Loop 产品决策或降低 required no-skip 口径；
- 不部署 `prod`，不 push 任何远程分支。
