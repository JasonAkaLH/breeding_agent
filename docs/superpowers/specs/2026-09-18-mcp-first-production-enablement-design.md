# 用户自定义 MCP 首次生产启用

状态：`draft_for_user_review`。本文细化用户已确认的首次直接启用方向；尚未修改业务代码或生产数据库。

## 1. 目标与现状

生产原来没有用户自由配置 MCP 的功能。本次将该功能首次上线：用户级 Gateway 开启、enforce 100%、cohorts 为空、旧全局 Runtime 关闭，保持真实的 prod/production 环境身份。用户上线后自行添加 MCP Server 和 Tool 授权；不迁移旧用户 MCP 配置、凭据，也不复制开发库中的用户 MCP 数据。

当前准入限制与旧数据是否存在无关：`src/api/runtime.py` 在生产 routing 非 off 时强制要求专用 PostgreSQL app 身份、activation 和实例租约；`scripts/postgres/user_mcp_rollout_permissions.sql` 又在数据库写入接口中验证阶段转换。因此只跳过 Python 的一处检查不能完成首次启用。

配置文件、主密钥和 Sidecar 信任材料已由用户在服务器准备并验证。现有 Backend 镜像尚不支持本文的首次启用入口。正式库的结构升级仍未执行，旧库恢复演练不能替代修改后的重新验收。

## 2. 方案选择

| 方案 | 实际影响 | 结论 |
|---|---|---|
| 部署前单独执行一次管理员初始化 | 复用数据库迁移窗口；初始化完成后，常驻 Backend 继续使用受限身份和既有准入检查 | 推荐，作为本文方案 |
| Backend 启动时自动初始化 | 必须增加具备管理权限的启动阶段，以及多实例竞争和权限退出机制 | 会扩大当前启动流程，不采用 |
| 关闭生产发布准入检查 | 会连同后续配置匹配、租约和安全阻断一起改变 | 不符合保留后续校验的要求，不采用 |

初始化属于一次性部署操作，不是 HTTP 接口，也不是常驻服务可以开启的豁免开关。之后的普通启动不得尝试补建或修复准入记录。

## 3. 初始化命令与执行边界

新增一个独立管理脚本，提供只读检查和显式执行两种模式。检查报告只含数据库名称、角色身份、发布标识、配置摘要、表行数及阻塞原因；不得输出 DSN、密码、API Key、主密钥或 MCP 凭据。

输入为预期数据库名称、本次发布标识、已验收的源码版本和 Backend 镜像摘要，以及实际部署使用的 MCP 配置。MCP 配置沿用当前显式环境变量，不要求新增 `seedpilot_prod.env`。

执行使用独立的临时管理员连接，实际 `session_user` 必须与 `current_user` 一致且为 superuser；本次沿用已用于数据库迁移的 postgres 身份，不使用 SET ROLE 冒充。管理员 DSN 仅传入一次性容器，常驻 Backend 明确拒绝接收这一新变量。日常 MCP app、operator 等角色的既有权限矩阵不扩大。

部署命令先核验候选镜像的 platform、revision、digest，再从固定镜像运行管理脚本。Backend 镜像只增加该脚本及需要交付的固定 SQL 文件，不整体复制 scripts 目录。源代码和生产参数继续分离。

## 4. 允许首次启用的条件

只允许 PostgreSQL；声明的数据库与 `current_database()` 必须一致。本次目标是 `biobin_db`，验证库使用独立且显式指定的名称。

首次写入须同时满足：

- 所需 schema、约束及 PostgreSQL 分角色权限已经安装，生产配置、主密钥与 Sidecar 信任检查通过。
- Gateway=true、routing=enforce、percent=100、cohorts 为空、legacy=false，使用现有终态 stage `legacy_assembly_off`。这个内部名称表示最终配置，不表示执行过旧 MCP 迁移。
- 数据库中没有用户 MCP Server、Tool 授权、分支/调用、远端任务/密封状态、连接/作用域租约、健康尝试、MCP 审计或旧 MCP 配置迁移记录。
- 没有既有 rollout evidence、approval、activation、实例租约、指标、演练或 shadow 记录；没有未解决的 rollout 安全 blocker。
- 新功能的写入方保持停止。初始化不删除或修改已有记录来满足条件。

上述空数据条件只针对 MCP 功能及其发布记录。已有会话、消息、Task、Artifact、登录记录、文件，以及纯 schema/bootstrap 元数据不要求为空。不得把生产库清空后再初始化。

## 5. 真实且可追溯的首次启用记录

复用现有 gate scope、evidence、approval、activation 表，不新增业务表。新增明确的“部署初始化”来源、producer 和 `first_enablement` evidence 类型，与 CI 和生产观察证据区分。

