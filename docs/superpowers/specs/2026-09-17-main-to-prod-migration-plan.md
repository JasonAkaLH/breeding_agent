# main → prod 首次迁移计划

状态：`source_synced_not_deployed`。按用户最新范围，本次以 `main@3ff5aef465e1ba11d766910de3da3a29e9c4df9d` 为源码基线迁入 `prod`，保留原 prod 历史；业务源码、测试、Dockerfile、Nginx 和本地开发 Compose 与该 main 提交一致，仅增加生产入口说明和迁移状态记录。生产参数由本地 `docker_cmd/docker_cmd_prod.md` 和外部配置承载。本次未新增 MCP 初始化逻辑，未发布镜像、执行生产 DDL 或替换生产容器。下列数据库、生产配置及部署步骤仍是后续工作。

源码同步验证：后端/部署定向 129 项、前端全量 353 项、前端类型检查和 production build、Python 编译、部署文件保护与相对 main 的差异检查通过。构建仅有既有大于 500 kB 的 chunk 提示；未执行 Rust 全量门禁、真实生产数据库迁移或生产启动验收，不将上述源码测试等同于上线验收。

## 1. 目标与已确认边界

把主工程 `main` 的最新功能迁移到 `prod`，保留生产数据、历史文件和生产环境隔离；先完成生产参数适配与隔离演练，再安排停写切换。

| 项目 | 本次生产目标 |
|---|---|
| 主工程环境分支 | 开发 `main`；生产 `prod` |
| Backend 镜像 | `registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend-prod:0.1.23` |
| Frontend 镜像 | `registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-frontend-prod:0.1.23` |
| Runtime Sidecar 镜像 | `registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-runtime-sidecar-prod:0.1.23` |
| 对外端口 | Frontend `51999 → 80`；Backend `51888 → 8000` |
| 前端路径 | `/seedpilot/`；API 经 `/seedpilot/api/` 代理 |
| Docker 网络 | 沿用 `breeding-agent-net`；Backend alias 保持 `backend` |
| PostgreSQL | 生产 `biobin_db`；远端调查入口 `175.6.25.109:15432`；容器内使用 `postgres:5432` |
| Skill | 用户已确认生产 Skill 发布到服务器；沿用 `/data/peihai/vibe-breeding-main/skills`，只读挂载到 `/app/skill` |
| 旧生产容器 | `breeding-agent-backend`、`breeding-agent-frontend`；原镜像无 `-prod` 后缀，均为 `0.1.23` |
| 部署命令 | 本地受保护的 `docker_cmd/docker_cmd_prod.md`，继续严格沿用 dev 文档格式 |

不覆盖 `biobin_dev`，不把开发数据或开发密钥复制到生产。共享 `postgres-longrun` 不重建、不重启；其他业务容器及开发环境网络不在范围内。具体部署命令和秘密材料保持本地、Git-ignored，不进入镜像或版本库。

## 2. 调查结论与迁移方式

### 2.1 主工程并非快进合并

2026-09-17 调查基线：

- `main@7a4f078a8c4f04df0f53c6a4f2ea888f6560fa1b`，`prod@a592825c403ff56bf03d1d24649a1f330ac552e0`；当时 GitHub/Gitee 对应分支一致，工作区干净。
- 共同祖先为 `95f8c8538587f06ea3013939b4c9c12f1c7e7714`。`prod...main` 两侧分别有 106 / 589 个提交，文件差异为 852 个；排除 patch 等价提交和 merge 后，生产侧仍有 15 个独有提交。
- 这些提交涉及生产 Skill 路径、部署文件保护、子路径代理、TSV 上传、动态 Skill 列表、输入框和并发限制。其中 `/seedpilot/`、TSV、输入 10,000 字符上限、任务并发 30、外部 Skill 等在当前 main 已有对应实现；不能按提交数量判断全部缺失，也不能直接保留旧实现覆盖新架构。

推荐：从固定的 `prod` 基线创建隔离候选分支 `codex/main-to-prod-20260917`，合入固定的 main 提交，逐项审查生产独有差异，再补最小生产适配。候选通过验证后才推进 `prod`。

另外两种方式不作为默认方案：直接把 prod 重置为 main 会丢失未核实的生产独有变更和合并关系；逐个 cherry-pick 数百个开发提交容易漏掉依赖、数据库合同或测试。正常合并仍必须人工检查冲突，禁止全局 `ours/theirs`。

