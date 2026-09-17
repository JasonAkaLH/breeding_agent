# User-scoped MCP CP7 Manual Retirement Design

## 状态与决策优先级

本设计于 2026-08-13 获批，并于同日经过十二轮 `document-perfectization` 审查与修订。第九轮起按项目负责人授予的连续修改授权，以 95% 信心门自动循环。它只适用于开发分支 `main`。生产分支 `prod`、生产容器、生产流量和生产数据均不在执行范围内。

本设计取代 Phase 3 Runbook 中“CP-7 定时灰度观察后再执行 CP-8”的本次开发执行顺序：不等待 24 小时、48 小时或 7 天观察窗，也不以 production evidence 作为本次开发分支的退役门禁。原 CP-8 物理删除并入 CP-7，但保留一个不可跳过的人工确认点。

如本设计与 Phase 3 PRD 或 Runbook 的生产灰度步骤冲突，本设计只覆盖 `main` 的 CP7-A/CP7-B 开发流程；不得据此宣称生产 CP-7 或 CP-8 已完成。未来把结果合入或部署到 `prod` 时，仍由生产发布流程独立决定准入、回滚和验收要求。

## 问题、用户价值与目标

全局 MCP Runtime 已不再是目标架构，但直接删除会同时失去可人工验证的候选和代码级回退点。本设计把退役分为两个可验证步骤：

1. CP7-A 在 `main` 交付一个只装配 user-scoped MCP Gateway 的、可复现的单机 Docker Compose 候选；legacy 全局 Runtime 源码仍保留。
2. 项目负责人基于该候选完成人工测试，并明确回复“可以退役”后，CP7-B 才物理删除 legacy 全局 Runtime 的代码路径。

完成后，新执行路径只使用 user-scoped MCP；普通非 MCP 对话不因用户没有配置 Server 而失效；历史数据仍可读；旧全局 Runtime 不能通过环境变量或残留入口重新装配。

## 参与者与受影响系统

| 参与者/系统 | 职责或影响 |
|---|---|
| 项目负责人 | 唯一人工验收人和退役批准人；只有其明确回复“可以退役”才能触发 CP7-B。 |
| 实施者 | 修改代码、构建候选、运行自动验证、交付候选标识；无权代替项目负责人批准退役。 |
| 测试用户 A / B | 验证各自 Server、Grant、调用与跨用户隔离；不得共享凭据。 |
| 现有测试 MCP Server | 提供人工可用的真实 capability；不为本流程新建专用 Server。无法人工覆盖的协议恢复场景由固定自动化测试覆盖。 |
| backend / frontend / runtime-sidecar | CP7-A Compose 的三个服务；三者必须来自同一候选 commit 的干净 Git archive。 |
| SQLite/Sidecar 数据、runtime 文件、audit | CP7-A 只使用独立的新项目卷和测试文件，不读取或修改既有开发数据。 |
| `prod` 与生产基础设施 | 完全不受本设计操作影响。 |

## 范围与明确不做的事项

### 范围内

- 在 `main` 实现三服务 CP7-A Compose、候选构建/验证工具和人工验收说明。
- 关闭全局 MCP assembly，同时验证 user-scoped 配置、授权、调用、恢复、安全和无 Server 行为。
- 人工批准后删除全局 Runtime 的可执行代码及重新装配入口。
- 保留 user-scoped 所需 Client、Transport、协议 Adapter、远程任务恢复、Rust Sidecar、凭据、Grant、审计、安全校验和用户数据。

### 范围外

- 不修改、切换、合并或部署 `prod`。
- 不操作真实生产容器、镜像仓库、DSN、密钥、数据或流量。
- 不实现或伪造原 CP-7 的定时观察窗和 production evidence。
- 不把本地测试、SQLite 数据或人工测试结果标记为 production evidence。
- 不在 CP7-A 删除 legacy 源码。
- 不改变单机 Compose 的对外端口、代理或停机策略。
- 不删除历史记录、用户 Server、密文、Grant、Task、result、audit 或 rollout ledger。
- 不新建测试 MCP Server，不借此重构共享协议栈。

## 当前状态、依赖与前置条件

当前 Compose 只有 backend/frontend；候选配置切到 canonical `enforce` 后，backend 还要求 Rust Runtime Sidecar、与实际运行 binary 绑定的可信工件、credential key 和两个正整数容量门禁。现有 `Dockerfile` 还会 `COPY config.yaml`，但该文件不在 Git archive 中，且可能包含本地配置。因此 CP7-A 不能只改 MCP 环境变量；必须先建立可复现的 legacy-on 基线，再完成本设计规定的自包含候选装配。

执行 CP7-A 前必须满足：

- Docker Engine 与 Docker Compose 可用，且支持 `linux/amd64` 当前构建约束。
- `runtime/cp7-a/inputs/compose.env` 必须提供已在宿主预载且使用 immutable digest 的 `CP7_RESTORE_DIND_IMAGE=<repository>@sha256:<64-lower-hex>`、`CP7_POSTGRES_VALIDATION_IMAGE=<repository>@sha256:<64-lower-hex>` 与 `CP7_RUST_GATE_RUNNER_IMAGE=<repository>@sha256:<64-lower-hex>`；最后一项必须是固定 `linux/amd64` 的 Ubuntu 22.04 runner，并绑定 Rust/Python/Conda 工具链。候选运行期间不得从 registry 拉取可变 tag，也不得把镜像凭据写入本流程。
- 所有本地输入只放在已被 `/runtime/` 整体忽略的 `runtime/cp7-a/inputs/`；不得在仓库根目录假设 `.env.cp7-a`、manifest 或 allowlist 已被忽略。
- 除下述显式 `B_L` pre-freeze bootstrap 外，`runtime/cp7-a/inputs/` 必须且只能包含下表九个 staged input，加另一个只供 Compose CLI 读取、不进入 staged volume 的 `compose.env`；不得把内容复制进镜像、Git 或交付摘要。
- credential key 与 retirement HMAC key 权限不高于 `0400`；其内容和 hash 均不得写入 manifest。
- `runtime/cp7-a/inputs/compose.env` 显式给出两个正整数容量值；设计不提供可能误用的默认值。
- 使用独立 Compose project name、独立新卷和无冲突端口；不得挂载现有开发数据库或 runtime 卷。
- 候选构建上下文必须来自 Git archive；工作树中的本地输入只能进入下述受控 staging，不得直接挂到长期运行的 backend 路径。

### 本地输入 secure staging

三个长期运行服务仍只有 backend、frontend、runtime-sidecar。Compose 额外定义一个只在 `cp7-bootstrap` profile 中显式调用的 one-shot `cp7-input-stager`；它复用当前可运行阶段 exact release（`B_L`、`C_A`、`C_B`、`R_A` 或 `R_L`）的 backend image，不形成第四个常驻服务。`A_B` 只是 `C_B` 失败后的 source-only 恢复 commit，不构建、不启动，也不进入 runtime validator。固定调用顺序是：创建空的 phase/candidate-scoped `cp7-inputs` named volume，运行 stager，stager 成功退出，随后同一阶段 backend 以 `depends_on: condition: service_completed_successfully` 只读挂载该 volume 到 `/run/cp7-inputs`。stager 必须 `network_mode=none`、read-only rootfs、临时 `/tmp` tmpfs、禁止宿主 socket/device、`no-new-privileges`、`cap_drop=[ALL]`，只添加 `CHOWN`、`DAC_OVERRIDE`、`FOWNER` 三项 capability；只读挂载该阶段的 `runtime/cp7-a/inputs/` 快照，并只写目标 volume。退出后不得有 root 进程留存。

首次启动固定使用带 `cp7-bootstrap` profile 的 Compose 命令；stager 只接受完全空的目标 volume，已存在任一文件即拒绝，成功后写 immutable staging receipt。普通 backend/Sidecar restart 不重新运行 stager；输入变化必须生成新 candidate/request ID 和新 staged-input volume，不得原地覆盖。

只有 pre-freeze `B_L` bootstrap 是九项闭集的唯一例外：它必须且只能暂存除 `legacy-retirement-inventory.json` 外的其余八项，staging receipt 使用独立 schema `maf.user_mcp.cp7_bl_prefreeze_staging.v1`，并记录 `phase=bl_prefreeze`、`inventory_expected=false`。共享 validator 在该阶段只能以 `expected_release=B_L,expected_phase=bl_prefreeze` 运行，明确拒绝 inventory 存在；backend 只可执行 legacy-on smoke、key anchor、quiesce 和 inventory export，不得进入 rehearsal、candidate Ready 或 approval 路径。inventory 和 freeze receipt 发布后，必须销毁该 bootstrap project/volume，并以完整九项闭集创建新的 frozen `B_L` staged volume；除这一显式阶段外，缺少 inventory 一律 fail closed。

`cp7-inputs` 是阶段抽象，不是可跨 commit 复用的共享卷。`B_L`、回滚演练中的 `C_A`、正式人工候选 `C_A`、未来 `C_B` 以及 post-completion 可运行恢复 release `R_A/R_L` 必须分别使用 no-clobber 新建的 immutable staged-input volume 和独立 staging receipt。每个卷中的 Sidecar binary/manifest/allowlist/SBOM/provenance 只能绑定该阶段的 exact commit/tree/image；即使文件字节偶然相同，也不得共享 volume、receipt 或 commit binding。`A_B` 不生成 staged volume。`B_L` freeze 前的一次性 bootstrap 卷不是候选信任根；只有下述 freeze/anchor/inventory 全部完成后生成的 frozen `B_L` staged volume 才可进入回滚演练。

stager 不使用普通 `cp` 跟随路径。它必须对源目录和每个 allowlisted basename 逐项执行 `O_NOFOLLOW`/regular-file/当前宿主 UID/无 group-or-other bits/`nlink=1`/逐文件 size bound/pre-post inode 检查，以 `O_EXCL` 写目标、`fsync(fd)` 和 `fsync(volume root)`，再把所有目标固定为 `uid=10001,gid=10003,mode=0400`。这里的 `0400` 是精确 mode，不是“至多 0600”的模糊比较；共享 validator 只接受精确 owner/group/mode。目标不得包含额外 basename、symlink、hardlink 或非 regular file。

| 固定 basename | 来源/用途 | source mode | size 上限 | receipt 摘要 |
|---|---|---:|---:|---|
| `config.yaml` | runtime 配置 | `0400` 或 `0600` | 1 MiB | SHA-256 |
| `mcp-credential.key` | credential cipher secret | 精确 `0400` | 4 KiB | 只记 `present=true` |
| `legacy-retirement.key` | retirement HMAC secret | 精确 `0400` | 4 KiB | 只记 `present=true` |
| `legacy-retirement-inventory.json` | 认证 inventory | `0400` 或 `0600` | 4 MiB | SHA-256 |
| `runtime-sidecar.bin` | 从当前阶段 exact immutable sidecar image 提取的实际 binary 副本，仅用于 hash，不执行 | `0400` 或 `0600` | 128 MiB | SHA-256 |
| `runtime-sidecar-manifest.json` | artifact manifest | `0400` 或 `0600` | 1 MiB | SHA-256 |
| `runtime-sidecar-allowlist.json` | artifact allowlist | `0400` 或 `0600` | 1 MiB | SHA-256 |
| `runtime-sidecar-sbom.json` | CycloneDX SBOM | `0400` 或 `0600` | 16 MiB | SHA-256 |
| `runtime-sidecar-provenance.json` | in-toto/SLSA provenance | `0400` 或 `0600` | 4 MiB | SHA-256 |

builder 必须直接从 immutable Sidecar image ID 提取 `runtime-sidecar.bin`，再由其摘要生成后四个 trust 文档；不得从工作树编译输出或可变 tag 复制。backend startup 与 candidate verifier 都只读同一 staged volume，并以 expected owner `10001:10003`、mode `0400` 验证；共享 loader 自己读取并 hash staged binary，不信任 caller 传入的摘要。共享 runtime validator 的必填参数固定为 `expected_release={B_L|C_A|C_B|R_A|R_L}`、`expected_phase={bl_prefreeze|frozen_baseline|rehearsal|authoritative_candidate|retirement|rollback_to_ca|rollback_to_bl}`、`expected_commit`、`expected_tree`、`expected_backend_image_id`、`expected_sidecar_image_id` 和 `inventory_required`，且必须把参数与 staging receipt、trust subject、live image label 逐项比较；caller 不得通过省略参数退回硬编码 `C_A`。`A_B` 只由 abort writer 通过 parent/tree/inverse-patch/recovery-profile 校验，不得传给 runtime validator。外层 verifier 在 Compose 启动后再次从运行容器的 immutable image ID 提取 `/usr/local/bin/maf-runtime-sidecar`，其 SHA-256 必须与 staged binary、manifest、allowlist、SBOM/provenance subject 和 candidate manifest 全部相等；这一步才是 live image identity 的权威绑定。

CP7-0 为配置加载增加固定 `MAF_CONFIG_PATH`，本候选只能指向 `/run/cp7-inputs/config.yaml`。candidate verifier 必须通过同一 backend image、`uid=10001,gid=10003` 和 staged volume 调用共享 trust loader；host 侧读取只能生成非权威诊断。staging receipt 只记录上述 exact basename、目标 mode/owner、非敏感文件摘要和两个 secret-present 布尔值；credential/retirement key 的内容、文件摘要、inode 和宿主路径均不得进入 manifest、日志或 stdout/stderr。

## 总体方案

采用“可复现 legacy-on 基线 + 单个 assembly-off 候选 + 条件代码退役”：

- CP7-0 先只完成 Docker/Compose 可复现性、安全挂载、三服务和验证工具，保持 gateway off、routing off、legacy on；提交并验证最后可执行的 legacy 基线 `B_L`。
- CP7-A 再将开发 Compose 默认值切为 user-scoped `enforce=100%`、legacy assembly off，提交候选 `C_A`。
- backend、frontend、runtime-sidecar 均从同一候选 commit 的干净 Git archive 构建，并写入同一 OCI revision。
- CP7-A 完成自动验证后停止实施，等待项目负责人人工验收。
- CP7-B 只能由绑定当前候选的明确“可以退役”触发。
- 如果候选 commit、镜像 ID、manifest 或配置发生变化，原人工验收自动失效，必须重新测试。

不创建 production rollout ops 镜像，不运行 production approval/activation，也不把本流程扩展为 production 发布流程。

### CP7-A candidate generation 语义

本文中未带下标的 `C_A` 表示 current lifecycle 所绑定的 CP7-A generation；初次候选为 `C_A[0]`。无论是人工批准前用新修复 supersede 当前 pending `C_A[n]`、CP7-B abort 已产生 `A_B`，还是 post-completion rollback 已产生 `R_A/R_L`，下一个可运行候选都必须是新 commit `C_A[n+1]`，不得 reset/checkout/rebase 回任何旧 `C_A[n]`。其 closed contract 为：

- `C_A[n+1]` 是 non-merge commit，唯一 parent 精确为本轮 current pending `C_A[n]` 或已认证的 `A_B`、`R_A`、`R_L`；从 pending `C_A[n]`、`A_B` 或 `R_A` 重入时允许 tree 与前一 `C_A[n]` 相同或包含本轮已验证的 CP7-A 修复，从 `R_L` 重入时必须以显式 CP7-A assembly-off patch 从 `tree(B_L)` 得到新 tree。两种情况的 `candidate_tree` 都是该新 commit 的实际 tree，不得借用旧 tree 摘要代替。
- commit profile 精确为 author/committer name=`MAF CP7 Candidate`、email=`maf-cp7-candidate@localhost`、author time=committer time=`<parent committer UTC second> + 1 second`、timezone=`+0000`、message=`cp7-a: qualify candidate generation <n+1>\n`；禁止 GPG signature、encoding 或其他 extra commit header。同 generation 只能有一个被 manifest/root 引用的 child；多个符合或任一 profile 漂移都 fail closed。
- manifest `identity` 必须额外绑定 `candidate_generation`、`candidate_parent_commit`、`candidate_parent_release`，其中 parent release 只允许 `B_L|C_A|A_B|R_A|R_L`；`C_A[0]` 的 parent 为 `B_L`，普通 pending supersede 的 parent release/commit 精确为 `C_A`/current `C_A[n]`，其余 generation 只允许对应的已认证 recovery release。candidate binding、manual approval 与 claim 通过 manifest file/payload SHA 继承这三项，旧 approval 不得跨 generation 复用。
- 运行时 logical `expected_release` 仍为 `C_A`，phase 只能为 `rehearsal|authoritative_candidate`，但 `expected_commit=C_A[n+1]`、`expected_tree=tree(C_A[n+1])`。所有镜像 OCI revision、archive、staged-input receipt、manifest/allowlist/SBOM/provenance subject、validator 参数和 live image identity 都必须重新绑定该 exact commit/tree；不得复用旧 `C_A[n]` 的 image、trust、volume、receipt 或验证结果。
- `A_B` 始终是 source-only abort recovery：不构建、不 staging、不启动、不进 runtime validator。普通 pending `C_A[n]` 修复和 `A_B` 后重入都必须先按 `candidate-supersede-root` 发布新 `C_A[n+1]` manifest/root/receipt；`R_A/R_L` 是已完成 rollback 的可运行 recovery release，但不能直接成为新 pending candidate，必须走 `lifecycle-root`。三种分支都只能在 root/receipt 完整后投影 `current=pending_manual_approval`，并完整重跑 CP7-A 自动与人工验收。

## CP7-0：可复现 legacy-on 基线 `B_L`

`B_L` 是 CP7-A 之前的独立 Git commit，不得直接把当前不可复现的旧镜像当作基线。它只包含以下基础修改：

- backend 不再 `COPY config.yaml`，改为运行时只读挂载；
- `Dockerfile` 新增 runtime-sidecar final target，三个 final target 接受 `VCS_REF` 并写 OCI revision；
- Compose 具备三服务、独立卷、Unix socket、健康检查和本地输入挂载；
- MCP 默认值仍为 `gateway=false`、`routing=off`、`legacy=true`；
- 候选构建、inventory 导出、manifest/approval 和验证工具可运行，但不关闭 legacy assembly。

`B_L` 必须从 `git archive B_L` 构建三个镜像并通过 legacy-on smoke；随后以 `docker image save` 或等价无损方式导出三个 immutable image，记录每个 image ID、OCI revision、导出文件 SHA-256 和冻结的 legacy-on Compose canonical SHA-256。导出文件位于 Git-ignored 的 `runtime/cp7-a/baseline/`，权限不高于 `0600`。在 freeze 前必须先生成独立 retirement HMAC key ID anchor，再由 exact `B_L` backend 以 no-clobber 方式导出 inventory 和 baseline freeze receipt；下述 anchor/inventory/receipt 任一不完整都不得开始演练。

CP7-A preflight 必须实际执行一次 `B_L → C_A → B_L → C_A` 启动/健康回滚演练。演练固定使用独立 project `cp7-rehearsal-<approval_request_id>`、一组只属于演练且在四次切换间不重建的 backend application-data/Sidecar SQLite 卷，以及分别绑定 `B_L` 和 `C_A` 的 immutable staged-input volume。演练的 audit、safety ledger、container ID、candidate ID 和 Ready 观测只属于演练，不能进入正式人工批准的 `[0,end)` 窗口；四次切换后必须完整销毁该 project 的 container/network/evidence volume，保留 application-data/Sidecar 连续性结果的 immutable rehearsal receipt 和 digest 后再删除演练数据卷。

第一次 `B_L` 启动时写入一组非敏感连续性 sentinel：Server 只记固定本地测试类型与安全别名、Grant 只记授权类型、Task 只记不会调用外部工具的固定 payload digest，credential sentinel 仅保存随机明文的加密结果并在每次启动后以“可解密且摘要一致”证明连续性，receipt 不记明文、密文或密钥 hash。四次启动都必须证明 Server/Grant/Task/credential sentinel 数量与 digest 连续，Task event/outbox/call 计数不增加，不发生重放。如果 `C_A` 需要 additive schema 才能读写该卷，该 schema 与双向兼容逻辑必须先在 `B_L` 落地，不得让回切后的 `B_L` 读取未知 schema。

演练通过后才可创建正式 authoritative `C_A` project `cp7-candidate-<approval_request_id>`。它必须使用全新、初始为空且与 rehearsal 没有任何 mount/volume/container 关系的 backend data、Sidecar SQLite、audit、safety ledger、socket 和 authoritative `C_A` staged-input volume。正式 audit 从该 project 的第一次 `C_A` backend 启动前 offset 0 开始，正式 safety 观测也只从该 project 的第一个 Ready epoch 开始。manifest 只绑定 rehearsal receipt digest，不得把 rehearsal 的 audit/ledger 行合并或复制到 authoritative 窗口。任一工件丢失、hash 不符、rehearsal 或 authoritative smoke 失败都阻断 CP7-A 交付。

## CP7-A：assembly-off 测试候选

### 三服务 Compose 架构

CP7-A Compose 包含：

1. `runtime-sidecar`：从 `native` workspace 构建 `maf-runtime-sidecar` 和只读 health probe，通过共享 Unix socket 提供服务；使用独立 Sidecar 数据卷。
2. `backend`：只在 input stager 成功退出且 Sidecar 的 Version、CheckCompatibility 和 Readiness 全部通过后启动；只读挂载 candidate-scoped staged-input volume，取得 `config.yaml`、credential key、retirement inventory/key、sidecar artifact manifest、allowlist、SBOM 和 provenance；通过只读 socket 卷连接 Sidecar。
3. `frontend`：保持现有 nginx 服务和对 backend 的健康依赖。

Sidecar endpoint 固定为容器内 Unix socket `unix:///run/maf-runtime-sidecar/runtime.sock`。Sidecar 的持久 SQLite 卷固定挂载到 `/var/lib/maf-runtime-sidecar`，数据库文件固定为 `/var/lib/maf-runtime-sidecar/runtime.sqlite3`；容器的唯一持久启动命令精确为：

```text
/usr/local/bin/maf-runtime-sidecar --serve unix:///run/maf-runtime-sidecar/runtime.sock --sqlite /var/lib/maf-runtime-sidecar/runtime.sqlite3
```

backend 与 Sidecar 只共享 socket 卷，不通过宿主 TCP 端口暴露 Sidecar。Sidecar 数据卷、backend runtime 卷与测试数据库均属于独立 CP7-A project，不能复用现有开发卷。

Compose 和三个 final image 的身份不得依赖宿主用户：backend 固定 `uid=10001`，runtime-sidecar 固定 `uid=10002`，共享 socket group 固定 `gid=10003`，两个进程都以固定数字 supplemental group `10003` 访问 socket。Sidecar 数据目录及 `runtime.sqlite3` 由 `10002:10002` 所有，目录权限 `0700`，数据库及 SQLite journal/WAL/SHM 文件权限不高于 `0600`。socket 目录 `/run/maf-runtime-sidecar` 由 `10002:10003` 所有、权限精确为 `0770`；`runtime.sock` 由 `10002:10003` 所有、权限精确为 `0660`。Sidecar 以可写方式挂载 socket 卷，backend 对同一卷只读挂载并只通过 `gid=10003` 连接 socket，不拥有 socket 目录。

