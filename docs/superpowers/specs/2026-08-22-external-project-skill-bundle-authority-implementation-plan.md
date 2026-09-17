# External Project Skill Bundle Authority Implementation Plan

- 日期：2026-08-22
- 状态：待执行
- 主仓：`breeding_agent/main`
- Skill 仓：`vibe-breeding/dev`
- 设计依据：`docs/superpowers/specs/2026-08-22-external-project-skill-bundle-authority-design.md`（document-perfectization 四轮 100/100 `Pass`）
- 上游目标：解除统一 Agent Loop P6-A 最后一项 external Skill required no-skip 阻断，冻结双仓 clean rollback checkpoint。

## 1. 完成声明

仅在以下事实同时成立时宣称本计划完成：

1. `vibe-breeding/dev:skills/` 包含受审的 Mini BreedStat RCBD v2 bundle，Field Design/Analysis、Rice Genie 合同对齐；
2. 主仓对外部 bundle 执行确定性 digest、安全上限与 pre-catalog startup fail-closed；
3. Mini BreedStat 和 Field Design 同时存在时，路由矩阵全部归属正确；
4. Agent Skill canonical discover 零 skip/零失败，API canonical suite 不依赖 Git-ignored 本地配置；
5. 两仓 clean archive 重复同样门禁，Docker 以 exact digest 只读挂载通过发现/禁写/漂移拒绝 smoke；
6. `cutover-readiness.md` 绑定两仓 commit/tree、archive digest、bundle digest、命令和成对回滚证据；
7. P6-A 才转 green；此前不进入 P6-B，不修改生产 route，不部署 `prod`。

## 2. 全局安全与 Git 约束

- 开始每个 checkpoint 前检查两仓 branch/status；任一仓出现未归属修改则保留原位并停止，不 stash/reset。
- 主仓 `skill/` 始终 Git-ignored 且不进入 index/object/archive；本地链接只用于工作树回归。
- 不读取、移动、跟踪或删除 `docker_cmd.md`；每个主仓 commit 前只检查 exists/ignored/untracked。
- 不读取或复制 Git-ignored `config.yaml`；clean archive 只使用新增的非敏感 test fixture。
- 不 push 两仓远程，不变更当前运行开发数据，不操作外部网络服务。
- 外部 Skill 修复和主仓集成分开 commit；双仓回滚使用正常 `git revert`，不移动分支指针。

## 3. Checkpoint S0：冻结双仓起点与红测

### 任务

1. 确认主仓`main`和 Skill 仓`dev`均 clean，记录 commit/tree。
2. 在主仓外解压当前两仓 archive，建立临时`skill -> <skill-archive>/skills`。
3. 复现三组起点：无 bundle 43 skips；当前 bundle 8 failures/errors + 6 skips；恢复旧 Mini 后的 v1/route 红测。
4. 检查外部仓 license/依赖文件与 `d38952c` 恢复对象，记录无新第三方依赖。

### 验证

```bash
git status --short
git branch --show-current
git rev-parse HEAD
git rev-parse HEAD^{tree}
git -C ../vibe-breeding status --short
git -C ../vibe-breeding branch --show-current
git -C ../vibe-breeding rev-parse HEAD
git -C ../vibe-breeding rev-parse HEAD^{tree}
conda run -n multi_agent python -m unittest discover -v -s tests/integrations/agent_skills -p 'test_*.py'
```

Green gate：红测数量和 safe reason 与设计基线一致，没有未知失败或两仓污染。

## 4. Checkpoint S1：Bundle digest 纯函数与 operator

### 主仓文件

- 新增`src/integrations/agent_skills/bundle_digest.py`；
- 新增`tests/integrations/agent_skills/test_project_skill_bundle_digest.py`；
- 新增`scripts/validate_project_skill_bundle.py`与`tests/scripts/test_validate_project_skill_bundle.py`；
- 按需更新`src/integrations/agent_skills/__init__.py`、受影响`AGENTS.md`和`CHANGELOG.md`。

### 实现合同

- 实现`ProjectSkillBundleDigest`安全结果与闭合错误码；
- 按设计的`path NUL size NUL file_sha256 LF`序列计算`sha256:<64-lower-hex>`；
- 忽略`.git`/`__pycache__`/`.pyc`，其他文件全部纳入；
- 拒绝非UTF-8路径、symlink、特殊文件、越界、>1,000文件、>256 MiB和可注入clock的>2秒deadline；
- operator 只输出safe JSON：root不输出，digest可完整输出到 evidence，错误不含文件正文。

### 验证

```bash
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_project_skill_bundle_digest
conda run -n multi_agent python -m unittest tests.scripts.test_validate_project_skill_bundle
conda run -n multi_agent python -m compileall -q src tests scripts
git diff --check
```

