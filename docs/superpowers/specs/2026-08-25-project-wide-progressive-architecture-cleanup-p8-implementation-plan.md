# 全仓业务代码渐进式架构清理 P8 实施计划

## 1. 状态与硬边界

- 日期：2026-08-25
- 分支：`main`
- 状态：`complete`
- P8 start commit：`ff0a65d9f39bb7fb500a48dc2a069f3dc4690259`
- P8 start tree：`e078560140bd3d7ea7e808bad60a5b4aaba277dc`
- P8 start tracked set：1093
- P8 start path-list SHA-256：`3e3f10d884fd1f464d033a057ca76a52b5a5a03a8ba8369aa1407b8b2b5666be`

P8不重新开启架构拆分，只处置当前HEAD重新证明为private/零生产引用/无动态入口/无副作用的dead或exact duplicate，并闭合P0 inventory与最终全仓门禁。公开compat facade、测试seam、registration/manifest/cfg/PyO3/tonic入口、安全authority、masking fallback及任何行为修复均不删除。

## 2. 当前inventory与finding register

P0 inventory 1045 paths到P8 start新增48、删除0：25 business、16 tests、7 phase plans。原320个business中59 changed、261 reviewed_no_change；新增25 business均changed，当前345 business=`84 changed + 261 reviewed_no_change`。

| Finding | 分类 | P8处置 |
|---|---|---|
| `P8-AUTH-PORT-001` | missed handoff | `UsernameTokenService` annotation由aggregate `StoragePort`改为既有`AuthStoragePort`；6个unique methods/调用体不变 |
| `P8-PY-F401-001` | dead import | 删除API 3个、Integrations/Orchestration 4个已证明无patch/export/import side effect的unused names |
| `P8-PY-F841-001` | dead local | 删除Prompt `best_tokens`两次纯赋值；删除`answer_interrupt`旧`resume_capability_id`赋值与只为其存在的pure registry分支 |
| `P8-PY-F841-002` | deferred trace | `_schedule_v2_slot_resume`同名dead chain涉及`await get_task_node`异常/I/O trace；保留并解释，最终F841允许1项 |
| `P8-FE-UNUSED-001` | dead local/import | 删除`Flex`与logout未读locals；保留`beginRestoreGeneration()`裸调用副作用 |
| `P8-FE-DUAL-SSE-001` | dead old implementation | 删除仅测试引用的browser `EventSource` factory及其专用测试；production authenticated fetch-SSE唯一owner |
| `P8-FE-DEAD-EXPORT-001` | dead private/export | 删除仅测试引用的`replayTaskEvents`、`interruptVisibleFieldNames`及两个全仓零引用DTO；必要测试只移除对dead API的断言 |
| `P8-FE-DUP-ATTACHMENT-001` | exact duplicate | keep-open Interrupt分支调用既有`markDraftAttachmentsSent`，state update时点/参数不变 |
| `P8-FE-CSS-001` | dead CSS | 删除零引用`.interrupt-card*`、`.composer-attachments`、`.upload-file-tag` blocks |
| `P8-NATIVE-DEAD-001` | dead private field | 删除Skill `LimitedReaderState.done`未读字段、初始化/写入与test fixture字段；Receiver同步不变 |
| `P8-FE-TERMINAL-SET-001` | reviewed_no_change | App与transport terminal sets字面相同，但建立domain→transport或transport→domain边会形成错误方向/循环；不为小常量新建共享模块 |
| `P8-EXACT-15-001` | reviewed_no_change | Python 15组exact body均为error class、invalidation、gRPC frame、runtime response、trivial cleaner、安全/minute、retain/release等既有不同authority/错误/协议；不合并 |
| `P8-PUBLIC-SEAMS-001` | reviewed_no_change | MCP registries/feature marker、Rust root wrappers、SQLite base/models re-export、四条StoragePort export、ApiRuntime公开aggregate annotation、MathJax reset均为解释过的compat/test/manifest seam |

## 3. Checkpoints

### Checkpoint A：计划与基线

基线：Python Ruff为7 F401 + 3 F841，三语句以上exact body 15组；Frontend strict unused 3项、production import graph 26节点全可达；Native Clippy clean且只有`LimitedReaderState.done`一个确证未读private field。P1 aggregate扫描只有Auth service一个未解释内部consumer。

提交：`docs(cleanup): plan P8 final dead-code audit`

### Checkpoint B：闭合Auth窄port handoff