Sidecar 启动 wrapper 在 bind 前必须以 no-follow metadata 检查上述数字 owner/mode；只允许删除专用目录中由 `10002:10003` 拥有且类型确为 socket 的旧 `runtime.sock`。目录或 socket 为 symlink，socket 为普通文件/FIFO/device/目录，owner 不符，目录存在 group/other 写以外的过宽权限，数据卷为 symlink，SQLite 文件为 symlink/多链接/错 owner/过宽 mode，或启动后 socket owner/mode 不符，都必须 fail closed。hostile tests 必须逐项构造上述情况，并覆盖 backend 不在 `gid=10003`、backend 伪造 socket、Sidecar 重启后陈旧合法 socket 可安全替换。health probe 必须通过同一 Unix socket 调用 Version、以编译期 contract/proto/schema/error-table 常量调用 CheckCompatibility，再确认 Readiness；仅检查 socket 文件存在不算健康。backend 使用 `condition: service_healthy`。

`Dockerfile` 必须新增 Sidecar final target，并让 backend、frontend、runtime-sidecar 三个 final target都接收同一个 `VCS_REF` build arg，写入：

```text
org.opencontainers.image.revision=<candidate-commit>
```

backend 镜像不再 `COPY config.yaml`；该文件只在运行时只读挂载。镜像构建不得读取 Git-ignored 的本地配置、密钥、报告或 runtime 文件。

### Sidecar binary 与 trust 工件绑定

Sidecar trust 文件不能预先复用。backend 启动路径与候选 verifier 必须调用同一个共享 secure trust validator，不得各自复制或弱化规则。该 validator 必须对 manifest、allowlist、SBOM 和 provenance 使用 `O_NOFOLLOW` secure read，检查 regular file、预期 owner/group、mode 精确为 `0400`、`nlink=1`、size limit、pre/post `fstat` 及 path inode 一致；JSON 解析拒绝 duplicate key、unknown key、NaN/Infinity、非 canonical UTF-8/ASCII 表示、多余空白和非单一终结 newline。manifest/allowlist 必须是 closed schema 且只有一个 exact allowlist match；重复 exact entry 也拒绝，不能用 set 去重隐藏重复。

validator 还必须检验 SBOM 与 provenance 的固定 schema/version和全部 required/unknown fields，并要求两者的 subject name 精确指向 `maf-runtime-sidecar`、subject digest 精确等于从实际 immutable image 提取的 binary SHA-256；provenance 的 source commit/tree/build subject 必须绑定调用时的 `expected_release/expected_commit/expected_tree`，manifest 中的 `sbom_sha256`/`provenance_sha256` 必须等于 secure-read 文件摘要。只验证“字段存在”或信任 caller 传入的 binary digest 不合格。`C_A` 的固定构建顺序是：

1. 从 `git archive C_A` 构建 runtime-sidecar image；
2. 从该 immutable image ID 提取实际 `/usr/local/bin/maf-runtime-sidecar`，计算 binary SHA-256，并计算同一 archive 的 Cargo.lock SHA-256；
3. 生成只对应此 binary 的 manifest/allowlist/SBOM/provenance，要求 `git_commit=C_A`、`artifact_sha256=<actual-binary-sha>`，并绑定 `cargo_lock_sha256`、proto/schema/error-table hashes；source 只允许 `ci_pipeline`、`deployment_pipeline` 或 `runtime_allowlist`；
4. 从同一 archive 构建 backend/frontend；
5. 运行 one-shot stager，把上表九个 staged input 全部安全复制到 candidate volume；
6. 以同一 backend image、`uid=10001,gid=10003`、只读 staged volume 运行共享 secure trust validator，校验 manifest/allowlist/SBOM/provenance；
7. 完成三镜像 export，并在 pinned、空 data-root 的 disposable DinD 中实际 load/运行固定 smoke，确认恢复身份；
8. 使用 restored images 启动 Compose；验证器再次从运行容器对应 image ID 提取 binary，重算 SHA，与 manifest、allowlist 和 candidate manifest 三方精确匹配。

任何预先存在但不匹配 `C_A` 或实际 image binary 的 trust 文件都 fail closed。candidate manifest 记录 binary、Cargo.lock、SBOM、provenance、proto/schema/error-table 和两个 trust 文件的 SHA-256，不记录文件内容。

### 运行配置

CP7-A 的 canonical MCP 配置精确为七项：

```text
MCP_USER_SCOPED_GATEWAY_ENABLED=true
MCP_ROUTING_MODE=enforce
MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=false
MCP_ENFORCE_COHORTS=
MCP_ENFORCE_PERCENT=100
MCP_ENFORCE_HASH_SALT=main-cp7a-user-scoped-v1
MCP_ENFORCE_COHORT_CONFIG_FILE=
```

同时必须满足以下本地装配项：

```text
MAF_API_ENV=dev
MAF_STATE_STORE_BACKEND=sqlite
MAF_STATE_PLATFORM_CONFIG_BRIDGE=0
MAF_CONFIG_PATH=/run/cp7-inputs/config.yaml
MAF_RUST_RUNTIME_STORE_MODE=off
MAF_RUST_EVENT_LOG_MODE=off
MAF_RUST_TASK_DISPATCHER_MODE=off
MAF_RUNTIME_SIDECAR_ENDPOINT=unix:///run/maf-runtime-sidecar/runtime.sock
MAF_RUNTIME_SIDECAR_BINARY_PATH=/run/cp7-inputs/runtime-sidecar.bin
MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH=/run/cp7-inputs/runtime-sidecar-manifest.json
MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH=/run/cp7-inputs/runtime-sidecar-allowlist.json
MAF_RUNTIME_SIDECAR_SBOM_PATH=/run/cp7-inputs/runtime-sidecar-sbom.json
MAF_RUNTIME_SIDECAR_PROVENANCE_PATH=/run/cp7-inputs/runtime-sidecar-provenance.json
MAF_MASTER_KEY_FILE=/run/secrets/maf-master.key
MCP_LEGACY_RETIREMENT_INVENTORY_PATH=/run/cp7-inputs/legacy-retirement-inventory.json
MCP_LEGACY_RETIREMENT_KEY_FILE=/run/cp7-inputs/legacy-retirement.key
MAF_MCP_CP7_LOCAL_SAFETY_ENABLED=true
MAF_MCP_CP7_CANDIDATE_ID=<approval_request_id>
MAF_USER_MCP_MAX_ACTIVE_CALLS=<explicit-positive-int>
MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES=<explicit-positive-int>
```

CP7-A 和未来 CP7-B 都明确选择 Python/SQLite 作为 Task/TaskNode、event log 与 task-dispatch/bundle-pin 权威：`MAF_RUST_RUNTIME_STORE_MODE`、`MAF_RUST_EVENT_LOG_MODE`、`MAF_RUST_TASK_DISPATCHER_MODE` 必须全部精确为 `off`。Rust Sidecar 仍是 canonical MCP enforce 启动所需的可信 binary、兼容握手和 readiness 端点，并保留将来可独立启用的实现/contract，但本流程不把 Task、TaskNode、Event 或 bundle pin 写入 Sidecar，不要求 Task authority migration evidence，也不新增 `ConvergeLegacyRuntimeRetirement` RPC。`MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_PATH` 与 `MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_HMAC_KEY_PATH` 必须不存在；三个 mode 任一为 `shadow/enforce`、缺失、未知值或存在任一迁移 evidence key 都阻断候选。Sidecar 的持久 SQLite/reopen 测试仅证明工件和服务重启正确，不能被表述为 CP7-A/CP7-B authority 已迁移。

CP7-A 的展开环境还必须拒绝 `MAF_POSTGRES_STATE_DSN`、`MAF_POSTGRES_STATE_SCHEMA`、`MAF_MCP_ROLLOUT_APP_DSN`、`MAF_MCP_ROLLOUT_SNAPSHOT_DSN`、`MAF_MCP_ROLLOUT_EVALUATOR_DSN`、`MAF_MCP_ROLLOUT_OPERATOR_DSN`、`MAF_MCP_ROLLOUT_DRILL_DSN`、`MAF_MCP_LEGACY_MIGRATION_DSN` 及任何 production rollout admission ID/key。`config.yaml` 即使声明 state-platform bridge 或 PostgreSQL，也不得覆盖上述显式 SQLite 环境；候选 preflight 必须在进程启动前检查展开环境和实际 backend，发现 PostgreSQL/rollout DSN 即拒绝。

phase contract 不允许调用方自由选择旧开关形态。共享 runtime validator 必须只接受下表七个 tuple；`tree` 是 trust subject、Git tree、镜像 revision 所指 commit 的实际 tree，`legacy key` 同时约束 canonical Compose、`config.yaml`、validation runner 与长期容器 inspect environment：

| expected release | expected phase | expected tree | inventory required | legacy key |
|---|---|---|---:|---|
| `B_L` | `bl_prefreeze` | `tree(B_L)` | false，且 inventory 必须不存在 | present，值精确为 `true` |
| `B_L` | `frozen_baseline` | `tree(B_L)` | true | present，值精确为 `true` |
| `C_A` | `rehearsal` | `tree(C_A)` | true | present，值精确为 `false` |
| `C_A` | `authoritative_candidate` | `tree(C_A)` | true | present，值精确为 `false` |
| `C_B` | `retirement` | `tree(C_B)` | true | absent |
| `R_A` | `rollback_to_ca` | `tree(C_A)` | true | present，值精确为 `false` |
| `R_L` | `rollback_to_bl` | `tree(B_L)` | true | present，值精确为 `true` |

`R_A` 虽不是 `C_A` commit，但 tree 精确等于 `C_A`，因此继承 assembly-off 配置并要求 legacy key `present=false`；只有 `C_B/retirement` 要求 key absent。`A_B` 的 tree 同样等于 `C_A`，但它只证明 abort 后源码恢复，不是 deployment release，不能与任何 phase 拼成第八个 tuple。release、phase、tree、inventory-required 或 key presence/value 任一不符合表中同一行都拒绝；不得把 `false` 与 absent 互换，也不得把任意合法列拼成表外组合。

### CP7-local 安全 detector 与 Ready 门禁

CP7-A 不启用 production rollout admission，因此不能依赖只在 production activation 存在时才装配的 rollout recorder。`MAF_MCP_CP7_LOCAL_SAFETY_ENABLED=true` 与 `MAF_MCP_CP7_CANDIDATE_ID=<approval_request_id>` 只在 `{branch=main, MAF_API_ENV=dev, MCP_ROUTING_MODE=enforce, MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=false}` 的本候选闭集内有效；任一 scope 不符、candidate ID 不是当前 request、或 production admission/rollout DSN 同时存在都拒绝启动。

backend 必须独立构造与 production 相同的 `AuthoritativeMCPSafetyDetectorRegistry`，并把下列八个 red line 精确注册到既有权威 hook；hook 名、red-line 名、reason code 或数量不得由 Compose/caller 自选：

| red line | authoritative hook | closed violation reason |
|---|---|---|
| `cross_user_access` | `gateway.task_owner_boundary` | `task_owner_mismatch` |
| `secret_exposure` | `audit.secret_payload_boundary` | `secret_payload_rejected` |
| `dual_tool_call` | `dispatch.durable_call_idempotency_boundary` | `call_idempotency_conflict` |
| `unauthorized_tool_call` | `dispatch.permission_boundary` | `permission_denied_boundary` |
| `endpoint_policy_bypass` | `gateway.endpoint_policy_boundary` | `endpoint_policy_rejected` |
| `unknown_result_replay` | `recovery.unknown_replay_boundary` | `unknown_replay_blocked` |
| `shadow_tool_call` | `gateway.persisted_assignment_boundary` | `shadow_call_blocked` |
| `persistent_resource_leak` | `gateway.resource_cleanup_boundary` | `cleanup_failed` |

CP7-0 新增候选专用 append-only SQLite 表 `mcp_cp7_safety_ledger`。closed row 精确包含 `record_id`、`candidate_id`、`epoch_id`、`config_fingerprint`、`record_kind`、`red_line`、`hook_id`、`bucket_started_at`、`bucket_ended_at`、`reason_code`、`value`、`boundary_source_sha256`、`payload_sha256`、`recorded_at`；`record_kind` 只允许 `registration | attestation | violation | gap`。registration 必须是 exact epoch/red-line/hook、无 bucket、`reason_code=registered,value=0`；attestation 必须是 exact epoch/red-line/hook、UTC 分钟边界且恰好 60 秒、`reason_code=observed_zero,value=0`；violation 必须使用上表 exact reason、观测时间所属 epoch/分钟和 `value=1`；gap reason 只允许 `detector_unregistered | detector_unhealthy | interval_attestation_missing | safety_metric_write_failed | terminal_metric_write_failed | producer_interval_missed | zero_series_write_failed | unplanned_process_exit | maintenance_boundary_invalid` 且 `value=1`。registry-wide gap 的 red-line/hook 同时为空，hook-specific gap 则同时非空且精确匹配。普通用户在调度前拒绝 Grant 时必须在 Gateway 之前收敛为零 Tool call，这是正常 deny，不记 `unauthorized_tool_call`；只有已被拒绝后仍尝试穿越 `dispatch.permission_boundary` 才是该 violation，此 positive 路径只允许在 disposable probe 中执行。

registration/attestation 使用由 candidate/epoch/red-line/hook/window 派生的确定性 record ID，violation/gap 还绑定 durable boundary source。相同 ID/相同 logical payload 只允许 idempotent no-op，相同 ID不同 payload 返回 conflict。StoragePort 不提供 update/delete/reset API，SQLite trigger 与 bootstrap contract 必须拒绝修改/删除既有 row。SQLite CHECK、Python enum 与 manifest validator 必须共享同一 closed export；unknown、重复、负值、非整分钟、交叉 candidate/epoch 或 payload SHA 不符全部 fail closed。`config_fingerprint` 必须绑定七项 MCP 配置、三个 Rust mode、Sidecar trust identity 和本地安全开关。

为允许人工检查中已计划的 backend restart，CP7-0 同时新增 append-only `mcp_cp7_ready_epoch_event`。closed row 精确包含 `event_id,candidate_id,epoch_id,predecessor_epoch_id,event_kind,container_id,image_id,config_fingerprint,boundary_at,audit_device,audit_inode,audit_offset,ledger_record_count,inflight_state_sha256,payload_sha256`；`event_kind` 只允许 `opened | ready | maintenance_started | closed | invalidated`。每个 epoch 必须严格遵守 `opened→ready→maintenance_started→closed`，最后一个 epoch 在取 approval snapshot 时可以 `opened→ready→closed`；任一 `invalidated` 为终态。event ID 由 candidate/epoch/kind/predecessor payload 确定性派生，表不提供 update/delete API。

另设每 candidate 唯一的 durable `mcp_cp7_candidate_guard`，其 `invalid_latched` 初始为 0，只允许在与首个 positive violation/gap 同一事务中从 0 单向变为 1，并固化 `first_invalid_record_id/reason/at`；SQLite trigger 拒绝 `1→0`、替换首个原因或删除 guard。读到 latch=1 后所有 Ready、manifest、approval 和 claim writer 都必须拒绝。若底层 ledger 故障导致 gap row 与 latch 事务本身无法 durable，进程必须立即 hard-exit；外层 verifier 根据缺失的 epoch close/attestation 或突然终止将候选判为无法批准，不得声称一定已持久化 gap。

每次进程启动都必须在 Not Ready 中先持久化新 `opened` epoch，持久化并重验该 epoch 的八条 exact registration，确认 Audit/Gateway/Coordinator/Recovery 的 hook 已实际注入，再对 ledger 做一次写入/重开读取 canary。backend 在该 epoch 的第一个完整 UTC 分钟内保持 Not Ready；Compose backend healthcheck 的 start period 必须至少 90 秒。只有 latch=0、八个 detector 都 registered/instrumented/healthy，且该完整分钟的八条 attestation 均已 durable，才可持久化 `ready` event 并对外 Ready。之后每个完整 Ready 分钟都必须完成八条 fresh interval attestation；不得补写过去窗口或以进程内状态代替持久证据。

已计划 restart、人工批准 quiesce 或其他 controlled-maintenance 只能由 candidate verifier 发起，并且只能在当前 Ready epoch 已完成正在进行的 UTC 分钟八项 attestation 后，于紧接该 bucket 的分钟边界原子停止接收新请求并切换 Not Ready；不得在部分分钟中途关停或把该部分分钟排除出观测。随后等待已完成写入刷盘，对允许保留的在途 Call/intent/outbox 写定界 digest，记录 audit offset 和 ledger count，再持久化 `maintenance_started` 与 `closed`；`closed.boundary_at` 必须精确等于最后八条 attestation 共同的 `bucket_ended_at`，`maintenance_started.boundary_at` 也必须等于该值。若边界 CAS 失败、最后 bucket 缺任一 attestation，或切换 Not Ready 后仍接收请求，必须写 gap 并 latch invalid。

restart 只允许对同一 container 执行不 recreate；container ID、image ID、config fingerprint、staged volume、application data/Sidecar/audit/ledger 卷必须不变。重启后在对外工作前重验 predecessor close、audit/ledger 边界与在途状态恢复，开启 successor epoch，再完成新的整分钟观测才 Ready。只有这类完整、相邻且 minute-aligned 的 close/open chain 所覆盖的 Not Ready 维护时间不计 detector gap；未计划 crash、缺 receipt、identity 变化、维护期仍接收请求或 successor 观测不完整都必须生成 gap/latch 或由外层缺口证据使 candidate 失效。approval snapshot 的 `observation_ended_at` 必须等于 final `closed.boundary_at`，不存在“最后完整 bucket 后仍 Ready、但不在批准证据中”的尾窗。

同一 candidate 出现任一 positive violation、gap、漏分钟、unhealthy/unregistered hook、ledger tamper 或 durable write failure 后永久失去批准资格；restart 和新 epoch 不能清除 latch。只能停止该 candidate，生成新 approval request ID 和全新 authoritative project/卷后重新测试。候选 verifier 在 controlled quiesce 后以同一 SQLite transaction 读取 closed ledger/epoch/guard snapshot，验证所有 Ready epoch 内整分钟的八项连续性、所有相邻维护边界和 latch=0。snapshot 使用 closed schema，精确包含：

```text
schema
candidate_id
config_fingerprint
registry_definition_sha256
epoch_chain_sha256
ready_epochs
maintenance_boundary_count
observation_started_at
observation_ended_at
registration_count_by_red_line
attestation_interval_count_by_red_line
violation_count_by_red_line
gap_count
invalid_latched
record_count
ordered_record_payload_sha256s
snapshot_sha256
```

manifest 绑定自动阶段 snapshot；approval 绑定从 authoritative 候选首个 epoch 到 approval 时点的完整 epoch union；CP7-B preflight 必须在单事务重算并得到与 approval 相同的 SHA。缺表、空账本、unknown row、candidate/config mismatch、事务期间漂移、epoch fork/缺边界、Ready epoch 内八项 registration/attestation 不完整、latch=1、violation 非零或 gap 非零都阻断。

安全 hostile tests 必须在独立的 disposable probe Compose project/SQLite ledger 中运行，证明每个边界的 positive violation 或 gap 都会阻断 Ready；probe 的容器、卷、candidate ID 和 ledger SHA 只能作为 automation check digest 绑定，绝不能复用为人工候选。权威人工候选启动后不得故意触发 red line；尤其跨用户真实拒绝尝试只能以 probe automation substitution 证明，人工阶段只允许比较 A/B 各自 owner-scoped 列表和已脱敏可见性，不向 A 的资源发送 B 的越权请求。

### 运行时行为

启动和运行期间必须满足：

- 不构造进程级 `MCPRuntimeState`。
- 不创建 legacy 全局 MCP Client。
- 不执行启动期 legacy `server/discover` 或 `tools/list`。
- 不把 legacy Server/Tool 动态注册为全局 capability 或 instance。
- 不为新 Task 写入或依赖 legacy bundle revision。
- 新 Task 只可选择 user-scoped 路径，不得静默回落到 legacy。
- 普通对话在用户未配置、已禁用或没有健康 MCP Server 时仍返回 HTTP `202`；Task 固化 `mcp_execution_mode=user_scoped`、`mcp_route_reason_code=enforce_selected`，owner-scoped registry/planner 隐藏 `mcp.dispatch`，由非 MCP capability 正常完成，不发 `mcp.runtime_unavailable`。
- 用户以 `force_capability=mcp.dispatch` 显式请求 MCP、但没有可用 owner-scoped Server profile 时，仍返回 HTTP `202` 和 Task ID；Task 固化 `mcp_execution_mode=unavailable`、`mcp_route_reason_code=no_user_scoped_server`，不创建 MCP Node、不调用 executor/network，SSE 依次发 `mcp.runtime_unavailable` payload `{"status":"unavailable","reason_code":"no_user_scoped_server"}` 和终态 `task.failed` payload `{"code":"mcp_runtime_unavailable"}`。
- Task 已创建后 Server 被禁用/删除或健康状态失效时，dispatch 边界返回同一非重试 capability error `mcp_runtime_unavailable`、发同一 frontend event/reason，并以 `task.failed` 收敛；不得换 Server、改派或调用 legacy。

这三个值属于不同 closed category，不要求跨类别出现同名成员：

| 类别 | 固定值与同步范围 |
|---|---|
| Task route reason | `no_user_scoped_server`；只进入 Python route-reason enum/validator、Task SQLite/PostgreSQL CHECK、必要的 Rust Task DTO/contract 和 API Task DTO。 |
| terminal/capability error | `mcp_runtime_unavailable`；只进入 Python capability/terminal error table、Task terminal payload/error validator、必要的 Rust error contract 和 frontend terminal-error parser。 |
| SSE event | event name `mcp.runtime_unavailable`；payload exact schema 为 `{status:"unavailable",reason_code:"no_user_scoped_server"}`，由 API event allowlist 与 frontend reducer共同闭合。 |

CP7-0 必须分别同步上述 category 的 canonical export 与 additive schema migration；不得要求一个 category 接受另一个 category 的值，也不得用开放字符串把未知值降级。旧 row/event 仍可只读解析；新值必须能跨其实际持久化边界 restart round-trip。未知值、错误类别、大小写近似、空白、前后缀或 fallback string 均 fail closed。

#### 无 Server durable intent 与原子终态

“当前没有 Server”不能只靠 Task route 字段或启动时重新推断。CP7-0 新增 durable、CAS 状态受限模型 `MCPNoServerIntent`，并在 SQLite/PostgreSQL metadata 中 additive 建表。closed row 精确包含：

```text
intent_id
owner_user_id
task_id
node_id | null
trigger = initial_no_profile | target_server_revalidation
requested_server_id | null
requested_server_config_version | null
requested_server_security_version | null
owner_server_set_fingerprint | null
resume_envelope_json | null
resume_envelope_sha256 | null
status = armed | available | unavailable | dispatched | resolved | converged | unknown
revision
evidence_sha256
created_at
updated_at
terminal_at | null
```