### 2.2 数据库需要先升级结构

本次会话此前只读远端核对：生产 public 有 24 张表，开发为 66 张；生产仍有 7 个旧 DAG 对象，新后端会以 `agent_schema_migration_required` 拒绝启动。

迁移应形成 **24 → 23 → 66** 张 public 表：

1. 管理员受控删除 `task_edge` 表、`task.root_node_id`，以及 `task_node` 的 `criticality`、`dependency_type`、`retry_policy`、`timeout_policy`、`resource_class` 五列。
2. 由当前版本 PostgreSQL bootstrap 补齐 43 张新表、`message` 的 `message_type/metadata/updated_at` 三列、`task` 的五个 `mcp_*` 字段，以及对应约束、索引、触发器。
3. 用当前源码 schema reconciliation 验证完整结构，不能只核对表数量。

生产缺少旧版 `mcp_call_record` 和 `mcp_dispatch_resume_outbox`，应直接创建最终结构；无需机械重放开发当时的旧 MCP 表改造。生产防删表事件触发器仍启用，删除旧对象需要真实管理员连接，不使用 `SET ROLE` 冒充管理员，也不关闭防删保护。

此前本机 PostgreSQL 17 合成数据演练已验证 24→23→66 和保留字段不变；这只是技术可行性证据，尚未替代真实生产备份恢复、生产参数和新镜像的完整演练。

### 2.3 生产 Skill 已发布

用户已确认生产 Skill 完成发布，服务器上已有目标内容。本次主工程迁移沿用现有生产 Skill，不再安排 Skill 分支合并、重复发布或能力增删。

此前本机 `vibe-breeding` 的分支差异不代表服务器的发布状态。执行前只读记录服务器实际 revision、能力清单和目录内容，以候选后端计算 bundle digest，并验证挂载权限及合同兼容性；发布已完成不等同于这些技术验证已通过。演练使用同一内容的副本，生产启动前复核 digest 一致。如发现不兼容，先报告具体差异，不自动修改生产 Skill。回滚演练同时验证旧后端与保留的 Skill 快照配套可用。

## 3. 生产适配清单