只修改`src/auth/services.py` import与`UsernameTokenService.__init__.storage`延迟注解为`AuthStoragePort`。不修改runtime对象、constructor调用、storage method、顺序或error。补现有adoption test的直接断言，使除冻结公开`ApiRuntime.__init__.storage: StoragePort`和facade/implementer外内部aggregate consumer为0。

运行Auth API、SQLite Auth repository、P1 contract/adoption tests。提交：`refactor(auth): adopt narrow storage port`

### Checkpoint C：删除已证明Python unused项

- API删除`MCPToolExecutor`、fallback build/sanitize偶然binding；模块仍由其它canonical imports加载；
- `answer_interrupt`删除所有未读`resume_capability_id`赋值及末尾无effect registry分支，保留task-node lookup、MCP binding、metadata、schedule顺序；
- Agent Skills删`os`/`Mapping`，Result Parser删`Any`，Agent invocation删`NodeStatus`；
- Prompt binary search删`best_tokens`初始化/赋值，token counting与最终strip recount不变；
- 第二条slot-resume F841保留为`deferred_trace`，不制造`pass`或dummy local。

运行Ruff、相关47+32项focused、compileall；提交：`refactor(cleanup): remove proven Python dead bindings`

### Checkpoint D：Frontend dead/duplicate收尾

分两个可回滚批次：

1. `App.tsx`删除strict-unused import/locals，保留generation invalidation裸调用；keep-open attachment disposition改用原helper；
2. 删除browser SSE旧factory、两个test-only production helper、两个零引用DTO与三组CSS；只删/调整这些dead API的专用测试，不改变fetch-SSE、reducer、Interrupt或DOM业务tests。

`TERMINAL_TASK_EVENT_TYPES`不合并；P6 effects/DOM/ARIA/CSS active selectors不改。运行strict noUnused、Frontend full、typecheck、build与production reachability/exact-block scan。

提交：

- `refactor(frontend): reuse attachment disposition helper`
- `refactor(frontend): remove unreachable legacy surfaces`

### Checkpoint E：Native dead field

删除`LimitedReaderState.done`、唯一初始化、唯一写入及test fixture字段；`LimitedReaderHandle.done: Receiver<()>`、thread send、deadline/snapshot/error行为原样。运行Skill Runtime/PyO3、workspace fmt/Clippy及相关Python contract/sandbox。

提交：`refactor(skill-runtime): remove unread stdio state`

### Checkpoint F：最终inventory、依赖与全仓门禁

Inventory必须闭合：

- 当前tracked universe=`P0 1045 + 48 additions + P8 plan`，无unclassified/deleted path；
- 原320与新增25 business按owner给出最终`changed|reviewed_no_change`计数；P8 plan为validation dependency；
- 新增cross-owner layer pair=0；既有P3→P2、P5→P2/P3明确`baseline_reviewed`；
- 未解释内部aggregate consumer=0，新旧双实现=0，orphan facade=0；
- Python exact 15组全部有reviewed理由，Ruff=0 F401 + 1 deferred F841；Frontend strict unused=0；Native private dead scan=0。

最终业务commit后运行：

- Backend canonical全量及Scripts full；真实PG/Linux只按未触及N/A准确记录；
- Frontend full、typecheck、strict noUnused、production build；
- Rust workspace fmt、Clippy、cargo test、nextest、coverage、audit、deny、provenance与MCP fuzz；
- contract/Proto/Cargo/public surface、tracked set/path hash、dependency/license、file mode、`git diff --check`及`docker_cmd.md` metadata保护。

同步本计划、P0 baseline终态附录、`docs/AGENTS.md`、根/受影响目录`AGENTS.md`与`CHANGELOG.md`。提交：`docs(cleanup): close P8 project-wide proof`

## 4. 停止与回滚

若删除项存在动态/字符串/registration/manifest consumer，若RHS/branch删除改变调用、异常、日志、event、timer、DOM或transaction，若测试必须修改业务期待，若需要新跨层共享模块或行为修复，则停止该candidate并记`reviewed_no_change|deferred`。

每个checkpoint独立commit并可逆序revert。P8完成不授权部署`prod`、删除仓外备份、读取受保护正文或修复任何延期行为。

## 5. 终态实现账本