`intent_id` 固定为 `mcp-no-server-intent:v1:<task_id>:initial` 或 `mcp-no-server-intent:v1:<task_id>:<node_id>`，每个 Task 最多一个 initial intent，每个 `(task_id,node_id)` 最多一个 target intent。owner 只能由 repository 从 Task→Conversation 推导，caller 不能传入或覆盖。`initial_no_profile` 必须有 owner server-set fingerprint，且 server/node/version/resume 字段全为 null；fingerprint 只 canonicalize 按 `server_id` 排序的 `[server_id,config_version,security_version,enabled,health_status,deletion_pending,deleted_at_is_set]`，不含 endpoint、credential、auth metadata 或 display text。

`target_server_revalidation` 必须绑定 exact owner、Task、`mcp.dispatch` Node、requested Server ID 和一个 size-bounded canonical resume envelope。Server row 存在时 config/security version 必须是同一锁定快照中的正整数；目标在绑定事务中已不存在或不属于 owner 时两者必须同时为 null，intent 直接为 `unavailable`。新 writer 使用 `maf.user_mcp.dispatch_resume.v2`，只保存重建原 Node 所需的 Task/Conversation/root Message/Node 引用、exact Server ID、immutable Task assignment、validated node/edge 快照、TaskInputAttachment ID 和 dependency Node 的 Artifact refs；实际 MCP input/output、Tool 参数/结果、附件正文、Base64、endpoint、credential 和 auth metadata 全部禁止，并用独立 SHA 绑定。恢复时按持久化引用重新验证并投影安全 summary；无顶层 schema 的历史 v1 intent 继续只读兼容，未知 active schema 阻断 Ready。完整合同见 `2026-08-18-mcp-dispatch-reference-resume-envelope-design.md`。`revision` 从 0 开始、每次状态 CAS 加一；`resolved|converged|unknown` 必须有 terminal time。partial、跨 owner、同 ID 不同 payload、摘要漂移、unknown trigger/status 或自由文本全部拒绝并阻断 Ready。

Server 可用条件固定为 `enabled=true AND health_status=available AND deletion_pending=false AND deleted_at IS NULL`。为封闭“空 Server 集合与并发创建第一个 Server”的竞争，CP7-0 新增持久表 `user_mcp_owner_mutation_guard`，closed row 精确包含 `owner_user_id,revision,server_set_fingerprint,created_at,updated_at`，`owner_user_id` 为主键且 `revision` 从 0 单调增加。Server create、影响 availability/config/security/credential binding 的 update、health completion、disable/delete，以及 initial intent 的 Server-set snapshot 都必须先 ensure-and-lock 同一 guard；第一个 Server 创建不得绕过空集合 guard。SQLite 使用同一 `BEGIN IMMEDIATE` 写事务执行 guard insert-if-absent、Server mutation、重算 fingerprint 与 revision CAS；PostgreSQL 使用 `INSERT ... ON CONFLICT DO NOTHING` 确保空集合也有 durable row，再 `SELECT ... FOR UPDATE` 锁 guard，然后按后述固定顺序锁定 Server/intent/outbox/Call/Task/Node。无 guard、guard fingerprint 与重算集合不符、revision 回退或 repository 外的 Server 写入都是 corruption 并阻断 Ready。

首次显式 `force_capability=mcp.dispatch` 只能调用：

```text
create_user_mcp_initial_intent(
  task,
  occurred_at,
) -> CREATED_UNAVAILABLE | RETRY_ROUTE | ALREADY_CREATED
```

caller 不能传 owner、Server fingerprint、route reason、terminal code、event payload或“是否无 Server”。repository 在同一事务从 Conversation 推导 owner、锁 owner guard、重读全部 owner Server 并计算 fingerprint；发现可用 Server 时零写入返回 `RETRY_ROUTE`，API 用同一 Task ID 重新计算路由；仍为空时才原子保存 Task assignment `unavailable/no_user_scoped_server` 和 `initial_no_profile/unavailable` intent。事务提交后立即调用统一收敛命令。该路径不得创建 MCP Node/Branch/Call、scope lease 或 dispatch-resume outbox，也不得调用 executor/Gateway/network；Task、intent 和收敛事务都提交后才返回 HTTP `202`/Task ID。

validated WorkflowPlan 每出现一个 `mcp.dispatch` Node，必须把对应 TaskNode 与 target intent 在同一 StoragePort 事务首次落库：

```text
arm_user_mcp_target_intent(
  task_id,
  node_id,
  requested_server_id,
  resume_envelope,
  occurred_at,
) -> ARMED | UNAVAILABLE | ALREADY_ARMED
```

repository 验证 Task assignment 仍为 immutable `user_scoped/enforce_selected`、Node capability/input payload 闭合，锁 owner guard 与目标 Server。Server exact 可用时把 versions 和 `armed` intent 落库；不存在、跨 owner、disabled、非 available、deletion-pending 或已删除时记录 requested ID、null versions 与 `unavailable`，不得换 Server、重新规划或回落 legacy。

当前线程与 startup recovery 都只能通过下列命令重验证 target：

```text
resolve_user_mcp_target_intent(
  intent_id,
  occurred_at,
) -> AVAILABLE | UNAVAILABLE | ALREADY_RESOLVED
```

只有 owner、Server ID、config/security version 和完整 availability predicate 与 intent 精确相等，repository 才在同一事务 CAS `armed→available` 并插入唯一 `outbox_id=mcp-dispatch-resume:v1:<intent_id>`。outbox closed payload 绑定 intent/task/node/server/resume-envelope SHA，并使用 `pending|claimed|completed|aborted`、claim owner/token、lease、revision 和时间字段；同 ID同 payload exact-idempotent，同 ID不同 payload为 corruption。任一目标漂移则 intent=`unavailable` 且不建 outbox。

dispatch-resume consumer 通过 claim token/lease/revision CAS，恢复前再次验证 Task/Node 非终态、intent/outbox revision、resume envelope SHA 与 exact Server 快照。漂移时原子 abort outbox、把 intent 转为 `unavailable` 并进入无 Server 收敛；验证通过时只恢复原 `mcp.dispatch` Node。真正 Tool call admission 必须把 `available→dispatched` 与 durable Call reservation、`may_have_dispatched=true` 在同一 authority transaction 提交，只有该提交成功者可以发送一次网络请求。consumer/claim 在任何 Tool side effect 前失效且没有 `may_have_dispatched` Call 时，允许 CAS 回 `available` 并重排同一 outbox。

对 `may_have_dispatched=true` 的 Call，recovery 必须先在同一 authority transaction 查找可信 sealed terminal-result candidate 或已提交 receipt，而不是直接转 unknown。只有同时满足以下 closed predicate 的 candidate/receipt 可信：与 exact Call reservation/intent/task/node/server/version 一致，terminal state 属于已有闭集，result payload/ref 的 canonical SHA 与 sealed manifest/receipt匹配，且不存在第二个竞争 candidate/receipt。命中未消费 candidate 时调用下述唯一 authority writer 正常分支；命中既有 receipt 时重验其正常投影。两者都把 outbox 转 `completed`、intent 转 `resolved` 并按既有正常结果收敛 Task/Node，即使 Server 同时已删除也不得丢弃该结果。只有 `may_have_dispatched=true` 且没有可信 candidate/receipt 时，才绝不重排、换 Server 或重放，转 `mcp.execution_status_unknown`/no-replay；无 may-have-dispatched 证据时才可按 lease/revision 规则回到 `available`。

> 2026-08-18 后续合同：上述“任一可信terminal receipt立即把整个outbox/intent/Task/Node
> 收敛”的单Call语义，仅适用于聚合加固实施前的legacy路径。实施
> `2026-08-18-mcp-dispatch-aggregate-recovery-hardening-design.md` 后，receipt只终结当前Call；
> ordinary completed保持dispatch active，MRTR/remote按各自持久证据续作，只有统一dispatch
> finalizer终结outbox/intent/Node/Task。本设计的candidate-before-receipt、late-result、
> no-replay、safety ledger和人工退役门禁仍保持权威。

普通 Tool result、remote-task result 和 MRTR terminal result 不得使用各自的旁路 writer。三者先调用同一个 durable result persister，把已验证的 normalized terminal result 封存为 task-owned、content-addressed `MCPValidatedTerminalResultCandidate`。candidate manifest ID 固定为 `mcp-terminal-candidate:v1:<call_id>:<result_payload_sha256>`，closed payload 精确为 `{candidate_id,owner_user_id,conversation_id,task_id,node_id,intent_id,call_id,server_id,server_config_version,server_security_version,terminal_state,result_payload_sha256,safe_result_ref,safe_result_ref_sha256,safe_error_code,sealed_at}`；completed 要求 safe result ref/ref SHA non-null 且 error null，failed/cancelled 相反。manifest 使用 no-clobber、file+directory fsync、secure read 和同 ID逐字段比对，不含 raw payload/credential/endpoint。其 task/call 索引同样 durable，startup 能枚举尚无 terminal-result receipt 的 sealed candidate；文件 seal 后、authority transaction 前崩溃只留下可重试 candidate，不得据此改变 Task/Call。

唯一状态 writer 为 `commit_authoritative_mcp_terminal_result(call_id,candidate_id,occurred_at) -> COMMITTED_NORMAL | COMMITTED_LATE | ALREADY_COMMITTED | CONFLICT`。它 secure-read candidate 后遵守完整锁序 `owner guard → target Server → intent → dispatch-resume outbox → MCPCallRecord → sealed candidate → terminal-result receipt → terminal projection → Task → TaskNode`，并从 candidate 重算唯一 `result_receipt_id=mcp-terminal-result:v1:<call_id>:<result_payload_sha256>`。若 intent/Call 尚处于正常 dispatched 路径，单个 SQLite/PostgreSQL transaction 同时 insert-or-compare receipt、CAS intent/outbox、保存安全 result ref、正常收敛 Task/Node并插入既有确定性 terminal event；若 intent/projection 已是下述 unknown，writer 在同一 transaction insert-or-compare同一 receipt并走 late-result projection分支。任一步失败整体回滚；sealed candidate 保留供 startup 重试。事务提交后响应丢失只返回 `ALREADY_COMMITTED`；相同 receipt/candidate exact-idempotent，相同 ID不同 payload、第二个可信 candidate/receipt或 candidate binding漂移均为 corruption 并阻断 Ready。被 receipt 消费的 candidate 只在 receipt/result store retention允许时清理；unconsumed candidate 不得在 startup reconciliation 前由 janitor 删除。

`MCPTerminalResultReceipt` 是 authority DB 中的 immutable closed row，exact fields 为 `{result_receipt_id,candidate_id,owner_user_id,conversation_id,task_id,node_id,intent_id,call_id,server_id,server_config_version,server_security_version,terminal_state,result_payload_sha256,safe_result_ref,safe_result_ref_sha256,safe_error_code,completion_mode,committed_at}`。`result_receipt_id` 与 `candidate_id` 各自 unique；所有 owner/task/node/call/server/version/result 字段必须与 sealed candidate 和锁定 reservation逐项相等。`terminal_state` 只允许 `completed|failed|cancelled`；safe result/error nullable 组合与 candidate相同。`completion_mode` 只允许 `normal_terminal_projection|late_result_no_continuation`，由 writer 根据锁内 intent/projection state 推导，caller 不得传；late 模式永远不得调度 continuation。相同 ID同 row exact-idempotent，同 ID不同 row或同 call第二个 receipt都是 authority corruption。

`unknown` 是无可信结果时的 no-replay fence，不是删除之后到达的已提交结果。CP7-0 新增独立 durable `MCPExecutionTerminalProjection`；它不改动现有 Task/TaskNode enum，也不把终态 Task/Node 反向迁移。closed row 精确包含：

```text
projection_id
owner_user_id
conversation_id
intent_id
call_id
task_id
node_id
status = unknown | late_result_resolved
revision
no_replay
reason_code = trusted_terminal_result_absent
unknown_intent_revision
unknown_event_id
task_failed_event_id
unknown_terminal_at
task_terminal_status = failed
node_terminal_status = failed
result_receipt_id | null
result_payload_sha256 | null
resolved_terminal_state | null
safe_result_ref | null
safe_result_ref_sha256 | null
safe_error_code | null
resolved_intent_revision | null
resolution_event_id | null
correction_event_id | null
result_committed_at | null
resolved_at | null
created_at
updated_at
```

`projection_id=mcp-terminal-projection:v1:<call_id>`；`projection_id`、`call_id`、`intent_id`、`unknown_event_id`、`task_failed_event_id` 各自 unique，非空的 result receipt/resolution/correction ID 也各自 unique。owner/conversation/task/node 全部由 repository 从已锁定 Call/Task 推导，caller 不能传。首次 unknown transaction 原子把 intent 置 `unknown`、Task/Node 置既有 `FAILED`、Call 保持 no-replay unknown 终态，并插入 projection `status=unknown,revision=0,no_replay=true`；此时 result/resolution/correction/resolved 字段全部 null。该 transaction 使用 insert-or-compare 依次追加两个 durable event：

1. `event_id=mcp-execution-status-unknown:v1:<call_id>:<intent_revision>:01-unknown`，event type=`mcp.execution_status_unknown`，closed payload 精确为 `{schema,projection_id,intent_id,call_id,task_id,node_id,projection_revision,intent_revision,unknown_terminal_at,reason_code,no_replay,result_receipt_id,predecessor_event_id}`；`schema=maf.user_mcp.execution_status_unknown.v1`、`projection_revision=0`、`reason_code=trusted_terminal_result_absent`、`no_replay=true`、`result_receipt_id=null`、`predecessor_event_id=null`，created_at=`unknown_terminal_at`。
2. `event_id=mcp-execution-status-unknown:v1:<call_id>:<intent_revision>:02-task-failed`，event type=`task.failed`，closed payload 精确为 `{schema,projection_id,call_id,task_id,node_id,code,no_replay,unknown_event_id,predecessor_event_id}`；`schema=maf.user_mcp.unknown_task_failed.v1`、`code=execution_status_unknown`、`no_replay=true`、两个 predecessor 字段都等于第一条 event ID，created_at=`unknown_terminal_at+1 microsecond`。

两条 event 与 Task/Node/Call/intent/projection 在同一 transaction 提交；任一步失败整体回滚。unknown 必须先于会关闭 live SSE 的 `task.failed`，使在线客户端能先看到 no-replay 原因。这里的 predecessor 只描述 terminal-projection 子链；不得绑定“该 Node 最近任意 event”，以免并发事件制造伪冲突。projection 和 event 都不得删除或覆盖。

可信 terminal result candidate 在 unknown 提交前被统一 writer 看到时，走正常分支并正常收敛 Task/Node。只有 unknown 已提交后，同一个 `commit_authoritative_mcp_terminal_result` 才进入 late-result 分支；不存在另一个依赖预先存在 receipt 的 resolver。caller 只传 opaque call/candidate ID与时间，terminal state、payload SHA、result ref 和错误码全部由 sealed candidate 推导。该分支在同一固定锁序中重验 candidate、Call reservation、intent、projection revision、unknown event及 Server/version binding，并在 transaction 内先 insert-or-compare receipt，再执行其余 CAS。CAS 条件固定为 projection `status=unknown,revision=0,result_receipt_id IS NULL,unknown_event_id=<expected>` 与 intent `status=unknown,revision=projection.unknown_intent_revision,terminal_at=projection.unknown_terminal_at`。transaction 随后 CAS intent `unknown→resolved`、CAS projection `unknown/revision=0→late_result_resolved/revision=1`、把 dispatch outbox 收敛为 `completed` 并绑定 receipt 与 `completion_mode=late_result_no_continuation`、保存 candidate 推导的安全结果，再追加下列两个事件。原 Task、Node、Branch 与 MCPCallRecord 保持 fail-closed unknown/failed 终态，不恢复下游、不再次调度、不改写其 terminal time；late result 只成为独立 projection 的最新可见证据。`resolved_terminal_state` 只允许 `completed|failed|cancelled`；`completed` 要求 safe result ref/ref SHA non-null 且 `safe_error_code=null`，其余两项相反。相同 candidate/receipt 幂等返回 `ALREADY_COMMITTED`；第二个可信 candidate/receipt、状态不匹配或任一步失败均整体回滚并阻断 Ready。

1. resolution event：`event_id=mcp-late-terminal:v1:<call_id>:1:01-resolution`，closed payload 精确为 `{schema,projection_id,intent_id,call_id,task_id,node_id,unknown_event_id,task_failed_event_id,result_receipt_id,from_projection_revision,to_projection_revision,from_intent_revision,to_intent_revision,unknown_terminal_at,resolved_at,predecessor_event_id}`，其中 `schema=maf.user_mcp.execution_status_resolution.v1`、`from_projection_revision=0`、`to_projection_revision=1`、`predecessor_event_id=<task_failed_event_id>`。
2. late-result correction event：`event_id=mcp-late-terminal:v1:<call_id>:1:02-correction`，closed payload 精确为 `{schema,projection_id,intent_id,call_id,task_id,node_id,unknown_event_id,resolution_event_id,result_receipt_id,result_payload_sha256,projection_revision,terminal_state,safe_result_ref,safe_result_ref_sha256,safe_error_code,resolved_at,task_remains_failed,node_remains_failed,no_replay,predecessor_event_id}`，其中 `schema=maf.user_mcp.late_terminal_result_recovered.v1`、`projection_revision=1`、两个 remains-failed 与 `no_replay` 都为 true、`predecessor_event_id=<resolution_event_id>`。

resolution event 的 `created_at=max(database_now,task_failed_event.created_at+1 microsecond)`，correction event 的 `created_at=resolution_event.created_at+1 microsecond`；现有 replay 的 `(created_at,event_id)` 排序因此严格为 unknown→task.failed→resolution→correction，不依赖 event ID 偶然排序。该 authority transaction不得调用现有 generic merge-style event append；必须使用 insert-or-compare：相同 ID/相同 canonical payload 是 no-op，相同 ID不同 payload、第二个 resolution/correction 或 predecessor 不连续都是 corruption。

现有 live SSE 在 `task.failed` 后会关闭，因此设计不宣称终态客户端必然实时收到 late correction。Task detail/history API 必须 secure-read terminal projection，并在恢复终态 Task 时执行一次完整 durable event replay；frontend 为 resolution/correction 增加 terminal-phase 例外 reducer，保存独立 `lateResult` 状态，但绝不把 Task phase 改成 completed。reducer 以 `event_id` 去重并按 predecessor 链消费：unknown 显示执行状态不确定；resolution 只建立 supersede 关系；correction 显示“任务仍因未知执行状态失败，但已恢复可信迟到结果”及安全结果引用/固定错误。先收到 correction 而缺 predecessor 时缓存并补拉，补拉后仍缺失或 payload 冲突则显示 recoverable sync error。旧 unknown 与 `task.failed` 历史永久保留，禁止 mutate/replace 已投递 event。没有 durable terminal-result receipt 的迟到网络响应仍拒绝。delete-before-result 时已有 may-have-dispatched reservation必须保留 projection/result-writer 所需记录；result-before-delete 时 delete 读取已 resolved receipt，不得重新写 unavailable/unknown。

Server delete/disable/security update、target dispatch、普通/remote/MRTR terminal result writer 共享上述固定锁序。delete 先提交且尚无 may-have-dispatched Call 时，把 intent 置 unavailable、pending/claimed outbox 置 aborted，后续 call admission 零网络调用并统一收敛；call admission 先提交时，后续 delete 不得伪装为“未调用”，必须先消费已持久的可信 terminal result，只在无该 result 时转 unknown/no-replay。禁止 repository 外“先读后写”决定赢家。

唯一 StoragePort 收敛命令为：

```text
converge_user_mcp_no_server(
  task_id,
  occurred_at,
) -> CONVERGED | ALREADY_CONVERGED | ALREADY_TERMINAL
   | UNKNOWN_REQUIRES_NO_REPLAY
```

幂等键由 repository 固定派生为 `mcp-no-server:v1:<task_id>`；caller 不能传 intent selector、route reason、error code、event payload 或判定结果。authority 在单事务锁定 Task 的全部 unresolved intent/outbox/Node/Call/sealed candidate/receipt 后，必须先按上述 closed predicate消费可信 candidate/receipt；若因此完成正常 Task/Node 收敛，返回 `ALREADY_TERMINAL`，绝不写 no-server terminal。无可信 candidate/receipt 时才重算 closed precondition：initial 必须存在唯一 unavailable intent、Task 为 `unavailable/no_user_scoped_server` 且没有 MCP Node/Branch/Call；target 必须保持 `user_scoped/enforce_selected`，存在 unavailable target intent 和精确非终态 `mcp.dispatch` Node。任一关联 Call 已 `may_have_dispatched=true` 且无可信 candidate/receipt 时零写入 no-server terminal，返回 `UNKNOWN_REQUIRES_NO_REPLAY`；Task 已其他终态则 `ALREADY_TERMINAL` 零写入。

成功事务必须把 Task=`FAILED`、target `mcp.dispatch` Node=`FAILED`、其 downstream 非终态 Node=`BLOCKED_BY_CANCELLATION`、全部关联 resume outbox=`aborted`、参与判定的 intent=`converged` 原子写入，保存固定 terminal code `mcp_runtime_unavailable`、唯一 receipt `mcp-no-server:v1:<task_id>:receipt`，并插入两个确定性 event/outbox：`mcp-no-server:v1:<task_id>:01-runtime-unavailable` 后接 `...:02-task-failed`，payload 分别精确为 `mcp.runtime_unavailable {status:"unavailable",reason_code:"no_user_scoped_server"}` 与 `task.failed {code:"mcp_runtime_unavailable"}`。两者使用同一 occurred time，以 ordinal event ID 保序；事务任一步失败整体回滚。initial 路径没有 Node，只写 Task/intent/receipt/events。该命令是这些 Task/Node/event 的唯一 terminal writer，调用方不得再走 generic orchestration failure 写第二份终态。

commit 后响应丢失重试只能返回 `ALREADY_CONVERGED`，不得重复 durable event row。SSE 是可重放投影：客户端重连、live publish/replay 竞争都可能再次传输同一 deterministic event ID；API client/frontend reducer 必须按 event ID 幂等，重复应用状态不变。验收必须分别证明“durable row exactly once”和“SSE delivery at-least-once、client idempotent”，不得宣称网络 exactly once。

startup 在 backend Ready、remote recovery、dispatch-resume consumer 和 Server-deletion reconciler 对外工作前扫描所有 nonterminal Task 的 intent，并另行扫描全部 unconsumed sealed terminal-result candidate，而不是只扫描 unavailable Task：`armed` 重做 exact target resolve；`available` 必须有内容完全一致的 pending/claimed outbox；`dispatched` 先按上述 closed predicate消费可信 candidate/receipt，其后只有 may-have-dispatched 且二者都不存在时转 unknown/no-replay，无 may-have-dispatched 证据且 claim lease 过期时才可 CAS 回 available 并重排原 outbox；`unavailable` 调统一收敛；`converged` 重验 Task/Node/receipt/两个 event；`unknown` 若有唯一 sealed candidate则调用 authority writer late 分支，否则只进 existing unknown recovery。双 worker 依赖 revision/unique constraint 单赢家。orphan intent、missing/duplicate/mismatched outbox、多个 candidate/receipt、unknown state 或 digest drift 都阻断 Ready；只有 sealed candidate 已被唯一 receipt 消费或仍由 supervised reconciler持有，且 unresolved intent 已成为 `available+valid outbox`、`converged`、正常终态或 `unknown/no-replay` 后才可 Ready。

