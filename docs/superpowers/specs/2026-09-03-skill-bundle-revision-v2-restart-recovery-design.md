# Skill Bundle Revision v2 与缺失 Revision 启动隔离设计

状态：`approved_pending_written_review`
日期：2026-09-03
目标分支：`main`

## 1. 问题与目标

开发环境切换到backend-dev `0.1.30`后，FastAPI在startup recovery阶段读取到一个仍可恢复的
Agent Run。该Run的prepared authority固定了旧进程内的Skill revision
`skillrev-000002-f84bc49b3ad8`，而新进程的`SkillRuntimeState`不含这个key，
`bundle_for_revision()`抛出`KeyError`，异常继续穿透lifespan并导致整个服务退出。

旧revision由进程内递增序号和12位fingerprint摘要组成：

```text
skillrev-<process-local counter>-<12 hex fingerprint prefix>
```

序号不能跨重启重建。即使当前挂载的Skill文件与旧任务使用的内容相同，新进程生成的序号也可能不同。
当前系统又只持久化revision字符串，不持久化旧Skill bundle或script package snapshot，因此缺失的旧内容
没有可恢复authority。

本设计完成两个目标：

1. 新Skill bundle改用跨重启稳定、完整内容寻址的revision v2；只要当前挂载内容完全一致，新任务就能
   在重启后重新取得同一个revision。
2. prepared Run引用的revision确实不存在时，不加载当前内容代替旧内容，不重建、不迁移、不猜测；只将
   对应Run安全终止，继续恢复其他Run并完成应用启动。

## 2. 已确认决策

采用以下方案：

- 新格式固定为`skillrev-v2-<64 lowercase hex sha256>`；
- v2摘要沿用当前Skill fingerprint的canonical输入，但使用完整SHA-256，不再拼接进程内计数器；
- v1 revision只有在当前进程的`_bundles`中已经存在精确key时才可继续使用；跨重启缺失时不得根据12位
  后缀映射到当前bundle；
- 缺失revision不得降级到active bundle，也不得删除或改写prepared snapshot；
- 缺失revision只终止受影响的Run，稳定错误码按bundle kind区分；应用startup继续处理其余Run；
- 本轮只升级Skill bundle revision，不修改独立的MCP parser/projection revision，也不升级
  `MCPRuntimeState`的`mcprev-*`格式。

### 2.1 未采用方案

1. **用v1的12位后缀匹配当前bundle**：可以快速恢复，但12位摘要不是完整内容authority，而且会把
   已经不存在的旧revision静默替换成当前对象，违反“不加载相应内容”的决定。
2. **只隔离失败Run，继续生成旧计数器revision**：能恢复服务可用性，但新任务仍无法可靠跨重启恢复，
   同类事故会持续出现。
3. **立即持久化完整Skill/script package snapshot**：可提供最强历史复现能力，但涉及新的持久化格式、
   生命周期、容量、完整性和清理策略；当前`script_package_snapshot=false`阶段不扩大到该范围。

## 3. Revision v2 合同

### 3.1 Canonical摘要

继续复用`SkillRuntimeState._fingerprint_roots()`产生的有序fingerprint。其每一项包含当前规范化文件路径、
文件大小和文件内容SHA-256；忽略规则继续排除`__pycache__`和`.pyc`。将现有canonical文本：

```text
<resolved path>\t<size>:<file sha256>\n...
```

编码为UTF-8后计算完整SHA-256，并生成：

```text
skillrev-v2-<64 lowercase hex>
```

同一部署环境中，只要挂载根路径和全部纳入fingerprint的文件完全相同，跨进程实例必须得到同一个v2
revision。任一纳入文件的路径、大小或内容改变，都必须得到不同revision。内容改回原状态时允许回到
原revision，因为该revision表示内容身份，不再表示进程内刷新次数。

### 3.2 内存快照与保留计数

`_bundles`、`retain_revision()`、`release_revision()`和inactive eviction继续按revision key工作：

- 同内容的force refresh可以用同一个v2 key替换等价active bundle；
- 被运行中任务retain的不同内容revision继续保留在当前进程内；
- 如果内容已经从当前进程和挂载目录消失，重启后不能仅凭revision恢复其bundle；
- 不新增磁盘快照、数据库字段或隐藏fallback。

### 3.3 v1边界

旧格式`skillrev-<6 digit counter>-<12 hex>`仍被视为合法的历史opaque identifier，但不具备跨重启
重建能力：

- exact key仍在当前进程`_bundles`中：按现有行为返回该内存bundle；
- exact key不存在：抛出明确的revision unavailable信号；
- 禁止用后12位与active v2摘要前缀比较后建立alias；
- 禁止把prepared authority中的v1字符串改写成v2；
- 禁止读取当前active bundle来继续原任务。

这是一种“识别旧格式但不伪造旧内容”的兼容，而不是v1内容恢复。

## 4. Startup recovery 数据流

```text
list_recoverable_runs
  → load durable prepared authority
  → restore exact Skill/MCP bundle revisions
       ├─ exact revision exists → retain and recover normally
       └─ exact revision missing
            → do not load active/current content
            → fail only this Agent Run with stable safe code
            → atomically project Task/Node terminal state
            → clear conversation.current_task_id
            → continue with the next recoverable Run
  → startup completes
```

`_restore_prepared_bundle_revisions()`应把bundle lookup的`KeyError`转换为只在startup recovery边界消费的
typed/private异常，至少携带`bundle_kind`，但不得携带或向前端输出完整revision、文件路径或bundle内容。