Green gate：确定性向量、漂移、排除、unsafe entry、容量和deadline边界全部通过。

建议主仓 commit：`feat(skill): verify external bundle digest`。

## 5. Checkpoint S2：Pre-catalog startup gate 与 clean API fixture

### 主仓文件

- 修改`src/api/runtime.py`的`build_api_runtime`与 Skill assembly；
- 新增`tests/api/test_project_skill_bundle_startup_gate.py`；
- 修改`tests/api/support.py`，内建最小非敏感 model-edition/reasoning/tool-call config；
- 新增`tests/fixtures/unified_agent_loop_clean_archive_config.yaml`；
- 修改`docker-compose.yml`，仅传递`MAF_PROJECT_SKILL_BUNDLE_DIGEST`，不改变既有只读挂载目标；
- 更新安全日志/低基数指标与对应测试。

### 实现合同

- 默认 public root 有 Skill 时，`MAF_PROJECT_SKILL_BUNDLE_DIGEST`必填；空root可无digest启动；
- 格式错、缺失、不匹配、unsafe/limit/deadline 都在`SkillRuntimeState.from_roots`之前失败；
- 显式 test root 仅在同时注入 expected digest 时开启 gate，生产/default assembly 无 bypass；
- 失败时零 capability 部分注册，无空/legacy bundle fallback；
- 日志/指标仅包含设计 allowlist，不包含正文、schema值、绝对路径或完整digest；
- API tests 在 clean archive 无`config.yaml`时使用内建 fixture，不降低生产 reasoning/model gate。

### 验证

```bash
conda run -n multi_agent python -m unittest tests.api.test_project_skill_bundle_startup_gate tests.integrations.agent_skills.test_output_contract
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
git diff --check
```

Green gate：digest 先于catalog、所有失败闭合、API full suite 脱离本地敏感配置。

建议主仓 commit：`feat(skill): gate project bundle at startup`。

## 6. Checkpoint S3：恢复 Mini BreedStat RCBD 并迁移 v2

### Skill 仓文件

- 从主仓`d38952c:skill/mini_breedstat_rcbd_skill/`机械恢复到`skills/mini_breedstat_rcbd_skill/`；
- 保留`run_rcbd.py`、`run_rcbd_local.R`、`rcbd_design_core.R`、`render_rcbd_layout_html.R`和两份示例；
- 将`SKILL.md`缩减为只含`name`/`description`的轻量入口和必要运行指导；
- 新增`skill.contract.yaml`和`schemas/rcbd.input.yaml`。

### v2 合同

- capability：`skill.mini_breedstat_rcbd`，display name 非空；
- runtime：`python_subprocess` + `direct`；entrypoint 为`run_rcbd.py`、auto-run；
- required input：`blocks`、`material_data`；optional：`planter`、`seed`、`site_num`、`site_random`；
- required output：`answer`；optional public file：HTML；
- public resources：两份示例；scripts 可执行但不投影到 PublicSkillProfile。

### 验证

```bash
conda run -n multi_agent python <resolved-quick-validate-path> ../vibe-breeding/skills/mini_breedstat_rcbd_skill
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_mini_breedstat_rcbd_skill tests.integrations.agent_skills.test_project_skill_manifest_contract
```

Green gate：历史算法样例与6项 Mini 回归全通过，v1 泄漏为零，capability 可注册/执行。

## 7. Checkpoint S4：对齐 Field Design/Analysis、Rice Genie 与路由

### Skill 仓修改

- Field Design：Diagonal/Interval `ncols.required=true`，恢复闭合 question、`ped_id/hyb_check/set`推荐列和主仓 aliases；
- Field Analysis：wrapper 以 script-local 路径加载 renderer/preflight，补 RCBD/LSD 查询 tokens；
- Rice Genie：补“统计优良变异并解读”及等价 routing tokens；
- Mini/Field Design：按设计路由矩阵去除泛化重叠，不修改 matcher。

### 主仓修改

- 新增`tests/integrations/agent_skills/test_project_skill_route_authority.py`，同时加载两个 Skill；
- 锁定 Mini 显式 RCBD/重复/对照位置与 Field Design 通用/Diagonal/Interval/泛 fieldbook 归属；
- 锁定无字母序 tie，同时保留两份原有查询集。

### 验证

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.agent_skills.test_field_design_skill \
  tests.integrations.agent_skills.test_input_schema_validation \
  tests.integrations.agent_skills.test_missing_input_interrupt_contract \
  tests.integrations.agent_skills.test_field_analysis_skill \
  tests.integrations.agent_skills.test_rice_genie_skill \
  tests.integrations.agent_skills.test_project_skill_route_authority