| 检查面与源码入口 | 计划中的处理 | 验证要求 |
|---|---|---|
| `Dockerfile`、本地 prod 命令 | 三类镜像保留已确认的 `-prod:0.1.23` 发布标签，实际部署锁定验收过的 digest；记录候选 commit、linux/amd64 manifest digest | 发布、回拉及运行的工件与验收一致；重新构建必须重新验收；旧无后缀镜像保留用于回滚 |
| `docker-compose.yml` | 当前是 dev/local 默认配置：dev 环境、SQLite、开发 Skill 路径、local 镜像；不能直接作为生产启动入口 | 本次仍以现有 prod Docker 命令为入口；若需 Compose，另行适配后验证 |
| `src/state/runtime_factory.py`、`src/api/runtime.py` | 明确 `MAF_API_ENV=prod`、`MAF_ENV=production`、PostgreSQL backend、生产 DSN；保持 config bridge 关闭 | `current_database()` 为 `biobin_db`，连接角色符合职责，无 SQLite 回退 |
| `Dockerfile`、配置加载器 | main 已取消把 `config.yaml` 打入镜像；准备兼容新 schema 的生产外部配置和 env 文件 | 校验文件权限、链接限制、配置格式及模型/Tokenizer/provider/只读业务库配置；不复制开发凭据 |
| `src/api/runtime.py`、`src/integrations/master_key.py`、`src/auth/services.py` | 使用生产独立、持久的 `MAF_MASTER_KEY_FILE`；清理新版拒绝的旧密钥环境变量 | 主密钥解码为 32 字节、受限文件权限；跨重启 sentinel、登录和 MCP 凭据校验一致 |
| 登录与既有密钥 | 旧 prod 用独立文本 secret 做 token HMAC，新版改为主密钥派生；不能假设旧 token 延续 | 在恢复库演练旧 token、新登录及历史 owner 访问；默认准备重新登录，不通过旧 hash 反推密钥 |
| `docker/nginx.conf`、`frontend/vite.config.ts` | 保持构建 base `/seedpilot/`、API base `/seedpilot`、代理 `backend:8000`；使用生产宿主机端口 | 页面、静态资源、API、SSE、历史 Artifact 和 API 文档可从真实入口访问 |
| `src/api/cors.py` | 当前同源 Nginx 路径不必增加 CORS；如真实入口跨域，仅配置明确生产 Origin | 不使用 `*`，不残留开发 Origin |
| `src/api/app.py` 的 `runtime/dev.sqlite3` | 此路径在 PostgreSQL 分支仍用于取 runtime 父目录；实际数据库由 backend/DSN 决定 | 不机械替换文件名；检查 PostgreSQL 选择和 runtime 路径即可 |
| `/app/runtime` | 复用旧生产数据挂载；先确认实际是 volume 还是 bind mount | 历史上传、Artifact、审计等可读取，UID/GID/容量符合新容器要求；不创建空卷替代旧数据 |
| Skill 加载与 bundle gate | 沿用服务器已发布的生产 Skill，只读记录 revision，并由候选后端计算 digest | 挂载权限、合同及新后端兼容性验证通过，演练与生产内容 digest 一致；旧任务/历史 Skill revision 不被强行重放 |
| MCP routing / rollout | 用户已确认首次生产部署直接启用与 dev 一致的 MCP 功能：enforce、100%、legacy assembly off；保持真实生产身份 | 完成第 4 节首次启用适配及生产 ledger/权限/真实 activation 验证后才允许启动 |
| Sidecar trust | 首次引入生产 Sidecar；建立独立 socket/data 卷，准备与发布工件一致的 manifest/allowlist | Unix socket 健康、工件信任检查通过；仅填写文件路径不算验收 |
| Runtime authority | 沿用本次命令中 PostgreSQL 为主存储、`MAF_RUST_RUNTIME_STORE/EVENT_LOG/TASK_DISPATCHER=off` 等现有开关 | 不顺便把生产任务权威迁到 Sidecar；需验证 MCP 使用 Sidecar 的路径正常 |
| 网络与本地地址 | prod `breeding-agent-net`；dev `breeding-agent-dev-net`；双方 alias 为 `backend` 可保持 | 停止旧 prod 后再启新 prod，避免同网同 alias/端口冲突；容器 localhost 健康检查和 Docker DNS 不替换为外网 IP |

拟用的配置、env、master key、Sidecar trust 文件路径已记录于本地 prod 命令，但文件是否真实存在、内容与生产是否匹配仍需现场核对。禁止把旧容器整个 Env 原样复制到新版：旧 `MAF_AUTH_TOKEN_HASH_SECRET[_REQUIRED]`、`MCP_CREDENTIAL_KEY_FILE[_HOST]` 等仅存在就会触发新版拒绝启动。

若发现生产已有不同根密钥体系加密的数据，必须先设计迁移；不能生成新 key 覆盖旧文件。旧登录 token 更新与用户/会话/文件保全分别验收。

## 4. MCP 首次生产准入：已确认直接启用新版功能

用户于 2026-09-18 明确：生产此前没有用户自由配置 MCP 的功能，本次按新功能首次上线处理，不安排旧用户 MCP 配置或凭据迁移，也不复制开发库用户 MCP 数据。具体首次初始化方案见 [用户自定义 MCP 首次生产启用设计](2026-09-18-mcp-first-production-enablement-design.md)，当前待用户审阅，尚未实施。

用户已确认：本次首次生产部署直接启用与 dev 一致的用户级 MCP 功能。目标配置为 Gateway 开启、`MCP_ROUTING_MODE=enforce`、`MCP_ENFORCE_PERCENT=100`、cohorts 为空、`MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=false`、`MCP_ROLLOUT_STAGE=legacy_assembly_off`；环境继续使用 `MAF_API_ENV=prod` / `MAF_ENV=production`。

本次首次启用不采用原有逐级灰度路线，不把其 24h、48h 及后续按周计算的观察窗作为首次部署前置条件。迁移演练、镜像验收、生产配置检查和上线后的运行观察仍需完成；普通用户的逐 Tool 授权规则保持不变。

当前代码尚无这条首次直接启用流程：生产 mode 非 off 时，仍要求分角色 PostgreSQL ledger、Sidecar 工件信任，以及与 environment/deployment/stage/config fingerprint 匹配的真实 activation；现有 operator 的阶段转换也不能直接从 off 到 assembly-off。因此发布策略已确定，技术适配仍是执行前置条件，不能仅修改环境变量或手工填写 activation ID 就部署。

