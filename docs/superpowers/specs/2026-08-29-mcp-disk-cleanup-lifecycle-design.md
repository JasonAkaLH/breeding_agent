# MCP 磁盘清理生命周期最小修复设计

状态：`written_review_pending`
日期：2026-08-29
目标分支：`main`

## 1. 问题与目标

当前用户级 MCP 磁盘数据已有多条独立生命周期，但存在两个生产接线硬伤：

1. `MCPTemporaryResultJanitor`只在backend启动时运行一次，运行期间产生的无manifest崩溃残留必须等到
   下次重启才会清理；
2. `MCPPendingActionPayloadStore.cleanup_orphans()`和
   `delete_with_terminal_evidence()`已有严格实现与测试，但生产runtime没有调用，pending action加密payload
   可能长期累积。

本设计只闭合这两个缺口。成功标准是：正常terminal action及时释放payload；commit后崩溃残留在24小时
保护期后释放；无manifest临时结果在1小时安全年龄后周期释放；任何可恢复authority不被误删。

## 2. 范围

### 2.1 范围内

- backend恢复完成后启动唯一MCP disk cleanup后台任务；
- 启动后立即运行一轮，之后固定每小时运行；
- 周期调用现有临时结果orphan janitor；
- terminal durable evidence提交后best-effort调用现有pending payload精确删除；
- 增加一个窄存储查询，返回仍需保护的pending payload refs；
- 周期调用现有24小时pending orphan cleanup；
- SQLite、PostgreSQL与Runtime装配/关闭测试。

### 2.2 明确排除

- 不修改durable raw result的60秒Artifact reconciler；
- 不修改managed Artifact、会话删除和审计30天保留；
- 不修改磁盘水位、结果大小、并发、授权、input-required或recovery合同；
- 不新增表、列、migration、环境变量、生产metric、audit schema或通用janitor框架；
- 不修改Frontend、Rust Sidecar、外部MCP Server、Skill或部署镜像。

## 3. 方案选择

采用生命周期感知的双层清理：正常终态以durable evidence精确删除，后台任务负责崩溃窗口与临时orphan。

未采用仅增加timer的方案，因为consumed action仍可能持续保护文件；未采用纯文件年龄扫描，因为会误删长期
等待授权、input-required或恢复中的有效payload。

## 4. 数据与删除边界

### 4.1 临时结果

继续使用`MCPTemporaryResultJanitor.cleanup_orphans()`的现有合同。仅删除同时满足以下条件的目录：

- 名称属于既有`task-*`私有目录；
- 不属于当前store中的active task；
- 不含durable manifest；
- 最后修改时间超过1小时安全年龄。

durable raw result继续由既有60秒reconciler在Artifact ownership持久化后删除，后台任务不得处理它。

### 4.2 Pending payload正常终态

普通Call的`completed`、`failed`、`cancelled` receipt，或`unknown` terminal projection成功持久化后，
才允许构造既有`MCPPendingActionPayloadDeletionEvidence`并调用
`delete_with_terminal_evidence()`。删除发生在durable terminal commit之后；失败不回滚Task、Call、receipt或
projection，只记录低敏错误并等待周期清理。

`input_required`不属于terminal evidence，必须保留payload。

### 4.3 Pending payload保护集合

新增一个窄存储查询，只返回仍需保护的`arguments_payload_ref`：

- action为`proposed`、`waiting_approval`或`approved`；
- action为`consumed`，但关联Call仍为active、input-required或其他可恢复非终态；
- action为`consumed`且Call已声明终态，但对应receipt或unknown projection尚未完整持久化。

`denied`、`invalidated`以及已有完整terminal evidence的consumed action不进入保护集合。查询只返回opaque ref，
不返回参数、路径、owner或正文。

周期任务把该集合交给现有`cleanup_orphans()`；后者只删除不受保护、超过24小时且通过既有文件类型、mode、
owner与ref校验的文件。这样正常终态后的删除崩溃窗口也会最终收敛。

## 5. Runtime生命周期

`ApiRuntime`只增加一个后台Task和一个固定3600秒sleep seam：

1. startup恢复与现有MCP authority reconciliation完成；
2. 创建后台Task；Task进入后立即执行首轮，不阻塞API readiness；
3. 每轮分别执行临时结果cleanup和pending payload cleanup；
4. 两类cleanup各自捕获异常，一类失败不阻止另一类；
5. 每轮结束后等待3600秒；
6. shutdown取消并`gather(..., return_exceptions=True)`等待唯一Task退出。

不增加运行时配置；测试通过注入sleep seam控制时间。

## 6. 失败与观测

- cleanup统一fail-open：保留文件、记录固定低敏错误、下一小时重试；
- 不记录路径、payload ref、action ID、用户、参数、正文、DSN或secret；
- 成功清理不逐文件记录日志；
- 不把cleanup失败升级为Task/Call失败，也不终止backend；
- 文件identity、mode、owner、link或digest不满足既有合同继续fail closed，不尝试宽松删除。

## 7. 验证

### 7.1 临时结果

- 后台Task启动即运行一轮，之后每小时运行；
- active、未满1小时和带manifest目录保留；
- 超过1小时、非active、无manifest目录删除；
- cleanup异常后下一类仍执行，下一轮仍可重试。

### 7.2 Pending payload

- waiting approval、approved、input-required与recoverable refs均受保护；
- terminal evidence不完整时保留；
- ordinary terminal与unknown terminal完整后正常路径精确删除；
- 模拟terminal commit后删除前崩溃，24小时内保留，满24小时后周期删除；
- SQLite与PostgreSQL保护集合语义一致；
- 文件校验失败只记录低敏失败，不删除。

### 7.3 Runtime

- startup只创建一个cleanup Task；
- shutdown准确取消并等待；
- cleanup不产生LLM、MCP网络或外部服务调用；
- 现有durable result reconciler、Artifact与audit保留回归保持green。

## 8. 回滚

单个实现commit回滚即可恢复当前行为。回滚不删除文件、不修改数据库、不需要migration；已存在的pending
payload和临时orphan继续保留，之后可重新应用修复清理。