必须覆盖：ordinary 无 Server 不建 intent；initial 的 Task+intent/owner empty-set guard；无 guard 时并发创建第一个 Server；cross-owner/missing/disabled target；TaskNode+intent/resume envelope；`armed→available+outbox` 每个崩溃点；claim expiry/reclaim；missing/duplicate/mismatched outbox 阻断 Ready；Server create/delete/disable/health/config/security 与 resolve/outbox/claim/Tool admission 的所有交错；普通/remote/MRTR result writer 均走统一锁序；terminal result receipt commit 成功后、正常 Task projection 前崩溃必须原事务回滚或恢复已持久结果；delete-before-result、result-before-delete；unknown projection 的 unique/revision/nullable constraints；可信迟到 result 的唯一 `unknown→late_result_resolved` CAS、相同 receipt 幂等、竞争 receipt corruption；Task/Node/Call 保持 failed/unknown、outbox 不重排、零 continuation；insert-or-compare event 在两 event 中间回滚/响应丢失/同 ID不同 payload时 fail closed；严格 created-at 顺序；终态 Task detail projection 查询、完整 replay、frontend terminal-phase reducer与重复/乱序/缺 predecessor；仅 may-have-dispatched 无可信结果转 unknown；双 worker、SQLite reopen；PostgreSQL fixed lock order/unique constraint；durable event各一 row、SSE/历史重投和 reducer 幂等。CP7-A 使用 SQLite authority；PostgreSQL 只验证共享 SQLAlchemy transaction/constraint 的 additive 兼容，Rust Sidecar 不执行这些命令。

### 历史 Task 收敛

`Task.mcp_execution_mode=legacy` 是历史路由分配，不等同于该 Task 实际执行过 legacy MCP。因此该字段不得单独作为失败条件。现有 user-scoped `MCPCallRecord` 也不得被误用为 global legacy discriminator。

#### 认证 inventory 与 cutover freeze

在 `B_L` freeze 开始前，baseline writer 必须在 artifact lock 下先 no-clobber 发布 `runtime/cp7-a/baseline/retirement-key-anchor.json`。该 closed receipt 精确绑定 `B_L` commit/tree、exact backend image ID/revision、legacy-on config SHA、一个新生成的非敏感 `hmac_key_id`、`retirement_key_present=true`、domain-separated key commitment、created UTC 和 predecessor 为 none；不记 key 内容、普通 key hash、inode 或宿主路径。commitment 固定为 `HMAC-SHA256(retirement_key, ASCII("maf.user_mcp.cp7.retirement-key-commitment.v1\0") || UTF8(hmac_key_id) || 0x00 || lowercase_hex(B_L) || 0x00 || lowercase_hex(B_L_tree))`，只以 `hmac-sha256:<64-lower-hex>` 记录。每次使用 key 签名或验 inventory 前都必须重算 commitment 并 constant-time 比较，因而同 key ID 下替换 key 也会 fail closed。anchor 一旦发布不得替换 key ID/commitment 或重用到另一个 baseline/candidate。缺 anchor、anchor 晚于 freeze 开始、key ID/commitment 漂移或密钥不可用都 fail closed。

`B_L` 仍在运行时先停止接收新的 legacy MCP 调度，等待普通写入静止，然后只由这个 exact `B_L` backend 进程内 global Runtime 导出 closed-schema `maf.user_mcp.legacy_retirement_inventory.v1`：

```text
{
  "schema": "maf.user_mcp.legacy_retirement_inventory.v1",
  "payload": {
    "inventory_id": "string",
    "generated_at": "RFC3339-UTC string",
    "source_commit": "40-lower-hex string",
    "source_tree": "40-lower-hex string",
    "source_backend_image_id": "sha256:<64-lower-hex> string",
    "source_config_sha256": "sha256:<64-lower-hex> string",
    "active_revision": "mcprev-[0-9]{6,}-[0-9a-f]{12} string",
    "entries": [{"bundle_revision":"mcprev-[0-9]{6,}-[0-9a-f]{12} string","capability_ids":["sorted unique non-empty string"]}],
    "union_capability_ids": ["sorted unique non-empty string"],
    "unreleased_pin_revisions": ["sorted unique mcprev-[0-9]{6,}-[0-9a-f]{12} string"]
  },
  "payload_sha256": "sha256:<64-lower-hex>",
  "authentication": {
    "algorithm": "hmac-sha256",
    "hmac_key_id": "non-empty string",
    "hmac_sha256": "hmac-sha256:<64-lower-hex>"
  }
}
```

所有列出的 key 均 required/non-null，数组允许空但不得为 null，object/entry 不得有额外 key。revision 是字符串而不是整数，必须全文匹配正则 `^mcprev-[0-9]{6,}-[0-9a-f]{12}$`；`entries` 按 `bundle_revision` 的 UTF-8 字节序升序且 revision 不得重复，`unreleased_pin_revisions` 同样按字符串字节序排序去重，禁止按数字前缀或生成时间另行排序。每个 `capability_ids` 与 `union_capability_ids` 也按 UTF-8 字节序排序去重。`payload_sha256=SHA256(canonical_json(payload))`，不把自身、`schema` 或 `authentication` 纳入摘要；HMAC 输入固定为 `ASCII("maf.user_mcp.legacy_retirement_inventory.v1\0") || canonical_json({schema,payload,payload_sha256})`，明确排除整个 `authentication`，因此不存在 digest/HMAC 自引用。

inventory 目标固定为 `runtime/cp7-a/baseline/legacy-retirement-inventory.json`，writer 必须以 `O_EXCL|O_NOFOLLOW`、mode `0600`、file fsync 与 parent fsync 一次性发布；已存在即拒绝，不得先导出再由另一 commit 重写。`source_commit/source_backend_image_id/source_config_sha256/hmac_key_id` 必须与已锁定的 exact `B_L` 和 key anchor 一致。HMAC 使用该 anchor 对应的独立 retirement key；签名、schema、commit/image、key ID、payload digest 或 canonical ordering 任一不符都以启动错误 `legacy_retirement_inventory_untrusted` 拒绝，且零写入。inventory 文件、retirement key presence、anchor SHA 和源配置摘要绑定进 candidate manifest；key 内容/hash 不记录。

freeze 成功后还必须 no-clobber 发布 `runtime/cp7-a/baseline/baseline-freeze-receipt.json`，绑定 anchor file/payload SHA、inventory file/payload SHA、`B_L` commit/tree/image/config、freeze 前后 Task/Call/outbox 计数、所有 retirement evidence receipt SHA 和 completed UTC。只有该 receipt 通过 secure read 且 inventory 仍为原 inode/bytes，才可把 inventory 复制进 frozen `B_L` 与 `C_A` 各自的新 staged volume；复制后仍必须重验 HMAC 和 source binding。

freeze 过程必须确认所有非终态 Task 的 legacy bundle pin 都能在 inventory 中解析，并把每个实际匹配持久化为 closed evidence receipt；进程内 global inflight request 若无法确定终态，receipt 标记 `may_have_dispatched=true`，后续只允许 fail closed，绝不重放。未知 revision、无法解析的 plan/capability 或 inventory 缺项阻断切换；普通 `legacy` route 且无直接证据的 Task 不生成 receipt。

#### Durable discriminator

只有以下 durable 证据可命中：

- 非终态 TaskNode 的 immutable capability ID 属于认证 inventory；
- 已认证的 Task retirement evidence/bundle-pin/plan-event receipt 精确绑定 inventory revision/capability；
- receipt 标记 global legacy inflight 或 `may_have_dispatched`。

若 durable plan 明确引用 global legacy revision/capability，但 inventory 无法解析，结果为 `legacy_reference_unresolvable`，仍按 `legacy_runtime_retired` 收敛。仅有 `mcp_execution_mode=legacy` 或无直接证据时结果为 `NOT_APPLICABLE`、零写入。

#### 权威原子收敛

所有 startup recovery、普通 continue、interrupt resume 和 cancel/recovery 入口只调用同一个 StoragePort 命令：

```text
converge_legacy_runtime_retirement(
  task_id,
  inventory_id,
  inventory_sha256,
  idempotency_key,
  occurred_at,
) -> NOT_APPLICABLE | CONVERGED | ALREADY_CONVERGED | ALREADY_TERMINAL
```

调用者不能传“是否命中”。Task authority 在同一锁/事务内重算 discriminator，并原子写 Task、相关 Nodes、retirement receipt 和确定性 event/outbox：

- 将相关非终态节点收敛为 `FAILED`；
- 将 `ACCEPTED/PLANNING/RUNNING/CANCELLING` Task 收敛为 `FAILED`；已经 `COMPLETED/FAILED/CANCELLED` 的历史对象保持只读；
- 持久化 `terminal_reason_code=legacy_runtime_retired`、`terminal_evidence_sha256`；
- 恰好一次 durable 插入 `task.failed` event/outbox，payload 只含 `code=legacy_runtime_retired` 和安全 evidence digest；projection 按 deterministic event ID 幂等，SSE 重连允许重投同一事件；
- 不改派、不调用 user-scoped、不重新发送工具调用。

幂等键固定为 `legacy-retire:v1:<task_id>:<inventory_sha256>`；同键不同 payload 返回 `runtime_store_idempotency_conflict`。CP7-A 只在 SQLite authority 的单事务写 Task/Node/event/receipt，API projection worker 幂等投影 frontend event，且所有 pending projection 完成前 backend 不 Ready。PostgreSQL 仅验证共享 SQLAlchemy transaction/schema 的 additive 兼容；Rust Sidecar 不接收此命令、不新增 retirement RPC，也不得串联现有多个 CAS 假装原子。

必须覆盖：Node 更新后崩溃全回滚、commit 后响应丢失重试不重复 event、两 worker 单赢家、execution/cancel 竞态、SQLite reopen、PostgreSQL additive contract、inventory 缺失/篡改零写入、普通 legacy-assigned 非 MCP Task `NOT_APPLICABLE`，并证明 Sidecar RPC surface 未新增 retirement operation。终态历史 Task/Node/event/metric/audit 始终只读可见。

CP7-A 人工测试使用全新独立卷，因此不会扫描或改写既有开发数据；上述历史兼容由固定自动化回归证明。

### 候选构建与可追溯工件

在 `B_L` 完成并导出后提交 CP7-A 修改，得到候选 commit `C_A`。构建器执行 `git archive C_A` 到临时目录，并只以该目录为 Docker build context；不能直接构建当前工作树。三个镜像均使用 `VCS_REF=C_A`，本地 tag 包含 `C_A` 的短 SHA。

`Dockerfile` 还必须从同一 clean archive 提供非部署的 one-shot `cp7-validation-runner` target，以 `VCS_REF=C_A` 绑定 OCI revision，并包含固定 backend/storage 回归所需的 `src/`、`tests/`、`scripts/`、migration/schema 与 dependency lock files。它不得从宿主 bind mount 工作树、不得作为第四个长期服务、不得进入候选运行 Compose；只在 disposable validation profile 中以 exact immutable image ID 执行后立即删除。运行前 verifier 必须确认容器内测试/脚本/lockfile digest 与 `git archive C_A` 一致，防止 production backend image 未包含测试却被误报为已验证。

`C_A` build/static/trust preflight 通过后、第一次 Compose 启动前，构建器必须对 backend、frontend、runtime-sidecar 三个 immutable image 分别执行无损 `docker image save`，写入 `runtime/cp7-a/candidates/<approval_request_id>/exports/`。每个导出文件必须是 regular file、当前 UID 所有、mode 不高于 `0600`、`nlink=1`，并记录固定 basename、image ID、config SHA、OCI revision、文件大小和 SHA-256；不得只记录可变 tag。

restore smoke 必须使用 disposable 的空 Docker daemon，不能删除宿主 daemon 中的 image 后再加载，也不能依赖宿主 layer cache。verifier 创建唯一 project `cp7-restore-<approval_request_id>`，在专用 `internal:true` network 和 no-clobber 新建的临时 data-root volume 上，以预载且 digest-pinned 的 `CP7_RESTORE_DIND_IMAGE` 启动一次性 Docker daemon。首选 rootless/unprivileged 模式，必须显式拒绝宿主 Docker socket/device/data-root、宿主端口和非候选 export；只有本地 Engine/kernel 不支持已固定的 rootless check，且 verifier 在 result 中记录 closed fallback reason 时，才允许 daemon 容器使用 `privileged=true`。privileged DinD 只用于证明空 cache/data-root 与导出可恢复性，不是对恶意镜像或宿主的安全隔离边界，不得在报告中宣称为 sandbox。两种模式都禁止发布 TCP 端口，只能只读看到本 candidate 的三个 export；daemon 固定使用专用 data root、`--bridge=none --iptables=false`，inner container 固定 `--network none`。已存在同名 project/volume、image reference 不是 digest、宿主缺该 image 或流程尝试 pull 都拒绝。

daemon readiness 通过后、执行 load 前，必须经其内部 Unix socket证明 inner image/container 都为 0，并记录 daemon ID、DockerRootDir、fresh volume CreatedAt 与 canonical empty-state SHA。随后依次 `docker load` 三个 export，重验 image ID、config SHA、OCI revision 和 allowlisted tag，再以 `docker create → start --attach → wait` 实际运行以下固定 argv；inner container 均使用 `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`，仅允许命令必需的 tmpfs：

```text
backend:         /opt/conda/envs/multi_agent/bin/python -c "from src.api.app import create_app; assert callable(create_app)"
frontend:        /usr/sbin/nginx -t
runtime-sidecar: /usr/local/bin/maf-runtime-sidecar --version
```

三项都必须退出 0；`--version` 因而成为 Sidecar image contract。每个 smoke 必须来自刚加载的 immutable image ID，禁止用宿主 image 或 bind-mounted binary 替代。

verifier 必须记录 pinned daemon reference、宿主解析的 daemon image ID/revision、`rootless_unprivileged|privileged_fallback` 模式与 fallback reason、data-root 初始为空的证明、loaded image identities、三个 smoke 的 fixed check ID/exit code/stdout-stderr digest 和清理结果；不记录内部 daemon token/临时路径。完成或失败后都必须删除 inner container，并对 exact project 删除 disposable daemon、network 和 data-root volume，再证明这些对象不存在。既无可用 rootless 也不满足受限 privileged fallback、启动或清理不完整时候选保持 blocked，不得回退到宿主 daemon。三个导出工件、摘要或 isolated restore smoke 任一缺失都禁止发布 manifest；通过 isolated restore 后，宿主 Compose 仍必须以三个原 immutable image ID 完成完整健康与人工候选运行。

isolated restore 不得只用一个间接“result SHA”代表上述身份与清理过程。下列四个文件全部使用 exact envelope `{schema,payload,payload_sha256}` 和前述 canonical JSON/file SHA 规则，payload 不允许 unknown/null key（仅 daemon profile 中按 restore mode 规定的 `fallback_reason` 可 null）：

```text
daemon / maf.user_mcp.cp7_isolated_restore_daemon.v1
payload={restore_id,approval_request_id,release,phase,commit,tree,daemon_reference,daemon_image_id,daemon_config_sha256,daemon_revision,mode,fallback_reason,daemon_id,docker_root_dir_sha256,data_root_volume_identity_sha256,fresh_volume_created_at,empty_state_sha256,ready_at}

exports / maf.user_mcp.cp7_isolated_restore_exports.v1
payload={restore_id,items}
items[]={service,basename,file_sha256,size_bytes,mode,image_id,image_config_sha256,image_revision}

load-start-health / maf.user_mcp.cp7_isolated_restore_execution.v1
payload={restore_id,loaded_images,checks,started_at,completed_at}
loaded_images[]={service,image_id,image_config_sha256,image_revision,loaded_tag}
checks[]={check_id,service,argv_sha256,container_id,image_id,created,start_exit_code,wait_exit_code,health_status,stdout_sha256,stderr_sha256}

cleanup / maf.user_mcp.cp7_isolated_restore_cleanup.v1
payload={restore_id,inner_container_ids_sha256,inner_containers_absent,daemon_container_absent,project_absent,network_absent,data_root_volume_absent,cleanup_result,completed_at}
```

`release/phase` 只允许前述 runtime tuple 中可运行的同一行；daemon reference 必须是 input 中的 digest-pinned reference，image ID/config SHA/revision 都由宿主已预载镜像 secure inspect 直接得出。exports `items` 按 `backend,frontend,runtime-sidecar` 固定顺序各一项；loaded images 与其逐项相等。checks 按同一服务顺序各一项，argv 必须精确为上述固定 smoke，`created=true`、两个 exit code 均为 0、`health_status=passed`。cleanup 的五个 `*_absent` 布尔值必须全为 true，`cleanup_result=passed`。任一 identity、数量、顺序、运行或清理结果不符都 fail closed。

closed aggregate `isolated_restore_subject_sha256` 精确为 `SHA256(canonical_json({schema:"maf.user_mcp.cp7_isolated_restore_subject.v1",daemon_file_sha256,daemon_payload_sha256,exports_file_sha256,exports_payload_sha256,execution_file_sha256,execution_payload_sha256,cleanup_file_sha256,cleanup_payload_sha256}))`。该 aggregate 不替代八个直接 SHA；manifest 必须同时记录它们，后续文中的 `restore_result_sha256` 如未展开，均精确指该 `isolated_restore_subject_sha256`。

两个回退目标含义不同：

- `C_A`：回退 CP7-B 的代码删除，但继续保持 assembly off。
- `B_L`：只有必须恢复 legacy 全局 Runtime 时才使用；不得与 CP7-A 人工候选混为同一工件。

本轮不推送镜像仓库，因此 RepoDigest 不是必填证据；本地 immutable `.Id` 与导出文件 SHA-256 才是工件身份。

### Candidate manifest

每次候选生成随机 `approval_request_id=cp7a-<32-lower-hex>`。验证器只在所有检查成功后，把 immutable manifest 写入 Git-ignored 的：

```text
runtime/cp7-a/candidates/<approval_request_id>/manifest.json
```

manifest 使用 closed envelope `{schema,payload,payload_sha256}`，其中 `schema` 精确为 `maf.user_mcp.cp7a_candidate_manifest.v1`。envelope 三个 key 均 required/non-null，`payload` 必须是 object，`payload_sha256=SHA256(canonical_json(payload))`，摘要输入明确排除 envelope 的 `schema` 和 `payload_sha256` 自身。canonical JSON 固定为 ASCII、key 排序、无空白、拒绝 duplicate/unknown key、NaN 和非 canonical newline；所有 digest 使用 `sha256:<64-lower-hex>`。

manifest payload 的 exact 顶层 key 固定为 `identity`、`baseline`、`images`、`configuration`、`staging`、`sidecar_trust`、`postgres_validation`、`rust_gate`、`inventory`、`runtime`、`audit`、`safety`、`checks`、`manual_checklist`、`timestamps`。这些值全部 required/non-null object；除 `configuration.mcp.MCP_ENFORCE_COHORTS` 与 `configuration.mcp.MCP_ENFORCE_COHORT_CONFIG_FILE` 必须精确为空字符串外，其他字符串不得为空；digest/image ID/commit/tree/time 使用前述固定格式；布尔和整数必须使用 JSON 原生类型；数组允许空但不得为 null，元素类型固定且不得重复。唯一允许 null 的字段是 rootless 模式下不适用的 `configuration.restore_fallback_reason`，其余缺失与 null 均拒绝。exact hierarchy 为：

```text
identity={status,branch,environment,production_evidence,approval_request_id,candidate_generation,candidate_parent_commit,candidate_parent_release,candidate_commit,candidate_tree,candidate_archive_sha256}
baseline={commit,tree,images,export_sha256s,legacy_compose_sha256,key_anchor_sha256,freeze_receipt_sha256,inventory_file_sha256,rehearsal_result_sha256,rehearsal_cleanup_sha256}
images={backend,frontend,runtime_sidecar,validation_runner,exports,isolated_restore}
images.isolated_restore={subject_sha256,daemon_file_sha256,daemon_payload_sha256,exports_file_sha256,exports_payload_sha256,execution_file_sha256,execution_payload_sha256,cleanup_file_sha256,cleanup_payload_sha256}
configuration={mcp,authority_modes,compose_env,runner_image_digests,restore_mode,restore_fallback_reason,compose_canonical_sha256}
staging={receipt_sha256,volume_identity_sha256,basenames,container_paths,config_sha256,api_env,state_backend,max_active_calls,temporary_disk_low_watermark_bytes,sidecar_endpoint,credential_key_present,retirement_key_present}
sidecar_trust={binary_sha256,cargo_lock_sha256,sbom_sha256,provenance_sha256,proto_sha256,schema_sha256,error_table_sha256,manifest_sha256,allowlist_sha256}
postgres_validation={image_reference,image_id,image_revision,runner_image_id,profile_sha256,fresh_schema_result_sha256,integration_result_sha256,secrets_content_equal,secrets_cleanup,no_leak,cleanup_result_sha256}
rust_gate={runner_reference,runner_image_id,runner_revision,runner_provenance_sha256,runner_file_sha256,runner_argv,runner_argv_sha256,plan_sha256,deny_sha256,cargo_lock_sha256,vendor_sha256,cache_inventory_sha256,advisory_snapshot_sha256,conda_lock_sha256,wheelhouse_sha256,toolchains,targets,cargo_extensions,result_sha256,offline,no_fetch,clean_archive,cleanup,started_at,ended_at}
inventory={schema,inventory_id,payload_sha256,source_commit,source_tree,source_backend_image_id,source_config_sha256,hmac_key_id,key_commitment}
runtime={project_name,containers}
audit={container_id,device,inode,start_offset,end_offset,span_sha256,record_count,complete_final_newline}
safety={candidate_id,config_fingerprint,epoch_chain_sha256,maintenance_union_sha256,observation_started_at,observation_ended_at,record_count,counts_by_red_line,gap_count,invalid_latched,continuity_passed,definition_sha256,snapshot_sha256}
checks={items}
manual_checklist={schema,definition_sha256,check_ids}
timestamps={build_started_at,runtime_started_at,verification_completed_at}
```

