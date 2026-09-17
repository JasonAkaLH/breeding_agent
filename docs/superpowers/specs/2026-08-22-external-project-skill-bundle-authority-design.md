# External Project Skill Bundle Authority Design

- 日期：2026-08-22
- 状态：方案 A 与路由分工已批准；document-perfectization 四轮审查以100/100 `Pass`
- 适用分支：`breeding_agent/main`与`vibe-breeding/dev`
- 上游门禁：统一 Agent Loop P6-A required no-skip pre-cutover proof
- 目标：恢复一份可版本化、可独立回滚、可由 clean archive 复验的权威 Project Skill bundle，不把 Skill 重新 vendoring 进 `breeding_agent`。

参与者与职责：

- Agent/Skill runtime 维护者：维护 parser、catalog、digest gate 与主仓 required contract；
- Project Skill 维护者：维护 `vibe-breeding/dev:skills/` 内容、schema、script、asset 和自有测试；
- 发布/安全审查者：确认双仓版本、bundle digest、只读挂载与成对回滚证据；
- P6 operator：只使用已绑定 archive 执行 clean rehearsal，不修改 Skill 或部署 `prod`。

## 1. 问题与决策

`breeding_agent` 已明确不跟踪根目录 `skill/`；开发部署从外部仓库的 `skills/` 目录只读挂载到
`/app/skill`。当前 P6-A 环境无该权威挂载，Agent Skill canonical suite 因此有 43 项 skip。已找到独立
`vibe-breeding` 仓库的 `dev` 分支；它包含大部分目标 Skill，但与 2026-08-22 主仓合同有 8 项失配，且缺少
Mini BreedStat RCBD Skill。

已复现基线：

- `breeding_agent` 设计起点 commit `97639c7`、tree `a517da2d19574df2d3d56e8bae56b8086cf311eb`；
  无外部 bundle 时 Agent Skill discover 为 200 项通过、43 项 skip；
- `vibe-breeding/dev` 起点 commit `0e56ad33e89be42c97a2a1e972319f370824fd5d`、tree
  `f9682e681cee74b6a6463cd9d0f7fd10078fcb49`；与主仓 clean archive 组合时运行 200 项，产生 6 failures、2 errors、6 skips；
- 再加入 `d38952c` 历史 Mini bundle 的聚焦 14 项诊断为 6 failures、3 errors，证明必须执行 v2 manifest 迁移和路由分工，
  不能直接复制历史目录后宣称通过。
- clean archive 中的 API output-contract 聚焦诊断返回 400，safe detail 为`No model reasoning_efforts config is available`，
  证明失败源是 Git-ignored 本地模型配置缺失，不是外部 Skill output contract。

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

bundle digest 对 archive/挂载中 `skills/` 下的全部普通文件计算：按 UTF-8 POSIX 相对路径字节序排序，每行编码
`path NUL size NUL sha256 LF`，再对全部行计算 SHA-256，闭合格式为`sha256:<64-lower-hex>`。计算忽略`.git`、
`__pycache__`和`.pyc`；其他未绑定文件会改变 digest。非 UTF-8 路径、符号链接、socket/device 和越界路径直接拒绝。
evidence 从 clean Git archive 计算 expected digest；runtime 从只读挂载计算 actual digest，两者使用同一纯函数和测试向量。

### 3.2 Runtime digest gate

- 纯函数与闭合错误实现位于`src/integrations/agent_skills/bundle_digest.py`；`src/api/runtime.py::build_api_runtime`在它通过后才调用
  `SkillRuntimeState.from_roots`/`SkillCatalog.from_roots`。这两个调用顺序由 spy 测试锁定。
- 新增闭合配置`MAF_PROJECT_SKILL_BUNDLE_DIGEST`，值必须符合`sha256:<64-lower-hex>`；不允许请求级覆盖或多值 fallback。
- 默认 runtime 的第一个 public Skill root 若包含任何`*/SKILL.md`，则 expected digest 必填；缺失或不匹配必须在构建
  `SkillCatalog`和注册 capability 之前失败。空 public root 可在未配置 digest 时启动。
- 测试显式注入`skill_roots/public_skill_roots`时，只在同时注入 expected digest 时执行该 gate；这个 seam 只用于单元/API fixture，
  不是部署 bypass。P6 clean archive 和 Docker smoke 必须显式传 expected digest。
- 闭合失败码为`project_skill_bundle_digest_required`、`project_skill_bundle_digest_invalid`、
  `project_skill_bundle_digest_mismatch`和`project_skill_bundle_unsafe_entry`。启动日志/指标只记录 result、file_count、total_bytes、duration_ms
  与 digest 前 12 位，不记录 Skill 正文、schema 值或绝对路径。