首次证据使用独立的封闭 payload：包含版本号、实际数据库名称、经部署命令核验的镜像摘要、数据库校验得到的 MCP 初始状态摘要及初始化身份。已有通用记录字段继续承载 environment、deployment、源码 SHA、配置 fingerprint、时间和记录 ID。

不得填造 CI 成功标记、历史调用样本、影子比较结果或观察时长。首次证据不宣称完成原阶段观察；其可信写入来源是经过身份核验的管理员部署操作及同一事务内取得的数据库状态。它不冒充具有生产观察 HMAC 的 snapshot，也不放宽既有 CI/production attestation 规则。

新增类型的验证必须限定 source、producer、kind 和 payload 的组合；普通 evidence 写入、阶段升级和回滚入口不得借该类型跳过既有要求。旧 payload 的序列化、摘要与签名保持逐字兼容，不通过给旧 payload 增加默认字段改变历史 digest。

初始化在同一数据库事务中登记首次证据、批准和 activation，目标 activation 为 `legacy_assembly_off`，没有 previous activation。审批原因如实记录为新功能首次上线。专用 rollout app 身份无权直接写入这些表；append-only 保护继续有效。

## 6. 并发、重试与正常启动

执行前取得数据库级初始化事务锁，并按固定顺序锁定参与空数据判定的 MCP 表及发布记录表。在锁内重新核对数据、权限和预检摘要，再一次性写入；任一步失败全部回滚。锁等待或检测到新写入时停止，不扩大权限或重试覆盖。

同一发布、数据库、源码/镜像、配置和记录标识的重复执行返回已有完整记录，不重复写入。这个判定先于首次空数据检查，因此上线后产生用户配置不会破坏同一初始化操作的精确重试。发现不同发布、标识或配置、部分记录、已有后续 activation 或安全 blocker 时拒绝复用。

正常 Backend 启动继续通过既有 app 连接、activation 匹配、实例租约和安全 blocker 检查。实例不能在启动失败时自行生成新 activation。用户隔离、逐 Tool 授权、Endpoint 策略、密钥派生和 Sidecar 信任保持既有实现。

## 7. 数据库和发布影响

新增 evidence 来源和类型需要更新对应 CHECK 约束及 schema manifest。提供独立、明确列出目标约束的兼容升级 SQL：接受原有记录，并允许首次启用记录的严格组合。初始化命令不隐式执行 DDL。

最终 public 表数量仍为 66，但 schema manifest/checksum 会改变。必须用新 Backend 工件重新完成恢复库结构升级、23 张保留表数据保全和首次启用演练。此前 24→23→66 的备份恢复结果保留为基线，不将旧镜像的验收冒认为新工件验收。

代码和相关 schema 定义同步到 main/prod；不自动修改开发数据库或重启开发服务。已有开发库需要的兼容约束升级作为下一次开发部署的显式前置步骤交付。

Backend 必须重新构建、验证并发布，记录新 digest；保留旧候选 digest 及镜像回溯信息。Sidecar 的二进制和既有信任文件可继续使用，但必须与新 Backend 重做兼容检查。Frontend 的 MCP 功能实现不在本改造范围。

正式生产仍按停写、最终备份、结构升级、权限准备、首次初始化、只读准入校验、服务启动顺序执行。正式写入前只交付已演练的精确命令。

## 8. 验收与改动范围

验收至少覆盖：

1. 有历史会话/Task、但 MCP 从未启用的恢复库成功初始化；23 张保留表内容不变。
2. 缺表、权限不符、错误数据库、错误发布配置、已存在 MCP 数据或旧发布记录时拒绝，且没有部分写入。
3. 同一操作重复执行幂等；不同环境/发布/镜像/配置拒绝复用；两个并发初始化最多产生一组记录。
4. 在证据、批准、activation 的各写入阶段注入失败，事务均完整回滚。
5. 新 Backend 首次启动和重启可通过既有租约检查；缺少/伪造记录、配置不符、权限不足、工件不受信任及安全 blocker 仍会阻止启动或新请求。
6. 既有 CI/production evidence 的摘要、签名及普通阶段转换回归通过；新增 evidence 不能走普通写入入口取得首次启用权限。
7. 独立 PostgreSQL 实例中的真实角色测试通过后，再使用服务器恢复库和新镜像演练；不直接以正式库作调试环境。

改动集中于首次初始化命令、evidence 类型及验证、schema CHECK 定义/兼容 SQL、镜像交付、相关测试和部署前检查。复用现有 Python、SQLAlchemy、PostgreSQL 和 Docker 能力，不增加第三方依赖，不改 MCP 调用业务链，不实施旧用户 MCP 数据迁移。

本文经静态自查覆盖首次空状态、已有历史业务数据、幂等、并发、角色边界、旧 evidence 兼容及新工件重验；这些是实现验收要求，不是已通过的测试结果。

License Requirement：设计复用现有依赖，无新增依赖或许可例外。