`images.backend|frontend|runtime_sidecar` 的 exact keys 为 `{name,image_id,config_sha256,revision}`，`images.validation_runner` 为 `{name,image_id,config_sha256,revision,content_verdict}`；`images.exports[]` 为 `{basename,image_id,size_bytes,mode,sha256}`，按 basename 固定顺序 `backend.oci.tar,frontend.oci.tar,runtime-sidecar.oci.tar` 各出现一次；`baseline.images[]` 为 `{name,image_id,config_sha256,revision}`，按 `backend,frontend,runtime-sidecar` 固定顺序；`baseline.export_sha256s[]` 为 `{service,sha256}`，使用同一固定顺序；`runtime.containers[]` 为 `{service,container_id,image_id,started_at}`，同样按该三服务顺序且各出现一次；`checks.items[]` 为 `{check_id,exit_status,started_at,ended_at,stdout_sha256,stderr_sha256}`，按 `check_id` UTF-8 字节序排序且唯一。`configuration.mcp` 精确为前述七个 MCP key，其中只有 `MCP_ENFORCE_COHORTS` 与 `MCP_ENFORCE_COHORT_CONFIG_FILE` 允许且要求空字符串；`configuration.authority_modes` 精确为三个 Rust mode；`configuration.compose_env[]` 只允许 `{key,value}`，按 key UTF-8 字节序排序，key 唯一并精确等于 verifier 版本绑定的非敏感 allowlist export。`configuration.runner_image_digests` 的 exact keys 为 `{restore_daemon,postgres_validation,rust_gate_runner}`，每个 value 都是 `sha256:<64-lower-hex>`；`configuration.restore_mode` 只允许 `rootless_unprivileged|privileged_fallback`，前者要求 `restore_fallback_reason=null`，后者要求 `restore_fallback_reason=rootless_runtime_unavailable`。`staging.basenames`、`staging.container_paths`、`manual_checklist.check_ids`、`rust_gate.toolchains|targets|cargo_extensions` 都是 sorted unique string array；`rust_gate.runner_argv` 精确为 `['conda','run','-n','multi_agent','python','scripts/run_rust_quality_gates.py','--run','--offline','--no-fetch']`，按原顺序参与 `runner_argv_sha256`，不得排序；`safety.counts_by_red_line` 是只含八个固定 red-line key、value 为非负整数的 object。`sidecar_trust` 和所有 digest object 不允许增删 key。下列说明只解释这些 exact 字段的语义，不扩展 schema：

- `status=passed`、`branch=main`、`environment=development`、`production_evidence=false`、approval request ID；
- candidate commit/tree/archive SHA、baseline commit、`B_L` 三个 image/export SHA、legacy-on Compose SHA、retirement-key anchor SHA、baseline-freeze/inventory receipt SHA 和独立 rehearsal result/cleanup SHA；
- backend/frontend/runtime-sidecar 的 image name、immutable image ID、config SHA、OCI revision，clean-archive `cp7-validation-runner` image ID/config SHA/revision/content verdict，以及三个 `C_A` export 的固定 basename、文件大小、mode、SHA-256 和 isolated restore-smoke result SHA；
- 七项 MCP 配置、三个 `MAF_RUST_*_MODE=off`、完整 allowlisted 非敏感 `compose.env`、pinned daemon/PostgreSQL/Rust-gate runner image digest、restore daemon mode/fallback reason、脱敏 Compose 展开结果的 canonical SHA；
- authoritative `C_A` secure staging receipt SHA、与 `B_L`/rehearsal staged volume 不同的 volume identity digest、staged trust/config basenames 与 fixed container paths、`config.yaml` 文件 SHA、`MAF_API_ENV=dev`、SQLite backend、容量门禁的非敏感数值、Sidecar endpoint；credential/retirement key 只记录 present，不记录内容或 hash；
- 实际 Sidecar binary、Cargo.lock、SBOM、provenance、proto/schema/error-table、artifact manifest/allowlist SHA；
- pinned PostgreSQL validation reference/image ID/revision、clean-archive validation-runner identity、validation profile canonical SHA、fresh-schema/integration result SHA、tmpfs-secret cleanup/no-leak verdict 与 cleanup result SHA；不得记录数据库名、用户名、密码或 DSN；
- pinned Ubuntu 22.04 `linux/amd64` Rust runner reference/image ID/revision/provenance SHA、canonical quality runner file SHA、`--plan-json` canonical output SHA、`native/Cargo.lock`/`native/deny.toml` SHA、full-run result SHA、toolchain/target、clean-archive/cleanup verdict 与起止 UTC；不得记录 raw command output；
- retirement inventory 的 schema/inventory ID/payload SHA/source binding；inventory 只含 canonical capability ID/revision，不含 owner、Server、Task 或 Call 标识；
- Compose project name、container ID、Docker `StartedAt`；
- audit 的 container ID、device/inode、`start_offset=0`、end offset、span SHA、record count、完整 newline 标志；
- CP7-local safety ledger/Ready epoch/candidate guard 的 candidate/config binding、epoch-chain/maintenance union、snapshot window、row count、八项 totals、`invalid_latched=false`、gap/continuity verdict、detector-definition SHA 和 canonical snapshot SHA；
- 固定 check ID、退出状态、起止时间及 stdout/stderr SHA；不记录可能含路径/环境/secret 的 raw command/output；
- manual checklist schema、definition SHA 和固定八项 check ID；
- 构建开始、启动、验证完成时间，全部为 UTC RFC3339 `YYYY-MM-DDTHH:MM:SSZ`；
- `timestamps` 只含构建开始、启动、验证完成三个 UTC 时间；payload digest 仅位于 envelope，不得在 payload 内自引用。

`runtime/cp7-a/` 与 candidate 目录必须由当前 UID 所有、无 symlink、目录权限不高于 `0700`；artifact 文件权限不高于 `0600`。manifest、approval、current pointer 和 CP7-B claim 共用 `runtime/cp7-a/artifact.lock`：以 `O_NOFOLLOW|O_CREAT` 打开并取得非阻塞 `flock(LOCK_EX)`，并发 writer 直接失败。

immutable 文件用同目录随机 temp、`O_EXCL|O_NOFOLLOW` 创建，写完 `fsync(fd)`；再用 no-clobber link/等价原子发布，`fsync(parent-dir)`，删除 temp 后再次 `fsync(parent-dir)`。final 已存在即失败，永不覆盖。任何检查失败只生成非权威诊断，不得留下该 request ID 的 manifest。secure read 必须检查 regular file、UID、mode、nlink、size limit、pre/post `fstat` 和 path inode 一致。

#### Pending candidate 与单次消费生命周期

唯一权威候选/部署指针固定为 `runtime/cp7-a/current.json`，使用 closed envelope `maf.user_mcp.cp7a_current.v1`。payload 的 exact keys 为 `approval_request_id`、`candidate_binding_sha256`、`manifest_file_sha256`、`state`、`state_receipt_sha256`、`updated_at`；`state` 只允许 `pending_manual_approval | claimed | completed | aborted | rolled_back_to_ca | rolled_back_to_bl`。每次状态转换还必须先在 `runtime/cp7-a/candidates/<approval_request_id>/lifecycle/` no-clobber 发布一个 immutable closed receipt，绑定前一 state/receipt SHA、新 state、candidate binding、manifest SHA、转换时间和固定 reason code；随后才以同目录 temp、file fsync、`os.replace`、final/path identity check 和 parent fsync 原子更新 `current.json`。current pointer 本身不是批准证据，immutable lifecycle receipt 才是历史来源。`rolled_back_to_ca|rolled_back_to_bl` 只能从下述 post-completion rollback receipt 投影，不得用于跳过 claim/progress/result。

##### 权威 lifecycle artifact schema

本小节是 lifecycle 工件的唯一 normative schema；下文“绑定”叙述只能解释字段，不能增加可选 key。除 mutable projection `current.json` 外，所有文件都 immutable/no-clobber。所有类型共用 exact envelope `{schema,payload,payload_sha256}`：三个 key required/non-null、无 unknown key，`payload` 是 closed object，`payload_sha256="sha256:" + lowercase_hex(SHA256(canonical_json(payload)))`，摘要不包含 envelope 的 `schema` 或 `payload_sha256`，因而不自引用。canonical JSON 固定 UTF-8、Unicode 不转义、object key 按 UTF-8 字节序、无额外空白、单个 LF 结尾；拒绝 duplicate key、NaN/Infinity、surrogate、非最短 UTF-8。文件 SHA 是整个 canonical envelope bytes 的 SHA-256，与 payload SHA 分开记录。所有列出的 key 都 required；除表中明确标 `nullable` 者外一律 non-null。commit/tree 为 40 lowercase hex，digest 为 `sha256:<64-lower-hex>`，ID/enum/time/string 必须非空，时间精确为 UTC RFC3339 秒。数组不得为 null，按各自固定业务顺序或明确字节序排序，元素不得重复。

| artifact / exact schema | exact payload keys | nullable 与闭集 |
|---|---|---|
| current / `maf.user_mcp.cp7a_current.v1` | `{approval_request_id,candidate_binding_sha256,manifest_file_sha256,state,state_receipt_sha256,updated_at}` | 无 nullable；state 使用上述六值闭集。 |
| lifecycle receipt / `maf.user_mcp.cp7_lifecycle_receipt.v1` | `{transition_id,approval_request_id,candidate_binding_sha256,manifest_file_sha256,previous_state,previous_receipt_sha256,next_state,reason_code,evidence_file_sha256,lifecycle_root_sha256,transitioned_at}` | nullable 与 state/reason 只能使用下表 exact transition contract；不得由 writer 自选。 |
| lifecycle root / `maf.user_mcp.cp7_lifecycle_root.v1` | `{root_id,root_kind,predecessor_approval_request_id,predecessor_state,predecessor_state_receipt_sha256,predecessor_evidence_file_sha256,recovery_release,recovery_commit,recovery_tree,recovery_profile_sha256,recovery_images_sha256,recovery_trust_sha256,next_approval_request_id,next_candidate_binding_sha256,reason_code,created_at}` | 无 nullable；`root_kind=post_rollback_requalification`，predecessor 只允许 rolled-back state，reason 同名，recovery release 只允许 `R_A|R_L`，profile SHA 必须精确等于 predecessor rollback artifact 中的同名字段。 |
| candidate supersede root / `maf.user_mcp.cp7_candidate_supersede_root.v1` | `{root_id,root_kind,predecessor_approval_request_id,predecessor_state,predecessor_state_receipt_sha256,predecessor_candidate_binding_sha256,predecessor_manifest_file_sha256,predecessor_terminal_evidence_file_sha256,recovery_release,recovery_commit,recovery_tree,recovery_profile_sha256,next_approval_request_id,next_candidate_binding_sha256,next_manifest_file_sha256,reason_code,created_at}` | `predecessor_terminal_evidence_file_sha256` 仅 predecessor=`pending_manual_approval` 时必须为 null；predecessor=`aborted` 时必须绑定已经存在的 abort receipt 或 candidate-supersede root evidence。`recovery_release=C_A` 时 `recovery_commit/recovery_tree` 必须精确为 predecessor current `C_A[n]` 及其 tree，且 `recovery_profile_sha256=null`；`recovery_release=A_B` 时 commit/tree/profile 必须全部 non-null 并精确等于 abort artifact 中的同名字段。其余无 nullable；`root_kind=candidate_supersede`、`reason_code=candidate_supersede`；`recovery_release` 只允许 `C_A|A_B`。root 只绑定已存在 predecessor，不得绑定未来 lifecycle receipt。 |
| claim / `maf.user_mcp.cp7b_claim.v1` | `{claim_id,approval_request_id,approval_id,approval_file_sha256,pending_state_receipt_sha256,candidate_binding_sha256,candidate_commit,candidate_tree,manifest_file_sha256,images_sha256,exports_sha256,restore_result_sha256,combined_definition_sha256,combined_result_sha256,state,claimed_at}` | 无 nullable；`state=claimed`。 |
| progress / `maf.user_mcp.cp7b_progress.v1` | `{progress_id,approval_request_id,claim_id,claim_file_sha256,ordinal,step,previous_progress_file_sha256,candidate_a_commit,candidate_b_commit,current_tree,tracked_patch_sha256,check_result_sha256,result_file_sha256,result_payload_sha256,created_at}` | `previous_progress_file_sha256` 只在 ordinal 01 可 null；`candidate_b_commit` 只在 01..03 可 null，04..08 必须 non-null；`result_file_sha256/result_payload_sha256` 在 01..07 必须同时为 null，在 `08-result_published` 必须同时 non-null 并精确绑定 final result envelope bytes 与其 payload；ordinal/step 只允许固定 01..08 对应关系。 |
| result / `maf.user_mcp.cp7b_result.v1` | `{result_id,approval_request_id,approval_file_sha256,claim_file_sha256,last_progress_file_sha256,candidate_a_commit,candidate_b_commit,candidate_b_tree,candidate_b_archive_sha256,images_sha256,exports_sha256,restore_result_sha256,staging_file_sha256,trust_sha256,validation_runner_sha256,postgres_result_sha256,rust_result_sha256,reachability_result_sha256,tombstone_result_sha256,rollback_artifacts_result_sha256,state,completed_at}` | 无 nullable；`state=completed`，predecessor 必须是 07 progress。 |
| abort / `maf.user_mcp.cp7b_abort.v1` | `{abort_id,approval_request_id,approval_file_sha256,claim_file_sha256,last_valid_progress_file_sha256,reason_code,failure_release,failure_commit,failure_tree,failure_images_sha256,recovery_required,recovery_commit,recovery_tree,inverse_patch_sha256,recovery_profile_sha256,tree_equals_ca,state,aborted_at}` | `last_valid_progress_file_sha256` 只允许在 01 尚未发布时 null；`failure_images_sha256` 可在尚未构建镜像时 null；`recovery_commit,recovery_tree,inverse_patch_sha256,recovery_profile_sha256,tree_equals_ca` 在 `recovery_required=false` 时必须全为 null，在 true 时全部 non-null 且 recovery commit=`A_B`、tree=`tree(C_A)`、tree_equals_ca=true；`state=aborted`。 |
| abort receipt / `maf.user_mcp.cp7b_abort_receipt.v1` | `{abort_receipt_id,abort_id,abort_file_sha256,approval_request_id,claim_file_sha256,last_valid_progress_file_sha256,recovery_commit,recovery_tree,verified_state_sha256,created_at}` | nullable 必须逐项与 abort 对应字段一致；不得用 receipt 补造 abort 未记录的 recovery。 |
| emergency B_L approval / `maf.user_mcp.cp7b_emergency_bl_approval.v1` | `{emergency_approval_id,decision,scope,source,predecessor,baseline,evidence,context,created_at}` | nested schema 见下文；只有 source 的 opaque ref 可 null，其他字段无 nullable；decision=`restore_legacy_global_runtime`。 |
| post-completion rollback / `maf.user_mcp.cp7b_post_completion_rollback.v1` | `{rollback_id,approval_request_id,completed_result_file_sha256,completed_state_receipt_sha256,predecessor_state,predecessor_state_receipt_sha256,predecessor_evidence_file_sha256,reason_code,target_state,recovery_release,recovery_commit,recovery_tree,source_parent_commit,inverse_patch_sha256,recovery_profile_sha256,images_sha256,configuration_sha256,staging_file_sha256,trust_sha256,regression_result_sha256,data_compatibility_result_sha256,emergency_approval_file_sha256,emergency_approval_payload_sha256,emergency_approval_context_sha256,rolled_back_at}` | predecessor/target/reason/emergency nullable 只允许下表组合；target/release 只允许 `rolled_back_to_ca/R_A` 或 `rolled_back_to_bl/R_L`；`recovery_profile_sha256` 必须 non-null 并与下述 exact profile 匹配。completed result/receipt 永远保留为原始成功锚。 |

所有聚合 `*_sha256` 都是对应 closed nested object/array 的 canonical SHA，而不是任意 writer 自选字符串：`images_sha256` 的输入为按 `backend,frontend,runtime-sidecar` 固定顺序的 `{service,image_id,revision}` 三项；`exports_sha256` 同顺序 `{service,sha256}` 三项；`recovery_images_sha256` 同样闭合。`check_result_sha256` 的输入精确为 schema `maf.user_mcp.cp7b_step_result.v1` 的 payload `{ordinal,step,status,subject_sha256,checks}`，其中 status 只能为 `passed`，checks 按 `check_id` UTF-8 字节序排序且每项 exact keys 为 `{check_id,status,exit_code,started_at,ended_at,stdout_sha256,stderr_sha256}`，status=`passed`、exit_code=0；没有外部命令的步骤也必须使用固定 synthetic check ID 记录 commit/tree/receipt identity 验证，不能 hash 空 object。`08-result_published` 的 `subject_sha256` 不得在 result file SHA 与 payload SHA 中任选其一；它精确为 `SHA256(canonical_json({schema:"maf.user_mcp.cp7b_result_subject.v1",result_file_sha256,result_payload_sha256}))`，而 progress payload 中仍必须直接保存同一对 `result_file_sha256/result_payload_sha256`。三者任一不匹配都拒绝发布或崩溃采纳。claim/result/abort/rollback 不得内嵌未在表中列出的 context map。

确定性 ID 不含 wall-clock 字段，并固定为下式 domain-separated SHA 的完整 64 lowercase hex；`H(X)=lowercase_hex(SHA256(ASCII(<domain> || "\0") || canonical_json(X)))`：

- `transition_id="cp7-transition-v1-" + H({approval_request_id,previous_state,previous_receipt_sha256,next_state,reason_code,evidence_file_sha256,lifecycle_root_sha256})`，domain 为 `maf.user_mcp.cp7.transition-id.v1`；
- lifecycle/candidate-supersede `root_id="cp7-root-v1-" + H(payload 去掉 root_id、created_at)`，domain 分别为 `maf.user_mcp.cp7.lifecycle-root-id.v1` 和 `maf.user_mcp.cp7.candidate-supersede-root-id.v1`；
- `claim_id="cp7b-claim-v1-" + H({approval_id,approval_file_sha256,pending_state_receipt_sha256,candidate_binding_sha256})`，domain=`maf.user_mcp.cp7b.claim-id.v1`；
- `progress_id="cp7b-progress-v1-" + H({claim_file_sha256,ordinal,step,previous_progress_file_sha256,check_result_sha256,result_file_sha256,result_payload_sha256})`，domain=`maf.user_mcp.cp7b.progress-id.v1`，01..07 的两个 result SHA 以 JSON null 参与；
- `result_id="cp7b-result-v1-" + H({claim_file_sha256,last_progress_file_sha256,candidate_b_commit,validation_runner_sha256,postgres_result_sha256,rust_result_sha256})`，domain=`maf.user_mcp.cp7b.result-id.v1`；
- `abort_id="cp7b-abort-v1-" + H({claim_file_sha256,last_valid_progress_file_sha256,reason_code,failure_commit,recovery_commit,recovery_profile_sha256})`，domain=`maf.user_mcp.cp7b.abort-id.v1`，null 按 JSON null 参与；`abort_receipt_id="cp7b-abort-receipt-v1-" + H({abort_file_sha256,claim_file_sha256,verified_state_sha256})`，domain=`maf.user_mcp.cp7b.abort-receipt-id.v1`；
- `emergency_approval_id="cp7b-emergency-bl-v1-" + H({source_role,phrase_utf8_sha256,predecessor_state,predecessor_state_receipt_sha256,predecessor_evidence_file_sha256,baseline_commit,baseline_inventory_file_sha256,no_unknown_inflight_sha256,legacy_on_acceptance_sha256,data_compatibility_sha256,security_acceptance_sha256})`，domain=`maf.user_mcp.cp7b.emergency-bl-approval-id.v1`；
- `rollback_id="cp7b-rollback-v1-" + H({completed_result_file_sha256,completed_state_receipt_sha256,predecessor_state,predecessor_state_receipt_sha256,predecessor_evidence_file_sha256,reason_code,target_state,recovery_commit,recovery_profile_sha256,emergency_approval_file_sha256})`，domain=`maf.user_mcp.cp7b.rollback-id.v1`。

lifecycle receipt 的 state、reason、evidence 与 root nullability 只允许下表组合；`evidence`/`root` 表示对应 SHA 字段的精确来源：

| previous_state | next_state | reason_code | evidence_file_sha256 | lifecycle_root_sha256 |
|---|---|---|---|---|
| `none` | `pending_manual_approval` | `candidate_published` | null | null |
| `pending_manual_approval` | `aborted` | `candidate_superseded` | candidate-supersede root file SHA | 同一 root file SHA |
| `aborted` | `pending_manual_approval` | `candidate_requalified` | next manifest file SHA | candidate-supersede root file SHA |
| `pending_manual_approval` | `claimed` | `retirement_claimed` | claim file SHA | null |
| `claimed` | `completed` | `retirement_completed` | result file SHA | null |
| `claimed` | `aborted` | `retirement_aborted` | abort receipt file SHA | null |
| `completed` | `rolled_back_to_ca` | `post_completion_rollback_ca` | rollback file SHA | null |
| `completed` | `rolled_back_to_bl` | `emergency_rollback_bl` | rollback file SHA | null |
| `rolled_back_to_ca` | `rolled_back_to_bl` | `emergency_rollback_bl` | rollback file SHA | null |
| `rolled_back_to_ca` | `pending_manual_approval` | `post_rollback_requalification` | next manifest file SHA | lifecycle root file SHA |
| `rolled_back_to_bl` | `pending_manual_approval` | `post_rollback_requalification` | next manifest file SHA | lifecycle root file SHA |

只有首行允许 `previous_receipt_sha256=null`；其余行都必须绑定 exact predecessor receipt。表外 state pair、reason、nullable 组合或 caller 自定义字符串一律拒绝。

##### Deterministic recovery commit profiles

`A_B` 与 post-completion `R_A/R_L` 使用两个分离的 closed profile，不得因 tree 偶然相同而共用 release、message 或 receipt。profile object 的 exact keys 为 `{schema,release,parent_commit,target_tree,patch_sha256,author_name,author_email,author_timestamp,committer_name,committer_email,committer_timestamp,timezone,message,gpg_signature,encoding,extra_headers}`；`gpg_signature=false`、`encoding=null`、`extra_headers=[]`，时间为 UTC RFC3339 秒且 Git commit header 使用对应 epoch 与 `+0000`。`recovery_profile_sha256=SHA256(canonical_json(profile))`，commit object 必须使用 profile 的 parent/tree/identity/time/message 逐字节构造：

| release / schema | parent / target tree / patch | author = committer | timestamp | exact message |
|---|---|---|---|---|
| `A_B` / `maf.user_mcp.cp7b_abort_recovery_commit.v1` | `parent=C_B`；`target_tree=tree(C_A)`；`patch_sha256` 为 `C_B` deletion patch 的 canonical inverse | `MAF CP7 Abort Recovery <maf-cp7-abort-recovery@localhost>` | `04-c_b_committed.created_at + 1 second` | `cp7-b: deterministic abort recovery to C_A\n` |
| `R_A` / `maf.user_mcp.cp7_post_completion_recovery_commit.v1` | `parent=C_B`；`target_tree=tree(C_A)`；`patch_sha256` 为已绑定 `C_B` deletion patch 的 canonical inverse | `MAF CP7 Rollback Recovery <maf-cp7-rollback-recovery@localhost>` | completed lifecycle receipt `transitioned_at + 1 second` | `cp7: deterministic post-completion recovery R_A\n` |
| `R_L` / `maf.user_mcp.cp7_post_completion_recovery_commit.v1` | `parent=C_B` 或 current `R_A`，与 rollback predecessor 表的 source parent 精确一致；`target_tree=tree(B_L)`；`patch_sha256` 为从 parent tree 到 `tree(B_L)` 的 canonical binary patch SHA | `MAF CP7 Rollback Recovery <maf-cp7-rollback-recovery@localhost>` | exact emergency approval `created_at + 1 second` | `cp7: deterministic post-completion recovery R_L\n` |

canonical patch 统一指 Git binary diff bytes：path 按 Git byte order，固定 `--binary --full-index --no-renames --no-ext-diff`，LF 结束，不得受 locale、external diff 或 attributes 漂移影响。`A_B/R_A` 的 patch apply 后必须与 `tree(C_A)` 逐 object 相等；`R_L` 必须与 frozen `tree(B_L)` 逐 object 相等。`A_B` 的 profile SHA 只写入 abort/abort receipt chain；`R_A/R_L` 的 profile SHA 只写入 post-completion rollback/lifecycle-root chain，两者不得交换。