### 3.3 本地接入

`breeding_agent/skill` 只可作为 Git-ignored 的本地符号链接，指向已绑定的外部 bundle checkout。它不进入 index、archive、
stash 或任何 Git object。clean archive 验证在仓库外临时目录解压两个 archive，然后为代码 archive 建立指向
Skill archive `skills/` 的临时链接。

## 4. Skill 修复边界

### 4.1 Mini BreedStat RCBD

从 `breeding_agent` 已审计历史提交 `d38952c` 恢复完整 R/Python 算法、HTML renderer 和示例数据，不重写统计实现。
恢复后将旧 manifest 适配为当前 v2 `skill.contract.yaml`，保留 `mini-breedstat-rcbd`、`skill.mini_breedstat_rcbd`、
`blocks`/`material_data` 缺输入、原始上传 artifact 和 HTML output contract。不改 RCBD 算法与样例期望。

v2 迁移必须明确以下字段：

- `SKILL.md` frontmatter 只保留`name`/`description`，不保留 v1 `parameters`、`scripts`、`auto_run`或 capability 字段；
- `skill.contract.yaml` 定义 capability `skill.mini_breedstat_rcbd`、非空 display name、`python_subprocess`、`direct`、
  `scripts/run_rcbd.py` auto-run entrypoint、RCBD input schema 和 required `answer`、optional HTML file output；
- input schema 必须定义 required `blocks`/`material_data`，以及闭合`planter`/`seed`/`site_num`/`site_random`；
- public resources 至少列出两份示例数据；R/Python scripts 仅为 executable resources，不作为 public profile 正文。

### 4.2 Field Design

以主仓 v2 合同为准：Diagonal/Interval 的 `ncols` 恢复为 required，question 使用主仓安全、闭合文案；材料文件问题保留
`ped_id`/`hyb_check`/`set` 推荐列。补齐已批准的对角线/间比法 aliases，不降低 schema validation。

### 4.3 Mini BreedStat / Field Design 路由分工

不修改 matcher 算法，通过不重叠的闭合 routing triggers/examples 达到以下归属：

| 意图 | 必须排名第一的 Skill |
|---|---|
| 显式 RCBD、随机完全区组、重复/区组数、对照位置约束 | `mini-breedstat-rcbd` |
| 通用“田间试验设计”、Diagonal、Interval | `field-design` |
| 未显式包含 RCBD 或对照位置约束的 fieldbook/layout 请求 | `field-design` |

主仓新增双 Skill 同时存在的 route matrix 回归，逐条锁定上表及两份现有查询集。任一无 match、并列依赖字母序或
归属反转都是失败。

### 4.4 Field Analysis

修复 wrapper 在被 `importlib` 直接加载时的 script-local import，不依赖当前工作目录或全局 `PYTHONPATH`。补齐 RCBD/LSD 查询
tokens，保留现有计算后端边界和 UTF-8 Rscript 环境。

### 4.5 Rice Genie

补齐“统计优良变异并解读”等已批准查询 tokens，不修改 VCF/QTN 解析、SQLite asset 或 HTML report contract。

### 4.6 Bundle-wide 合同

全部 Project Skill 必须具有唯一 capability ID、display name、闭合 runtime mode、至少一个 input schema 或明确
`platform_service` 边界、只允许 public resource 投影。任何无效 bundle 必须在 startup/canonical test fail closed，不得部分注册。

## 5. 实施与数据流

1. 从两仓 clean HEAD 开始，记录原 commit/tree。
2. 在 `vibe-breeding/dev` 恢复 Mini BreedStat 受审历史文件，再执行必要的 v2 manifest 适配。
3. 手术式修复 Field Design、Field Analysis 和 Rice Genie，运行每个 Skill 自有测试和结构校验。
4. 在主仓的 Git-ignored `skill` 链接中运行完整 Agent Skill/API 回归。API `APITestCase` 必须使用仓库内非敏感
   model-edition fixture，不得隐式依赖 Git-ignored `config.yaml`。
5. 将外部 commit/tree、bundle digest、主仓 commit/tree 和命令结果写入 `cutover-readiness.md`。
6. 在仓库外解压双 clean archive，建立临时 Skill 链接。Agent Skill 命令直接运行；API suite 使用上述内建 fixture。
   Docker startup smoke 仅在临时 build context 中将新增的非敏感`tests/fixtures/unified_agent_loop_clean_archive_config.yaml`
   复制为`config.yaml`，该 fixture 只包含 fake model edition/reasoning/tool-call capability，不含 credential、DSN 或外部 endpoint。