`_recover_agent_runs()`只捕获这一明确异常。它通过现有Agent Loop terminal writer将Run、Task和适用Node
收口为failed，并记录稳定safe error：

- Skill缺失：`agent_skill_bundle_revision_unavailable`
- MCP缺失：`agent_mcp_bundle_revision_unavailable`

虽然本轮不修改MCP revision生成格式，但共享恢复函数已经处理两种bundle；同一“不加载替代内容、只隔离
受影响Run”的startup行为必须覆盖两种缺失情形。

失败收口成功后清理`conversation.current_task_id`，不执行Agent sampling、Skill、MCP Tool或continuation，
不重放任何副作用。其他recoverable Run继续恢复。

如果terminal writer或必要的Task/current-task持久化失败，startup仍须fail closed：不能在没有持久化终态
证据时静默跳过该Run。并发冲突时只允许重新读取；若Run已由其他owner进入终态可继续，否则传播现有
storage conflict。

## 5. 错误与可见性

| 场景 | 行为 |
|---|---|
| v2 revision与当前bundle精确一致 | 正常retain并恢复 |
| 当前进程仍持有精确v1 bundle | 保持现有进程内恢复行为 |
| v1或v2 revision不存在 | 不加载active bundle；仅失败该Run；startup继续 |
| prepared revision为空 | 保持既有nullable合同，不新增失败 |
| bundle lookup内部出现非“revision不存在”错误 | 不降级，传播并阻断startup |
| 失败终态无法持久化 | 不跳过Run，startup fail closed |

面向用户的任务失败只暴露稳定safe error和通用安全文案。审计允许记录bundle kind、Run/Task现有安全标识
和错误类型，不记录Skill正文、文件列表、绝对路径或完整历史revision。

## 6. 修改边界

预计生产代码只涉及：

- `src/integrations/agent_skills/skill_runtime_state.py`：生成稳定v2 revision；
- `src/api/runtime.py`：typed missing-revision信号、单Run终态隔离和startup继续；
- 必要时在现有Agent Loop私有模块中放置窄异常类型，但不新增公共API。

测试集中在：

- `tests/integrations/agent_skills/test_skill_runtime_state.py`；
- `tests/api/test_submission_admission_runtime_startup.py`；
- 必要的现有Skill dynamic reload和runtime startup回归。

明确不修改：数据库schema/data、prepared snapshot格式、MCP Result Parser/Projection Store、
`MCPRuntimeState` revision格式、Gateway/Selector、Frontend、Rust Sidecar协议、外部Skill内容、外部MCP Server、
公开API DTO/路由和`prod`。

## 7. 验证要求

### 7.1 Revision单元测试

1. 两个独立`SkillRuntimeState`实例读取同一路径和内容，得到完全相同的64位v2 revision；
2. 任一纳入fingerprint的文件变化后revision变化；内容恢复后revision恢复；
3. refresh后的旧bundle在retain期间仍可按exact revision读取，release后按既有规则淘汰；
4. 伪造或缺失的v1/v2 revision均不得返回active bundle；
5. revision格式严格拒绝计数器、短摘要和非小写十六进制作为新写格式。

### 7.2 Startup recovery测试

1. prepared Run引用缺失Skill v1 revision时，不读取active bundle、不调用恢复执行，只将该Run/Task失败；
2. 缺失Skill v2 revision具有同样行为；
3. 缺失MCP revision走独立safe error，但不修改MCP revision生成；
4. 第一条Run因revision缺失安全失败后，第二条有效Run仍被恢复，证明应用不会因单Run退出；
5. terminal writer失败或非missing lookup错误仍阻断startup；
6. 失败Run清除current task，不执行Agent sampling、Skill/MCP Tool或副作用重放；
7. 原有prepared exact facts、waiting、lease retry、terminal release和动态Skill刷新测试保持通过。

### 7.3 相关门禁

运行聚焦Skill/API startup测试，然后运行Integrations、Orchestration、API和E2E相关回归、compileall、Ruff、
package import与`git diff --check`。如果实施后发布新backend镜像，还需独立验证远端OCI `linux/amd64`、
镜像不含`/app/config.yaml`，并在开发环境证明旧v1 Run被单独终止、API成功启动、新建v2 Run可完成一次
重启恢复。构建、推送和部署仍需实施阶段的明确授权。

## 8. Rollout 与回退

首次部署revision v2 binary时，已有且仍可恢复的v1 prepared Run若其内存bundle已随旧进程消失，将按设计
失败；不会恢复、改写或重放。新提交任务写入v2 revision。

一旦任何环境写入v2 prepared authority，不得回退到只能理解旧计数器revision的binary。安全cutback下限
必须保留v2生成和读取；可以向前修复startup隔离，但不能把v2 revision截短或改回v1。回退业务部署前需
先证明没有非终态v2 Run，否则停止。

## 9. 完成定义

只有以下条件全部满足，才可声明仓库实施完成：

- 新写Skill revision稳定为`skillrev-v2-<64 hex>`且跨独立进程状态可复现；
- 缺失v1/v2 revision不加载active/current内容、不改写prepared authority；
- 单个缺失revision Run安全失败且不阻断其他Run与应用startup；
- 失败持久化异常继续fail closed；
- 所有聚焦与相关自动门禁通过；
- 无数据库schema/data、Frontend、Rust、MCP parser/projection或`prod`变化。

当前仅完成设计并获用户原则确认；尚未生成实施计划、修改生产代码、处理远端失败Run、构建镜像或部署。

License Requirement：复用现有Python、SHA-256 fingerprint、Agent Run terminal writer、prepared authority与
unittest；不新增依赖、第三方代码或许可变化。