崩溃采纳不按“最新 child”或时间选择。writer 先由 exact parent/tree/profile 重算唯一 expected commit SHA：对象不存在时只能创建该对象；对象已存在时必须 secure-read 并逐字节等于 expected object 才可采纳。同时 `main` 必须仍精确指向 parent 或已精确指向 expected commit，index/worktree clean，且所有 CP7 recovery refs 下不存在另一个以同 parent 为唯一父的 child。任一 unexpected child/ref、第二个 matching object、profile/patch/tree 漂移或 dirty state 都 fail closed；不得 reset/rebase/cherry-pick 来“修复”唯一性。

abort artifact 只能在源码已恢复到表中 `verified source state` 后发布；pre-04 失败若留下 partial/unbound patch，writer 必须先用已绑定 deletion patch/step evidence恢复并证明 clean `C_A`，无法证明则保持 claimed/fail closed，不得发布含任意 tree 的 abort。`last valid progress`、reason 与字段组合精确为：

| last valid progress | reason_code 闭集 | failure release/commit/tree | failure_images_sha256 | recovery fields | verified source state |
|---|---|---|---|---|---|
| null | `preflight_failed | operator_abort_before_c_b` | `C_A/C_A/tree(C_A)` | C_A images SHA | `recovery_required=false`，五个 recovery 字段全 null | clean `C_A` |
| `01-preflight_verified` | `deletion_patch_invalid | operator_abort_before_c_b` | `C_A/C_A/tree(C_A)` | C_A images SHA | 同上 | clean `C_A` |
| `02-deletion_patch_applied` | `source_regressions_failed | operator_abort_before_c_b` | `C_A/C_A/tree(C_A)` | C_A images SHA | 同上 | clean `C_A` |
| `03-source_regressions_passed` | `c_b_identity_invalid | operator_abort_before_c_b` | `C_A/C_A/tree(C_A)` | C_A images SHA | 同上 | clean `C_A` |
| `04-c_b_committed` | `c_b_image_build_failed | operator_abort_after_c_b` | `C_B/C_B/tree(C_B)` | null | `recovery_required=true`，`A_B/tree(C_A)/inverse-patch/tree_equals_ca=true` | clean `A_B` |
| `05-c_b_images_built` | `isolated_restore_failed | operator_abort_after_c_b` | `C_B/C_B/tree(C_B)` | C_B images SHA | 同上 | clean `A_B` |
| `06-c_b_isolated_restore_passed` | `post_delete_regressions_failed | operator_abort_after_c_b` | `C_B/C_B/tree(C_B)` | C_B images SHA | 同上 | clean `A_B` |
| `07-c_b_regressions_passed` | `result_publication_failed | operator_abort_after_c_b` | `C_B/C_B/tree(C_B)` | C_B images SHA | 同上 | clean `A_B` |

`08-result_published` 后不允许 abort；只能完成 claimed→completed 的 receipt/current projection或按 corruption recovery 阻断。abort reason 不接受自由文本、路径或错误消息；详细诊断只通过前一 progress/check-result digest 和安全日志关联。

emergency B_L approval 的 nested exact hierarchy 固定为：

```text
scope={branch,environment,production_evidence,single_user_local}
source={kind,role,principal_scope,phrase,phrase_utf8_sha256,observed_at,thread_message_ref_sha256}
predecessor={approval_request_id,state,state_receipt_sha256,evidence_file_sha256,completed_result_file_sha256}
baseline={commit,tree,key_anchor_sha256,freeze_receipt_sha256,inventory_file_sha256,rehearsal_result_sha256,images_sha256,exports_sha256}
evidence={no_unknown_inflight_sha256,legacy_on_acceptance_sha256,data_compatibility_sha256,security_acceptance_sha256}
context={approval_context_sha256}
```

decision 精确为 `restore_legacy_global_runtime`；scope 精确为 `{branch:main,environment:development,production_evidence:false,single_user_local:true}`。source 只允许 `{kind:direct_top_level_user_message,role:user,principal_scope:current_task_top_level_user,phrase:"恢复 B_L",phrase_utf8_sha256:"sha256:dc9b2aeb8172a307055b0eba809fd472451397025fe0aeefc38b4bbd248ca63e"}` 加 observed UTC 与运行环境可用时的 opaque thread/message ref SHA；ref 不可用时该字段精确为 null，禁止伪造或保存 raw ID。predecessor 只允许 current=`completed|rolled_back_to_ca`，并绑定其 exact state receipt/evidence 和原 completed result。baseline 必须绑定冻结 `B_L` 的 commit/tree/key anchor/freeze/inventory/rehearsal/images/exports；四个 evidence SHA 都来自本轮独立重验且不得复用退役 approval。

`approval_context_sha256` 是 canonical `{source_role,phrase_utf8_sha256,predecessor,baseline,evidence}` 的 SHA；ID 使用上述 domain-separated公式。唯一 writer 仍是主 Codex leader 处理新的、完整内容精确为“恢复 B_L”的实时顶层 user event；仓库不得暴露 phrase/approval CLI、stdin/env/file入口。writer 在 artifact lock 下 secure-read current及所有 evidence后，no-clobber 发布 `post-completion-rollback/emergency-bl-approval/<emergency_approval_id>.json` 并 file+directory fsync。相同 predecessor/context exact-idempotent；同 predecessor 第二个不同 approval、旧消息重放、引用/转发/subagent/system文本或跨 current reuse均拒绝。文件发布后 current 不变；rollback writer secure-read file/payload/context SHA并以当前 predecessor单次消费。approval-only 崩溃只留下可重验文件，不产生 rollback effect；rollback receipt/current 成功后该 approval不得再次消费。

post-completion rollback 的 predecessor、reason 与 emergency 字段只允许：

| predecessor state | predecessor evidence | target / release / source parent | reason_code 闭集 | emergency approval fields |
|---|---|---|---|---|
| `completed` | original completed result file SHA | `rolled_back_to_ca / R_A / C_B` | `post_completion_regression | post_completion_security_defect` | file/payload/context 三项全 null |
| `completed` | original completed result file SHA | `rolled_back_to_bl / R_L / C_B` | `assembly_off_unrecoverable | data_compatibility_failure` | 三项全部 non-null且精确匹配当前 predecessor的 emergency approval |
| `rolled_back_to_ca` | 前一 R_A rollback file SHA | `rolled_back_to_bl / R_L / R_A` | `assembly_off_unrecoverable | data_compatibility_failure` | 三项全部 non-null且精确匹配当前 predecessor的 emergency approval |

每行 `predecessor_state_receipt_sha256` 必须是 current 的 exact receipt，`completed_result_file_sha256/completed_state_receipt_sha256` 必须始终指向原 CP7-B success anchor；表外 predecessor/target/reason/parent/emergency-nullability 组合全部拒绝。

每个 writer 必须重算 ID；caller-supplied ID 只可比对，不可决定身份。同 ID同 canonical payload exact-idempotent；同 ID不同 payload、同 predecessor 多个 next、或相同业务材料得到不同 ID 都是 corruption。

所有跨 candidate 切换都必须先发布 root，不能直接把 current 改成另一个 request ID。candidate-supersede root 只绑定旧 current 已存在的 state receipt、candidate binding、manifest，以及旧 state 已有的 terminal evidence；旧 state 为 pending 时 terminal-evidence 字段必须 null，绝不预先绑定尚未生成的 aborted receipt，因此不存在 root↔receipt 哈希环。

普通 pending supersede 的新 `C_A[n+1]` 唯一 parent 必须是 current `C_A[n]`，root 的 `recovery_release/recovery_commit/recovery_tree` 必须精确为 `C_A`/current `C_A[n]`/`tree(C_A[n])`。其持久化顺序固定为：发布新 manifest → 发布 candidate-supersede root → 在旧候选发布 `pending_manual_approval→aborted/candidate_superseded` receipt（evidence/root 都为该 root file SHA）→ 立即把 current 投影为旧 candidate 的 `aborted` 并 fsync → 在新候选发布 `aborted→pending_manual_approval/candidate_requalified` receipt（previous receipt 为刚发布的旧 aborted receipt、evidence 为新 manifest file SHA、root 为同一 root file SHA）→ 再把 current 投影为新 candidate 的 pending 并 fsync。若旧 current 已是 aborted，则 root 绑定它已有的 abort/candidate-supersede evidence，跳过旧 aborted receipt 与中间投影，只发布新 pending receipt再投影；pre-04 abort 未产生 `A_B` 时，新 `C_A[n+1]` 同样以恢复后 current `C_A[n]` 为唯一 parent。`A_B` recovery 完成后仍表现为旧 abort artifact 中的 source-only recovery release；root 的 recovery release/commit/tree 绑定该 `A_B`，新 candidate 则以该 `A_B` 为唯一 parent并重新产生可运行的 `C_A` identity。rollback 后开启新候选使用 lifecycle-root，并同样先发布新 manifest/root/receipt再投影。任何新 candidate 都重新生成 manifest、approval 与 claim，旧批准不能跨 root。

崩溃恢复只允许下表 projection；recovery 每次先 secure-read 全链并确认不存在 fork：

| durable 状态 | 唯一动作 |
|---|---|
| effect/root/claim/progress/result/abort/rollback 文件尚未 no-clobber 发布 | 重验外部 effect；可证明未发生则执行，可证明已按 exact digest 发生则采纳，否则 fail closed。 |
| immutable evidence 文件已发布，所需 lifecycle receipt 尚无 | 由 evidence、predecessor 和确定性 ID 补发唯一 receipt；不得重写 evidence。 |
| lifecycle receipt 已发布，current 仍是其 exact predecessor | 只把 current 投影到该 receipt 的 next state/request ID。 |
| current 已与 receipt/evidence/root 全匹配 | 幂等成功，零写入。 |
| current 已前进但缺 predecessor evidence/receipt，或同 predecessor 有多个 root/receipt/evidence | corruption，阻断 Ready，绝不按 mtime、目录顺序或“最新文件”选择。 |

`C_B` commit、`A_B` recovery commit、`R_A/R_L` rollback commit 是上述第一行的外部 effect：只有 parent/tree/patch/profile 全部精确匹配时才可采纳。特别是 abort 文件发布前必须完成并验证所需 `A_B`；abort 已发布而 abort receipt 缺失只补 deterministic receipt，随后补 aborted lifecycle/current。pending predecessor 的 supersede 任一 fsync 崩溃都按 `root→old-aborted receipt→current=old-aborted→new-pending receipt→current=new-pending` 前缀恢复；predecessor 已 aborted 时按 `root→new-pending receipt→current=new-pending` 恢复。不得跳过或合并中间 current projection。result、abort 与 rollback 三类终态 evidence 对同一 claim/result predecessor 互斥。

manifest 发布成功后，在尚无 current 的首次 lifecycle 中唯一合法初始转换是 `none → pending_manual_approval`。已有 current 时，新的候选只能 supersede 仍处于 `pending_manual_approval` 或 `aborted` 的旧候选，并严格走上述与 predecessor state 对应的单向前缀；`claimed` 候选不能被静默替换。`rolled_back_to_ca|rolled_back_to_bl` 开启新候选则严格走 `lifecycle-root → new pending receipt → current` 顺序。所有 root/receipt 都使用上表 exact schema/ID/recovery，且新候选重新完成全部 CP7-A 验收，不得复用旧 manifest/approval/claim。approval writer 只能消费 current 精确指向的 `pending_manual_approval` binding；CP7-B claim 发布后转换为 `claimed`。成功退役必须依次 no-clobber 写 `cp7b-result.json state=completed`、`08-result_published` receipt 和 completed lifecycle receipt，再把 current 转为 `completed`。明确放弃走下述独立 abort branch，不得写 `08-result_published` 或伪造 completed result。

执行失败但可安全续作时 current 保持 `claimed`，并在同一 candidate 下写 immutable、逐步递增的 `cp7b-progress` receipts；恢复只允许从最后一个完整 receipt 的下一固定步骤继续，并重验此前 receipt、Git/image/config/audit binding。不得把“文件存在但没有 receipt”推断为已完成。writer 崩溃后若发现恰好一个 predecessor 精确匹配 current 的未投影 next receipt，可在 artifact lock 下完成 pointer 更新；零个以上歧义、多个 next receipt、stale pointer、symlink、权限/inode 漂移或 candidate mismatch 均 fail closed。

claim 发布也使用同一恢复规则，且不得把 claim 文件本身当作已 claimed：若 immutable claim 已存在、`current.json` 仍为 `pending_manual_approval` 且没有 claimed lifecycle receipt，恢复器必须在 artifact lock 下 secure-read current/manifest/approval/claim，重验 claim 的 predecessor 正是当前 pending receipt、binding/approval/combined evidence 全部精确匹配，并确认不存在其他 next receipt、result 或 progress；随后发布 deterministic `pending_manual_approval→claimed` receipt，再原子更新 current。若 claimed receipt 已存在但 current 仍 pending，只允许补做同一 pointer projection。任一 mismatch、多个 claim/receipt、current 已指向其他 candidate、或 claim 与 receipt 状态不一致都 fail closed，绝不重新生成/覆盖 claim。测试必须在 claim fsync、claim no-clobber publish、receipt fsync、receipt publish、current replace 和 parent fsync 的每个边界注入崩溃。

验证器输出 manifest file SHA、payload SHA 和 candidate binding SHA；candidate binding 只包含 request ID、`C_A`、manifest file/payload SHA、baseline anchor/freeze/rehearsal receipt SHA、三个部署 image ID/revision、validation-runner identity、authoritative staged-input receipt SHA、三个 `C_A` export SHA、isolated restore environment/mode/result SHA、PostgreSQL validation result SHA、pinned Ubuntu Rust full-gate result SHA、probe-result SHA、CP7-local epoch/guard/safety snapshot SHA 与 checklist definition SHA。人工批准、current pointer 和 CP7-B claim 都绑定这些值，而不是只绑定文件路径。

manifest、镜像 label 和日志不得包含密钥、DSN、Authorization/header 值、Server URL/query、credential、用户名、raw Task/Call ref、用户输入、本地路径或本地敏感配置内容。

### Audit 窗口完整性

CP7-A 使用全新 runtime 卷；第一次 backend 启动前 `runtime/audit.jsonl` 必须不存在，因此自动窗口从 offset 0 开始。verifier 在 `docker compose up` 前记录卷和路径状态；自动检查结束后先 quiesce backend，再以 `O_NOFOLLOW` 读取 audit，验证 regular file、device/inode/path、size、UTF-8 unique-key JSONL、首尾 newline 和完整 `[0,end)` span。缺失、替换、symlink、truncate、partial line 或 identity 变化全部使候选失效。

人工检查完成后再次 quiesce backend，从 manifest end offset 连续读取到 approval end；device/inode/container 必须相同，start 必须精确等于 manifest end。生成 CP7-B claim 前 audit EOF 必须等于 approval end，不允许未覆盖 gap。当前 audit sink 无跨进程锁/fsync，因此 live writer 下的 offset 不是权威证据；只有 quiesced 后的 inode/offset span 可用于 manifest/approval。Docker stdout 只作辅助诊断，不作为唯一零活动证明。

manual tail 不能只做连续性 hash。legacy global Client/descriptor/startup discovery 的零活动结论来自合并后的权威 audit `[0, approval_end)`：verifier 只消费 closed structured event/source，不得用裸日志字符串推断。八个安全 red line 的权威结论只能来自同一 candidate 的 `mcp_cp7_safety_ledger` transaction snapshot，不能从 JSONL 缺少事件推断为零。approval writer 必须分别重验 audit inode/offset 连续性和 ledger minute/candidate/config 连续性，再生成一个按 definition SHA 固定的 combined result；任何 malformed/unknown audit event、ledger gap/tamper、非零 violation、缺分钟或 snapshot 漂移均拒绝 approval。approval 和 CP7-B claim 必须同时绑定 audit definition/span/result SHA、safety detector definition/ledger snapshot SHA、combined canonical result SHA、每项计数和 verifier build SHA；manual-tail hash 或进程内 detector 状态不能替代持久语义结果。

### 自动验证矩阵

| 证明项 | 固定验证入口与判定 |
|---|---|
| Compose 与 Docker 合同 | 新增 `tests/deployment/test_user_mcp_cp7a_compose_contract.py` 与 `tests/deployment/test_user_mcp_cp7a_candidate_verifier.py`：解析 Compose 展开结果；精确断言三个常驻服务、one-shot stager、七项 MCP 配置、`MAF_API_ENV=dev`、SQLite、三个 Rust mode 全为 `off`、migration evidence/DSN 全拒绝、固定数值 UID/GID、正式阶段九项 staged-input fixed paths/owner/mode、`B_L` pre-freeze 独立八项闭集、phase-specific legacy key `true|false|absent`、Unix socket 权限/只读挂载、Sidecar `--sqlite` 命令/持久卷、semantic health 依赖、pinned DinD/PostgreSQL validation images 和三个 OCI revision label。 |
| Sidecar trust/readiness | 新增 `tests/integrations/mcp/test_user_mcp_cp7a_sidecar_trust.py`：stager no-follow/O_EXCL/fsync/secret-redaction，实际 image binary 提取/hash、secure-read closed manifest/allowlist/SBOM/provenance exact subject binding、backend/verifier 同 digest、Version/CheckCompatibility/Readiness、陈旧 socket、安全权限、SQLite reopen 与重启回归；仅 socket 存在不得通过。 |
| assembly-off 装配 | `tests.api.test_user_mcp_runtime_wiring` 增加 CP7-A 回归：legacy Runtime/Client factory、startup discovery、bundle revision 写入和动态 legacy capability 数均为 0；Sidecar 与 user-scoped Gateway 正常装配。 |
| API、配置与 Grant | 运行 `tests.api.test_user_mcp_api`、`tests.api.test_user_mcp_grants_and_call_control`、`tests.integrations.mcp.test_user_mcp_credentials` 和 `tests.integrations.mcp.test_user_mcp_gateway`。 |
| 调度、健康与边界 | 运行 `tests.integrations.mcp.test_dispatch_coordinator`、`tests.integrations.mcp.test_user_mcp_health`、`tests.integrations.mcp.test_user_mcp_endpoint_policy`、`tests.integrations.mcp.test_mcp_auth_header_validation`、`tests.integrations.mcp.test_user_mcp_resource_baseline` 和 `tests.capabilities.mcp_dispatch.test_selector_router_executor`。 |
| 协议与官方兼容 | 运行 `tests.integrations.mcp.test_protocol_version_negotiation`、`tests.integrations.mcp.test_official_sdk_conformance_matrix`、`tests.integrations.mcp.test_2025_11_25_task_recovery`、`tests.integrations.mcp.test_2026_07_28_adapter` 和 `tests.integrations.mcp.test_user_mcp_recovery_worker`。 |
| recovery、history 与 no-replay | 运行 `tests.api.test_user_mcp_recovery_startup`、`tests.api.test_user_mcp_task_assignment_restart`、`tests.storage.test_mcp_task_route_assignment`、`tests.storage.test_mcp_recovery_claims`、`tests.orchestration.test_fake_capability_flow` 和 `tests.orchestration.test_runtime_replanning`；新增 `tests/storage/test_user_mcp_legacy_retirement.py` 覆盖 inventory/HMAC、SQLite 原子 retirement receipt、crash/retry/race、PostgreSQL additive contract 和 Sidecar retirement RPC 缺席。历史普通非 MCP Task 必须继续；实际 legacy MCP Task 必须固定 fail closed。 |
| 无 Server 合同 | 新增 `tests/api/test_user_mcp_cp7a_no_server.py` 与 storage/frontend tests：ordinary HTTP 202/隐藏 dispatch/正常完成；explicit dispatch 的 Task+initial intent；target TaskNode+server/version intent；availability CAS、dispatch reservation、delete-vs-dispatch 单赢家、startup dispatch-resume、config/security/credential drift、`may_have_dispatched` unknown/no-replay；late trusted receipt 只 CAS 独立 projection、Task/Node/Call 不改写、零重放/continuation；四条 durable event（unknown→task.failed→resolution→correction）严格排序且 insert-or-compare；终态详情重读 projection、完整 replay、frontend lateResult 幂等。 |
| 安全 detector | 运行 `tests.observability.test_user_mcp_safety_detectors` 并新增 `tests/storage/test_mcp_cp7_safety_ledger.py`、`tests/api/test_user_mcp_cp7_safety_readiness.py`：八个 exact hook、append-only registration/attestation/violation/gap、minute continuity、SQLite reopen、tamper/write-failure fail loud、Ready 门禁和 candidate snapshot binding；hostile violation 在 disposable probe ledger 中使 probe Not Ready，权威 candidate ledger 保持独立。 |
| 前端行为 | 精确运行 `MCPSettingsPanel.test.tsx`、`MCPApprovalDialog.test.tsx`、`MCPRuntimeStatus.test.tsx`、`domain/taskEvents.test.ts` 和 `api/taskEvents.test.ts`；无 Server/不可用/授权/恢复状态必须可解释。 |
| Docker 构建、回滚与健康 | 分别从 `git archive B_L`、`git archive C_A` 构建/导出三镜像；在 pinned、internal-only、空 data-root 的 disposable daemon 中实际 load 并运行三项固定 smoke，禁止宿主 socket/cache fallback。只在 rehearsal project 的同一组专用 application-data/Sidecar 卷执行 `B_L → C_A → B_L → C_A`，每次三服务均 `running/healthy`，backend `/api-doc` 与 frontend `/seedpilot/` 返回 2xx，且 Server/Grant/Task/credential sentinel 连续、Sidecar SQLite reopen 可读、event/outbox/call 不增加；随后销毁 rehearsal project，并以全新 authoritative project/卷和 exact `C_A` immutable IDs 启动正式候选，证明 audit 从 offset 0、safety epoch 从新的首个 `opened` 开始。 |
| legacy 零活动 | 新增 `scripts/verify_user_mcp_cp7a_candidate.py`：以全新 audit `[0,end)` 为权威窗口，只匹配 closed legacy event/descriptor/source，不把 user-scoped `server/discover`/`tools/list` 当作 legacy；public capability 列表不得含 legacy 动态描述符。constructor=0 由装配测试证明。 |
| manifest/approval 安全与原子性 | 新增 `tests/scripts/test_verify_user_mcp_cp7a_candidate.py` 与 `tests/scripts/test_user_mcp_cp7_artifacts.py`：验证 flock、O_EXCL/no-clobber、file+directory fsync、current lifecycle、supersede/claim/progress/result、claim-only recovery 的每个 fsync 边界、inode/truncation/gap、audit+SQLite-ledger combined evidence、crash/concurrency、immutable artifact、`C_A` exports/isolated restore、image/config/binding 一致、UTC 时间和 SHA；特别覆盖 pending `C_A[n]→C_A[n+1]` 的唯一 parent、deterministic profile、root recovery tuple 与每个前缀崩溃恢复，并扫描输出中不存在敏感字段。 |