| Checkpoint | Commit | 终态 |
|---|---|---|
| A 计划与基线 | `41a2c88` | 冻结P8 finding、停止条件与验证矩阵 |
| B Auth窄port | `8e5b5a3` | `UsernameTokenService`采用`AuthStoragePort`，调用体与runtime对象不变 |
| C Python dead bindings | `3989b62` | F401清零；只保留1项trace-sensitive F841 |
| D Frontend收尾 | `2ef4fe2`、`84fa7aa` | 复用附件helper，删除不可达旧SSE/test-only exports/DTO/CSS |
| E Native dead field | `fdc6940` | 删除`LimitedReaderState.done`，Receiver同步链不变 |
| F 最终证明 | 本提交 | inventory、合同与全仓门禁闭合 |

P8业务实现HEAD=`84fa7aa19179dff1ca1e1d1e5a62bcdd9e14d5d7`，tree=`c33456c48afe2868abfc929f9a8d222ee7f864e9`。P8相对start未修改Cargo/Proto/checked-in contract、公开Rust root、数据库schema/data、transaction/CAS、生产依赖或`prod`。

## 6. 最终inventory与静态审计

- tracked set=`1094`，排序路径清单SHA-256=`b0dd66c0d71371a198dc06e1a5ac273351634ce3b6518973ce6ed2c02540c6b7`；相对P0 final的1045条只新增25个business、16个test与8份P1～P8计划，删除0、未分类0；
- 当前345个business=`93 changed + 252 reviewed_no_change`。P0原320个按owner的changed计数为P1=1、P2=7、P3=24、P4=8、P5=18、P6=5、P7=5，余252保持reviewed；25个新增business均为各阶段已验证的owner实现；
- 新增cross-owner layer pair=0；既有P3→P2、P5→P2/P3依赖保持`baseline_reviewed`。内部aggregate `StoragePort` consumer=0；公开`ApiRuntime` annotation、四条re-export及SQLite/PostgreSQL facade仍为compatibility seams；
- Python Ruff=`0 F401 + 1 F841`，唯一F841为`_schedule_v2_slot_resume`的I/O/异常trace保留项；三语句以上exact body=15组且全部按authority/协议/error边界复核为`reviewed_no_change`；C901=158，仅作度量，不为指标拆分；
- Frontend strict noUnused=0，production import graph无不可达节点，三语句以上exact function body=0；旧browser SSE与test-only production surfaces已清除，authenticated fetch-SSE保持唯一owner；
- Native Clippy `-D warnings` clean，P8唯一确证未读private field已删除；无新旧双实现或orphan facade。

## 7. 最终验证

| 门禁 | 结果 |
|---|---|
| Python compile + Backend/Agent Skills/Scripts/Deployment | 2,165项通过；Storage 7项真实PostgreSQL与Integrations 2项Linux gate为环境N/A |
| Frontend | 24 files / 320 tests通过；typecheck、strict noUnused、production build通过；保留既有>500 kB chunk warning |
| Rust fmt / Clippy / cargo test / nextest | 全部通过；nextest 149/149，保留1个既有leaky标记 |
| Rust coverage | workspace 84.26%、Skill Runtime 92.86%、MCP Runtime 92.97%；全部超过既定阈值 |
| Rust audit / deny / provenance | 通过；保留已允许的`anyhow` `RUSTSEC-2026-0190` warning及传递依赖duplicate warnings |
| MCP protocol fuzz | 30秒、1,099,890 runs、无crash；macOS `atos`只影响symbolization；生成corpus/target已清理 |
| Contract / public surface | StoragePort、Rust public surface、Proto/checked-in contract与production Cargo相对P8 start零diff；相关合同测试通过 |
| Repository hygiene | `git diff --check`通过；`docker_cmd.md`存在、mode `0600`、仍被忽略且未跟踪，正文从未读取 |

`skill/sql-query`按仓库既定结构为Git-ignored的外部只读挂载，不存在于当前工作树，因此旧组合命令的可选子目录步骤记为N/A；其缺失不替代任何当前tracked业务测试。真实PostgreSQL、Linux、manylinux、外部MCP与`prod`未被P8触及或冒充为通过。

## 8. 退出结论

P0～P8在原设计边界内闭合。所有P8 candidate均已处置为已验证修改、`reviewed_no_change`或明确延期；未新增行为修复、跨层抽象、dependency、schema/data migration或部署动作。后续工作如需处理保留的F841、C901、terminal set、跨authority exact body或既有行为缺陷，必须另立目标，不能继续借用本P8授权。