阶段 B 应先形成首次启用适配设计，并按以下边界实现、验证：

1. 为本次首次启用提供受控初始化入口，生成真实、可审计且绑定本次发布的生产启用记录；不得伪造原逐级灰度流程的观察证据，也不得把生产标成 dev 使用开发豁免。
2. 同一发布的初始化重试必须幂等；不匹配的环境、部署、阶段或配置指纹必须拒绝复用。后续正常启动仍验证既有准入记录，初始化入口不能成为永久绕过检查的开关。
3. 保留用户隔离、逐 Tool 授权、密钥领域隔离、Sidecar 工件信任、分角色数据库权限，以及对未解决安全 blocker 的检查。
4. 新 Backend 启动前，以只读方式验证真实 ledger/activation 的存在和匹配关系；当前 Docker 命令只校验参数形状，需随适配补齐。
5. 在隔离 PostgreSQL 与正式候选镜像上覆盖首次初始化成功、同一发布重试、正常重启，以及身份/配置不匹配、权限不足、工件不受信任和未解决 blocker 的拒绝路径。

上述适配设计、实现和验证尚未完成。本次路线确认不表示生产已准入、已完成灰度观察或已部署，也不扩大到删除现有灰度机制等无关改造。

## 5. 实施顺序与每步交付

### 阶段 A：固定源码和现场基线

1. 重新确认主工程两远端 main/prod HEAD、工作区状态；固定本次来源 commit。审查期间 main 若更新，先说明纳入范围，再重跑受影响验证。
2. 对两个本地部署文件做仓库外 `0600` 备份并记录内容摘要；后续保持文件存在、权限和忽略状态。本轮不需要切换当前工作区。
3. 只读记录旧生产容器 image digest、启动参数名称、挂载类型与位置、网络、health、资源/磁盘状况；秘密值仅保存在受限本地备份。
4. 复核生产 schema、角色权限、防删触发器、业务行数和在途 Task/Command/Interrupt；与之前快照不同则先更新变更清单。固定可接受的停写窗口和备份保留期。

完成证据：无秘密的发布基线清单；明确在途任务排空方式。不能自动复活旧 DAG 任务、把其改成成功，或依赖新 Agent Loop 自动续跑旧任务。

### 阶段 B：候选分支、必要适配与命令核验

1. 在隔离工作树从 prod 基线创建候选分支，合并固定 main。逐项核对 15 个非等价生产独有提交；保留有效生产行为，采用 main 最新架构和已替代实现。
2. 完成第 3 节必要参数适配；环境说明按分支和入口准确更新，不把所有 dev 字样全局改为 prod。对应更新 AGENTS、CHANGELOG 和必要测试。
3. 按第 4 节已确认的直接启用路线，先设计、实现并验证首次生产初始化及准入适配；补充新 Backend 启动前的真实 ledger/activation 只读检查，核对未解决 blocker 和 config fingerprint。生产尚未建表时须明确报告“待迁移后验证”，不能把缺表、缺 activation 判为通过。
4. 完成生产配置/密钥准备方案，核对服务器已发布 Skill 的 revision、digest、挂载权限和兼容性；明确旧全局 MCP 能力转为用户级配置后的设置与逐 Tool 授权流程，不向所有用户复制凭据或 Grant。
5. 完善本地 prod Docker 命令，保持 dev 的章节与单一 Bash 块格式；长期启动命令只保留前置检查，首次破坏性 DDL 独立记录和执行。

完成证据：候选 diff 可审阅；没有开发 DSN、Skill 路径、端口、密钥或本地信任豁免泄漏到实际生产参数；部署文件保护检查、Shell 语法、相关参数拒绝/接受测试通过。

### 阶段 C：真实恢复演练与候选镜像验收