`tests.capabilities.mcp_tool.test_executor` 只在 CP7-A 作为 `B_L` 源码仍可回滚的兼容测试，不得作为 user-scoped 执行证明；CP7-B 删除 legacy executor 后同步删除该测试。

结构化 legacy 零活动匹配仅允许以下来源：legacy 专属 `mcp.server_discovery_*`、`mcp.capability_registered` 且 descriptor/source 被标记为 global legacy，以及明确的 legacy assembly telemetry。不得用裸字符串 `tools/list` 或 `server/discover` 扫描，因为 user-scoped discovery 合法使用这些方法。

固定后端命令以完整模块名执行，不能使用文档中的缩写：

```text
conda run -n multi_agent python -m unittest \
  tests.deployment.test_user_mcp_cp7a_compose_contract \
  tests.deployment.test_user_mcp_cp7a_candidate_verifier \
  tests.api.test_user_mcp_runtime_wiring \
  tests.api.test_user_mcp_cp7a_no_server \
  tests.api.test_user_mcp_api \
  tests.api.test_user_mcp_grants_and_call_control \
  tests.api.test_user_mcp_recovery_startup \
  tests.api.test_user_mcp_task_assignment_restart \
  tests.capabilities.mcp_dispatch.test_selector_router_executor \
  tests.integrations.mcp.test_user_mcp_credentials \
  tests.integrations.mcp.test_user_mcp_gateway \
  tests.integrations.mcp.test_user_mcp_cp7a_sidecar_trust \
  tests.integrations.mcp.test_dispatch_coordinator \
  tests.integrations.mcp.test_user_mcp_health \
  tests.integrations.mcp.test_user_mcp_endpoint_policy \
  tests.integrations.mcp.test_mcp_auth_header_validation \
  tests.integrations.mcp.test_user_mcp_resource_baseline \
  tests.integrations.mcp.test_protocol_version_negotiation \
  tests.integrations.mcp.test_official_sdk_conformance_matrix \
  tests.integrations.mcp.test_2025_11_25_task_recovery \
  tests.integrations.mcp.test_2026_07_28_adapter \
  tests.integrations.mcp.test_user_mcp_recovery_worker \
  tests.storage.test_mcp_task_route_assignment \
  tests.storage.test_mcp_recovery_claims \
  tests.storage.test_mcp_cp7_safety_ledger \
  tests.storage.test_user_mcp_legacy_retirement \
  tests.api.test_user_mcp_cp7_safety_readiness \
  tests.orchestration.test_fake_capability_flow \
  tests.orchestration.test_runtime_replanning \
  tests.observability.test_user_mcp_safety_detectors \
  tests.scripts.test_verify_user_mcp_cp7a_candidate \
  tests.scripts.test_user_mcp_cp7_artifacts
```

真实 PostgreSQL 只用于证明 route-reason/error category、`MCPNoServerIntent`、`user_mcp_owner_mutation_guard` 与原子收敛所需 metadata/CHECK/transaction 的 additive 兼容，不能由 SQLite 单测替代；它不表示 CP7-A backend 使用 PostgreSQL 或 Sidecar Task authority。Compose 必须定义不默认启动的 `cp7-validation` profile，包含 `cp7-validation-postgres` 与 one-shot `cp7-validation-test-runner`。PostgreSQL 必须使用预载且 digest-pinned 的 `CP7_POSTGRES_VALIDATION_IMAGE`，test runner 必须使用上述从 clean `git archive C_A` 构建的 exact `cp7-validation-runner` immutable image ID；禁止改用 production backend image、宿主工作树、pull 或 tag-only reference。两者只连接 candidate-scoped `internal:true` network，不发布宿主端口，并使用 no-clobber 新建、初始为空且不与 backend/Sidecar/既有开发数据共享的 disposable data volume。

launcher 每次生成新的数据库名、用户名和至少 32-byte 随机密码。Compose 固定 PostgreSQL consumer 为 `uid=10004`、test runner consumer 为 `uid=10001`；不得依赖镜像内可漂移的名字 UID。launcher 在 candidate-scoped tmpfs 中从同一内存密码分别 no-clobber 写两个内容完全相同但 inode/owner 不同的 regular secret：`postgres-password` owner `10004:10004`、`runner-password` owner `10001:10001`，mode 均精确 `0400`；写后分别 fsync、重读并 constant-time 比较内容摘要，只记录 `content_equal=true`，不记录摘要值。PostgreSQL 只读挂载前者并只使用 `POSTGRES_PASSWORD_FILE=/run/secrets/cp7-postgres-password`；test runner 只读挂载后者并只接收非敏感 hostname/project/database/user 与 `MAF_POSTGRES_CP7_PASSWORD_FILE=/run/secrets/cp7-postgres-password`，在进程内读取后构造不输出的临时连接参数。两个容器不能看到对方的 secret inode；密码和含密码 DSN 不得进入 container environment/inspect、command argv、`compose.env`、staged input、backend service、manifest、Docker label、日志或 stdout/stderr。test runner 必须确认连接目标都属于本次 validation project、等待 `pg_isready`，在空数据库执行 fresh schema bootstrap/additive migration，再运行固定测试。测试完成或失败都必须停止 exact project，删除 container、internal network、data volume 和两个 tmpfs secret，证明对象不存在，并扫描结果中无 secret/DSN 泄露。

固定验证入口由 candidate verifier 启动 profile，并在同一 test-runner 中执行：

```text
conda run -n multi_agent python -m unittest \
  tests.storage.test_user_mcp_cp7_postgres_integration
```

profile/image unavailable、凭据泄漏、对宿主开放端口、非空旧 volume、测试 skip/未发现、连接目标不属于本 project、schema 非空或 cleanup 失败都使候选 blocked；不得接受用户手工提供的 production/长期 DSN 作为替代。manifest 只绑定 pinned PostgreSQL reference、宿主 image ID/revision、validation profile canonical SHA、fresh-schema/test result SHA 和 cleanup receipt，不记录 DSN/数据库名/用户名/密码。

Sidecar contract 不新增 retirement RPC、SQLite reopen、数值 UID/GID wrapper 与 Rust dependency/security 的 canonical native 门禁只能从仓库统一入口执行：

```text
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --offline --no-fetch
```

这是唯一 canonical Rust 命令；`--offline` 与 `--no-fetch` 是 CP7-0 必须实现并由 runner 自身强制的参数，不允许 verifier 只设置环境变量后省略 argv。容器必须以 exec form 直接启动，不经过 shell；manifest 的 `rust_gate.runner_argv`、plan JSON 与 full-run result 必须逐元素绑定实际进程完整 argv `['conda','run','-n','multi_agent','python','scripts/run_rust_quality_gates.py','--run','--offline','--no-fetch']` 及其 canonical SHA。不同 conda environment、Python launcher、额外参数、不同顺序或缺少任一参数都不是本门禁。该命令必须在预载、digest-pinned 的 `CP7_RUST_GATE_RUNNER_IMAGE` 中执行。verifier 必须先以 immutable image ID 验证 runner 的 OS 精确为 Ubuntu 22.04、platform 精确为 `linux/amd64`、Rust/Python/Conda 工具链与固定 runner provenance 一致；runner provenance 必须闭合绑定 Rust stable 与指定 nightly 的完整版本、rustup component、全部 target triples、cargo 扩展及版本、Cargo vendor tree/`.cargo/config.toml` SHA、registry/git cache inventory SHA、冻结 RustSec advisory-db commit/archive SHA、Conda explicit-lock SHA、离线 conda package cache inventory SHA 和 Python wheelhouse/requirements-lock SHA。再把 clean `git archive C_A` 解包到 no-clobber disposable source volume，使用独立 writable target/cache volume、无宿主工作树 mount、无 network/pull 执行 full gate。结束后必须删除 source/target/cache volume并证明清理完成。

运行时必须设置 `CARGO_NET_OFFLINE=true`，Cargo 只允许 vendored source/cache，RustSec 只允许绑定的 advisory snapshot，Conda/pip 只允许上述本地 cache/wheelhouse；任一工具尝试 DNS、registry、git、index、advisory update 或缺包时直接失败，不得临时联网补齐。不得使用 `--only`、`--skip-unavailable` 或把直接 `cargo test/check/fmt` 的局部成功冒充完整门禁；局部 cargo 命令只可作为失败诊断。candidate manifest 必须绑定 runner image reference/ID/revision/provenance SHA、Ubuntu/platform verdict、runner 文件 SHA、完整 argv 及 SHA、`--plan-json` canonical 输出 SHA、full-run result SHA、`native/deny.toml` SHA、Cargo.lock SHA、vendor/cache/advisory/Conda/wheel inventories、实际运行的 stable/nightly/components/targets/extensions 和 cleanup receipt，不得静默跳过依赖策略、audit 或 contract checks。

固定前端命令：

```text
cd frontend
npm test -- --run \
  src/components/MCPSettingsPanel.test.tsx \
  src/components/MCPApprovalDialog.test.tsx \
  src/components/MCPRuntimeStatus.test.tsx \
  src/domain/taskEvents.test.ts \
  src/api/taskEvents.test.ts
npm run typecheck
npm run build
```

## 人工测试门禁

### 候选交付绑定

自动验证通过后，实施停止并向项目负责人交付：

- `C_A` 完整 Git SHA；
- manifest 文件 SHA-256；
- 三个 image ID、OCI revision、export SHA 和 restore-smoke result SHA；
- disposable PostgreSQL validation、canonical Rust full gate、safety probe 与正式 ledger snapshot 的 result SHA；
- current candidate binding 与 `pending_manual_approval` state receipt SHA；
- Compose project name 与启动命令；
- 自动验证摘要；
- 下表中的人工检查步骤。

人工批准必须绑定同一个 `C_A`、manifest SHA-256、三个 image/export/restore identity、current candidate binding 和验收完成 UTC 时间。任何 rebuild、commit、配置、镜像 ID、export 或非法 current pointer 变化都会使批准失效，必须重新运行自动和人工测试。但是，按本设计固定顺序生成且完整绑定该 approval 的唯一 claim，以及随后合法的 `pending_manual_approval→claimed` receipt/current 投影，是批准的正常单次消费，不是候选漂移，不使已绑定 approval 失效。

### 人工检查表

| 场景 | 前置条件 | 操作 | 预期结果 | 留存证据 | 清理 |
|---|---|---|---|---|---|
| Server CRUD | 测试用户 A；独立 CP7-A 卷 | 新增、修改、禁用、重新启用、删除测试 Server | 状态和 capability 可解释；禁用/删除后不再可调用 | 脱敏截图/时间、Server 安全引用 | 删除临时 Server |
| 凭据重启 | 用户 A Server 可用；当前 Ready epoch 健康 | 保存凭据，由 verifier 执行 controlled-maintenance/no-recreate backend restart，等 successor epoch 整分钟观测后再做健康检查 | 凭据仍可解密使用；container/image/config/staged volume 不变；epoch chain 完整；日志无 secret | maintenance/close/open/ready receipt SHA、StartedAt、健康结果 | 保留或删除测试凭据 |
| 授权与普通调用 | 用户 A 有可用工具 | 分别允许、在 Gateway 前普通拒绝 Grant 并执行调用 | 允许仅调用一次；普通拒绝在 Gateway 前零调用且不记 violation；结果归属正确；绕过拒绝的 unauthorized hostile path 只由 probe 证明 | audit 安全引用、调用结果、probe digest | 撤销临时 Grant |
| 跨用户隔离 | disposable probe 已通过真实越权 hostile automation；权威候选中 A/B 各自登录 | 只比较 A/B 各自 owner-scoped Server/Grant/result 列表与脱敏可见字段，不在权威候选向 A 的资源发送 B 的越权请求 | 双方只见自己的资源；真实拒绝边界由 probe `automation_substituted` 证明；权威 candidate ledger 保持零 violation | probe verifier digest、候选列表摘要 | 删除临时数据 |
| 长调用与恢复 | 现有 Server 支持相关能力时 | 继续、取消；对允许保留的在途状态由 verifier 执行 controlled-maintenance/no-recreate restart，验证 successor epoch 后恢复结果 | 不重放 `tools/call`；可信 terminal result 优先；无可信 result 且 may-have-dispatched 才 unknown；Task/Node/result 收敛 | Task/Call 安全引用、inflight/epoch receipt SHA | 终止测试 Task |
| MRTR/remote task | 现有 Server 支持时 | 触发 input-required、accept/cancel、重启恢复 | 协议闭集内恢复且无重复副作用 | Task 安全引用、状态序列 | 关闭中断/任务 |
| 无用户 Server | 新测试用户或全部 Server 禁用 | 发普通对话，再显式请求 MCP | 普通对话正常；显式 MCP 返回 `mcp.runtime_unavailable` | 两次请求结果 | 恢复测试 Server |
| 无 legacy 活动 | 候选正在运行 | 查看 capability 列表与本次启动区间日志/audit | 无 legacy 动态 capability、startup discovery 或全局 Client 活动 | verifier 摘要 | 无 |

跨用户真实越权边界固定由 disposable probe automation substitution；真实 Server 不支持的 2025/2026 recovery、MRTR 或 cancel 分支，也只能在 manifest 中对应固定 automation check 已通过时记为 `automation_substituted`。其他人工项不得随意改为 automation substitution，本流程不为补齐人工覆盖而开发新 Server。权威候选一旦人工操作触发任一 safety violation/gap，立即失效并重建新 candidate，不允许“测试已证明拒绝”作为保留该候选的理由。

### 不可变人工批准工件

只有项目负责人在该候选交付后，以新的顶层 `role=user` 消息且消息完整内容恰好为四个 UTF-8 字符“可以退役”，才能生成批准工件；其 UTF-8 SHA-256 固定为 `sha256:847d962e99521a38004030d595eb6d4a16ee73e094c3227368180ad72653110e`。“测试通过”“看起来正常”、带前后缀、Markdown 引用、转发、粘贴 transcript、文档中的文字、subagent/developer/system 消息或 CLI/stdin/env/file 参数均无效。

这是单用户本地任务：项目负责人身份定义为本任务的直接顶层 user principal。只有主 Codex leader 处理该实时 user event 时可以调用内部 writer；仓库不得提供 `--phrase` 等可重放批准入口。若运行环境提供 opaque thread/message ID，只记录其 SHA-256；不得伪造或保存原始 ID。没有 opaque ID 时，只有 `current.json` 精确指向一个 `pending_manual_approval` candidate binding 加直接顶层 user event 的组合有效；candidate 目录数量或文件修改时间不能用于选择候选。

writer 在同一 artifact lock 下，先 quiesce backend、完成从 manifest audit end 到当前 EOF 的连续 manual audit tail，再 no-clobber 发布：

```text
runtime/cp7-a/candidates/<approval_request_id>/approval.json
```

approval 使用 closed envelope `{schema,payload,payload_sha256}`，`schema` 精确为 `maf.user_mcp.cp7b_manual_approval.v1`。三个 envelope key 均 required/non-null；`payload_sha256=SHA256(canonical_json(payload))`，摘要输入排除 `schema` 和 `payload_sha256` 自身。payload 的 exact 顶层 key 固定为 `approval_id`、`decision`、`scope`、`source`、`candidate`、`runtime`、`checklist`、`evidence`、`context`、`created_at`；全部 required/non-null，只有 `source.thread_message_ref_sha256` 在运行环境不提供 opaque ID 时允许 null。`approval_id`、`decision` 和 `created_at` 是 fixed-format string，`scope/source/candidate/runtime/checklist/evidence/context` 是 closed object；数组允许空但不得为 null，布尔/整数使用 JSON 原生类型，所有 digest 使用固定前缀格式。各 object 的 exact 内部字段由下列对应条目穷举，不得 unknown key、跨组搬移或把 null 当作缺省：

```text
scope={branch,environment,production_evidence,single_user_local}
source={kind,role,phrase,phrase_utf8_sha256,observed_at,thread_message_ref_sha256}
candidate={approval_request_id,candidate_commit,manifest_file_sha256,manifest_payload_sha256,candidate_binding_sha256,images,exports,restore_result_sha256}
runtime={project_name,containers,restarts}
checklist={definition_sha256,result_sha256,items}
evidence={audit,safety,combined}
context={approval_context_sha256}
```

`source.kind` 只允许 `direct_top_level_user_message`；`candidate.images[]` 为 `{service,image_id,revision}`，service 按固定顺序 `backend,frontend,runtime-sidecar` 各出现一次；`candidate.exports[]` 为 `{service,sha256}`，使用同一顺序且各出现一次；`runtime.containers[]` 为 `{service,container_id,image_id,started_at}`，使用同一顺序且各出现一次；`runtime.restarts[]` 为 `{check_id,container_id,image_id,started_at_before,started_at_after,maintenance_receipt_sha256,closed_receipt_sha256,opened_receipt_sha256,ready_receipt_sha256,audit_offset,ledger_record_count,inflight_state_sha256}`，按 `check_id` UTF-8 字节序排序去重；`checklist.items[]` 为 `{check_id,result,evidence_kind,evidence_sha256}`，按 checklist definition 的固定 check ID 顺序且每项一次，result 只允许 `owner_attested_passed|automation_substituted`，`evidence_kind` 只允许 `owner_attestation|automation_manifest_check`，并要求前者只配 `owner_attested_passed`、后者只配 `automation_substituted`。`evidence.audit={device,inode,start_offset,end_offset,span_sha256,record_count,definition_sha256,result_sha256}`；`evidence.safety={snapshot_start_sha256,snapshot_end_sha256,epoch_chain_sha256,maintenance_union_sha256,invalid_latched,counts_by_red_line,gap_count,definition_sha256,result_sha256}`；`evidence.combined={definition_sha256,result_sha256,verifier_build_sha256}`。`counts_by_red_line` 只含八个固定 key 和非负整数；不得用任意 map 代替 closed object。

- `decision=retire_legacy_global_runtime` 与 scope `{branch:main, environment:development, production_evidence:false, single_user_local:true}`；
- source kind、`role=user`、exact phrase、固定 phrase UTF-8 SHA、observed UTC 时间、可用时的 thread/message ref SHA；
- approval request ID、`C_A`、manifest file/payload SHA、candidate binding SHA、三个 image ID/revision、三个 `C_A` export SHA 与 restore-smoke result SHA；
- Compose project、三个 container ID 与 manual test 后的最终 `StartedAt`；人工重启必须由 verifier 走 controlled-maintenance 边界并使用不重建容器的 restart，container ID/image ID/config/staged volume 不变，StartedAt 变化、epoch predecessor/close/open/ready、audit offset、ledger count 和 inflight digest 按固定 restart check 记录；任何 recreate、新 container ID 或缺失 epoch boundary 使候选失效；
- 八项 checklist definition/result SHA、逐项 `owner_attested_passed` 或允许的 `automation_substituted`；项目负责人的 exact phrase 对所有人工项构成一次整体 attestation，若另有证据只存 closed kind 与 digest，不存 raw screenshot/path/user/server/task ID；
- 与 manifest 相同 audit device/inode、start=manifest end 的 tail digest/count/end offset；CP7-local ledger/Ready epoch 从 manifest snapshot end 到 approval end 的连续追加、合法 maintenance boundary union、epoch-chain SHA 与 `invalid_latched=false`；以及合并 audit+ledger 的 definition/result/verifier build SHA 和全零 closed detector counts；
- `context` 只含 approval context SHA；`created_at` 只含 created UTC 时间。envelope payload SHA 只位于 envelope，不得在 payload 内自引用。

`approval_context_sha256` 精确为 canonical SHA-256：`{role,phrase_utf8_sha256,approval_request_id,candidate_commit,manifest_file_sha256,candidate_binding_sha256,images,candidate_export_sha256s,manual_checklist_result_sha256,combined_detector_result_sha256}`。其中 `images` 的元素 exact 为 `{service,image_id,revision}`，固定按 `backend,frontend,runtime-sidecar` 排列；`candidate_export_sha256s` 的元素 exact 为 `{service,sha256}` 并使用同一固定顺序。两数组都必须恰好三项、service 唯一，禁止由 map iteration、writer 输入顺序或排序后的 digest 值决定顺序。`approval_id` 固定为 `cp7b-` 加该 digest 去掉 `sha256:` 后的前 32 个 lowercase hex；caller 不能自选 approval ID，同一 context 只能得到同一 ID。

其中 `combined_detector_result_sha256` 必须同时绑定 legacy audit result SHA 与 CP7-local safety ledger snapshot SHA；只绑定 JSONL、Docker stdout、production rollout metric 或进程内 detector 状态都不合法。

approval 文件与 manifest 同样要求 UID/mode、closed schema、canonical JSON、O_EXCL/no-clobber、file+directory fsync 和 immutable secure-read。candidate 或配置任一变化、audit replacement/truncate/gap、检查表缺项、unknown outcome、detector 非零/缺口或 binding 不一致都拒绝生成。approval no-clobber 发布成功后仍不改变 current state；只有 CP7-B preflight 重验 approval 并成功发布 claim，才能把 current 从 `pending_manual_approval` 转成 `claimed`。该合法 claim/claimed transition 保留 approval 效力；任何不匹配的 claim、第二个 claim、无 receipt 的 pointer 跳转或其他 state 改动才使批准无效。

## CP7-B：物理退役

### 进入条件

进入 CP7-B 前同时满足：

- `current.json` secure-read 后精确指向本 candidate、state 为 `pending_manual_approval`，其 immutable state receipt、manifest 和 candidate binding 全部一致；
- secure-read 并重验 immutable manifest/approval 的 closed schema、file/payload/binding/context SHA，且批准来源是本轮直接顶层“可以退役”；
- 当前分支精确为 `main`，HEAD 精确为 `C_A`，tracked index/worktree 在物理删除前保持干净；
- 三个 `C_A` image ID/OCI revision/export SHA/restore-smoke result、Compose project/container、脱敏配置摘要、Sidecar trust/inventory SHA 与 approval 完全一致；
- backend quiesced，audit 当前 EOF 精确等于 approval end，device/inode/container 不变且不存在未覆盖 gap；
- legacy audit `[0, approval_end)` 已用 approval 绑定的 closed legacy detector definition 重扫并精确匹配；CP7-local safety ledger 已在单个 SQLite transaction 内重新生成 snapshot，candidate/config/definition/range/result SHA 与 approval 精确一致，八项 violation 全为 0、gap 为 0；combined result/verifier build SHA 精确匹配；
- 八项人工检查均 `owner_attested_passed` 或按固定规则 `automation_substituted`；
- `B_L` 与 `C_A` 三镜像导出文件仍存在、SHA 正确，并已通过 `B_L → C_A → B_L → C_A` 回滚 smoke；
- `prod` 未被切换或修改。

全部通过后，在同一 artifact lock 下 no-clobber 生成一次性：

```text
runtime/cp7-a/candidates/<approval_request_id>/cp7b-claim.json
```

claim 绑定 approval file SHA、approval ID、`C_A`、candidate binding、三个 image/export/restore SHA、combined detector definition/result SHA、claimed UTC 时间与 `state=claimed`。claim no-clobber 发布且 current 原子转换到 `claimed` 后才能开始物理删除；任一侧失败都按 lifecycle receipt 恢复或 fail closed，不能仅凭 claim 文件存在推断状态。