conda run -n multi_agent python -m unittest discover -s ../vibe-breeding/skills/field-design/tests -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s ../vibe-breeding/skills/rice-genie/tests -p 'test_*.py'
```

Green gate：已知8项失配全部闭合，无新路由反转、远程I/O或算法变更。

建议 Skill 仓 commit：`fix(skills): align project bundle contracts`。

建议主仓 commit：`test(skill): lock external bundle route authority`。

## 8. Checkpoint S5：工作树全量零 skip 门禁

### 任务

1. 主仓 Git-ignored `skill` 链接指向 exact Skill commit 工作树。
2. 对全部 Skill 运行 local `quick_validate.py`，结果绑定 Skill commit，不作 clean archive 依赖。
3. 运行 Agent Skill full discover，要求零 skip/零失败；运行 integrations/API/observability/scripts canonical suites。
4. 运行 full storage canonical with seven isolated PostgreSQL databases，确认上一阻断仍闭合。
5. 运行 Rust required gates 和 Frontend 三门禁，避免 P6-A 证据过期。
6. 逐条运行统一 Agent Loop README“验证口径”的全部 Backend canonical 目录：core、storage、lifecycle、integrations、
   agent_skills、orchestration、main_agent、mcp_dispatch、mcp_tool、API、E2E、observability、scripts和deployment；下方聚焦命令不可替代该全集。
7. `tests/integrations` 的 required authority 是 Linux candidate 环境内的完整 discover 零 skip。macOS 宿主上两项
   Linux-only skip 只作诊断，不记为 required pass；Linux 运行不得只跑那两项聚焦测试替代完整 discover。

### 主命令

```bash
conda run -n multi_agent python -m compileall -q src tests scripts
conda run -n multi_agent python -m unittest discover -s tests/integrations/agent_skills -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/scripts -p 'test_*.py'
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt --only cargo_clippy --only cargo_test
# 另在候选 Linux backend/test image 内运行：
python -m unittest discover -s tests/integrations -p 'test_*.py'
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

Green gate：所有 required 命令在其声明的权威平台中非零测试、零 skip、exit 0；任一缺工具/缺环境都保持 blocked。

## 9. Checkpoint S6：双 clean archive、Docker 与回滚冻结

### 双 archive

1. 从两仓 clean commits 生成仓库外 tar archive，记录 SHA-256；
2. 解压到两个新临时目录，建立临时 Skill 链接；
3. 计算 bundle digest，必须与工作树 evidence 一致；
4. 运行 v2 manifest/catalog、Agent Skill full discover、聚焦 API和 API canonical suite；
5. 在临时 build context 将跟踪的 safe fixture 复制为`config.yaml`，不使用开发者配置。

### Docker smoke

- 使用候选 code archive 构建新的唯一测试 image tag；
- 以 Skill archive `skills/` 只读 bind mount 到`/app/skill`，注入 exact digest；
- readiness 通过且 catalog 可发现全部 Skill；
- 容器内新建文件必须失败；
- 仓库外克隆一份 Skill archive，修改一个非cache文件后以旧 digest 启动，必须在 catalog 前失败；
- 删除测试容器/image和临时数据，不影响现有 backend/卷。

### Evidence 与状态

更新：

- `docs/prd/backend/unified-agent-loop/cutover-readiness.md`；
- `docs/prd/backend/unified-agent-loop/README.md`；
- Phase 6 PRD、统一 Agent Loop implementation plan、`docs/AGENTS.md`、主 PRD 索引与`CHANGELOG.md`。

记录：两仓 commit/tree、bundle/archive digest、门禁计数、Docker image ID、只读/漂移 smoke、最后 DAG rollback authority 和成对 revert 顺序。

Green gate：P6-A 从`blocked`转 green，生成最后 DAG clean rollback checkpoint；仍不改变 route。

建议主仓 commit：`docs(agent): freeze dual-repo pre-cutover checkpoint`。

## 10. 回滚矩阵

| 失败位置 | 动作 | 禁止 |
|---|---|---|
| S1/S2 主仓红测 | 正常 revert 对应主仓 commit | reset/checkout 覆盖用户工作树 |
| S3/S4 Skill 红测 | 保留旧 Skill authority，正常 revert Skill commit | 重写统计核心、放宽主仓测试 |
| S5 full gate 红测 | 两仓保持已提交但 P6-A blocked，修复后重跑 | 把 skip 记为pass |
| S6 archive/Docker 红测 | 删除仅测试的容器/image/temp，修复证据链 | 触碰现有后端、卷或生产数据 |
| P6-A 冻结后回滚 | 成对 revert 主仓和 Skill 仓 commit，恢复双 archive | 只回退单仓、使用未绑定 bundle |

## 11. 计划完成后的下一步

P6-A green 后回到`2026-08-22-unified-agent-loop-implementation-plan.md` P6-B，按原定单一受审 commit 序列切全部入口并删除
DAG runtime/wiring。本计划不替代 P6-B/P6-C 或 P7 门禁。
