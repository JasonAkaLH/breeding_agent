# CentOS 容器部署适配评估报告

评估日期：2026-10-13。报告已完成，目标环境尚未确定，部署适配和 CentOS 实机验收尚未开展。

本报告评估在 CentOS 主机上使用容器部署现有系统所需的改动和工作量。CentOS 版本待定，具体版本在服务器选型时确定。本次只整理文档，不修改代码或执行部署。

## 1. 结论与估算条件

如果采用仍受支持的 CentOS Stream 9/10、Docker 和现有 `linux/amd64` 镜像，预计需要 **3 至 5 人日**。从已检查的代码看，更换宿主机发行版暂不需要修改核心 Agent、API 或前端业务逻辑。工作主要集中在宿主机配置、挂载权限、生产参数和功能验收，改动规模较小。

这个估算来自源码检查和官方资料，尚未经过目标服务器实测。现有开发 Compose 也需要按生产要求配置后才能使用。估算采用以下条件：

- 使用单台 x86_64 服务器，操作系统和容器运行时仍受支持，部署人员具备必要的管理权限。
- 保留现有 Ubuntu/Debian 应用镜像，在目标机运行已经构建并核验的版本。
- 沿用兼容的 PostgreSQL 服务、现有外部 Skill 和模型/MCP 服务，不同时迁移整个数据库或升级业务功能。
- 能从仓库获取镜像，或已有镜像导入渠道，无需额外准备一套完全离线的构建依赖。
- 测试环境、代表性数据、网络和账号已经准备好。安全审批及其他外部等待时间不计入人日。

1 人日按 8 小时有效工作计算。满足上述条件时，一名熟悉项目的工程师可以按约一周安排。目标版本、硬件和容器运行时确定后，再确认最终排期。

## 2. 现有部署方式与检查范围

本次检查基于 `main@ebcd5d3`，并只读对比了本地 `prod@4bddf85`。两分支的 `Dockerfile`、`docker-compose.yml`、`requirements.txt`、`native/Cargo.toml`、`native/rust-toolchain.toml` 和 `docker/nginx.conf` 一致。此次对比只覆盖这六个文件，不能据此判断全部业务代码或实际部署状态相同。生产部署仍需确定具体的 `prod` 提交版本，并固定镜像摘要（digest）。

| 部分 | 当前实现 | 对部署的影响 |
|---|---|---|
| 后端 | Ubuntu 22.04、Conda Python 3.13.13、FastAPI/Uvicorn、tini | 复用镜像即可保留 Python 和系统依赖 |
| 前端 | Node 25 负责编译，Ubuntu 22.04 + Nginx 提供静态页面 | 目标宿主机不需要安装 Node |
| Runtime Sidecar | 使用 Rust 1.95 构建，在 Debian Bookworm slim 中运行 | 宿主机不需要安装 Rust 工具链 |
| Skill | 从外部目录只读挂载；镜像包含 R、jsonlite、OpenMP/C++ 运行库 | 基础环境可以复用，实际部署的 Skill bundle 仍需测试 |
| CPU 架构 | 三个 Compose 服务都指定 `linux/amd64`，Miniforge 安装器也固定为 x86_64 | ARM 服务器需要另做适配 |
| 持久化 | Backend runtime、Sidecar 数据和 socket 使用 Docker 命名卷（named volume） | 需要检查数据保留、卷权限和跨容器 socket 通信 |
| 配置与密钥 | 配置、主密钥和 Skill 保存在镜像之外 | 新宿主机需要提供正确的路径、文件内容和权限 |
| 环境配置 | 根目录 Compose 固定使用 `dev` 和 SQLite | 这份开发配置不能直接用于生产 |

以上信息来自 [Dockerfile](../Dockerfile)、[Compose](../docker-compose.yml)、[Python 依赖](../requirements.txt) 和 [Rust workspace](../native/Cargo.toml)。

应用在容器内使用镜像提供的用户态环境，同时共享宿主机内核。所以，CentOS 宿主机可以继续运行现有 Ubuntu/Debian 镜像。旧内核、容器运行时、SELinux 和存储驱动是否兼容，仍需在目标机上验证。

## 3. 版本与容器运行时的选择

| 候选环境 | 建议与限制 | 是否适用 3 至 5 人日估算 |
|---|---|---|
| CentOS Stream 10 + Docker | 新建环境优先评估。维护至 2030-05-31；x86_64 版本要求 x86-64-v3，需要检查物理 CPU 或虚拟机提供的 CPU 特性 | 满足硬件等条件时适用 |
| CentOS Stream 9 + Docker | 可以作为候选。维护至 2027-05-31，选用时需要安排后续系统升级 | 满足其他条件时适用 |
| Stream 9/10 + Podman | 需要处理 DNS、Compose provider、权限和服务管理差异 | 预计总计 5 至 8 人日 |
| CentOS Linux 7/8、CentOS Stream 8 | 已停止维护，不建议用于新部署；如果最终只能使用这些版本，需要重新评估旧内核和运行时 | 不适用 |