持久化顺序精确为：secure-read 并锁定 current/pending predecessor → no-clobber 发布并 fsync claim → no-clobber 发布并 fsync `pending_manual_approval→claimed` lifecycle receipt → 原子 replace/fsync `current.json=claimed`。claimed receipt 的 `transition_id` 只能使用权威 lifecycle 公式：previous/next/reason 固定为 `pending_manual_approval/claimed/retirement_claimed`，`evidence_file_sha256` 固定为 claim file SHA，`lifecycle_root_sha256=null`；approval ID 与 candidate binding 已由 claim payload和 evidence SHA 单向绑定，不得另立第二套 transition-ID 输入。artifact recovery 只允许四种状态：全部尚未发布时正常执行；唯一合法 claim 已发布但无 claimed receipt/current 仍 pending 时补 deterministic receipt 再更新 current；claim+receipt 已发布但 current 仍 pending 时只补 pointer projection；current/claim/receipt 全匹配时幂等成功。receipt 无 claim、多个 next receipt、已有 progress/result、binding/predecessor 不符或 current 指向其他 candidate/state 全部 fail closed。

CP7-B 的 progress state 只允许按以下固定顺序前进：

```text
01-preflight_verified
02-deletion_patch_applied
03-source_regressions_passed
04-c_b_committed
05-c_b_images_built
06-c_b_isolated_restore_passed
07-c_b_regressions_passed
08-result_published
```

每一步完成后写 immutable `cp7b-progress` receipt，固定 basename 为 `cp7b-progress/<ordinal>-<step>.json`，绑定 claim SHA、前一 receipt SHA、`C_A`、当前 tracked patch/tree、固定 check result SHA 和 UTC 时间；不得跳步、回退或覆盖。`02-deletion_patch_applied` 记录 `git diff --binary C_A` 的 canonical SHA 与 allowlisted changed-path set，且必须确认没有无关 tracked 修改；`03-source_regressions_passed` 之后才能创建退役 commit `C_B`。`C_B` 必须是一个新 non-merge commit、唯一父提交精确为 `C_A`、只包含已批准的 global-runtime 删除、tombstone、回归与状态文档；不能 amend/rebase 成另一个身份。`04-c_b_committed` 后任何源码/测试修复或不可继续的失败都使本 claim 进入 recovery-then-aborted，不能把未批准的新 commit 偷换为 `C_B`。

`C_B` 必须从 clean `git archive C_B` 构建 backend/frontend/runtime-sidecar 三个 image 和非部署 `cp7-validation-runner` image，四者 OCI revision 精确为 `C_B`。构建器必须重新从 exact `C_B` Sidecar image ID 提取 actual binary，重新生成只绑定 `C_B` commit/tree/binary/Cargo.lock/proto/schema/error-table 的 manifest、allowlist、SBOM 和 provenance，并把它们与 `C_B` config/inventory/key 安全复制到全新 immutable `C_B` staged-input volume。不得复用 `B_L`/`C_A` trust 文件、staged volume 或 receipt；backend startup 与 verifier 必须通过同一 secure trust validator，并从 live `C_B` Sidecar image 重算 binary SHA。

对三个部署镜像生成新的 immutable export，在同一 pinned、fresh disposable daemon 规则下 load/运行固定 smoke，再以这些 exact image ID 完成 CP7-B 全回归。PostgreSQL 回归必须使用 exact `C_B` validation-runner image，canonical Rust gate 必须在 pinned Ubuntu 22.04 `linux/amd64` runner 中对 clean `C_B` archive 重跑。`05-c_b_images_built`、`06-c_b_isolated_restore_passed` 和 `07-c_b_regressions_passed` receipts 分别绑定 `C_B` commit/tree/archive SHA、三个部署 image ID/config SHA/revision、validation-runner identity、`C_B` staged/trust receipt、export SHA、restore result SHA、PostgreSQL/Rust result 和测试摘要。`B_L`/`C_A` 工件保持只读，不被 `C_B` 覆盖。

只有 `07-c_b_regressions_passed` receipt 完整且工作树/index 精确等于 clean `C_B` 时，才可 no-clobber 发布 closed `cp7b-result.json state=completed`。result 必须绑定 approval/claim、此前 progress chain、`C_A`、`C_B` commit/tree/archive、三组 `C_B` image/export/restore identity、`C_B` trust/staging/validation-runner identity、全部回归与 reachability/tombstone结果、`B_L`/`C_A` 回滚工件仍可验证的证明和 completed UTC 时间。result fsync 后必须再 no-clobber 发布 deterministic `08-result_published` progress receipt，绑定 result file/payload SHA；随后才发布 `claimed→completed` lifecycle receipt并原子更新 current。final result 与 receipt 完整匹配时返回幂等成功；存在不匹配的 final result 才拒绝第二次消费。不得把旧 approval 用于新 candidate，也不得把未持久化的内存进度当作可恢复状态。

放弃/失败使用与成功 progress 序号完全独立的 abort branch：writer 按权威 schema、last-progress×reason 闭集和 domain-separated 公式派生 `abort_id`，no-clobber 发布 `cp7b-abort/<abort_id>.json`，绑定 approval/claim、最后一个完整 success progress receipt、固定 reason、当前 patch/commit/image 身份、`04` 之前恢复并证明 clean `C_A` 或 `04` 之后完成下述 `A_B` 恢复的结果和 UTC；随后发布唯一 `cp7b-abort-receipt/<abort_id>.json` 和 `claimed→aborted` lifecycle receipt，再投影 current。abort branch 不写 `cp7b-result.json`、不写或复用 `08-result_published`、不占用任何 `01..08` 成功序号；之后必须通过 `candidate-supersede-root` 生成新 candidate 并重新人工批准。已经生成 `C_B` 后不得以“仅停机”替代 `A_B`，也不得声称 HEAD 已恢复为旧 `C_A` commit。

若失败发生在 `04-c_b_committed` 之后，abort writer 在发布 abort receipt 前必须从 clean `C_B` 按上述独立 `A_B` profile 创建确定性 source-only recovery commit：`A_B` 是 non-merge commit、唯一父为 `C_B`，patch 精确为已绑定 deletion patch 的 canonical inverse，tree 精确等于 current `C_A[n]`；commit SHA、tree、inverse-patch SHA、`recovery_profile_sha256` 和 `tree_equals_ca=true` 写入 abort artifact。不得用 reset/checkout/rebase 或仅切回旧 commit 代替。`A_B` 创建前崩溃时只允许在 exact clean `C_B` 重试；commit 已创建而 receipt 未发布时，只能按 expected commit SHA 与唯一性规则采纳。存在 unexpected child/ref、工作树漂移或 tree 不等于 `C_A[n]` 时停止并 fail closed。完成 `A_B` 后 current 才可进入 `aborted`；不得构建或启动 `A_B`，新可运行候选必须是唯一 parent 为该 `A_B` 的 `C_A[n+1]`，并以新 candidate binding 完整重新批准。

恢复只允许在同一 claim、最后一个完整 progress receipt 和可证明未漂移的 patch/commit/image 状态上从下一固定步骤继续：receipt 前的副作用必须按 digest 重验，receipt 后没有证明的副作用必须安全重做或 fail closed。特别是 commit 已存在但 `04-c_b_committed` receipt 尚未发布时，只能在该 commit 唯一父为 `C_A`、tree 精确等于已记录 deletion patch、没有其他 commit/修改时补发 deterministic receipt；result 已存在但 `08-result_published` receipt 尚未发布时，只能在 result predecessor 精确为唯一 `07-c_b_regressions_passed`、current 仍 claimed 且不存在其他 next receipt/result 时补发 deterministic receipt；result receipt 已存在而 current 仍 claimed 时只补 lifecycle/current projection。存在多个候选 commit、patch/result drift、image identity 漂移或 progress fork 均 aborted。

### 删除符号与路径

物理删除的目标是 global runtime，不是旧协议兼容。至少删除：

- `src/integrations/mcp/runtime_state.py` 中 `MCPRuntimeState`、`MCPRuntimeBundle`、`MCPToolBinding`、refresh/pending activation、全局 clients/bundles/active revision/retention/inflight 生命周期及其仅有调用者；
- `src/capabilities/mcp_tool/executor.py` 的 `MCPToolExecutor` 及 legacy global binding 执行入口；
- `src/api/runtime.py` 中 legacy config 装载、`prepare_refresh_sync(reason="startup")`、startup discovery、动态 capability/instance 同步、legacy audit、Task MCP bundle revision retain/release/recovery和 legacy cancel 路径；
- orchestration registry 中只服务于 legacy 动态 descriptor 的注册/隐藏/fallback 分支；
- `src/integrations/mcp/__init__.py` 中上述 global-only symbol 的导出；
- Compose/配置中的功能性 `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED` 分支；
- 只服务上述路径的测试、fixture、文档和配置。

删除后执行 reachability 扫描：global runtime symbol 不得被 import、构造或注册，public capability 不得出现 legacy 动态 descriptor。

### 必须保留的符号与兼容面

必须保留：

- user-scoped `mcp.dispatch`、Gateway、Coordinator、Grant 和调用控制；
- `UserMCPClientFactory` 及 user-scoped Client；
- `PythonLegacyMCPClientAdapter`、`LegacyHTTPSSETransport`、`StreamableHTTPTransport` 和仍被 user-scoped 五版本协商使用的旧 wire-protocol Adapter/DTO；
- 2025/2026 remote-task、MRTR、durable continuation 与结果恢复；
- Endpoint Policy、SSRF、Header、Schema、Credential 和 audit 安全边界；
- Rust Sidecar binary、Task/TaskNode/Event/dispatcher 实现、协议与 contract 继续保留以供兼容和未来独立启用；本 CP7-A/CP7-B 流程中三个 Rust authority mode 始终为 `off`，不得把保留实现误述为当前 authority；
- User Server、Credential、Grant、Task、result、audit 和 rollout ledger 数据；
- 历史 `legacy`/`legacy_global_runtime` enum、DTO、metric、audit parser 的只读兼容。

不得因为类名或 transport 名包含 `legacy` 就删除仍被 user-scoped 协议兼容使用的实现。

### 旧环境变量 tombstone

CP7-B 从 Compose、配置模型和功能分支中删除 `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED`。`C_B` canonical Compose 展开环境、`config.yaml`、validation runner 和三个长期容器的 inspect environment 都必须证明该 key 完全不存在，而不是存在且设为 `false`。启动最前端仅保留一个无功能 tombstone：只要 hostile test 显式注入该 key，即使值为 `true`、`false` 或空字符串，也以固定错误 `legacy_runtime_retired` 拒绝启动。

tombstone 只检查 key presence，不解析布尔值，不导入 global runtime module，也不能重新装配任何 legacy 对象。

### CP7-B 固定回归

CP7-B 必须以同一 phase-parameterized 测试矩阵重跑 CP7-A 的 user-scoped、协议、恢复、authority、安全、前端和 Docker 行为；不是原样复用 `C_A` 配置断言。所有 `expected_release=C_B,expected_phase=retirement` 的用例必须把 CP7-A 的 `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=false` 断言替换为 key absent/tombstone 断言，其余共享行为断言保持不变，并新增：

- global runtime module/symbol/import/reachability 为 0；
- canonical `C_B` Compose/config/container/validation-runner 的 `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED` key 必须 absent；tombstone hostile test 对 `true`、`false`、空字符串及任意其他值均因 key presence 固定拒绝；
- 普通历史 `mcp_execution_mode=legacy` 非 MCP Task 可恢复；
- actual legacy MCP 非终态 Task 在 startup/continue/interrupt/cancel recovery 均固定 `legacy_runtime_retired` 且无跨路径重放；
- terminal legacy history/metric/audit 仍可读取；
- user-scoped 旧协议 Adapter/Transport 仍覆盖五版本；
- `C_B` 是唯一父为 `C_A` 的新 commit，clean archive/tree/patch 与 progress chain 精确一致；
- CP7-B clean archive 的三个 Docker target 使用 `VCS_REF=C_B` 构建、导出、在空 disposable daemon 恢复并健康启动；
- manifest/approval/claim secure reader 对 symlink、权限、nlink、truncate、inode drift、digest mismatch、旧 candidate 和并发 replay 全部 fail closed。
- CP7-B progress 在每个 receipt/fsync/commit/image/result 边界崩溃后只能续作一次；fork、跳步、commit/tree/image 漂移均 fail closed。

## 回滚与失败处理

### CP7-A 失败

停止并删除本次独立 CP7-A project 的容器和卷，不操作现有开发 project/volume。修复后生成新的 candidate commit、镜像和 manifest，旧人工结果失效。

### CP7-B 失败

`completed` 之前的放弃/失败只走上述独立 abort branch，不得写 post-completion rollback receipt。`current=completed` 后的回滚则是新的权威状态转换，必须在 artifact lock 下 no-clobber 发布 `post-completion-rollback/<rollback_id>.json`，再发布 lifecycle receipt，最后把 `current.json` 投影为 `rolled_back_to_ca` 或 `rolled_back_to_bl`。rollback receipt 必须绑定 completed result/receipt/current predecessor、原因闭集、源码恢复 commit、部署 image/config/trust、回归结果、数据兼容结果和 UTC；不得覆盖 CP7-B result 或倒改 success progress chain。

- 如果只需撤销物理删除但继续 assembly off，源码回滚必须按上述 `R_A` deterministic profile 创建新 non-merge commit；其唯一父精确为 `C_B`，patch 是已绑定 `C_B` deletion patch 的 canonical inverse，tree 精确等于 current `C_A[n]`。不得 reset/checkout/rebase/改写 `C_B`。从 clean `git archive R_A` 重建三个部署镜像、validation runner 和 exact `R_A` commit/tree-bound trust，以 `expected_release=R_A,expected_phase=rollback_to_ca`运行共享 validator，重跑 isolated restore、PostgreSQL、Rust 与 assembly-off 回归后，发布绑定 `recovery_profile_sha256` 的 rollback receipt 并把 current 转为 `rolled_back_to_ca`。
- 恢复 global runtime 不能沿用原“可以退役”approval。只有项目负责人在事故证据交付后发送新的顶层 `role=user`、完整内容精确为“恢复 B_L”的消息，主 leader 才能按权威 lifecycle schema与单次消费协议生成 deterministic emergency approval。rollback writer 必须重验其 file/payload/context SHA、current predecessor、`B_L` anchor/freeze/inventory/rehearsal/images/exports、无 unknown/inflight 和本轮 legacy-on/data-compatibility/security 验收；任一漂移都拒绝。源码必须按上述 `R_L` deterministic profile，以当前权威回滚 commit 为唯一父提交创建 non-merge recovery commit，tree 精确等于冻结 `B_L`，不得移动分支指针回到旧 commit。从 clean `R_L` 重建 exact commit/tree-bound image/trust，以 `expected_release=R_L,expected_phase=rollback_to_bl` 运行共享 validator，并重跑 isolated restore/legacy-on/data compatibility/security 后，才可发布绑定 `recovery_profile_sha256` 的 rollback receipt 并把 current 转为 `rolled_back_to_bl`。
- 两种回滚都不得删除或降级用户数据、credential key、Sidecar 数据、audit、safety ledger 和已有 artifact；未知或在途调用只能 fail closed，不得因回滚跨 execution path 重放。

`rolled_back_to_ca|rolled_back_to_bl` 不是死端，也不自动恢复旧批准。完成任一 rollback 后，只有按前述绑定 rollback receipt 的 `lifecycle-root` 创建新 root，才可生成新的 CP7-A candidate 并重新进入 `pending_manual_approval → claimed → completed`；root 发布/投影的每个 fsync 边界必须可幂等恢复，多个 root、predecessor 不匹配或跨 rollback receipt 复用均 fail closed。

任何回滚都只发生在 `main` 开发环境；不触碰 `prod`。

## 风险与控制

| 风险 | 控制 |
|---|---|
| canonical enforce 缺 Sidecar/密钥/容量而无法启动 | 三服务 Compose、实际 binary trust、semantic readiness、one-shot secure staging、显式正整数 preflight、启动 fail closed。 |
| fixed backend UID 无法读取宿主 `0400` 输入或被迫放宽权限 | stager secure-copy 到 candidate volume，目标精确为 `10001:10003`/`0400`，backend/verifier 只读同一 staged copy。 |
| Rust authority mode 隐式默认或漂移 | Compose 与 verifier 精确要求 runtime-store、event-log、task-dispatcher 三个 mode 全部为 `off`，拒绝缺失、未知、shadow/enforce 和 migration evidence；Sidecar 不承载 CP7-A/CP7-B Task、Event 或 dispatch authority。 |
| 本地 candidate 没有 production admission，八项安全零值被静默假设 | 独立 CP7-local registry、append-only SQLite ledger、Ready epoch 与单向 candidate-invalid latch；每个 Ready epoch 的整分钟 attestation 连续，受控维护区间以 close/open chain 证明零入口，positive/gap 永久阻断，probe、rehearsal 与正式候选卷隔离。 |
| 无 Server Task 在 intent/Task insert 后崩溃而永久 nonterminal 或改派错误 Server | Task/Node 与 durable intent 原子绑定，Server/delete/dispatch 单赢家，确定性 dispatch-resume/terminal outbox、startup recovery 与 unknown/no-replay fence。 |
| image export 未真正可恢复，只命中 daemon cache | digest-pinned disposable DinD、全新 data-root 与 inner image/container=0 证明；隔离 load/start/cleanup，禁止同 daemon fallback。 |
| disposable daemon 扩大本地攻击面 | 优先 rootless/unprivileged；只有受限环境检查通过时才允许 candidate-scoped privileged fallback。fallback 无 host Docker socket/data-root/端口、inner network none、exports 只读并强制清理，但明确不构成对恶意镜像或宿主的安全隔离；不可启动、身份不符或清理不完整即 blocked。 |
| archive 不含 `config.yaml` | 从镜像移除 `COPY config.yaml`，stager 写入 named volume，`MAF_CONFIG_PATH` 指向只读 staged 文件。 |
| route assignment 把普通 Task 误判为 legacy MCP | 只使用 durable direct evidence；`mcp_execution_mode=legacy` 单独不足以失败。 |
| 日志扫描遗漏 startup 或把 user-scoped discovery 误判为 legacy | 新卷从 offset 0、quiesced inode/offset 连续窗口、只匹配 closed legacy event/source；constructor 由 wiring test证明。 |
| 人工批准绑定了旧候选或来自引用文字 | 只接受新的顶层 exact user event；immutable approval 绑定 commit、manifest、三个 image、checklist、audit tail；任一变化重新测试。 |
| claim 已落盘但 current 仍 pending，导致重复消费或永久卡住 | 固定 claim→claimed receipt→current 顺序；只对唯一、完整绑定且无 progress/result 的 claim-only 状态允许确定性补投影；逐 fsync 边界 crash 回归。 |
| PostgreSQL/旧开发数据被配置文件意外启用 | canonical backend 显式 SQLite/bridge-off 并拒绝全部 DSN；真实 PostgreSQL 只在 digest-pinned、internal-only、fresh-volume 的 disposable validation profile 中运行，一次性 credential 不进入工件。 |
| 删除旧协议兼容实现 | CP7-B symbol-level preserve list与五版本回归门禁。 |
| CP7-B crash 后从漂移 worktree、错误 commit 或错误 image 继续 | 固定 progress chain；`C_B` 唯一父为 `C_A`，每步重验 patch/tree/archive/image/test binding，漂移或 fork 即 aborted/fail closed。 |
| 测试污染既有开发数据 | 独立 project name、全新卷和专用测试用户；清理只针对该 project。 |
| 本地结果被误报为生产完成 | 所有文档和状态明确 `main`/development only；production evidence 始终为空。 |

## 文档与状态同步

CP7-A 实施时更新 Compose、Dockerfile、人工测试说明、对应 AGENTS 索引和 CHANGELOG，状态保持“开发候选，等待人工验收”。

CP7-B 实施时更新 Phase 3 PRD、Runbook、docs 索引和 CHANGELOG：

- 原 CP-8 物理删除合并到本项目的 CP7-B；
- 记录定时观察窗由项目负责人针对 `main` 明确取消；
- 把绑定候选的人工确认记录为代码退役门禁；
- 明确结论只发生在 `main`，不宣称已部署到 `prod`。

## 完成标准

### CP7-A 完成

- `B_L` 与 `C_A` clean-archive 三镜像、export SHA、fresh isolated DinD load/start/cleanup、同卷 sentinel 数据连续性、legacy-on smoke 与完整回滚演练已通过。
- 三服务 Compose、one-shot input stager 与 clean-archive 三镜像可构建；宿主 Compose 只按已与 isolated restore 结果匹配的 immutable image ID 启动并健康。
- actual Sidecar binary/trust、staged owner/mode/path、三个 Rust authority mode 全部 `off`、SQLite/DSN isolation、disposable PostgreSQL validation、canonical Rust gate、assembly-off、user-scoped、恢复、安全、历史兼容、前端和 manifest 检查全部通过。
- CP7-local safety registry/ledger/Ready epoch/candidate guard 完整；权威 candidate 的全部 Ready epoch 内八项 attestation 连续且 violation/gap 全零，受控维护区间具有完整 close/open 零入口证明，`invalid_latched=false`；probe 与 rehearsal 结果来自独立卷。
- 普通无 Server 对话正常；显式 MCP 无 Server 通过 durable intent 与 SQLite 原子终态固定不可用，崩溃恢复后两个 durable event 仍有序且只插入一次；SSE 重连重投按 event ID 幂等。
- per-candidate immutable manifest 原子生成，`current.json=pending_manual_approval` 及 immutable state receipt 已发布；candidate binding 已包含候选 commit、manifest、三个 image/export/isolated restore、PostgreSQL validation、Rust gate、安全 ledger 与人工清单身份。
- legacy global runtime 源码仍存在。
- 实施已停止并等待项目负责人回复“可以退役”。

### CP7-B 完成

- 已从新的顶层 exact“可以退役”生成并重验 immutable approval 与一次性 CP7-B claim；legacy audit detector 与 CP7-local safety ledger combined result 全零，current lifecycle 已从 `pending_manual_approval` 走到 `claimed`。
- legacy global Runtime 的可执行代码和重新装配入口已删除，并以唯一父为 `C_A` 的 `C_B` commit/tree 和 canonical deletion patch 固化。
- tombstone 对旧环境变量任意 presence 固定拒绝。
- user-scoped MCP、协议兼容、恢复、安全、历史只读兼容、disposable PostgreSQL、canonical Rust gate 和 Docker 回归全部通过。
- `git archive C_B` 构建的三个镜像均写 OCI revision `C_B`，已导出、在 fresh isolated DinD 中 load/start/cleanup，并以 exact IDs 健康启动 Compose。
- 固定 CP7-B progress chain 完整；`B_L` 与 `C_A` 回滚工件仍保留、hash 正确且 restore smoke 可重复；immutable `cp7b-result.json state=completed` 绑定 `C_A/C_B`、patch/archive/images/exports/tests，result receipt 后 current lifecycle 已转换到 `completed`。
- `prod` 未被修改、切换或部署。
- Git 提交边界不包含本地敏感文件、`docker_cmd.md`、未跟踪报告或无关用户改动。
