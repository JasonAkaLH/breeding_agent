# 全仓业务代码渐进式架构清理 P8 实施计划

## 1. 状态与硬边界

- 日期：2026-08-25
- 分支：`main`
- 状态：`active`
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