1. 在受限环境取得完整生产备份，恢复到隔离 PostgreSQL 17 数据库；检查恢复完整性。演练库使用独立网络/端口和 runtime 副本，所有 DSN 指向演练目标。
2. 禁止演练进程访问真实业务写入口或启动外部工具自动恢复；先排查可恢复任务及旧密文，再用受控测试账号/任务验证。记录恢复耗时和空间要求。
3. 在恢复库执行与正式切换相同的 7 个旧对象删除、当前 bootstrap、权限/准入准备；完整对比保留字段/行数/摘要及 schema，不只检查 66 张表。
4. 新增对象允许有预期初始化数据；旧表除明确删除的 DAG 结构外，任何业务变化都要单独解释。删除对象的原始内容保留在完整备份中，不伪称所有字段零删除。
5. 基于候选 commit 构建并冻结三类 linux/amd64 镜像工件，记录各自 platform manifest digest，以这些工件完成隔离验收；执行相关 Backend 存储/鉴权/API/MCP/历史恢复回归、Frontend 全量测试/typecheck/build、Sidecar 对应 Rust 质量门禁。真实 PostgreSQL 用例不得用 skip 充当通过。
6. 验证真实生产环境标记下的启动、登录更新、历史会话/文件、Skill、MCP 授权/结果/重启恢复，以及前端 SSE；外部真实调用仅使用明确授权的测试输入。验证失败时不能靠 dev 豁免放行。
7. 实际执行一次备份恢复回旧结构和旧版本的演练，验证数据库、runtime、配置、密钥与旧镜像配套可用。

完成证据：候选源码、服务器 Skill revision/bundle digest、三类验收工件 digest、Sidecar 工件信任材料和配置指纹对应；迁移与回滚演练均通过，记录测试失败、skip 和尚缺的外部证据。任何镜像重新构建都使该镜像此前的工件验收失效，必须重新验收；Sidecar 重建还须同步核对其 manifest/allowlist。预估维护窗口据真实耗时制定，不凭合成演练猜测。

### 阶段 D：发布准备

1. 候选验收通过后再次确认远端 prod 未变化，将候选合并结果推进 prod；同步 GitHub/Gitee 并复核 HEAD，禁止 force push。
2. 只给阶段 C 已验收的同一镜像工件附加约定的三个 `-prod:0.1.23` 标签并发布，不重新构建。记录 registry 引用 digest；若使用 OCI index，同时记录其 linux/amd64 platform manifest digest。按锁定引用回拉，核对 platform manifest 与阶段 C 一致并做启动 smoke，实际部署使用 `仓库名@sha256:…`。如需重建或工件摘要变化，必须返回阶段 C 重新验收，Sidecar 信任材料同时匹配该工件。若同名 tag 已存在，先核对其所属发布，避免无记录覆盖。
3. 复核服务器已发布 Skill 的 revision、bundle digest、挂载权限及兼容性证据，并保存当前内容快照；不更新生产 Skill 目录。准备生产配置文件与独立卷，固定整个发布包，迁移期间不临时更换源码、镜像、配置或 Skill。
4. 产出可审阅的正式 DDL、bootstrap 入口、前后校验、停启和恢复命令及准确目标，确认备份空间、管理员连接和回滚材料全部可用。

完成证据：上线材料齐全、镜像已预拉取、维护窗口可执行。停旧生产前完成材料、连接、恢复演练和准入方案检查；依赖新表的真实 activation 校验留到阶段 E，但创建及验证方式必须已在阶段 C 闭合。生产写操作按用户最终确认的执行范围进行。

### 阶段 E：生产停写、结构迁移与切换

1. 关闭新请求入口，按既定方案排空在途任务并停止所有指向 `biobin_db` 的应用 writer；停止旧 frontend/backend，保留容器和镜像。仅停止前端不算停写。
2. 在停写状态获取最终完整 `pg_dump -Fc`，备份 runtime、生产配置、密钥与 Skill。使用 PostgreSQL 17 工具；限制目录/文件权限，校验 SHA-256 和 `pg_restore --list`。恢复可用性由阶段 C 的实际恢复演练证明，不能只凭目录列表判断。
3. 用真实管理员连接在单个事务中删除准确的 7 个旧对象；使用已演练的 advisory lock `5566807924744996692`、`lock_timeout=3s`、`statement_timeout=30s`。任何锁等待、对象漂移或保全校验异常立即回滚，不扩大删除范围。
4. 运行候选版本的显式 PostgreSQL bootstrap，补齐目标 schema；执行已通过演练的 MCP 首次启用初始化，准备权限与真实准入，核对 activation 和环境、部署、阶段、配置指纹及未解决 blocker。此检查必须在新 Backend 启动前通过。管理员/DDL/operator 凭据不留在常驻 Backend 环境中。
5. 对比保留数据，验证 schema reconciliation 无待处理项、应有约束/索引/触发器齐全、防删保护继续有效。DDL 提交后 bootstrap 失败属于“已变更数据库”，按第 6 节恢复，不能直接启动旧容器。
6. 按阶段 D 锁定的镜像 digest，依次启动 Sidecar → Backend → Frontend 新 `-prod` 容器；核对实际运行工件与验收记录一致，完成 health、生产 DB 身份、挂载、网络代理、真实 activation 及日志检查。仅保留一个生产 `backend` 网络别名提供者。
7. 维护入口保持关闭时完成受控 smoke，记录验证产生的测试数据；通过后开放流量并持续观察。不要删除旧容器、备份或清理原 runtime。