7. 使用只读 bind mount 启动候选 backend，注入精确 expected digest，证明 `/app/skill` 可发现且宿主 Skill 文件不可由容器写入。
8. P6-A 所有 no-skip 门禁通过后才冻结双 archive rollback checkpoint，然后进入 P6-B。

## 6. 错误处理与回滚

- 外部仓有未归属修改：原地保留并停止，不 stash/reset。
- 历史 Mini BreedStat 恢复后行为漂移：只允许修正 manifest/runner 适配，统计核心不重写；仍不通过则 P6-A 保持 blocked。
- 两仓版本不匹配、expected digest 缺失/非法或 actual digest 漂移：候选 backend 在 catalog/capability 注册前 fail closed，
  不加载未绑定 bundle，不 fallback 到空 bundle 或旧 bundle。
- clean archive 缺本地模型配置：不复制开发者`config.yaml`；API 使用代码 fixture，Docker smoke 使用上述跟踪的非敏感 YAML。
- 任一 required test skip/non-zero：不冻结 archive，不进入 P6-B。
- 回滚必须成对 `git revert` 外部 Skill commit 和主仓 evidence/integration commit；不移动分支指针，不使用 reset。
- 本设计不授权部署 `prod`、push 远端、变更外部服务或读取 `docker_cmd.md`。

## 7. 验证与验收

必须全部满足：

1. `vibe-breeding` 受影响 Skill 自有测试通过，无未跟踪 runtime cache进入 Git。
2. Skill 编写阶段在当前 Codex 主机使用 active `skill-creator` locator 解析`quick_validate.py`，并通过
   `conda run -n multi_agent python <resolved-quick-validate-path> <skill-dir>`。该结果绑定外部 Skill commit，但不是 clean archive/CI
   required gate，因为交付环境不保证安装 Codex Skill 包。P6 required gate 使用主仓 v2 manifest/catalog validation。
3. `tests/integrations/agent_skills` 完整 discover 实际发现每个测试，零 skip、零失败。
4. 聚焦 API output-contract/Skill execution 回归及 API canonical suite 通过。
5. 主仓 `git ls-files skill` 为空，`skill` 仍被忽略，不进入 code archive。
6. 双 clean archive 环境中重复主仓 v2 manifest/catalog validation及第 3～4 项，不依赖 Codex 本地 Skill 包，结果与工作树一致。
7. Docker backend 以精确 Skill archive 只读挂载后可发现全部 Skill，写入尝试失败。
8. `cutover-readiness.md` 绑定两仓 commit/tree、bundle digest、命令结果和 rollback 方法，P6-A 才可从 blocked 转 green。

聚焦 digest/startup 验收必须覆盖：

| 验收对象 | 必须证明的边界 |
|---|---|
| `tests/integrations/agent_skills/test_project_skill_bundle_digest.py` | 空目录、确定性向量、文件内容/路径/大小漂移、cache排除、非UTF-8/符号链接/特殊文件拒绝、file/byte上限、可注入clock的deadline |
| `tests/api/test_project_skill_bundle_startup_gate.py` | expected缺失/格式错/不匹配的闭合错误；成功时先验digest再建 catalog；失败时零capability部分注册；空root、显式test injection seam和无请求级override |
| log/metric sanitizer tests | 只包含闭合result/count/bytes/duration/digest prefix，不包含正文、schema值、绝对路径或完整digest |
| Docker startup smoke | 真实只读挂载、expected/actual相等、小于2秒、Skill可发现、容器写入失败；更改一份非cache文件后新容器必须启动失败 |

### 7.1 非功能门禁

- digest 计算只读本地文件，不发起网络或执行 Skill script；
- bundle 上限为 1,000 个普通文件、256 MiB，超限以闭合错误拒绝；当前候选基线为 111 个文件、约 11 MiB；
- 候选 Docker 中 digest 校验必须在 2 秒内完成，超时即 startup 失败；
- 校验必须确定性、跨 clean checkout 一致，不记录正文或绝对路径；
- Skill 路由和 script 回归不访问外部网络；Rice Genie 只读 bundle 内 SQLite asset。
- Mini BreedStat 恢复只使用主仓已存在的`d38952c`对象；修订前后核对两仓 license/依赖文件，不引入新的第三方代码、远程asset或runtime依赖。

## 8. 非目标

- 不改写 RCBD、Field Analysis 或 Rice Genie 业务算法；
- 不新增 Skill marketplace、网络下载、热更新或远程发布流程；
- 不修改 P6 Agent Loop 产品决策或降低 required no-skip 口径；
- 不部署 `prod`，不 push 任何远程分支。