Docker 官方 CentOS 安装文档目前支持 Stream 9/10，并说明了 Engine、containerd 和 Compose 插件的安装方法。[Docker 官方支持范围](https://docs.docker.com/engine/install/centos/)

维护日期和 Stream 10 的硬件要求见 [CentOS 下载页](https://www.centos.org/download/) 与 [Stream 10 发布说明](https://www.centos.org/centos10/)。旧版本中，CentOS Linux 7 于 2024-06-30、Stream 8 于 2024-05-31 停止更新；CentOS Linux 8 于 2021-12-31 结束维护。[7/Stream 8 公告](https://blog.centos.org/2023/04/end-dates-are-coming-for-centos-stream-8-and-centos-linux-7/)、[Linux 8 公告](https://www.centos.org/centos-linux-eol/)

上述官方资料已在评估时核对。正式部署前还需确认当时的支持范围，记录实际安装的 Engine、containerd、runc 和 Compose/provider 版本。CentOS 最终版本仍待选定。

## 4. 需要适配和检查的地方

下列事项都需要在部署前检查并验收。目标环境已经满足要求的部分，验证通过即可，无需为了适配而修改配置。

| 事项 | 需要做的工作 | 可能修改的位置 | 完成标准 |
|---|---|---|---|
| 容器运行时与镜像 | 检查 CPU、内核、存储驱动和 Docker/Compose 版本组合。固定三类镜像的 digest，验证拉取或导入 | 宿主机软件、部署参数 | 镜像架构正确，三个容器按依赖顺序启动并通过健康检查 |
| SELinux 与文件访问 | 当前 bind mount 未显式设置 SELinux 标签。按目标策略检查配置、密钥、Skill 和共享 socket 的访问权限 | 应用目录的 SELinux 标签，必要时调整挂载参数 | 在目标 SELinux 策略下正常访问，保留只读挂载和密钥权限 |
| 外部路径与 Skill | 替换 Compose 中写死的开发 Skill 路径。确认配置、密钥、Skill 版本及 bundle digest，核对实际挂载位置 | 目标环境的部署配置或覆盖文件 | 配置文件存在且可读，Skill 能被扫描到，代表性 Skill 执行成功 |
| 生产状态与准入 | 开发 Compose 使用 SQLite。生产需要配置 PostgreSQL、正确的数据库角色、MCP 准入和 Sidecar 信任信息 | 生产参数、现有运维流程 | 保留现有检查要求，生产启动检查全部通过 |
| 端口、网络和代理 | 当前映射为 `51888:8000`、`51999:80`，未限定宿主机绑定地址。检查入口、DNS、防火墙，以及模型/MCP/数据库连接 | 宿主机网络、发布端口，必要时调整入口代理 | 访问范围符合要求，出站连接可用，SSE 和长任务正常 |
| 持久化与运维 | 检查卷空间、UID/GID、文件锁和 socket。验证重建、重启，配置自动启动、日志保留和恢复流程 | 数据卷、宿主机服务管理、部署说明 | 重建或重启后数据和密钥可用，恢复及版本回滚演练通过 |

### 4.1 SELinux 与密钥

挂载设置见 [Compose 的 volumes 配置](../docker-compose.yml)。SELinux 的 `z` 用于多个容器共享内容，`Z` 用于私有内容，应按实际共享关系选择，也可以在宿主机预先设置应用目录标签。标签会作用于宿主机文件，操作范围应限于应用目录。访问问题应通过应用目录标签和必要权限处理，保留 SELinux，避免放宽其他目录的权限。[Docker bind mount 与 SELinux 说明](https://docs.docker.com/engine/storage/bind-mounts/#configure-the-selinux-label)

[主密钥加载器](../src/integrations/master_key.py) 要求文件为普通文件、硬链接数为 1，权限为 `0400` 或 `0600`，还会检查路径和内容格式。迁移时需要保留与现有数据匹配的密钥，不能用新生成的密钥替代。若使用 rootless 容器，还要检查 UID/GID 映射后进程能否读取密钥。

### 4.2 生产配置

[状态平台配置代码](../src/state/runtime_factory.py) 禁止生产模式使用 SQLite 作为主状态后端。因此，除了把 Compose 的 `MAF_API_ENV` 改为 `prod`，还需配置生产 PostgreSQL、配置文件和密钥，满足现有 MCP 与 Sidecar 要求，并核对将要部署的版本。

Sidecar 自身仍可保留 SQLite 数据文件，主状态后端的限制不要求将其一并改为 PostgreSQL。仅更换宿主机发行版也不需要修改业务数据库 schema。如果同时升级应用或迁移数据库，这部分工作应单独估算。首次启用生产 MCP 的流程见 [MCP 首次生产启用手册](runbooks/mcp-first-production-enablement.md)。

### 4.3 网络与 SSE

[Nginx 配置](../docker/nginx.conf) 已关闭 API 代理缓冲，读超时为 3900 秒。如果新环境增加统一网关或外层反向代理，需要保留 `/seedpilot/` 路径映射，检查流式响应、上传限制、长任务以及客户端断线后的行为。

Docker 会创建转发和防火墙规则，并与 firewalld 集成。配置完成后应实际测试访问范围，单看 firewalld 中某个端口的设置不足以判断容器是否可访问。[Docker 防火墙文档](https://docs.docker.com/engine/network/packet-filtering-firewalls/)

## 5. 哪些条件会增加工作量

| 条件 | 追加工作 | 对工期的影响 |
|---|---|---|
| 选择 Podman | 调整 Nginx 中针对 Docker 的 DNS 设置。检查所用 Compose provider 的健康检查依赖、挂载和启动行为；使用 rootless 时，再检查 UID/GID、密钥和 socket 权限 | 比 Docker 方案增加 2 至 3 人日，总计 5 至 8 人日 |
| 最终指定 CentOS 7 等旧系统 | 检查内核、历史运行时版本、seccomp、存储驱动、镜像运行情况和维护条件 | 先用 1 至 2 人日验证可行性，通过后再估算完整迁移工期 |
| 服务器是 ARM/aarch64 | 调整固定 amd64 的部署配置和 Miniforge 安装器，重新构建三类镜像，验证 Rust、Python/R 和外部 Skill | 另行估算；amd64 模拟运行不能代替生产验收 |
| 要求镜像内部也采用 CentOS 系 | 更换基础镜像、系统包安装方式和依赖来源，重新构建并回归测试 | 另行评估，已超出本报告复用现有镜像的范围 |
| 完全离线，或代理/证书受限 | 准备 RPM、镜像导入和仓库/证书配置。如果构建也必须离线，还需准备 Python、npm、Cargo、R 等依赖 | 按实际网络限制另行估算 |
| 迁移数据库、历史文件或大量数据 | 安排一致性备份、传输、停写、恢复和回滚 | 按数据量、停机窗口和恢复要求另行估算 |
| 外部 Skill 新增本地依赖或 GPU 需求 | 补充 R/Python 包、系统命令、驱动或容器设备配置，测试相关 Skill | 实际使用到这些依赖时才计入 |

Podman 需要特别检查 Nginx 的 `resolver 127.0.0.11` 设置，这是 Docker 的内置 DNS 地址。更换运行时后，应使用实际网络的解析配置，并测试 Backend 容器重建后能否继续通过服务名访问。[Docker DNS 文档](https://docs.docker.com/engine/network/#dns-services)

`podman compose` 会调用外部 Compose provider，兼容性取决于实际使用的 provider 及其版本。需要用本项目的配置验证启动行为，不能只把命令中的 Docker 换成 Podman。[Podman Compose 文档](https://docs.podman.io/en/latest/markdown/podman-compose.1.html)

## 6. 可以沿用的代码与依赖

- Agent、API、前端和 Skill 调用链可以先沿用。目标机回归测试如果发现具体缺陷，再针对缺陷修复。
- Dockerfile 中的 `apt-get` 以及 Python/Rust 版本可以保留。它们用于镜像内的环境，不需要因宿主机发行版变化就修改包管理器或降级版本。
- PyO3 wheel 的兼容性应按实际加载它的环境判断。[Rust CI](../.github/workflows/rust-quality.yml) 当前生成 `manylinux_2_35` wheel，要求该环境的 glibc 至少为 2.35。使用现有容器时，应检查容器内的 glibc，不能直接套用宿主机的版本。后端镜像默认关闭这些 PyO3 模式，部署迁移也不需要开启它们。[Python wheel 兼容规范](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/#manylinux)
- 当前无需为主体应用安排 GPU 适配。[项目运行基线](../README.md) 说明 Rust/PyO3 与 Sidecar 没有直接链接 CUDA；外部 Skill 或独立推理服务如果需要 GPU，再核实相应设备要求。

镜像与内核的兼容性仍需实测。验收时应固定镜像 digest，并在选定的宿主机上运行；后续镜像版本也不能直接沿用这次的兼容性结论。

## 7. 工作量与交付内容

以下按 Stream 9/10 + Docker、满足第 1 节条件估算，已经包含第 4 节的适配工作，无需重复累加。

| 阶段 | 工作内容 | 交付内容及验证结果 | 人日 |
|---|---|---|---:|
| 环境确认与镜像准备 | 检查系统、CPU、内核、容器版本、存储和镜像访问；确认目标分支对应的镜像 digest | 环境清单、固定的三类镜像及最小启动测试结果 | 0.5 至 1 |
| 挂载与权限适配 | 设置配置、密钥和 Skill 路径，处理 SELinux、数据卷与 socket 权限 | 目标部署配置、权限检查记录 | 0.5 至 1 |
| 生产参数与网络配置 | 配置数据库及现有准入信息，检查端口、DNS、防火墙、代理和外部服务连接 | 生产配置检查、连通性测试结果 | 0.5 至 1 |
| 功能与运维验收 | 回归代表性业务，验证 Skill/MCP/SSE、容器重建、主机重启、恢复和版本回滚 | 验收记录、部署与回滚说明 | 1.5 至 2 |
| 合计 | 小规模部署适配 | 环境准备好后按约一周安排 | 3 至 5 |

目前预计无需修改业务源码，主要调整目标环境的部署配置、必要的宿主机预检和部署文档。是否需要改代码，以目标机测试结果为准。如果选择 Podman，已知需要检查并适配 Nginx 解析配置。

现有 [部署合同测试](../tests/deployment/test_user_mcp_cp7a_lean_compose.py) 能检查 Compose/Dockerfile 的静态约束，但使用的是开发配置，无法代替 CentOS 生产环境验收。后续调整部署配置时，应同步相应检查，并保持原有开发环境可用。

## 8. 验收要求

以下检查作为部署验收依据，本次尚未执行。

1. 确认三个镜像的架构、来源和 digest；容器健康检查通过，Runtime Sidecar 的语义探测与 socket 通信正常。
2. 登录、创建会话、模型响应、SSE、取消任务、上传和结果下载均正常，入口路径符合现有 `/seedpilot/` 配置。
3. 代表性 Python Skill 和 R 统计/田间分析 Skill 运行正常；核对外部 bundle revision/digest、输入及结果文件，记录实际缺失的依赖。
4. 模型服务、PostgreSQL、只读业务数据源和 MCP 连接正常；在具备测试条件的环境中验证 MCP 授权、调用和恢复流程。
5. 应用能在目标 SELinux 策略和文件权限下运行，主密钥只读挂载，并与已有数据匹配。
6. 容器重建、宿主机重启后，历史数据、结果文件、配置和凭据仍可用；启动顺序及服务自动恢复符合部署要求。
7. 在测试环境完成数据恢复和固定镜像版本的回滚演练，写清回滚范围。数据库恢复需要独立方案，不能只回退镜像版本。
8. 核对生产分支、数据库角色，满足现有 MCP/Sidecar 准入条件。页面能打开或 `/api-doc` 返回成功，只能说明部分服务可用，不能作为全部验收结果。

## 9. 还需要确认的信息

| 待确认项 | 对评估的影响 |
|---|---|
| CentOS 最终版本、内核和维护方式 | 判断能否采用受支持环境的估算，还是需要先验证旧系统 |
| CPU 架构及指令集、内存、磁盘和文件系统 | 判断能否直接使用 amd64 镜像，容量和存储是否需要调整 |
| Docker 或 Podman，是否要求 rootless，是否允许安装运行时 | 确定部署参数、DNS、权限和管理方式 |
| 镜像仓库、数据库、模型及 MCP 的网络和证书条件 | 判断是否需要额外网络配置或离线交付工作 |
| 是否迁移历史数据，当前生产版本和 schema 状态 | 明确迁移范围，将应用升级与操作系统适配分别估算 |
| 外部 Skill 的最终版本和完整依赖 | 判断是否需要补充依赖和专项测试 |

本次已经核对仓库的构建与部署配置、相关源码、六个文件的分支差异和官方资料。目标 CentOS 主机配置尚未提供，完整外部 Skill 仓库也未检查。目标机容器运行、业务测试和恢复演练均未开展，部署兼容性仍待实测确认。

此次工作只涉及评估文档、文档索引和变更记录。业务代码、Dockerfile、Compose、部署命令、数据库、容器、外部 Skill 及生产环境均保持原样，部署适配尚未开始。