完成证据：用户入口、数据库、文件、Skill、MCP、重启后行为一致，开发环境和共享服务保持正常。

## 6. 回滚边界

| 失败时点 | 可执行的恢复方式 |
|---|---|
| 尚未提交结构删除 | 停止候选容器，确认数据库未变化；恢复旧配置及入口，确认 Skill 仍为已验证兼容旧后端的保留版本，再启动旧容器 |
| 已提交 DDL，尚未开放流量 | 停止所有新 writer；按演练方案从最终备份恢复旧数据库和一致的 runtime/配置/密钥/Skill，再启动旧镜像；不能只换镜像 |
| 已产生新的正式业务写入 | 立即停写并保全故障现场；先明确新增数据保全与恢复点，不能直接恢复旧备份造成静默数据丢失。评估前向修复或获准的数据回迁后执行 |

共享 PostgreSQL 实例不能整体覆盖。恢复的准确数据库目标、连接排空方式和权限须在演练中验证；保留原备份和失败现场直到回滚窗口结束。

## 7. 完成标准与尚未闭合事项

迁移完成需同时满足：prod 源码和两远端一致；三类实际运行镜像的 linux/amd64 工件 digest 与验收记录完全一致；生产 PostgreSQL 结构/数据校验通过；历史会话/文件及新任务可用；登录、Skill、MCP、Sidecar 与前端链路验收通过；回滚材料完整且 dev/共享服务未受影响。

当前尚未闭合：

- MCP 首次直接启用的适配设计、实现及隔离验收；路线已由用户确认，现有固定 assembly-off 命令仍不能直接视为可执行。
- 生产真实配置、master key、Sidecar trust、ledger 权限及 activation 的现场证据。
- 旧登录 token 的重新登录安排、在途任务排空结果；用户自定义 MCP 按新功能首次上线处理。
- 已发布生产 Skill 的服务器实际 revision、bundle digest、挂载权限及新旧后端兼容性验证记录；发布工作本身已由用户确认完成。
- 旧 `/app/runtime` 挂载类型、完整生产备份的实际恢复演练和可接受维护窗口。

本轮仅完成主工程源码同步；以上未闭合项仍是部署前置条件，不计为已上线或生产验证通过。此前规划的首次 MCP 初始化适配未纳入本次源码同步，main 现有检查保持原样。

## 8. 依据入口

- [开发远端 PostgreSQL hardcut 方案](2026-08-31-postgres-agent-schema-and-conversation-title-hard-defect-repair-implementation-plan.md)：数据库部分；本次会话还核对了其后的远端执行历史，不能仅以旧计划状态推断执行结果。
- [主密钥领域设计](2026-08-14-maf-master-key-domain-derivation-design.md)：当前密钥合同不包含旧密文自动迁移。
- [MCP 发布与回滚手册](../../runbooks/user-mcp-phase3-rollout.md)、`scripts/postgres/user_mcp_rollout_permissions.sql`、`scripts/control_user_mcp_rollout.py`。
- `src/storage/postgres/bootstrap.py`、`src/storage/sqlalchemy_models.py`、`src/state/postgres/`：当前 PostgreSQL schema 与启动门禁。
- `src/api/runtime.py`、`src/auth/services.py`、`Dockerfile`、`docker/nginx.conf`：实际生产环境、密钥、镜像与代理行为。
- `scripts/validate_project_skill_bundle.py`、`scripts/check_docker_cmd_policy.sh`、`scripts/run_rust_quality_gates.py`：Skill、部署文件保护和 Rust 验证入口。
