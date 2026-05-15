# 全局变更日志

本文件是 **multi_agent_framework 仓库的总变更记录**，按时间倒序汇总代码、文档以及仓库其他路径的重要变更。

面向全体协作者——**包括人类开发者与任意 AI 编码助手**。用于快速了解当前工程状态、最近改动，以及跨模块影响面，不依赖任何工具本地记忆。

> 语言：全部条目使用中文。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

---

## [Unreleased]

### 2026-05-15 — 推进 PRD03 Runtime sidecar mTLS transport

- 在 PRD03 Unix socket 远端全绿后继续同一 PRD：`maf_runtime_sidecar` 启用 tonic `tls-ring` transport feature，`RuntimeSidecarServeConfig` 新增 server certificate / private key / client CA mTLS 配置；非 loopback TCP bind 只有在完整 mTLS 配置存在时才允许，Unix socket 与 mTLS 组合继续 fail-closed；随 rustls/ring 传递依赖同步将 `ISC` 加入 `native/deny.toml` 允许许可证集合。
- `maf-runtime-sidecar --serve <addr> --sqlite <path> --tls-cert <cert> --tls-key <key> --client-ca <ca>` 或 server-side `MAF_RUNTIME_SIDECAR_TLS_CERT_PATH` / `MAF_RUNTIME_SIDECAR_TLS_KEY_PATH` / `MAF_RUNTIME_SIDECAR_TLS_CLIENT_CA_PATH` 可由外部进程管理器启动 mTLS gRPC sidecar；生产请求路径仍不由 Python spawn / restart / kill sidecar。
- `RuntimeSidecarGrpcClient` 支持 `https://host:port` mTLS endpoint，使用 Python stdlib `ssl` 包装现有 HTTP/2 unary client，不新增 `grpcio` 等 Python 依赖；`build_api_runtime()` 可从 `MAF_RUNTIME_SIDECAR_TLS_CA_PATH` / `MAF_RUNTIME_SIDECAR_TLS_CERT_PATH` / `MAF_RUNTIME_SIDECAR_TLS_KEY_PATH` / `MAF_RUNTIME_SIDECAR_TLS_SERVER_NAME` 装配 client-side mTLS 配置。
- 补充 mTLS 配置与真实 binary 集成测试：Rust config 测试覆盖 public bind 只有完整 mTLS 路径才放行，Python API 测试覆盖部署环境变量透传，integration 测试用临时 OpenSSL fixture 启动 Rust sidecar 并通过 Python mTLS client 完成 version / event append RPC。
- 本轮本地验证通过：`cd native && cargo test -p maf_runtime_sidecar --test runtime_sidecar_grpc serve_config_accepts_public_bind_only_with_complete_mtls_paths --all-features`、`conda run -n multi_agent python -m unittest tests.integrations.test_runtime_sidecar_grpc_client.RuntimeSidecarGrpcClientIntegrationTest.test_python_client_connects_to_rust_sidecar_binary_over_mtls`、`cd native && cargo fmt --check && cargo +stable clippy -p maf_runtime_sidecar --all-targets --all-features -- -D warnings && cargo test -p maf_runtime_sidecar --all-features && cargo check --workspace --all-targets --all-features`、`cd native && cargo test --workspace --all-features`、`cd native && cargo +stable clippy --workspace --all-targets --all-features -- -D warnings`、`conda run -n multi_agent python -m unittest tests.integrations.test_runtime_sidecar_grpc_client`、`conda run -n multi_agent python -m unittest tests.api.test_runtime_sidecar_contract`、`conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'`、`conda run -n multi_agent python -m compileall -q src/api src/storage tests/api tests/storage tests/integrations`、`git diff --check`。

### 2026-05-15 — 推进 PRD03 Runtime sidecar Unix socket transport

- 按 `docs/prd/rust` 顺序在 PRD02 远端全绿后继续 PRD03：`maf-runtime-sidecar --serve unix:///absolute/path --sqlite <path>` 现在可通过 Rust `RuntimeSidecarServeConfig` 进入 Unix domain socket 监听，TCP public bind 仍在缺少 production mTLS / 内网身份前 fail-closed。
- `RuntimeSidecarGrpcClient` 支持 `unix:///absolute/path` endpoint，复用现有 h2c unary client、endpoint allowlist、config source 与 compatibility handshake，不新增 Python 依赖；loopback TCP 路径保持不变。
- 补充真实外部 sidecar binary 集成测试：同一测试文件覆盖 loopback TCP 与 Unix socket 两种 transport；同时加固 loopback 端口启动重试，避免临时端口竞争导致的偶发 `Connection refused`；Python endpoint allowlist 也新增非绝对 Unix socket endpoint 负向校验。
- 同步更新 `README.md`、`AGENTS.md`、`docs/prd/rust/README.md` 与 `docs/prd/rust/03-DispatcherStoreEventSidecarPRD.md`，将 Unix socket 从待完成项移动到当前 PRD03 实现基线；production mTLS、shadow promotion 与 Python legacy 写路径最终下线仍未完成。
- 本轮本地验证通过：`conda run -n multi_agent python -m unittest tests.integrations.test_runtime_sidecar_grpc_client`、`conda run -n multi_agent python -m compileall -q src/storage tests/integrations`、`cd native && cargo fmt --check && cargo +stable clippy -p maf_runtime_sidecar --all-targets --all-features -- -D warnings && cargo test -p maf_runtime_sidecar --all-features && cargo check --workspace --all-targets --all-features`、`git diff --check`、`conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'`、`conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'`、`cd native && cargo test --workspace --all-features`。
- 推送提交 `7024bd7` 后，GitHub Actions run `25925601055` 已在 Ubuntu 22.04 x86_64 上全绿：Rust PRD01 quality gates（fmt、Rust 1.95 clippy、workspace cargo test / nextest、audit、deny、workspace coverage、coverage threshold runner、fuzz harness 编译、provenance self-test）、bounded fuzz smoke，以及 Python 3.13 / `manylinux_2_35` Rust PyO3 wheel smoke（Skill Runtime + Core/Lifecycle wheel build、SBOM / provenance / manifest、installed-module contract smoke、artifact upload）。本轮主 quality job 的 `Install Rust quality tools` 耗时约 10 分 21 秒；仍按既定门禁执行，未削弱 Ubuntu 生产基线验证。

### 2026-05-15 — 推进 PRD02 Core/Lifecycle PyO3 facade

- 按 `docs/prd/rust` 顺序在 PRD01 远端全绿后进入 PRD02：新增 `maf_core_lifecycle_pyo3` workspace crate，暴露 Core / Lifecycle contract JSON 与 lifecycle transition、target、cancel-node、late-result JSON bridge；crate 使用 `abi3-py313`，只作为 CI / 部署预构建 wheel source，不在 runtime 路径编译或下载。
- Core / Lifecycle contract artifact 新增 `supported_features`，Python facade 增加 `MAF_RUST_CORE_MODE` / `MAF_RUST_LIFECYCLE_MODE=off|shadow|enforce` 与 `MAF_CORE_LIFECYCLE_PYO3_MODULE` 加载门禁；`enforce` 下缺少预构建 PyO3 module 或 component / version / hash / features 不匹配会 fail closed，`shadow` 继续以 checked-in Rust contract artifact / Python facade 结果为用户可见路径。
- Lifecycle facade 在 `enforce` 下通过 PyO3 JSON bridge 调用 Rust transition 判定、transition target、cancel-node target 与 late-result policy；非法 / malformed Rust response 映射为 typed lifecycle contract error，不回退 Python 状态机。
- Rust quality workflow 与本地 gate plan 新增 Core/Lifecycle PyO3 wheel smoke；Ubuntu 22.04 x86_64 / Python 3.13 CI wheel job 会为 `maf_core_lifecycle_pyo3` 生成 SBOM / provenance / manifest 并运行 installed-module contract smoke。当前本地已完成 macOS 开发 wheel build/import smoke；最终生产证据仍以后续 Ubuntu CI artifact 与 allowlist / provenance 门禁为准。
- 推送提交 `d45ffa0` 后，GitHub Actions run `25923495746` 已在 Ubuntu 22.04 x86_64 上全绿：Rust PRD01 quality gates（fmt、Rust 1.95 clippy、workspace cargo test / nextest、audit、deny、workspace coverage、coverage threshold runner、fuzz harness 编译、provenance self-test）、bounded fuzz smoke，以及 Python 3.13 / `manylinux_2_35` Rust PyO3 wheel smoke（Skill Runtime + Core/Lifecycle wheel build、SBOM / provenance / manifest、installed-module contract smoke、artifact upload）。主 quality job 的 `Install Rust quality tools` 本轮耗时约 10 分 42 秒，CI 加速仍应通过缓存 / 预装 action 实现，不能削弱质量门禁。
- 本轮本地验证通过：`conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'`、`conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'`、`conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'`、`conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'`、`conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'`、`conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'`、`conda run -n multi_agent python -m compileall -q src/core src/lifecycle tests/core tests/lifecycle tests/integrations scripts`、`cd native && cargo fmt --check && cargo +stable clippy --workspace --all-targets --all-features -- -D warnings`、`cd native && cargo test --workspace --all-features`、`cd native && cargo check --workspace --all-targets --all-features`、`conda run -n multi_agent python scripts/run_rust_coverage_thresholds.py --run`、`git diff --check`、`maturin build` + installed `maf_core_lifecycle_pyo3` contract smoke。

### 2026-05-15 — 补齐 Rust release provenance 生成入口

- 根据 GitHub Actions run `25919136293`，PRD01 主 quality job 已通过 fmt / clippy / cargo test / nextest / audit / deny / workspace coverage，但在 `python scripts/run_rust_coverage_thresholds.py --run` 的安全敏感 crate 阈值处失败：`maf_skill_runtime` 包级 line coverage 85.51% 未达 90%；本轮补齐 Skill Runtime policy / sandbox preflight / stdio helper / binary contract export 覆盖，并将 lingering descendant stdio 测试改为验证有界 drain 而非依赖平台进程组 kill 时序。
- 推送提交 `dc0437d` 后，GitHub Actions run `25920992180` 已在 Ubuntu 22.04 x86_64 上通过 PRD01 三个 job：主 quality gates（fmt、Rust 1.95 clippy、workspace cargo test / nextest、audit、deny、workspace coverage、80/90 coverage threshold runner、fuzz harness 编译、provenance self-test）、bounded fuzz smoke，以及 Python 3.13 / manylinux_2_35 Skill Runtime PyO3 wheel smoke（含 SBOM / provenance / manifest artifact 上传）。主 job 的 `Install Rust quality tools` 仍耗时约 10 分 49 秒，后续若继续优化 PRD01 反馈速度，应优先补 CI 工具缓存 / 预装 action，而不是改弱质量门禁。
- 同一 coverage threshold runner 后续暴露 `maf_mcp_runtime` line coverage 81.65% 未达 90%；本轮补齐 MCP Runtime health / compatibility mismatch / typed error table / JSON-RPC fail-closed / sanitizer size limit / task cancellation / binary contract export 覆盖。本地 `python scripts/run_rust_coverage_thresholds.py --run` 已通过：workspace line coverage 87.74%，`maf_skill_runtime` 92.88%，`maf_mcp_runtime` 100%，其余安全敏感 crate 均超过 90%。本机 `cargo clippy` 的 1.95 macOS component wrapper 不可用，已用 `cargo +stable clippy -p maf_skill_runtime -p maf_mcp_runtime --all-targets --all-features -- -D warnings` 预扫；最终 clippy 仍以 Ubuntu CI 1.95 run 为准。
- 扩展 `scripts/rust_artifact_provenance.py`，新增 `write-sbom` 与 `write-provenance` 子命令，可从 `cargo metadata` 与预构建 artifact 生成脱敏 SBOM / provenance JSON，再由既有 `generate` 命令汇总 checksum、Cargo.lock digest、SBOM digest、provenance digest 与 contract/proto hash。
- 更新 Rust quality GitHub Actions 的 Ubuntu 22.04 x86_64 / Python 3.13 Skill Runtime PyO3 wheel job，在 `maturin` + `auditwheel check` 后生成 SBOM、provenance 与 manifest，并上传 wheel release evidence artifact；当前仍未宣称远端 CI 已实际产出或生产 allowlist 已验证。
- Rust quality workflow 的 push 触发分支补充 `rust_branch`，避免开发分支验证推送后没有远端 CI run 可观察；真实 GitHub Actions 通过状态仍以后续 run 结果为准。
- 根据远端 GitHub Actions run `25912752831` / `25913275197` 的失败日志，修复 `cargo install cargo-fuzz --locked` 在 Ubuntu 22.04 nightly 2026-05-15 上因 locked `rustix 0.36.5` / `rustc_*` 保留 attribute 失败的问题，改为安装顶层固定的 `cargo-fuzz 0.13.1` 并让 Cargo 解析兼容依赖；同时将 bounded fuzz smoke 显式改为 `cargo +nightly fuzz run`，避免 `native/rust-toolchain.toml` 的 stable override 导致 `-Zsanitizer` 被稳定版 rustc 拒绝。上述 run 的 PyO3 Ubuntu wheel evidence job 已通过，但整体 run 仍需后续重新触发验证。
- 根据后续 Rust quality 主 job 失败日志，补齐主 job 的 Python 3.13 / `PYO3_PYTHON=python` 配置，避免 workspace `cargo clippy` / `cargo test` 构建 `abi3-py313` PyO3 crate 时落到 Ubuntu runner 默认 Python 3.10；同时修复 `maf_runtime_store` 在 Rust 1.95 `-D warnings` 下触发的 `clippy::collapsible_if`。
- 根据后续 Rust quality 主 job 的新失败日志，继续修复 `maf_skill_runtime` 在 Rust 1.95 `-D warnings` 下触发的两处 `clippy::collapsible_if`（Rust adapter metadata 校验与 limited reader 错误记录路径），并同步前置修复 `maf_runtime_sidecar` SQLite lease acquire 中同形态的可折叠 `if`，减少下一轮 clippy 逐 crate 暴露同类阻断；后续仍需以最新推送触发的 Ubuntu run 为 PRD01 远端 CI 通过依据。
- 根据后续 Rust quality 主 job 的新失败日志，修复 `maf_runtime_sidecar` SQLite lease query 的两处 `clippy::redundant_closure`，并同步前置修复本地扫描发现的 `maf_runtime_store` operation policy 同形态 map closure；后续仍以最新 Ubuntu run 全绿作为 PRD01 远端 CI 通过依据。
- 根据后续 Rust quality 主 job 的新失败日志，修复 `maf_skill_runtime` Skill Sandbox 测试 helper 的 `clippy::ptr_arg`：脚本写入 helper 参数从 `&PathBuf` 收敛为 `&Path`，并同步修复同目录 process 测试中的同形态 helper；后续仍以最新 Ubuntu run 全绿作为 PRD01 远端 CI 通过依据。
- 根据 GitHub Actions run `25916700013`，PRD01 主 quality job 已通过 `fmt` / `clippy` / `cargo test` / `nextest` / `audit`，新阻断收敛到 `cargo deny check` license policy；补充 `native/deny.toml`，显式允许当前 Rust 依赖树所需的 MIT / Apache-2.0 / Apache-2.0 WITH LLVM-exception / BSD-3-Clause / Unicode-3.0 / Unlicense / Zlib，并忽略本 workspace private/proprietary crate，避免 cargo-deny 默认无 allowlist 时拒绝全部依赖 license。
- 根据 GitHub Actions run `25917559050`，`cargo deny check` 已读取 `native/deny.toml`，但当前 cargo-deny 版本拒绝已移除的 `unlicensed` / `copyleft` / `allow-osi-fsf-free` / `default` 配置键；已按 cargo-deny 现行 license config 口径移除这些 deprecated keys，保留显式 `allow` 与 `[licenses.private] ignore = true`。
- 根据 GitHub Actions run `25918206355`，`cargo deny check` 已进入 license evaluation，剩余阻断为 workspace 自有 `maf_*` crate 被视为 unlicensed；按 cargo-deny 现行 `private.ignore` 语义，所有 Rust workspace crate 显式标记 `publish = false`，并将 deny 配置收敛为 `[licenses] private = { ignore = true }`，使 license gate 只审查第三方依赖。
- 交接口径：上一轮 Python 3.13 / `maf_runtime_store` 修复提交为 `2166488`，对应 GitHub Actions run `25914331342`；本轮继续按最新 clippy 失败日志修复并推送后，应优先查看最新 `origin/rust_branch` Ubuntu run 的 `Rust PRD01 quality gates / Ubuntu 22.04 x86_64`、`Rust bounded fuzz smoke / Ubuntu 22.04 x86_64` 与 `Skill Runtime PyO3 wheel smoke / Ubuntu 22.04 x86_64 / Python 3.13` 三个 job。若 run 失败，优先抓取失败 job 最后 50-100 行日志并按日志修复；若仍长时间停在 `Install Rust quality tools` 或 wheel build，下一步应优化 CI 工具安装 / 缓存策略，而不是跳过 PRD01 复用门禁。只有最新 Ubuntu run 全绿后，才可把 PRD01 远端 CI 证据标记为通过并继续补长时 fuzz、coverage artifact、release allowlist / promotion 证据。
- 补充 integration 回归测试和 README / AGENTS / Rust PRD 状态说明，继续明确真实 production provenance、coverage 报告、长时 fuzz、ops / promotion 证据仍是后续门禁。

### 2026-05-15 — 补充目标运行环境约束

- 更新 `README.md`、`AGENTS.md` 与 Rust PRD 状态说明，明确项目目标运行环境为 Ubuntu 22.04.5 LTS（`GNU/Linux 6.8.0-49-generic x86_64`）、CUDA 12.6 与 NVIDIA V100 GPU；当前本地环境仅作为开发期测试与回归验证环境，macOS wheel 不能替代生产 wheel / sidecar 证据。
- 将 Skill Runtime PyO3 wheel 的 CI / 生产验证目标收敛为 Ubuntu 22.04 x86_64 / Python 3.13 / `manylinux_2_35` / `auditwheel check`；当前 Rust policy wheel 与 sidecar 不直接链接 CUDA，后续如引入 GPU/native 推理依赖必须补 CUDA 12.6 / V100 build 与 runtime 验证。
- 新增 `scripts/rust_artifact_provenance.py`，为 Rust wheel / binary / image release gate 提供 checksum、Cargo.lock digest、SBOM、provenance 与 allowlist 的生成 / 校验 / fail-closed self-test；该脚本目前提供可执行门禁面与本地 self-check，真实 SBOM / provenance 产物仍需 CI / 部署流水线生成。

### 2026-05-15 — 建立 Rust Runtime contract/kernel 首批实现基线

- 按 Rust 化 PRD 顺序补齐 Ralph 计划门禁，并在 `native/` workspace 新增 `maf_core_types`、`maf_lifecycle`、`maf_runtime_store`、`maf_event_log`、`maf_task_dispatcher`、`maf_skill_runtime`、`maf_artifact_store`、`maf_auth_core`、`maf_data_access`、`maf_audit_sanitizer` 等首批 contract/kernel crates。
- Core / Lifecycle 现在由 Rust 导出的 contract artifact 驱动 Python enum 与 lifecycle transition 校验基线；Python 保留 import path 与薄 facade，新增 artifact 一致性与残留 transition 语义 guard。
- 新增 runtime / skill sidecar proto 与 contract artifact，补齐 dispatcher/store/event 写路径 fail-closed、Skill policy / sandbox、MCP JSON-RPC / sanitizer / bundle / task registry、安全热点 path/auth/readonly/audit 等 Rust kernel 单元测试。
- 更新 README、AGENTS 与 Rust PRD 状态，明确当前是 contract/kernel 基线，不宣称 production enforce、完整 sidecar、PyO3 wheel、coverage/fuzz/SBOM/provenance 或 Python legacy 全量下线完成。
- 进一步收口 Core/Lifecycle 与 MCP Python canonical 残留：mailbox timeout / cancel、task finalize cancellation、终态任务取消 no-op、interrupt cancel、active task lookup、interrupt reopen guard 与 MCP task terminal/status 语义改由 Rust contract artifact 驱动，Python 仅保留 facade / storage / client glue。
- Skill Runtime Python manifest / execution facade 开始消费 `maf_skill_runtime` Rust contract artifact，执行模式、默认执行模式、默认 answer_mode 与 `x_runtime.rust` adapter / forbidden authority metadata guard 不再由 Python 独立维护。
- Upload preview 限制与 MySQL readonly SQL / result shape guard 开始消费 Rust safety contract resource limits，补充 data-access / upload 安全 facade 回归测试。
- Runtime Store / Event Python facade 开始消费 `maf_runtime_sidecar` Rust contract artifact，事件追加写路径按 Rust `event_append` policy、`event_payload_bytes` resource limit 与 `event_log_payload_too_large` typed error fail-closed 拒绝超限 payload；事件回放读取按 Rust `event_replay` policy、`replay_page_events` / `replay_page_bytes` 与 `event_log_replay_page_exceeded` typed error 拒绝无界 replay，并新增按 Rust page limit 分页的 `list_event_page_for_task()` storage facade；API/SSE 历史回放改为分页读取，补充正常事件往返、追加超限、回放超限与跨页 API replay 回归测试。
- Runtime sidecar mode 开始由 Rust contract 的 `mode_env` 驱动；`MAF_RUST_EVENT_LOG_MODE=enforce` 且尚未配置 Rust sidecar client 时，事件追加会以 `event_log_unavailable` fail-closed，不再继续落回 Python SQLite legacy 写路径。
- Runtime Store task / node 写路径开始消费 Rust `task_submit` / `node_state_transition` policy；`MAF_RUST_RUNTIME_STORE_MODE=enforce` 且尚未配置 Rust sidecar client 时，task 与 node 写入会以 `runtime_store_unavailable` fail-closed，不再继续落回 Python SQLite legacy 写路径。
- Runtime Store task graph 写路径继续收口：task edge 写入消费 Rust `task_submit` policy，artifact 写入消费 Rust `node_state_transition` policy；`MAF_RUST_RUNTIME_STORE_MODE=enforce` 且尚未配置 Rust sidecar client 时，edge / artifact 写入同样以 `runtime_store_unavailable` fail-closed，避免图结构和节点产物继续写入 Python SQLite legacy path。
- Task dispatcher bundle revision pin / release 开始消费 Rust `bundle_revision_pin` / `bundle_revision_release` policy；`MAF_RUST_TASK_DISPATCHER_MODE=enforce` 且尚未配置 Rust sidecar client 时，Skill / MCP bundle revision retain / release 会以 `dispatcher_unavailable` fail-closed，避免继续写入 `ApiRuntime` 进程内 revision pin 残留。
- Cancellation token 写路径开始消费 Rust `cancellation_token_write` policy；`MAF_RUST_RUNTIME_STORE_MODE=enforce` 且尚未配置 Rust sidecar client 时，任务取消会在写入 `cancel_requested_at` 前以 `runtime_store_unavailable` fail-closed，避免继续用 Python legacy task row 表示 cancellation token。
- 新增不持有 Python lease 状态的 `RuntimeLeaseFacade`，lease acquire / renew / release 只消费 Rust `lease_acquire` / `lease_renew` / `lease_release` policy；`MAF_RUST_RUNTIME_STORE_MODE=enforce` 且尚未配置 Rust sidecar client 时，以 `runtime_store_unavailable` fail-closed，避免后续新增 Python lease dict/repository 残留。
- Runtime sidecar 写路径 enforce guard 收敛为 `ensure_sidecar_write_allowed()`，API bundle pin/release、lifecycle cancellation token、storage event / runtime-store writes 与 lease facade 共用同一 Rust contract policy / mode / typed-error 校验，减少 Python 分散特判。
- Runtime sidecar Python facade 新增 compatibility handshake 校验，按 Rust contract artifact 校验 `component`、`protocol_version`、`schema_hash`、`error_code_table_hash` 与 `supported_features`；不兼容时以 `runtime_store_protocol_incompatible` fail-closed，为后续正式 sidecar client 接入预置协议门禁。
- Runtime sidecar Python facade 新增 endpoint allowlist 校验，正式 client 接入前先拒绝非 Unix socket、非 loopback / 内网 IP、非显式 allowlist host 的 sidecar endpoint，并以 `runtime_store_unavailable` typed error fail-closed，避免公网 / 任意 service discovery 地址进入连接路径。
- Runtime sidecar contract 新增 `runtime_store_response_invalid` typed error，Python facade 新增 structured response envelope 校验；正式 client 消费 event cursor、lease、task / node / cancellation / bundle response 或 sidecar typed error 前，会先校验 operation、字段形状、错误码、category、retriable 与 safe metadata，校验失败 fail-closed 且不推进状态。
- Runtime sidecar contract 新增 Rust-owned retry policy（max attempts / backoff / jitter / same-sidecar / idempotency key），Python facade 新增 `build_sidecar_retry_plan()`；只有 retriable typed error、幂等 operation、同一 sidecar 且具备 idempotency key 时才生成有界重试计划，其他场景不自动重试。
- Runtime sidecar contract 补齐 PRD 冻结的 max-in-flight 公式参数与 task submit / state transition / event append / lease / event replay deadline hard caps，Python facade 的 `runtime_sidecar_max_in_flight()` 只消费 Rust artifact 计算并发上限。
- Runtime sidecar contract 新增 config / identity policy 与 `runtime_store_config_untrusted` typed error，Python facade 新增 `validate_runtime_sidecar_config_authority()`；sidecar endpoint、mTLS identity、DB/SQLite/storage/lease owner 等配置来源只允许部署配置、环境变量、secret manager、只读配置文件或 runtime allowlist，用户输入、Skill manifest、LLM 输出和外部 tool output 会 fail-closed，跨主机访问缺 mTLS identity 同样拒绝。
- Runtime sidecar contract 新增 artifact provenance policy 与 `runtime_store_artifact_untrusted` typed error，Python facade 新增 `validate_runtime_sidecar_artifact_provenance()`；正式 client 连接前必须校验 sidecar binary/image 的 CI / deployment / allowlist 来源、checksum allowlist、Cargo.lock digest allowlist、proto hash、schema hash、SBOM digest 与 provenance attestation，校验失败 fail-closed。
- Runtime sidecar contract 新增 benchmark policy 与 `runtime_store_benchmark_invalid` typed error，Python facade 新增 `validate_runtime_sidecar_benchmark_report()`；进入 sidecar promotion 前必须同时提供 Python baseline 与 Rust sidecar baseline，并覆盖 task submit、node transition、event append、lease、event replay、SSE snapshot 的 P50/P95/P99、queue wait、CPU、memory 与 throughput 指标。
- Runtime sidecar contract 新增 shadow → enforce promotion policy 与 `runtime_store_promotion_blocked` typed error，Python facade 新增 `validate_runtime_sidecar_promotion_readiness()`；进入 enforce 前必须满足连续 7 天、至少 1000 个 shadow 样本、contract mismatch / panic / crash 为 0、P95 不超过 Python legacy 110%、error rate 不高于 legacy，并具备 artifact、benchmark、audit/redaction、rollback、ops runbook、regression、cargo test/clippy/fmt 证据。
- Runtime sidecar contract 新增 migration / DR policy 与 `runtime_store_migration_blocked` typed error，Python facade 新增 `validate_runtime_sidecar_migration_plan()`；SQLite schema、event log、lease、cursor、bundle pin 状态变更前必须具备 schema version、migration lock、preflight、dry-run、backup、restore、event replay validation、rollback / roll-forward runbook 证据。
- Runtime sidecar contract 新增 ops readiness policy 与 `runtime_store_ops_readiness_blocked` typed error，Python facade 新增 `validate_runtime_sidecar_ops_readiness()`；进入 enforce 前必须具备 dashboard、alert、SLO、drain / restart / rollback / restore / replay runbook，以及 unavailable、protocol mismatch、queue full、deadline spike、secret / identity mismatch、migration failure、crash recovery、restore / replay drill 证据。
- Runtime sidecar contract 新增 legacy decommission policy 与 `runtime_store_decommission_blocked` typed error，Python facade 新增 `validate_runtime_sidecar_decommission_readiness()`；最终删除 Python storage / dispatcher 写路径前必须证明 canonical sidecar 稳定、legacy 写路径已移除、只保留 facade/client/DTO adapter、rollback 走 deployment / sidecar artifact / restore 路径，并具备 promotion、ops、migration/DR、architecture guard 与 regression 证据。
- 新增 `maf_runtime_sidecar` Rust service kernel crate，先在 tonic/gRPC transport wrapper 前收口 Runtime sidecar 的 version / compatibility、task submit、node transition、event append/replay、lease、cancellation token 与 bundle revision pin/release 语义；对应状态由 Rust kernel 持有，并补充 idempotency 与 cursor 回归测试，避免这些写路径继续扩散 Python-owned 语义。
- `maf_runtime_sidecar` service kernel 补齐 health / readiness / compatibility handshake / shutdown drain 生命周期语义：readiness 必须在 compatibility handshake 通过后才进入 ready，shutdown drain 后 readiness 转 not-ready、health 转 degraded，新的写路径以 `runtime_store_unavailable` fail-closed 且不推进 event cursor。
- `maf_runtime_sidecar` 新增 protobuf/RPC-shaped service adapter 与 typed error envelope，将 health/readiness、compatibility、append/replay、lease、cancellation token、bundle revision 等请求/响应结构先收口在 Rust；后续 tonic transport 只负责绑定协议，不得由 Python facade 重新拼装 sidecar response envelope。
- `maf_runtime_sidecar` 接入 `tonic` / `prost` / `tonic-prost-build` 与 vendored `protoc` build script，从 `native/proto/maf/common/v1` 和 `native/proto/maf/runtime/v1` 生成 Rust gRPC bindings；新增 `RuntimeSidecarGrpcService`，将 protobuf request 映射到 Rust service adapter，并把 typed error envelope 映射回 protobuf `TypedError`，为后续外部 sidecar 进程监听与 Python client RPC 接入奠定协议层。
- `maf_runtime_sidecar` 新增 `maf-runtime-sidecar` 二进制入口，提供 `--version` 与 `--serve <addr>`；进程入口直接承载 Rust `RuntimeSidecarGrpcService`，并由 Rust `RuntimeSidecarServeConfig` 拒绝非 loopback 监听地址（mTLS / 内网身份支持未完成前 fail-closed），用于后续由外部进程管理器 / 容器编排启动 sidecar，而不是由 Python 生产请求路径 spawn。
- `maf_runtime_sidecar` 新增 `RuntimeSidecarSqliteAdapter` durable SQLite adapter，持久化 event append/replay cursor、event idempotency、task lease acquire/renew/release、task submit、node transition、cancellation token 与 bundle revision pin/release 状态，并补充 reopen 后事件回放、lease renewal / acquire idempotency、task/node/cancellation/bundle 回归测试；`RuntimeSidecarGrpcService::with_sqlite_adapter()` / `RuntimeSidecarServeConfig::build_service()` 已可将 RPC 委托到 durable adapter 并验证 reopen replay，二进制 `--serve ... --sqlite <path>` 可从外部进程配置 SQLite adapter；production mTLS / Unix socket、shadow/enforce 与 legacy 下线仍按后续门禁推进。
- Python `SQLiteStorage` facade 新增 `runtime_sidecar_client` 注入路径：`MAF_RUST_RUNTIME_STORE_MODE=enforce` / `MAF_RUST_EVENT_LOG_MODE=enforce` 且已配置 sidecar client 时，task submit、node transition 与 event append 会调用 sidecar client 并校验 Rust response envelope，不再写入 Python SQLite legacy 表；未配置 client 时继续 fail-closed。
- 新增 `RuntimeSidecarGrpcClient`，以无需新增 Python 依赖的 h2c gRPC unary client 连接外部 `maf-runtime-sidecar --serve 127.0.0.1:<port> --sqlite <path>`，连接前校验 endpoint / config source，每个 operation 前执行 compatibility handshake，并覆盖 version / compatibility、task submit、node transition、event append/replay、lease acquire/renew/release、cancellation token write、bundle revision pin/release 的真实 Rust sidecar RPC 集成测试。
- `build_api_runtime()` 可通过 `MAF_RUNTIME_SIDECAR_ENDPOINT`（可选 `MAF_RUNTIME_SIDECAR_ALLOWED_HOSTS` / `MAF_RUNTIME_SIDECAR_MTLS_ENABLED`）装配 Rust runtime sidecar client；配置 sidecar client 且进入 enforce 时，CancellationService、RuntimeLeaseFacade 与 ApiRuntime bundle revision pin/release 也会路由到 Rust response envelope，未配置 client 时继续按 typed error fail-closed。
- `maf_skill_runtime` 新增 `SkillSandboxService` Rust service kernel、`SkillSandboxGrpcService` tonic/prost binding 与 `maf-skill-sandbox` 二进制入口，收口 Skill Sandbox 的 version / compatibility / readiness（含 client version range 校验）、handler allowlist policy、loopback-only serve config、sandbox root 配置、相对 argv 执行、timeout、stdin 上限、stdout/stderr 并发有界 drain、`env_clear` 最小环境、Unix process-group cleanup、lingering descendant stdio bounded wait 与 path / symlink escape fail-closed；补充 `skill_sandbox_service` / `skill_sandbox_grpc` / `skill_sandbox_binary` / `skill_sandbox_process` Rust 集成测试，避免 Python subprocess runner 继续扩散 Skill Sandbox 核心协议语义。
- Python 新增 `SkillSandboxGrpcClient`，可通过内部 h2c gRPC 连接外部 `maf-skill-sandbox --serve ... --sandbox-root ...` 执行 sandbox RPC；`SkillScriptRunner` 在 `MAF_RUST_SKILL_RUNTIME_MODE=enforce` / 显式 enforce 下会 fail-closed 要求 Rust sandbox client，并将脚本执行路由到 Rust sandbox 后再复用原有 JSON 输出 / output processor 校验；h2c response 设定上限、精确 payload 长度校验、缺失/短头/截断/多余消息拒绝、空 `ExecuteSandboxed` 成功形状拒绝与 server client version range 校验，并补充真实 Rust sandbox binary、output processor 与响应异常回归测试。
- Skill Runtime 平台服务 trust gate 开始接入 Rust policy：`SkillSandboxGrpcClient` 支持 `ValidatePolicy`，`SkillPlatformHandlerRegistry` 在 `shadow` 下记录只含安全字段 / fingerprint / duration 的 Rust policy diff 审计事件、在 `enforce` 下要求 Rust policy client 并 fail-closed 禁止 Python allowlist legacy 直接放行；`build_api_runtime()` 会优先加载 `MAF_SKILL_POLICY_PYO3_MODULE` 指向的预构建 PyO3 policy module（校验 contract 后调用 Rust policy JSON bridge），未安装时复用 `MAF_SKILL_SANDBOX_ENDPOINT` 装配的 Rust sandbox client 作为 policy client，并补充 fake client、PyO3 facade 与真实 Rust binary 回归测试。
- Skill Runtime contract 新增 artifact provenance、benchmark、shadow→enforce promotion、ops readiness 与 legacy decommission policy，并新增 Python fail-closed gate validator；进入 Skill Runtime enforce、发布 PyO3 wheel / SkillSandbox binary 或下线 Python trust gate / subprocess policy 前必须提供 Rust contract 要求的 checksum / SBOM / Cargo.lock digest / provenance、Python vs Rust benchmark、连续 shadow threshold、dashboard / runbook / drill 与 decommission grep 证据。真实生产发布 provenance 与生产证据仍未宣称完成。
- 新增 `maf_skill_runtime_pyo3` PyO3 wheel crate，复用 `maf_skill_runtime` Rust policy kernel 暴露 `contract_json()` 与 `validate_policy_json()`，并通过 `maturin==1.13.3` 完成本地 wheel build、install 与 import smoke；wheel source 已纳入 workspace，但 lib test/bench 关闭以避免 macOS PyO3 test harness 误链接缺失的 `libpython`。生产 release 仍需 CI / 部署 provenance、SBOM、checksum 与 allowlist 证据。
- 新增 PRD01 Rust quality gate 执行面：`.github/workflows/rust-quality.yml` 覆盖 fmt / clippy / cargo test / nextest / audit / deny / llvm-cov、Ubuntu 22.04 x86_64 Skill Runtime PyO3 wheel smoke、artifact provenance self-check、80% / 90% coverage threshold runner 与 bounded fuzz smoke；`scripts/run_rust_quality_gates.py` 提供本地 gate matrix / partial runner，`scripts/run_rust_coverage_thresholds.py` 固化 workspace ≥80% / 安全敏感 crate ≥90% `cargo-llvm-cov --fail-under-lines` 命令；`native/fuzz/` 新增 Skill Runtime、MCP protocol、artifact path、audit sanitizer 与 readonly data-access fuzz harness，并补充静态回归测试。真实 CI 运行结果、coverage 实际报告、长时 fuzz、真实 SBOM / provenance 产物仍待后续 release gate 证据。

### 2026-05-15 — 收口 Rust / MCP PRD 实施口径

- 更新 Rust 化总体 PRD、Rust 专题索引与 MCP Rust Sidecar PRD，明确 MCP Runtime 已从 Rust 化候选进入 `docs/prd/MCP/` 联合 Phase 实施：Phase 0 / Phase 1 基线已落地，Phase 2-5、production enforce 与 Python legacy 下线仍未完成。
- 同步调整 MCP Phase 总览与 Phase 0 / Phase 1 状态，避免把 sidecar 接入骨架误表述为完整 MCP 长任务流式 SSE Runtime。
- 复审 Rust 工具链 PRD，修正“尚未引入 `native/` workspace”的过期表述，改为工具链 / MCP skeleton 部分落地、CI / 发布 / coverage / fuzz / provenance 门禁待完成。

### 2026-05-15 — 收敛 SQLQuery Skill 路由与触发词

- SQLQuery Skill manifest 移除“查一下”“数据库查询”等泛查询触发词，保留原有领域触发词，并追加 `approval_variety_db` / `genotype_db` 两个 route 的 `intent_keywords` 去重合集，避免 Skill 入口触发词与内部 route 判断分叉。
- SQLQuery Skill 路由配置移除 `variety_overview` route / schema profile，只保留 `approval_variety_db` 与 `genotype_db` 两个数据库 route。
- `intent_route` 改为先按 route 自身 `intent_keywords` 做确定性单路由判断；未命中时才调用 LLM 在已配置 route 中选择，未配置 / 未成功选择时返回审定品种库或基因型数据库的澄清中断。
- 清理 SQL 生成、schema context、prompt 与测试中的品种综合概览专属逻辑，并补充 Skill ownership / intent route 回归测试。

### 2026-05-14 — Composer 发送按钮切换为停止按钮

- 前端发送后在原发送键位置切换为“停止”按钮；任务活跃导致发送不可点击时，用户可直接从同一位置发起停止。
- 停止操作复用现有 conversation unfinished task 列表与 task cancel API，会取消当前对话下所有未完成任务，并以当前 task id 作为兜底，避免只取消单个前端订阅任务。
- 补充前端 API client 与 App 回归测试，覆盖 unfinished tasks 拉取、停止按钮替换发送按钮、批量取消当前对话任务。

### 2026-05-14 — 展示正在执行的 Skill 名称

- 前端任务进度文案从通用 `Skill` 标签改为优先展示后端事件中的 `skill_name`；缺少 `skill_name` 时回退展示 `skill.*` capability id，避免出现“正在执行 Skill：...”但看不出具体 Skill 的情况。
- 后端 `node.started` / `node.completed` frontend 事件透出计划 metadata 中的 `skill_name` / `forced_skill_name`；SQLQuery Skill 的 `skill.progress` 事件补充 `skill_name=sql-query`。
- 补充 orchestration、SQLQuery Skill 与前端进度展示回归测试，覆盖节点事件与 progress 事件的 Skill 名称展示。

### 2026-05-14 — 修复 SQLQuery Skill 长任务进度与 SSE 事件时间戳

- 修复 SQLQuery platform-service Skill 内部阶段进度只在 handler 完成后才批量返回的问题；SQLQuery 现在会通过 runtime `progress_events` 服务实时持久化并发布 `skill.progress` frontend 事件，长查询期间前端可见“理解意图 / 准备查询 / 生成 SQL / 检索数据库 / 筛选结果”等阶段。
- 统一补齐 runtime / orchestration 记录事件时缺失的 `created_at`，避免 `main_agent.output_delta`、`skill.progress` 等 live / executor 事件在 SSE 历史回放中因空时间戳出现顺序错乱。
- 补充 API、orchestration 与 SQLQuery Skill 回归测试，覆盖 live stream event 时间戳、executor 事件时间戳补齐以及 SQLQuery progress live recorder 行为。

### 2026-05-14 — 修复 LLM Planner Markdown JSON 围栏导致的对话失败

- 修复 LLM Planner 返回 ```json 代码块包裹 JSON 时被误判为非法 JSON 的问题；规划解析现在会在完整 Markdown JSON code fence 场景下剥离围栏后再校验原有 workflow schema。
- 补充 orchestration 回归测试，覆盖 code-fenced planner JSON，避免普通对话和 SQLQuery Skill 路由在规划阶段因格式包装失败。

### 2026-05-14 — 落地 MCP Runtime Phase0-5 联合改造基线

- 新增 MCP PRD Phase0 契约夹具、JSON-RPC / Streamable HTTP / SSE / Tasks / frontend event schema / typed error / sidecar contract artifacts 与 conformance matrix，形成可执行验收基线。
- 新增 `native/` Rust workspace、`maf_mcp_runtime` sidecar/proto Phase1 骨架、health/readiness/version/compatibility/typed error 结构与 Rust 单元测试；Python 侧新增 MCP Rust sidecar mode、compatibility handshake、feature gate、内部 endpoint 严格 allowlist 与 enforce fail-closed / shadow fallback 逻辑。Phase1 sidecar 只声明已实现的 health/readiness/version/compatibility 能力，完整 MCP runtime RPC 未接入前 enforce 不会回退 Python legacy。
- 增强 MCP Python runtime：支持多事件 SSE parser、GET stream、Last-Event-ID metadata、session 404 reinitialize、POST notification / response 202 no-body、task-augmented `tools/call`、`tasks/get/result/list/cancel`、taskSupport negotiation、safe ref registry、related-task metadata 校验、long-task live event bridge、API/SSE 持久化事件与 cancel propagation。
- 新增 shadow/enforce promotion gate 与 side-effecting shadow replay 安全判断；补充 MCP integrations/capability/API/Rust 回归测试并更新 README / AGENTS Rust 验证命令。

### 2026-05-14 — 拆分 MCP Runtime 联合改造 Phase PRD

- 新增 `docs/prd/MCP/` 目录，将 MCP 长任务流式 SSE 与 Rust MCP Runtime sidecar 作为同一最终交付目标拆分为总览、Phase 0 协议契约夹具、Phase 1 sidecar 契约与 Python facade、Phase 2 Streamable HTTP / SSE 内核、Phase 3 Tasks / durable registry、Phase 4 API 事件桥接与取消传播、Phase 5 shadow / enforce / legacy 下线等 PRD。
- 更新 PRD 总索引、后端总览、MCP 长任务 PRD、Rust MCP sidecar PRD 与 Rust 专题 README，明确 `docs/prd/MCP/` 是两个 MCP Runtime 改造的联合实施入口；Phase 是工程门禁，不是产品降级版本。
- 按 MCP 2025-11-25 官方规范复审 `docs/prd/MCP/`，补齐 JSON-RPC batch 禁止、POST notification / response 202、GET / DELETE / session 行为、Last-Event-ID 恢复、client capability 声明边界、task related metadata、progress token、cancellation 与 shadow side-effect 安全门禁。

### 2026-05-14 — 新增 MCP 长任务与流式 SSE PRD

- 新增 `docs/prd/backend/17-MCP长任务流式SSEPRD.md`，冻结 MCP Runtime 第 3 档升级目标：完整支持 Streamable HTTP SSE 多事件流、断线恢复、server-to-client notification / request、progress、task status、取消、任务结果获取与 API/SSE 事件桥接。
- 更新后端 PRD 索引、总览 PRD 与 MCP Runtime PRD，明确现有单条 SSE 兼容不是最终验收，后续长任务实现必须覆盖 `notifications/progress`、`notifications/tasks/status`、`tasks/get`、`tasks/result`、`tasks/cancel`、`notifications/cancelled`、reconnect 与去重。
- 复审并收口 MCP 长任务流式 SSE PRD，补齐长期交付要求、tool-level `execution.taskSupport` negotiation、durable registry 生产门禁、MCP executor live event bridge、取消传播、前端脱敏事件契约、验收矩阵与显式冻结假设。

### 2026-05-14 — 新增 MCP 服务器开发对接指南

- 新增根目录 `MCP服务器开发对接指南.md`，面向 MCP Server 开发同事说明当前项目支持的 MCP `2025-11-25`、JSON-RPC 2.0、Streamable HTTP 通信格式。
- 文档补充 `initialize`、`notifications/initialized`、`tools/list`、`tools/call` 请求 / 响应示例、错误格式、HTTP header、鉴权、工具 schema、输出安全边界与联调检查清单。

### 2026-05-14 — 冻结 Rust 最终交付门禁

- 更新 Rust 总览、工具链 PRD、Core/Lifecycle、Dispatcher/Store/Event、Skill Runtime、MCP Runtime、Artifact/Auth/DataAccess 聚合 PRD、Orchestration 条件候选 PRD、总体 Rust 化 PRD 与 `Codex-Skill构建指南.md`，将 Rust 决策清单从 33 项补齐为 38 项，确认剩余事项由实现 PRD / 测试计划承接，不再作为架构开放问题。
- 冻结 Rust artifact provenance / SBOM / supply-chain：所有 PyO3 wheel、sidecar binary / image、native binary 与 Skill-owned Rust artifact 必须由 CI / 部署流水线预构建，携带 checksum、SBOM、Cargo.lock digest、contract / proto hash 与 provenance；runtime 只加载 allowlist 校验通过的 artifact，请求路径不得编译、下载或替换产物。
- 冻结 Rust benchmark / SLO、state migration / backup / restore / DR、Python legacy path decommission 与 ops runbook / incident / rollback drill：进入 `enforce` 前必须有性能基线、状态恢复演练、运维告警 / runbook / 演练证据；Rust canonical 稳定后 Python 只保留 facade / client / DTO adapter，重复语义必须下线。

### 2026-05-14 — 冻结 Rust config / secrets / identity 策略

- 更新 Rust 工具链 PRD、Rust 总览、README、Dispatcher/Store/Event、Skill Runtime、MCP Runtime、Artifact/Auth/DataAccess 聚合 PRD、总体 PRD 与 `Codex-Skill构建指南.md`，冻结 Rust sidecar / PyO3 kernel 配置只允许来自部署配置、环境变量、secret manager、只读配置文件或 runtime allowlist；不得来自用户输入、Skill manifest、LLM 输出或外部 tool output。
- 明确 secret、token、mTLS key、数据库连接串、provider key、session / HMAC key 不得进入 tracked 文件、audit、metrics、structured logs、typed error message 或 `safe_metadata`；只能记录脱敏 secret id、version、fingerprint 或 configured 状态。
- 冻结身份与轮换策略：跨主机 sidecar 访问必须使用 mTLS 或等价服务身份校验；secret rotation 通过受控 reload 或滚动重启完成；`enforce` 下配置缺失、secret 缺失、identity mismatch、证书过期 / 不受信、client identity 未授权或 reload 失败必须 fail closed。

### 2026-05-14 — 冻结 Rust resource limit / backpressure 策略

- 更新 Rust 工具链 PRD、Rust 总览、README、Dispatcher/Store/Event、Skill Runtime、MCP Runtime、Artifact/Auth/DataAccess 聚合 PRD、总体 PRD 与 `Codex-Skill构建指南.md`，冻结所有 Rust sidecar / kernel 请求必须有 deadline，禁止无界队列、无界 stream、无界 stdout/stderr 与无界 payload。
- 冻结全局默认生产基线：retry `max_attempts=3`（含 initial attempt）、100ms 起指数退避、最大 1s、±20% jitter；health deadline 1s，readiness / version deadline 2s，shutdown drain 30s，audit / metrics event 单条 64KB，默认 request 1MB、response 4MB，非幂等请求禁止自动重试。
- 冻结模块级资源限制：Dispatcher / Store / Event 的 max in-flight、queue、写 deadline、event payload、replay page；Skill Sandbox 的并发、per-skill 并发、queue、timeout、stdout/stderr、result、artifact 与 cancel grace；MCP Runtime 的 tool call 并发、deadline、output cap、stream idle timeout；Artifact/Auth/DataAccess/Audit 的 auth、artifact、DB readonly、upload preview 与 archive hard cap。

### 2026-05-14 — 冻结 Rust sidecar network exposure / service discovery 策略

- 更新 Rust 工具链 PRD、Rust 总览、README、Dispatcher/Store/Event、Skill Runtime、MCP Runtime、Artifact/Auth/DataAccess 聚合 PRD 与总体 PRD，冻结 Rust sidecar 不对公网、前端、用户、普通 Skill 或外部系统直连暴露，只允许 Python runtime / 受控内部组件经 Unix domain socket、loopback、同 Pod / 内部网络、私有服务发现或 mTLS 内网访问。
- 明确 sidecar endpoint 必须来自部署配置、环境变量或 runtime allowlist；不得接受用户输入、Skill manifest、LLM 输出或外部 tool output 指定任意 sidecar 地址、端口、socket path 或 binary path。
- 冻结安全故障策略：health / readiness / metrics / debug endpoint 只能内部访问；`enforce` 下公网绑定、未授权 client、未配置 mTLS 的跨主机访问、非 allowlist service discovery 或 debug/metrics 外网可达必须 fail closed。

### 2026-05-14 — 冻结 Rust protocol compatibility / rolling upgrade 策略

- 更新 Rust 工具链 PRD、Rust 总览、README、Core/Lifecycle、Dispatcher/Store/Event、Skill Runtime、MCP Runtime、Artifact/Auth/DataAccess 聚合 PRD 与总体 PRD，冻结 Python sidecar client / PyO3 facade 在启动、connect、首次调用、reconnect 和 sidecar version 变化时校验 component、protocol / contract version、schema hash、error code table hash、build version、supported features 与 client version range。
- 明确 sidecar health / readiness / version response 必须包含 component、build_version、protocol_version、schema_hash、error_code_table_hash、supported_features、min_client_version、max_client_version；readiness 只有在 compatibility handshake 通过后才为 ready。
- 冻结滚动升级策略：兼容 minor 变更允许滚动升级；breaking change 必须进入 `v2` proto package / contract major version 或显式 dual-stack；`enforce` 下协议 / contract 不兼容必须 fail closed，`shadow` 下可回退 Python legacy path 并写 `rust.protocol_incompatible` audit event。

### 2026-05-14 — 冻结 Rust observability / structured output validation 策略

- 更新 Rust 工具链 PRD、Rust 总览、README、Core/Lifecycle、Dispatcher/Store/Event、Skill Runtime、MCP Runtime、Artifact/Auth/DataAccess 聚合 PRD 与总体 PRD，冻结所有 Rust kernel / sidecar 的 response、typed error、audit event、metrics event、shadow diff、retry event 与 correction event 必须通过 schema / protobuf / contract artifact 校验后才能被 Python、API、audit 或 metrics 消费。
- 明确结构化输出校验失败必须映射为 typed error，默认按 `contract` 类 fail closed；只有 transient / incomplete transport response、幂等或无副作用操作、retry policy 与脱敏 retry audit 全部满足时，才允许自动重试。
- 冻结 Rust observability 最低字段与事件类型：Python 必须透传 trace context，Rust structured event 至少覆盖 component、mode、version、duration、error code、retry attempt、input/output fingerprint 与 redaction 状态；禁止记录 raw prompt、完整 rows、secret、真实路径、连接串、base_url 或 raw provider / OS error。

### 2026-05-14 — 冻结 Rust typed error / retry-correction policy

- 更新 Rust 工具链 PRD、Rust 总览、README、Core/Lifecycle、Dispatcher/Store/Event、Skill Runtime、MCP Runtime、Artifact/Auth/DataAccess 聚合 PRD 与总体 PRD，冻结 Rust error code 继续使用 lowercase snake_case string，不改成 dotted code；Rust typed error 必须包含 `code`、`message`、`retriable`、`category`、`safe_metadata`。
- 冻结自动重试 / 自动修正策略：自动重试只允许在 `retriable=true`、幂等、retry policy 与 retry audit 均满足时执行；自动修正只允许处理系统生成的结构化内容，不得改变用户真实意图或放宽安全规则；权限、auth、service binding、allowlist、MCP sanitizer、artifact path、DB readonly、secret 泄露风险、contract mismatch 与 lifecycle 非法状态转移必须 fail closed。
- 冻结首批 error code 前缀与 sidecar error：`runtime_store_unavailable`、`dispatcher_unavailable`；Dispatcher / Store / Event `enforce` 写路径失败可对同一个 Rust sidecar 做幂等重试，但重试耗尽后 fail closed，仍不得 fallback 到 Python legacy store。

### 2026-05-14 — 冻结 Python facade 生成策略

- 更新 Core/Lifecycle Rust 专题 PRD、Rust 总览、README 与总体 PRD，冻结 Python facade 采用“生成 contract artifact + 手写薄 facade”的混合策略；Rust 作为 canonical source 生成 / 导出 JSON schema、error code table、enum/value snapshot、transition table snapshot 与 golden fixtures。
- 明确 Python facade 保持手写薄层，负责现有 import path、dataclass / Pydantic / API DTO 适配、Rust typed error 到现有 Python exception 映射和最小格式转换；禁止承载独立状态机、enum、默认值或 error code 语义，CI 必须校验 facade 与 Rust artifact 一致。

### 2026-05-14 — 冻结 Core / Lifecycle canonical source 策略

- 更新 Core/Lifecycle Rust 专题 PRD、Rust 总览、README 与总体 PRD，冻结 `maf_core_types` 与 `maf_lifecycle` 为唯一 canonical source；Rust 负责 core enums / structs、stable error code、JSON schema / serde contract 以及 task / node / mailbox / interrupt / cancel transition table。
- 明确 Python `src/core` / `src/lifecycle` 只保留兼容 facade / adapter，不得独立定义与 Rust 冲突的 enum 值、默认值、状态转移规则或 error code 语义；`off` / `shadow` 阶段允许保留 Python legacy，`enforce` 后 Rust 判定为准，稳定后删除重复 Python transition table。

### 2026-05-14 — 冻结 Dispatcher / Store / Event sidecar enforce 故障策略

- 更新 Dispatcher / Store / Event sidecar PRD、Rust 总览、README 与总体 PRD，冻结 sidecar 进入 `enforce` 后 task submit / create、node state transition、event append、lease acquire / renew / release、cancellation token 写入、bundle revision pin / release 等状态写入类操作失败必须 fail closed，不允许自动 fallback 到 Python legacy store。
- 明确 `enforce` 阶段只允许 health/status、metrics、无副作用 read-only snapshot 等受限只读降级；sidecar unavailable、protocol version 不兼容或写入失败时返回 `runtime_store_unavailable` / `dispatcher_unavailable` 等稳定 typed error，由 API 层暴露为可重试失败。

### 2026-05-14 — 冻结 Rust coverage / fuzz 门禁

- 更新 Rust 工具链 PRD、总览、README、Skill Runtime、MCP Runtime、Artifact/Auth/DataAccess 聚合 PRD 与总体 PRD，冻结 `cargo-llvm-cov` 对所有 Rust crate 必跑并生成 coverage report；普通 Rust crate 最低 line coverage 为 80%，安全敏感 crate 最低 line coverage 为 90%。
- 冻结 `cargo-fuzz` 对不可信输入边界强制启用，覆盖 Skill manifest / allowlist / sandbox policy、MCP JSON-RPC / schema / output sanitizer、artifact path / archive / filename sanitizer、audit redaction / secret masking、DB readonly policy / row shaping；PR 级别跑 bounded fuzz smoke，nightly / release gate 跑更长 fuzz job，fuzz 可用独立 pinned nightly toolchain，但不得改变生产 stable toolchain。

### 2026-05-14 — 冻结 Rust CI / 发布产物矩阵

- 更新 Rust 工具链 PRD、总览、README 与总体 PRD，冻结任一 Rust 代码进入 `native/` 或 `skill/<skill-name>/native/` 后必须启用 `cargo fmt`、`cargo clippy`、`cargo test`、`cargo nextest`、`cargo audit`、`cargo deny` 必跑门禁。
- 冻结 PyO3 wheel 使用 `maturin` 构建、sidecar binary 使用 Cargo 构建，生产 sidecar 以 Linux container image / binary 为主；macOS arm64 作为本地开发调试基线、Linux x86_64 作为生产部署基线，Windows 暂不作为必需发布目标，Python 版本跟随 Python 3.13 系列。

### 2026-05-14 — 冻结 Orchestration Rust 化最终归属

- 更新 Rust 总览、工具链 PRD、Orchestration 条件候选 PRD、README 与总体 PRD，冻结 Orchestration deterministic kernel 不进入必做 Rust 化目标集，只保留为条件候选；LLM planner、router glue、provider fallback、prompt 和产品策略继续保留 Python。
- 明确未来若启动 `maf_orchestration_kernel`，必须另开实施 PRD、提供性能或可靠性证据、只迁移 deterministic DAG / scheduler / payload policy 等可回放规则，并通过 Python baseline shadow compare。

### 2026-05-14 — 冻结 Rust toolchain / edition / MSRV 策略

- 更新 Rust 工具链 PRD、总览、README 与总体 PRD，冻结 `rust-toolchain.toml` 必须固定具体 stable 版本，不使用裸 `stable` channel；Cargo workspace 默认 Rust edition 2024，MSRV 等于 `rust-toolchain.toml` 固定版本，toolchain 升级必须单独 PR、更新 `CHANGELOG.md` 并跑完整 Rust / Python 回归。

### 2026-05-14 — 冻结 Rust dependency 技术栈

- 更新 Rust 工具链 PRD、总览与总体 PRD，冻结 `tokio`、`tonic`、`prost`、`serde`、`serde_json`、`schemars`、`thiserror`、`tracing`、`tracing-subscriber`、`pyo3`、`maturin`、`sqlx`、`uuid`、`time`、`sha2`、`hmac`、`pbkdf2`、`regex`、`jsonschema`、`proptest`、`insta`、`rstest`、`criterion`、`cargo-audit`、`cargo-deny`、`cargo-llvm-cov`、`cargo-nextest` 为 Rust 化长期技术栈；`axum` 仅可选用于本地 health/debug/admin endpoint，`cargo-fuzz` 对不可信输入边界强制启用。

### 2026-05-14 — 冻结 Rust sidecar protobuf 归属与版本策略

- 更新 Rust 工具链 PRD、总览、sidecar 专题 PRD 与总体 PRD，冻结所有生产 sidecar protobuf schema 统一归属 `native/proto/maf/<domain>/v1/`，初始 domain 为 `common`、`runtime`、`skill`、`mcp`；`v1` / `v2` 表示协议 schema 主版本 namespace，breaking change 必须新建 `v2` package，Rust sidecar server 与 Python sidecar client 必须从同一 proto source 生成或校验。

### 2026-05-14 — 冻结 Rust enforce 失败处理策略

- 更新 Rust 工具链 PRD、总览、各专题 PRD 与总体 PRD，冻结 `enforce` 模式下 Rust kernel / sidecar 失败默认 fail closed；仅当对应 PRD 显式声明可 fallback 且 fallback 不放宽安全、权限、数据一致性、路径、secret、外部输入校验或审计约束时，才允许回退 Python legacy path，并必须写 structured audit。

### 2026-05-14 — 冻结 Rust enforce promotion threshold

- 更新 Rust 工具链 PRD、总览、各专题 PRD 与总体 PRD，冻结任一 Rust kernel / sidecar / 子模块从 `shadow` 进入 `enforce` 的全局最低门槛：至少连续 7 天且不少于 1000 次有效 shadow 样本，并满足 contract mismatch rate = 0、panic/crash = 0、P95 latency 不高于 Python legacy 110%、error rate 不高于 legacy、audit/redaction/secret leak 测试 100% 通过、rollback 演练通过以及对应 regression/cargo/clippy/fmt 全部通过。

### 2026-05-14 — 冻结 Rust shadow compare 差异处理策略

- 更新 Rust 工具链 PRD、总览、各专题 PRD 与总体 PRD，冻结 `shadow` 模式下 Python legacy path 始终作为用户可见结果来源；Rust kernel / sidecar 仅旁路对比，差异写入脱敏 structured audit / metrics，不得记录完整 prompt、完整 rows、secret、真实文件路径或敏感 payload，且不得影响用户结果。

### 2026-05-14 — 冻结 Rust sidecar 进程管理方式

- 更新 Rust 工具链 PRD、总览、Dispatcher/Store/Event sidecar、Skill Runtime、MCP Runtime、README 与总体 PRD，冻结 sidecar 进程管理最终方案：生产环境由外部进程管理器 / 容器编排管理，Python runtime 不负责生产 sidecar 生命周期，仅负责 client connect、health/readiness/version check、shutdown drain 协调、protocol compatibility check 与 fallback；dev/test 可提供 launcher。

### 2026-05-14 — 冻结 Rust runtime config 命名规范

- 更新 Rust 工具链 PRD、总览、各专题 PRD 与总体 PRD，冻结所有 Rust kernel / sidecar / 子模块统一使用 `MAF_RUST_<COMPONENT>_MODE=off|shadow|enforce` 命名规则；默认值为 `off`，生产 `enforce` 前必须经过 `shadow`，新增 Rust 模块不得自定义另一套 enable / disable 环境变量。

### 2026-05-14 — 冻结 Audit Sanitizer crate 归属

- 更新 `docs/prd/rust/06-ArtifactUploadAuthDataAccessKernelPRD.md`、Rust 总览、工具链 PRD 与总体 PRD，冻结 Audit / Event sanitizer 的主体 crate 为 `maf_audit_sanitizer`，负责 audit payload sanitizer、event serializer privacy filter、redaction rule 与敏感字段屏蔽；该职责不并入 `maf_core_types`。

### 2026-05-14 — 冻结 Artifact/Auth/DataAccess 聚合 PRD 边界

- 更新 `docs/prd/rust/06-ArtifactUploadAuthDataAccessKernelPRD.md`、Rust 总览、README 与总体 PRD，冻结 Artifact / Upload / File Safety、Auth primitives、DataAccess readonly kernel、Audit / Event sanitizer 继续由一个聚合 PRD 承载；四个子模块可分别实现、分别 feature flag、分别验收，但共享同一安全验收基线。

### 2026-05-14 — 冻结 Skill Runtime Rust 最终方案

- 更新 `docs/prd/rust/04-SkillRuntime与SkillOwnedRust接入PRD.md`、Rust 总览、工具链 PRD 与总体 PRD，冻结通用 Skill Runtime 最终方案为 Rust policy kernel + Rust Skill Sandbox sidecar 双层架构。
- 明确 `maf_skill_runtime` 通过 PyO3 提供 manifest / fingerprint / trust gate / allowlist / `x_runtime.rust` contract 校验，不可信或进程型执行边界进入 Rust Skill Sandbox sidecar / isolated process manager；Skill-owned Rust runtime 仍由 Skill 在 `skill/<skill-name>/native/` 内按指南适配框架。

### 2026-05-14 — 冻结 MCP Runtime 独立 sidecar 接入方式

- 将 MCP Runtime Rust 专题重命名为 `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md`，冻结 MCP Runtime Rust 化最终接入方式为独立 Rust sidecar；Python 仅保留 sidecar client、runtime config、capability descriptor 与 executor wrapper。
- 同步 Rust 总览与总体 PRD，明确 Python ↔ MCP sidecar 生产协议沿用 gRPC / tonic + protobuf，HTTP JSON 不得作为生产 sidecar 协议。

### 2026-05-14 — 冻结 PostgreSQL 延期策略

- 更新 `docs/prd/rust/03-DispatcherStoreEventSidecarPRD.md`、`docs/prd/rust/00-Rust化总览与拆分索引PRD.md` 与 Rust 总体 PRD，冻结 Dispatcher / Store / Event sidecar 本专题最终交付边界为 SQLite adapter 与 PostgreSQL-compatible contract；PostgreSQL production adapter 不进入该专题，继续作为独立 PRD / 独立升级项推进。

### 2026-05-14 — 冻结 Rust workspace 与首批 crate 命名

- 更新 `docs/prd/rust/01-Rust工具链构建发布与质量门禁PRD.md`、`docs/prd/rust/00-Rust化总览与拆分索引PRD.md` 与 Rust 总体 PRD，冻结主体 Rust workspace 为 `native/`、Skill-owned Rust runtime 目录为 `skill/<skill-name>/native/`，并冻结首批主体 crate 名称；`maf_orchestration_kernel` 仅作为条件候选 crate 名保留，不进入必做 Rust 化目标集创建。

### 2026-05-14 — 冻结 Core/Lifecycle Rust 接入方式

- 更新 `docs/prd/rust/02-Core与LifecycleKernelPRD.md`、`docs/prd/rust/00-Rust化总览与拆分索引PRD.md` 与 Rust 总体 PRD，冻结 Core types 与 Lifecycle transition table 的 Rust 接入方式为 PyO3 extension；Python 保留 facade 和 feature flag fallback，不以 sidecar service 或 subprocess native binary 作为主路径。

### 2026-05-14 — 冻结 Rust sidecar 正式协议

- 更新 `docs/prd/rust/03-DispatcherStoreEventSidecarPRD.md`、`docs/prd/rust/01-Rust工具链构建发布与质量门禁PRD.md` 与 Rust 总体 PRD，冻结 Dispatcher / Store / Event sidecar 的正式生产协议为 gRPC / tonic + protobuf；HTTP JSON 仅允许本地开发或极早期 spike，不得作为生产协议。

### 2026-05-14 — 冻结 Rust 化实施波次顺序

- 更新 `docs/prd/rust/00-Rust化总览与拆分索引PRD.md` 与 `docs/prd/rust/README.md`，冻结 Rust 化实施顺序为 Wave 0 工具链 → Wave 1 Core/Lifecycle → Wave 2 Dispatcher/Store/Event sidecar → Wave 3 Skill/MCP Runtime → Wave 4 Artifact/Auth/DataAccess 与非 orchestration 热点优化；Orchestration deterministic kernel 归属为条件候选，不进入必做 Rust 化目标集。

### 2026-05-14 — 拆分 Rust 化实施专题 PRD

- 新增 `docs/prd/rust/` 专题目录，将 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md` 拆分为 Rust 化总览、工具链质量门禁、Core/Lifecycle kernel、Dispatcher/Store/Event sidecar、Skill Runtime 与 Skill-owned Rust、MCP Runtime、Artifact/Auth/DataAccess、Orchestration 条件候选热点优化等实施 PRD。
- 更新 Rust 化总体 PRD、后端总览 PRD 与 PRD 目录索引，明确 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md` 仍是总决策基线，`docs/prd/rust/` 负责后续实现专题评审与验收。

### 2026-05-14 — 复审 Rust 化 Runtime PRD 决策基线

- 复审 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md`，补齐通用 Skill Runtime 与 Skill-owned Rust runtime 的术语边界、`x_runtime.rust` / allowlist / 禁止 runtime cargo build 的功能需求，以及后续实现待决项不阻塞当前决策基线的说明。
- 同步 `docs/prd/backend/00-主代理框架PRD.md` 的 Rust 化 Runtime 专题索引与关键决策摘要，移除该 Rust 决策摘要中针对单一业务 Skill 的旧表述。

### 2026-05-14 — 泛化 Skill Rust runtime 接入规范

- 更新 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md`，移除针对单一业务 Skill 的 Rust runtime 表述，改为 generic Skill-owned Rust runtime 接入边界：Skill 必须适配框架 contract，框架不为具体 Skill 增加专属 Rust runtime 分支、API route、前端协议或 capability kind。
- 更新 `Codex-Skill构建指南.md`，新增 Rust 型 Skill runtime 接入限制，明确只接受 `skill/<skill-name>/native/` 下的 shared Rust core + PyO3 wheel / native binary / sidecar adapter 受控形态；禁止运行时编译 Rust、下载依赖、执行任意 native binary 或让普通 Skill 自行启动服务。

### 2026-05-14 — 冻结 Skill-owned Rust runtime 多适配器策略

- 更新 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md`，冻结 Skill-owned Rust runtime 采用 shared Rust core + PyO3 wheel / native binary / sidecar service 多适配器并存策略；不同 adapter 按场景选择，但必须共享同一 core crate 与 contract tests，禁止复制业务逻辑。

### 2026-05-14 — 延期 PostgreSQL 正式化与 Rust sidecar 合并推进

- 更新 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md`，将 PostgreSQL 正式化标记为后续升级项：Rust runtime sidecar 设计预留 production adapter、schema ownership 与 migration policy，但不在当前决策中强行合并落地。

### 2026-05-14 — 冻结 Rust dispatcher/store sidecar 决策

- 更新 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md`，冻结 dispatcher / store / event log 的目标集成方式为 Rust sidecar service，PyO3 仅用于纯规则、小校验器或 Python compatibility facade。
- 明确本项目按长期交付级 Agent 系统建设，Rust 化目标架构必须考虑生产级完整技术栈，包括 typed protocol、health check、可观测性、灰度开关、版本兼容、CI 构建与故障回退。

### 2026-05-14 — 冻结 Rust native workspace 目录边界

- 更新 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md`，冻结主体框架 Rust native workspace 使用 `native/`，Skill 自有 Rust runtime 使用 `skill/<skill-name>/native/`；当前仅冻结目录边界，不立即创建 native 目录。

### 2026-05-13 — 新增 Rust 化 Runtime 模块评估 PRD

- 新增 `docs/prd/backend/16-Rust化Runtime模块评估PRD.md`，在 SQLQuery 已归属 `skill/sql-query/` bundle 的前提下，重新评估后端 runtime substrate、Skill/MCP runtime、storage/event、artifact/file、安全与 deterministic kernel 的 Rust native 下沉边界。
- 更新 PRD 总目录与后端总览索引，明确 `ApiRuntime` 不整体 Rust 化，SQLQuery 如需 Rust 化应作为 Skill-owned native runtime，而不是主体框架 native capability。

### 2026-05-13 — 完成 SQLQuery 物理归属 Skill 迁移

- 将 SQLQuery runtime、配置、领域文档与专项回归全部收口到 `skill/sql-query/` bundle；系统目录只保留通用 Skill loader、service registry 与平台 handler 加载机制，移除 native capability / provider / executor / replanner / 旧测试入口。
- API runtime 改为通用 `platform_llm_*` 与 `llm.non_stream` service 装配，project platform-service handler 仅允许来自 public Skill root 的相对 `handler_module` 或显式 per-skill trusted handler allowlist，避免系统内置 SQLQuery handler 残留。
- 系统测试改用 generic `skill.generic_data_lookup` fixture 覆盖 Skill platform-service 编排、finalizer、任务查询、取消、权限与 MCP 共存；SQLQuery 行为测试只在 `skill/sql-query/tests/` 内运行。
- 前端 SQLQuery 专属卡片、显式 mode、旧内部 stage / 路由标识清理为通用 `DataQueryResultCard`、`data_query` artifact 与 generic Skill progress；系统文档改为说明可移除数据查询 Skill 模式，SQLQuery 细节随 bundle 自带文档维护。
- 新增 Skill bundle ownership guard，验证系统源码、测试、前端与活跃文档不再硬编码 SQLQuery 归属细节；完成全量后端分层 unittest、Skill bundle 测试、前端 Vitest/build、compileall、diff check 与架构复核。

### 2026-05-13 — 清理前端 SQLQuery 专属展示与可移除 Skill 文档口径

- 将前端 SQLQuery 结果卡片与 artifact display model 泛化为 `DataQueryResultCard` / `data_query`，移除 `skill.sql_query` 显式 UI mode 与 SQLQuery 专属文案，保留基于通用 query result artifact 的表格预览渲染。
- 更新前端事件 reducer 测试与 artifact 测试，进度文案改为 generic Skill / 数据查询表达，避免前端依赖 SQLQuery 专属 capability id。
- 同步 README、AGENTS 与活跃 PRD / 指南文档口径：SQLQuery 是可移除 `skill/sql-query/` bundle，系统 runtime 只提供 generic Skill loader / allowlisted platform-service handler。

### 2026-05-13 — 完成 SQLQuery Skill-only 能力收口

- 将 SQLQuery 可复用阶段实现迁入 `skill/sql-query/runtime/sql_query_skill/`，删除原 SQLQuery provider / executor / replanner、public/internal descriptor 与本地 instance builder。
- `skill/sql-query` 改为唯一公开 SQLQuery 入口 `skill.sql_query`，platform handler 改由 `skill/sql-query/SKILL.md` 声明并通过通用 project Skill loader 加载，API runtime 对非 public SQLQuery 请求 id 返回 unsupported capability。
- SQLQuery 行为测试迁移到 `skill/sql-query/tests/`，新增 Skill-only 架构守护测试与 engine 回归，覆盖无旧包/导入/符号、metadata 脱敏、Skill progress/artifact metadata 与 SQL Guard。
- 更新 Codex Skill 构建指南、README、AGENTS 与当前架构/PRD 文档，明确 SQLQuery 是 Skill platform-service 能力，domain stages 只在 handler 内部执行。

### 2026-05-12 — SQLQuery 迁移为 Skill platform-service 能力

- 新增项目级 `skill/sql-query/SKILL.md`，将公开数据库查询能力收口为 `skill.sql_query`，`/api/v1/capabilities` 只展示 Skill public capability；该日曾短暂保留 alias 转换，已在 2026-05-13 清理为 unsupported capability。
- 新增 `skill/sql-query/runtime/sql_query_skill/engine.py` 与 Skill bundle handler，通过 Skill Executor 的 `platform_service` handler 执行 SQLQuery 既有 intent route、schema context、SQL generation、SQL guard、readonly execute 与 result filtering 主链路，并向前端输出 `skill.progress` 阶段事件与带 `domain_kind=sql_query` metadata 的 artifact。
- API runtime 移除原 SQLQuery descriptor、instance、workflow provider、executor 与 runtime replanner 装配；`src/orchestration/` 去除 SQLQuery 业务 import / router special case / auto fallback hardcode，Planner prompt 改为依赖公开 `skill.*` 能力描述选择 SQLQuery。
- SQLQuery Skill 内部 LLM service 改用通用 `llm.non_stream` platform adapter；不再实例化专属 SQLQuery LLM client，并对 provider 返回的 `reasoning_content` 只取普通 answer 文本。
- 前端 SQLQuery 手动模式切到 `skill.sql_query`，任务进度支持 `skill.progress` + `domain_kind=sql_query`，结果卡片识别改为 artifact metadata 优先、旧字符串 fallback 兼容。
- 补充并更新 API / orchestration / e2e / observability / frontend 回归测试，覆盖 alias、Skill finalizer、service binding、capability list、artifact metadata、progress event 和 SQLQuery LLM Provider 复用规则。

### 2026-05-12 — 复审 SQLQuery Skill 化迁移计划

- 按已落地的 generic Skill Executor infra 重新审视 `docs/SQLQuery-Skill化迁移计划.md`，将迁移前提从“先实现 Skill Executor”更新为“使用现有 `platform_service` / `answer_mode` / service allowlist 能力承载 SQLQuery”。
- 明确 `skill.sql_query` 必须使用 `execution.mode=platform_service` 与 `answer_mode=requires_finalizer`，不能用带 MySQL / LLM service binding 的 `python_subprocess` 脚本模式。
- 重排 SQLQuery 迁移阶段：先固化迁移前行为基线，再抽 domain engine、并行注册 platform handler、清理 orchestration / runtime 特判、前端改 metadata / stage event 识别，最后删除原 capability 与 alias。
- 补充 SQLQuery Skill 化后的 LLM Provider 规则：迁移后的 `skill.sql_query` 新实现不再维护独立 LLM client，`llm.non_stream` 作为通用受控 service 名称，底层沿用主代理 LLMProvider 的非流式、`thinking=false` adapter，并明确忽略 `reasoning_content`；该迁移兼容期已在 2026-05-13 结束。

### 2026-05-12 — 新增 Skill Executor 实现需求 PRD

- 新增 `docs/prd/backend/15-SkillExecutor实现需求PRD.md`，明确 `skill.*` 一等执行器的职责边界、非目标、执行链路、service binding、安全约束、artifact / event / audit 归一化、测试计划和 SQLQuery Skill 化前置要求。
- 更新 `docs/prd/README.md` 与 `docs/prd/backend/00-主代理框架PRD.md`，将 Skill Executor 纳入后端 PRD 专题索引与关键决策基线。
- 对 Skill Executor PRD 做 document-perfectization 复核修订，补齐受众与影响面、`delegated_main_agent` / `python_subprocess` / `platform_service` 三种执行模式、`answer_mode`、service-bound Skill 只能走 runtime 预注册 handler、Skill executor instance 随 bundle 刷新同步、rollout / rollback 与新增测试验收项。
- 新增 `src/capabilities/skill_tool/` generic Skill Executor，实现 `skill.*` 的执行壳、`python_subprocess` / `platform_service` / `delegated_main_agent` 模式边界、direct / finalizer answer mode、受控 handler / service allowlist 和 direct text artifact 输出。
- 在 `src/integrations/codex_skills/` 新增 execution 配置与脚本执行公共服务，Skill capability registry 补充 planner payload policy，Skill runtime state 补充 retained revision 能力集合，主代理 Skill auto-run 脚本路径改为复用公共脚本执行服务，减少脚本执行逻辑散落。
- `src/orchestration/skill_workflow_provider.py` 支持基于 Skill manifest 的 executor-mode 展开；`src/api/runtime.py` 启动期与热刷新时同步 skill descriptor、payload policy 与本地 Skill executor instance，并为后续 trusted platform-service Skill 预留 runtime handler / service 注册入口。
- 新增 integrations / capability / orchestration / API 回归测试，覆盖 execution config 解析、SkillExecutor 执行、platform handler allowlist、workflow expansion、API 显式 skill 执行和动态刷新后的 skill instance 同步。

### 2026-05-12 — 新增 SQLQuery Skill 化迁移计划

- 新增 `docs/SQLQuery-Skill化迁移计划.md`，基于当前代码事实规划将 SQLQuery 从原生 capability 迁移为项目级 `skill.sql_query` 的分阶段方案，覆盖通用 Skill executor、受控 service binding、SQLQuery 领域服务拆分、orchestration 去特判、API runtime 清理、前端兼容与验收标准。

### 2026-05-12 — 实现 MCP Runtime Phase 1 基线

- 新增 `src/integrations/mcp/`，实现 MCP 2025-11-25 client runtime 基线：JSON-RPC lifecycle、`initialize` / `notifications/initialized`、request id 关联、Streamable HTTP transport、协议 / session header、tools/list 分页、tools/call、SSE JSON 响应解析、静态鉴权注入与授权 / 协议错误映射。
- 新增 `MCPRuntimeState` 不可变 bundle 与 allowlist 公开策略，将受控 read-only MCP tool 转换为 public `mcp.*` capability descriptor、payload policy 与 capability 到 server/tool binding；发现失败保留旧 bundle，optional server 失败不影响内置能力。
- 新增 `src/capabilities/mcp_tool/` generic executor，执行前按 planner allowlist 和 JSON Schema fail-closed 校验输入，执行后把 MCP `structuredContent` / text content 映射为 `CapabilityExecutionResult`，并输出脱敏 audit-only 调用事件。
- API runtime 支持显式 `mcp_config` / `mcp_client_factory` 注入，启动期注册 MCP capability、实例与 executor，shutdown 时关闭 MCP client；主代理依赖上下文允许接收清洗后的 MCP tool 输出用于最终汇总。
- 补充 integrations / capability / orchestration / API 回归测试，覆盖 lifecycle、分页、allowlist、schema 校验、注册、Planner 选择 MCP capability、主代理汇总、发现失败降级与 shutdown 关闭。
- 将 `docs/prd/backend/14-MCPRuntime实现需求PRD.md` 文档状态更新为 Phase 1 已实现，并追加当前实现范围与 Phase 2 留白。
- 根据架构复核补齐 MCP 安全闭环：外部输出进入主代理前会粗粒度脱敏并屏蔽 URL，执行结果按 `outputSchema` 校验，空 planner allowlist 改为 fail-closed，MCP refresh 采用 pending bundle 同步成功后再 commit 的激活流程。

### 2026-05-12 — 新增 MCP Runtime 实现需求 PRD

- 新增 `docs/prd/backend/14-MCPRuntime实现需求PRD.md`，明确 MCP Runtime 作为外部 MCP server / tools 的 client runtime 接入层，原始 MCP tool 必须先经 `src/integrations/mcp/` 适配并由 capability 包装后进入现有编排体系。
- 按 MCP latest spec 2025-11-25 重写通信协议要求，覆盖 JSON-RPC 2.0、lifecycle negotiation、Streamable HTTP / stdio 标准 transport 抽象、`MCP-Protocol-Version`、`MCP-Session-Id`、SSE reconnect、authorization、tools/list、tools/call、icons 与 experimental tasks metadata 边界。
- 对 MCP Runtime PRD 做 document-perfectization 复审修订，补齐最小 client capabilities、`notifications/initialized`、request id 唯一性、unsupported client feature 响应、schema dialect、OAuth Phase 1/2 边界、bundle 原子激活、SSE 复连、授权错误映射和协议级验收标准。
- 更新 `docs/prd/README.md` 与 `docs/prd/backend/00-主代理框架PRD.md` 后端专题索引和关键决策，将 MCP Runtime 纳入后端 PRD 基线。

### 2026-05-12 — 收紧 Skill 渐进式披露发现层

- 主代理隐式 Skill fallback 匹配默认只注入最高分的单个 Skill 指令，保留调用方显式 `max_matches` 扩展能力，降低多 Skill 全文同时进入 prompt 的过披露风险。
- `CapabilityDescriptor`、Planner public capability 目录与 `/api/v1/capabilities` 增加 Skill `source_path` 摘要，让发现层更接近 Codex 的 name / description / path 粒度，同时保持 API 字段向后兼容。
- Planner public capability 列表增加 8000 字符预算控制，超限时先缩短 description，再省略尾部 capability 并提示预算省略，避免大规模 Skill 池挤占规划上下文。

### 2026-05-12 — 拉宽历史会话条目边界

- 左侧历史会话列表条目改为 full-bleed 显示，左边贴齐页面边缘、右边贴齐左侧栏右边缘，并移除条目之间的纵向空隙。
- 历史条目的“重命名 / 删除”操作区与按钮说明小气泡保持同一触发机制：只在鼠标悬浮条目时出现，点击造成的 focus 不再让操作区常驻。

### 2026-05-11 — 提高上传文档默认大小上限

- 上传 JSON / CSV 文档的默认大小上限从 2MB 提高到 20MB，并用常量 `DEFAULT_MAX_UPLOAD_FILE_BYTES` 固化，避免后续代码分散硬编码。

### 2026-05-11 — 收紧上传文件大小早期校验

- 上传接口改为按固定 chunk 分块读取文件内容，并在读取过程中根据 `upload_store.max_file_bytes` 立即拒绝超限文件，避免先完整读入超大上传再校验。
- 保留 `InMemoryUploadStore.save()` 内部大小校验作为二次防线，并补充 API / helper 回归测试覆盖超限早拒绝、刚好等于限制、权限预检优先于大小校验和正常上传路径。

### 2026-05-11 — 实现 Skill 新聊天动态加载闭环

- 新增 `SkillRuntimeState` / `SkillRuntimeBundle`，将 Skill catalog、public `skill.*` capability、capability 到 Skill 映射、revision 与刷新结果收口为进程内不可变快照；刷新时按 `SKILL.md` 内容指纹判断是否需要激活新 bundle。
- API runtime 在新 conversation 首次任务提交前刷新 Skill bundle，并同步更新 `CapabilityRegistry`，确保新增、修改或删除公开项目 Skill 后，不重启服务也能影响后续新聊天的 Planner prompt、显式 `skill.*` 路由与 `/api/v1/capabilities`。
- `WorkflowExpander`、`LLMWorkflowProvider`、`AutoWorkflowProvider`、`MainAgentRuntimeReplanner` 与 `SkillWorkflowProvider` 支持动态 Skill macro provider / skill name resolver，避免只更新 registry 后出现 Planner 可见但 macro 展开或 forced skill 执行不可见的半刷新状态。
- `MainAgentExecutor` 支持按 `skill_bundle_revision` 解析对应 Skill catalog；任务调度时记录并 retain revision，运行中任务不会因后续 Skill 刷新改变 forced skill manifest 解析。
- 增加 `skill.bundle_refresh_*` 审计和 `skill_bundle_revision` 计划 / forced skill 事件字段，并补充 integration / orchestration / API 回归测试覆盖新增、删除、显式路由、刷新失败回退、上传先建 conversation 后首次消息、动态 macro 展开与旧 revision 保留。
- 同步更新 `docs/prd/backend/13-Skill动态加载与热部署PRD.md` 状态为 Phase 1 已实现，并明确当前仍以 `script_package_snapshot=false` 作为后续生产级热部署增强边界。

### 2026-05-11 — 新增 Skill 动态加载与热部署 PRD

- 新增 `docs/prd/backend/13-Skill动态加载与热部署PRD.md`，明确当前 Skill 加载仍是 runtime 启动期一次性扫描，新聊天热部署需要以 Skill runtime bundle 为单位同步刷新 catalog、capability registry、macro providers、Planner / Replanner 与主代理 forced skill 执行链路。
- PRD 将“新聊天动态加载”的后端可靠触发点定义为新 conversation 首次任务提交前，并补充新增 / 修改 / 删除 Skill、刷新失败回退、运行中任务 revision 保护、生产级 package snapshot、安全边界、审计事件与测试验收标准。
- 同步更新 `docs/prd/README.md` 与后端 PRD 总览索引，将 Skill 动态加载与热部署纳入后续后端专题基线。

### 2026-05-11 — 接入 SQLQuery 内部 LLM 语义路由

- SQLQuery 内部 `intent_route` 现在会复用已配置的 SQLQuery LLM runtime 判断具体查询路由，即审定品种库、基因型数据库或品种综合概览；LLM 输出仍必须命中已配置 `route_id`，否则回退到原有规则路由。
- 迁移前 SQLQuery native executor 将 `llm_text_generator` 注入到 intent route 阶段，让“具体查哪个库”的判断与 SQL 生成 / 结果筛选共用同一套 SQLQuery LLM 配置；路由判断固定走非流式、`thinking=False` 的轻量调用，不继承前端深度思考开关。
- 高层 LLM Planner 和迁移前 SQLQuery 宏展开不再向内部 intent route 阶段透传 `route_hint`，Planner 只能选择是否调用 SQLQuery public 数据能力，不能代替 SQLQuery 内部 LLM 决定具体数据库。
- 补充 executor 与 API runtime 回归测试，覆盖 SQLQuery 内部路由 LLM 调用、请求元数据透传和后续 SQL 生成 / 结果筛选链路。

### 2026-05-11 — 优化前端生成进度提示

- 前端生成中的助手气泡不再展示“等待任务事件...”，改为在助手气泡内保留转圈并展示当前任务进展，如提交中、任务已提交、正在规划、正在执行 SQLQuery 等。
- 任务事件到达时会用同一套任务状态文案实时刷新助手气泡内处理提示；答案开始流式输出后自动切换为回答内容。
- 修复任务中断后提交补充信息的续跑气泡仍显示固定“已收到补充信息”文案的问题，续跑阶段同样展示可刷新的任务进程。
- 删除右上角“任务进程”悬浮胶囊，避免同一任务进度在页面上重复出现。
- 悬浮发送栏收敛为单一半透明毛玻璃输入胶囊，去掉外层背景、网格与状态字，只保留克制的输入、功能菜单与发送入口。
- 页面按钮统一加入克制的 liquid glass 视觉语言，并弱化主页左侧栏硬分割线、历史列表分割线和卡片头部线条，让整体更轻。
- 功能菜单入口改为严格圆形按钮，`+` 号由字体字形改为两条 CSS 线绘制，避免字体基线导致视觉不居中。
- 新增 `frontend/public/pics/input-menu-plus-button.svg` 与 `frontend/public/pics/send-up-arrow-button.svg` 两个毛玻璃 / 液态玻璃按钮资源，并将发送栏功能菜单与发送按钮切换为图片按钮。
- 重新调整发送按钮图片中的向上箭头为深色主描边，并保留轻微高光，提升箭头与液态玻璃背景的对比度。
- 移除发送按钮 SVG 中覆盖在箭头中心的浅色高光线，改为深色箭头叠加外圈浅色 halo，同时提高发送按钮禁用态图片不透明度，避免空输入时箭头显得过浅。
- 发送按钮的向上箭头最终收口为整支纯深色描边，并取消发送按钮图片禁用态降透明度，确保空输入时箭头仍清晰可见。
- 发送按钮向上箭头从近黑粗描边调为深灰绿细描边，降低图标压迫感并保留足够对比度。
- 为发送按钮图片 URL 增加版本查询参数，避免 Chrome 继续复用同名 SVG 缓存导致箭头视觉调整不生效。
- 悬浮发送栏桌面端宽度缩短约 33%，并改为按右侧工作区中心点居中定位；窄屏仍保持左右安全边距铺开。
- 补充消息输入框长文本回归测试，明确发送栏不设置 `maxlength`，长文本会原样提交给后端。
- 调整发送栏长文本输入态：降低外层和文本域圆角、增加右侧内边距和稳定滚动槽，并将功能按钮底部对齐，避免多行输入被胶囊圆角或滚动条视觉遮挡。
- 去掉左侧用户信息卡片的“用户信息”头部顶栏，仅保留当前用户、账户设置和退出登录操作区。
- 左侧栏内部历史区与用户信息区改为 0 间距贴合布局，并移除左侧栏内部卡片与历史条目的圆角，让边缘更对齐。
- 在左侧历史区与用户信息区之间补充一条低存在感细分隔线，保持贴合布局同时提供轻量区分；分隔线改为半像素浅色伪元素，避免过粗过深。
- 新增 `frontend/src/domain/welcomePrompts.ts` 固化 20 条冷静专业的新任务欢迎语，空对话欢迎区每次进入新空会话时随机展示一句，并移除原固定副标题。
- 发送栏默认占位文本从长提示改为“从这里开始...”，降低输入入口视觉噪音。
- 新增 `frontend/public/pics/account-settings-gear-button.svg` 圆形齿轮图片按钮，并将用户信息卡片的“用户账户设置”改为左下角图形入口。
- 修正账户设置按钮 SVG 齿轮图层的 filter 裁剪范围，并为图片 URL 增加版本参数，避免浏览器继续显示无齿轮的旧缓存。
- 为刷新历史、用户账户设置、输入功能菜单与发送等图标 / 图片按钮补充 hover 小气泡说明，避免仅靠图标猜测按钮功能。
- 将按钮说明小气泡收口为纯 hover 触发，不再因点击后的 focus 状态常驻页面。
- 在已完成的助手回复气泡底部新增灰色图标“复制”操作，可复制该气泡的文本内容；流式生成未完成时不显示该操作。
- 复制操作在浏览器 Clipboard API 拒绝时会降级到临时文本域复制；历史助手消息也会依据 `stream_status=complete` 决定是否展示复制按钮。
- 补充前端任务状态与 App 回归测试，覆盖无回答流时助手气泡内进度刷新和胶囊入口移除。

### 2026-05-11 — 接入 LLM 保守对话补全

- `ConversationMemoryBuilder` 新增保守 LLM resolver：在任务规划前用结构化 JSON 判断当前用户问题是否需要基于同一会话历史补全实体，只有高置信、证据完整且无阻断风险时才采用补全文本。
- 多候选实体补全策略改为“最近明确业务实体优先”；若最近上下文包含多个并列实体且单数指代不清，则不补全。
- LLM resolver 输出非法、调用失败或未启用时保持确定性补全 fallback；SQLQuery 仍只接收系统补全后的 effective question，不接收完整 conversation memory metadata。
- 补充 orchestration / API 测试覆盖 LLM 补全、最近实体选择、并列歧义拒绝和非法输出 fallback，并更新对话记忆 PRD。

### 2026-05-11 — 新增 Agent 基础设施优化建议

- 新增 `docs/Agent基础设施优化建议.md`，沉淀当前 Agent infra 成熟度判断、主要短板，以及生产运行基座、资源治理、可观测性、Capability 插件化与 Skill sandbox 等后续优化优先级。
- 删除已被 PRD / Phase 体系吸收的根目录历史文档：`docs/SQLQuery-LLM版本改造方案.md`、`docs/LLM接入阶段建议.md`、`docs/一期开发计划.md`、`docs/第二阶段评估输入.md`、`docs/一期核心模块边界.md`、`docs/一期验收报告.md`。
- 删除 `docs/dev_processes/` 开发流程目录，将当前文档权威源收口到 `docs/prd/`、`README.md`、`AGENTS.md` 与 `CHANGELOG.md`。
- 将 `docs/一期验收报告.md` 的关键验收结论、测试证据与一期边界并入 `docs/prd/backend/00-主代理框架PRD.md`，避免删除历史文档与开发流程目录后丢失验收依据。
- 更新 `README.md`、`AGENTS.md`、`docs/prd/README.md`、后端专题 PRD、前端 PRD、Capability 接入指南与本建议文档中的引用说明，避免正式入口继续指向已删除历史文档或开发流程目录。
- 历史文档清理完成后，移除 `docs/Agent基础设施优化建议.md` 中的“历史文档清理判断”章节，让该文档聚焦 infra 优化建议。
- 本次仅调整文档，不改变运行时代码与测试基线。

### 2026-05-11 — 调整前端历史记忆条目交互

- 左侧历史 / 记忆栏条目从 Ant Design 气泡按钮改为扁平列表行，使用左侧农业绿强调线标记当前会话，减少侧栏视觉噪音。
- “重命名”“删除”操作收纳到条目右侧悬浮操作区，默认隐藏，仅在鼠标悬停或键盘聚焦该历史条目时浮现。
- 移除“历史会话”标题旁边的数量计数，把“刷新”文字按钮改为仅图标按钮，保留无障碍刷新标签。

### 2026-05-11 — 优化前端流式生成滚动跟随

- 调整前端 `conversation-list` 自动滚动策略：流式生成期间仅当用户当前视角已在对话底部附近时跟随新内容滚到底部；用户主动滚动查看历史消息时保持当前位置，不再被增量输出强制拉回最新气泡。
- 切换会话时重置为默认跟随最新消息，避免沿用上一会话的历史浏览状态。

### 2026-05-11 — 收口 LLM-only Planner 编排语义

- 调整自动规划语义：除用户请求明确携带 `capability_id` 的显式能力路由外，`routing_mode=auto` / 无显式 capability 的请求一律由 LLM Planner 基于 public capability pool 和对话记忆自行编排。
- `LLMWorkflowProvider` 不再在 Planner 输出非法 JSON、内部 capability、无效 DAG 或 provider 异常时静默退回 deterministic `AutoWorkflowProvider`；Planner 输出校验失败时只允许同一个 LLM 自修复一次，仍失败则 fail-closed 标记任务失败并记录 `planning_failed` 审计。
- 强化 Planner prompt：追问、参数调整、继续上次任务等请求必须结合 conversation memory 判断是否继续调用上一轮相关 public capability，禁止依赖系统确定性路由替 LLM 做能力选择。
- 默认 runtime replanner 在 LLM Planner 启用时只保留 LLM 型主代理 replan advisor；SQLQuery 专属确定性 runtime replan 仅在显式禁用 LLM Planner 的 legacy / 测试模式下保留。
- 补充 / 更新 orchestration 与 API 回归测试，覆盖 Planner 自修复、失败不 fallback、显式 capability 直达仍保留，以及 planning failure 事件语义。

### 2026-05-09 — 调整前端业务对话台视觉与任务提示

- 前端对话台改为浅米色页面底色与农业绿重点色，并调整为左侧历史会话栏、右侧完整对话工作区、底部悬浮输入栏的布局。
- 输入区域收口为单行发送栏加发送按钮，上传文件、思考强度、深度思考和当前任务取消保留在悬浮工具区，避免打断主要输入路径。
- 移除前端“未完成任务”提示显示栏及其轮询、API client 包装、响应类型、样式和测试依赖，同时保留当前任务取消能力。
- 移除前端工作区顶部错误提示栏，把事件流中断、历史会话加载失败、提交失败、上传失败等原提示栏消息统一改为 5 秒自动消失的浮层提示。
- 移除前端工具区里的“当前模式：自动规划”显示文案，自动规划提交逻辑保持不变。
- 将聊天输入区改为固定在页面底部的胶囊输入框，右侧新增“+”功能菜单承载上传文件、思考强度、深度思考和当前任务取消能力。
- 移除顶部“主代理可用 / SQLQuery可用”、`user:` 与 `conversation:` 状态标签，并清理前端不再使用的能力目录展示调用。
- 收紧前端页面滚动模型：根页面不再共享滚动条，右侧仅对话内容区独立滚动且顶部栏不动，左侧仅历史卡片内容独立滚动且“小奥Agent”标题不动。
- 放宽右侧对话工作区宽度限制，让对话卡片和底部输入胶囊占满右侧可用空间。
- 恢复用户 / 助手消息气泡原有宽度表现，仅补齐 `conversation-list` 及对话卡片 body 的 100% 宽度，让动态对话列表容器占满右侧对话框。
- 移除空对话欢迎区的固定最小高度，避免对话动态区域在空状态下额外设置视觉下限。
- 移除对话内容外层卡片包装，让 `conversation-list` 直接填满 `app-content`，底部 `chat-floating-stack` 继续作为固定浮层覆盖在其上。
- 新增对话流式生成期间的自动滚动锁定：`conversation-list` 在新消息和助手增量更新后自动滚到最新内容底部，保持视角跟随正在生成的信息气泡。
- 将拖拽上传命中区提升到 `chat-floating-stack`，拖入文件时展示农业绿虚线与提示气泡，释放后自动上传，同时保留输入框、加号菜单和发送按钮的点击行为。
- 将“用户信息”“用户账户设置”和“退出登录”集中收纳到左侧栏底部用户卡片，使账户操作固定在历史栏下方且不随历史列表滚动。
- 移除右侧工作区顶部栏，把“任务进程”改为右上角靠边悬浮胶囊，保留下拉任务详情但减少对对话区域的占用。

### 2026-05-09 — 完成 Skill 一等 Capability 能力池开发

- 将符合公开范围的项目级 Skill 注册为 `skill.*` public capability：新增 Skill capability 映射模块，支持 manifest 显式 `capability_id`、稳定名称派生、public root 过滤、unsupported runtime 排除、非法 / 重复 / reserved id 跳过诊断，并保留用户级 Skill 只走主代理内部 matcher 的兼容路径。
- 调整 API runtime 装配顺序，先解析 `SkillCatalog` 与 public skill roots，再把 Skill descriptor 注册进同一个 `CapabilityRegistry` public pool；`LLMWorkflowProvider`、`MainAgentRuntimeReplanner`、`AutoWorkflowProvider` 与 `WorkflowRouter` 共用包含 SQLQuery 与 `skill.*` 的 macro provider 映射。
- 新增 `SkillWorkflowProvider`，把 Planner / 显式路由选择的 `skill.*` public macro 安全展开为 `main_agent.respond` forced skill 节点；Planner 输出的 Skill `input_payload` 默认 fail-closed，不允许注入脚本路径、forced skill 字段或任意业务参数。
- 扩展 `CapabilityDescriptor`、API DTO 与 `/api/v1/capabilities` 响应，返回 `kind` / `source` 以区分内置能力与 Skill capability；`JsonlAuditSink` 新增启动期同步审计，记录 `skill.capability_registered` 与 `skill.capability_registration_skipped`，只保存 public root 相对路径摘要或 outside-public-roots 标记。
- 主代理执行器支持系统注入的 `forced_skill_name` / `forced_skill_capability_id`，forced Skill 优先于文本 matcher 并产生 `skill.forced_selected` / `skill.forced_missing` 审计事件；编排执行层会剥离用户请求 metadata 中伪造的 forced skill 保留字段，只有节点 metadata 可传入 forced skill。
- 调整 Planner / Replanner 的 answer-producing 尾节点规则：`main_agent.respond` 与 `skill.*` 不再被追加冗余最终主代理节点；非回答型数据能力尾节点仍保持“能力结果 → 主代理最终回答”的 finalizer 行为。
- 补齐 `tests/integrations/codex_skills/test_skill_capabilities.py`、`tests/orchestration/test_llm_workflow_provider.py`、`tests/orchestration/test_workflow_router.py`、`tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py`、`tests/api/test_capabilities_list.py` 与 `tests/api/test_skill_capability_pool.py`，覆盖能力注册、public/private root、Planner 可见性、安全展开、forced skill、安全 metadata 剥离、API 目录与 fake planner 端到端路径。
- 本次从 `.omx/context/skill-capability-pool-20260509T051631Z.md` 与下方中断记录继续完成；中断记录保留作为上下文，不再代表当前实现状态。

### 2026-05-09 — 新增 Skill 一等 Capability 能力池 PRD

- 新增 `docs/prd/backend/12-Skill一等Capability能力池PRD.md`，定义项目级 Skill 升级为 `skill.*` public capability 的目标、边界、注册规则、Planner / Replanner 可发现性、forced skill 执行模型、安全审计、验收标准与测试计划。
- 同步更新后端 PRD 总览、PRD 总索引与后端开发流程索引，将该专题列为后续 Phase 8.5 输入，明确 Skill 与 SQLQuery 等内置能力应进入同一个 public capability pool。

### 2026-05-09 — Skill 一等 Capability 能力池开发中断记录

- 已按 `$ralph` 启动 `docs/prd/backend/12-Skill一等Capability能力池PRD.md` 的实现，并建立上下文快照 `.omx/context/skill-capability-pool-20260509T051631Z.md`；本轮在实现中途被用户有意中断，代码尚未达到可验收状态，下一次应从当前工作树继续而不是视为完成。
- 已先补失败测试以锁定目标行为：`tests/integrations/codex_skills/test_skill_capabilities.py`、`tests/orchestration/test_llm_workflow_provider.py` 的 Skill public macro 用例、`tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py` 的 forced skill 用例、`tests/api/test_capabilities_list.py` 与 `tests/api/test_skill_capability_pool.py` 的 API / fake planner 用例。
- 当前部分实现已开始：新增 `src/integrations/codex_skills/skill_capabilities.py`、`src/orchestration/skill_workflow_provider.py`，并修改 `CapabilityDescriptor` / `WorkflowNodePlan` 增加 `kind`、`source`、节点 `metadata`，让 `OrchestrationService` 把节点 metadata 合并进 capability execution metadata；`LLMWorkflowProvider` 与 `MainAgentRuntimeReplanner` 已开始把 `skill.*` 视为 answer-producing capability，避免重复补主代理 finalizer。
- 当前 `MainAgentRespondCapability` 已开始支持 `forced_skill_name` / `forced_skill_capability_id`，会生成 `skill.forced_selected` / `skill.forced_missing` 审计事件；但相关实现还需继续审查事件顺序、缺失 forced skill 的失败 / fallback 语义，以及与脚本参数解析和 artifact 收集的完整兼容性。
- 当前 `src/api/runtime.py` 只改到引入 `build_skill_capability_registry` / `SkillWorkflowProvider` 并给 `build_api_runtime()` 增加 `public_skill_roots` 参数，尚未完成 runtime 装配顺序调整、Skill descriptor 注册、Skill macro provider 注入、WorkflowRouter 显式 `skill.*` 路由、API DTO `kind/source` 输出、测试 support 的 `public_skill_roots` 透传。
- 已运行一次目标失败测试命令，失败原因符合“实现尚未完成”：缺少 `skill_capabilities` / `skill_workflow_provider` 模块、测试类名误写、`APITestCase.reconfigure_runtime()` 尚不接受 `public_skill_roots`；之后已新增模块并修正部分代码，但尚未重新跑完整目标测试。下一次应先修完 runtime/API/support wiring，再运行 `tests/integrations/codex_skills`、`tests/orchestration`、`tests/capabilities/main_agent`、`tests/api` 相关回归。

### 2026-05-08 — 新增 Skill 参数解析与结构化入参传递

- 新增 `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`，定义 Skill 脚本产出 HTML / CSV / XLSX / PDF 等文件时的平台统一 managed artifact、受控输出目录、下载鉴权、prompt 安全摘要与前端附件展示边界；补充 v1 输出文件不设应用层单文件大小上限，但同一 account / conversation 只保留一个 active 输出文件，新输出顶替并删除旧输出，旧下载地址对外统一返回 `404` 并在内部审计记录 gone；HTML v1 只下载不做站内 sandbox 预览；单次 Skill 产出多个合法输出文件时由平台用 Python 标准库 `zipfile` 打包为 1 个 zip artifact；前端 v1 统一使用附件卡片，不做 Skill 类型定制文件 UI；Skill manifest `outputs.files` v1 可选，默认使用全局 allowlist，声明后只能收紧不能放宽。
- 落地 Skill 输出文件 artifact v1：`SkillScriptRunner` 为脚本提供 `MAF_SKILL_OUTPUT_DIR`，平台校验 `output_files` 相对路径、类型、manifest 收紧规则与 zip entry 安全；新增本地 managed artifact file store、单 active 输出替换/删除、旧 artifact 下载 404/gone 审计、`/api/v1/artifacts/{artifact_id}/download` 下载接口，以及前端统一“生成文件”附件卡片。
- 补强输出文件生命周期失败边界：新 artifact metadata 持久化后才 supersede 旧 active 输出；旧 metadata 替换失败会拒绝新输出并保留旧 active；旧文件正文删除失败不再暴露旧下载；主代理 LLM 在文件收集后失败时仍保留可下载的新 file artifact。
- 更新 `Codex-Skill构建指南.md`，补充脚本下载文件必须写入 `MAF_SKILL_OUTPUT_DIR`、通过 stdout `output_files` 声明、HTML 只下载、多文件由平台打包 zip、源压缩包默认拒绝等构建规则。
- 进一步补齐 Skill 输出文件构建规则：明确平台默认允许的扩展名、MIME 必须匹配扩展名、hardlink 禁止、`outputs.files` 只能收紧不能放宽；本地 mini BreedStat RCBD Skill manifest 收紧声明为 `.html` / `text/html` 输出。
- 修复前端输入框 IME 组合态 Enter 误发送问题：中文输入法确认英文 / 候选词时不提交消息，非组合态 Enter 仍发送，Shift+Enter 仍保留换行。
- 修复前端对话 Markdown 表格解析过严的问题：兼容助手输出中短列对齐分隔符如 `:--`，避免 RCBD / SQL 等结果表被当作普通段落显示。
- Codex Skill manifest 新增 `parameters` / `input_parameters` 扩展，支持 Skill 自声明业务参数的类型、必填性、别名和正则解析规则；主代理在自动脚本执行前调用通用 resolver，把解析结果作为脚本 stdin 顶层字段注入。
- 参数 resolver 新增 LLM 缺参补槽 fallback：确定性解析成功时不调用 LLM；仍缺少文本型标量参数时复用主代理 LLM runtime 生成候选 JSON，并经系统字段名、类型、source 与 artifact 边界校验后才注入脚本 payload。
- 主代理 Skill 自动脚本路径新增 `skill.input_resolved` / `skill.input_missing` 审计事件；缺少必填参数时不再盲目执行脚本，而是把结构化缺参结果注入最终 prompt，避免 LLM 空口承诺补参但脚本未收到参数。
- 参数解析层只读取当前问题、当前用户原文和安全的最近用户消息，脚本 payload 继续剥离完整 conversation memory / history summary / recent messages / resolved question，保持跨轮参数继承与上下文安全边界分离。
- Skill 构建指南补充参数契约规则：脚本可接受的所有业务参数都必须列入 manifest，无默认值且必需的参数声明 `required: true`，有默认值的参数声明为非必填并写明 `default`，枚举型参数必须列出完整 `enum` 可接受值。
- 本地 mini BreedStat RCBD Skill manifest 按参数契约补齐 `material_data`、`planter`、`seed`、`site_num`、`site_random`、`check_position_constraint` 与 `test_position_constraint` 声明，明确必填项、默认值和 `planter` 枚举范围。
- 扩充本地 mini BreedStat RCBD Skill 的自然语言触发表达，覆盖“随机区组”、随机区组设计/试验、fieldbook、田间小区排布、对照位置约束、多点/多环境随机区组以及英文 RCBD / plot layout 说法。
- 调整 `.gitignore`，允许项目级 `skill/` 目录随仓库入库，同时继续忽略 Skill 运行输出目录，避免后续 Skill 文件变更被本地忽略规则吞掉。
- 修复会话记忆实体抽取把“要求2”误当品种实体的问题，并让本地 mini BreedStat RCBD wrapper 支持“2次重复”解析为 `blocks=2`。
- 补充 parser、resolver、main_agent、conversation memory、API runtime 与本地 RCBD wrapper 回归测试，并更新 `Codex-Skill构建指南.md` 的参数声明与脚本输入边界说明。
- 按 `Codex-Skill构建指南.md` 收口本地 `skill/mini_breedstat_rcbd_skill`：补齐可被后端解析的 `SKILL.md` frontmatter / triggers / outputs / parameters / Python auto-run script manifest，新增 `scripts/run_rcbd.py` wrapper 以受控方式调用包内 R 脚本和 `Rscript`，并移除 Skill 包内 README / PRD / 历史 outputs 产物。
- 验证本地 mini BreedStat RCBD Skill 可被 `SkillCatalog` 发现并匹配，可通过上传 CSV 生成 30 行 RCBD fieldbook 与 `rcbd_layout.html` 输出文件；回归 `tests/integrations/codex_skills` 与 `tests/capabilities/main_agent` 通过。

### 2026-05-08 — 落地对话上下文记忆与压缩 v1

- 新增 `ConversationMemoryContext` 构建链路，在 workflow provider 规划前按当前 conversation / account 读取历史消息、assistant 最终回答和安全摘要，并生成 `current_user_message` / `resolved_user_message` / recent messages / history summary 等 prompt-safe 上下文。
- 新增 conversation memory SQLite 摘要快照表与 storage port，支持按 conversation / account 保存、读取最新摘要和删除会话时级联清理；摘要覆盖边界后的任务不会通过 TEXT artifact 回流进 prompt。
- Planner、AutoWorkflow、SQLQuery public macro 与主代理 workflow 统一使用系统侧 `effective_user_message`，使“那它的基因型呢”等追问在 LLM Planner 禁用或 fallback 时仍可路由到明确 SQLQuery 问题。
- 主代理 prompt 新增带边界标注的对话记忆区块，并在 Skill 自动脚本与 SQLQuery 内部 LLM 调用前剥离完整 conversation memory metadata，防止历史记忆、SQL、rows、guard token 或上传原文越界透传。
- 补充 storage / orchestration / API runtime / main_agent / SQLQuery 分层测试，覆盖 root message 排除、assistant artifact 去重、摘要复用、summary entity 指代补全、权限隔离、安全 allowlist、脚本隔离、fallback 审计与 SSE terminal event race。

### 2026-05-08 — 补强 Capability 接入指南

- 更新 `docs/Capability接入指南.md`，根据当前 SQLQuery 接入主代理的实际链路补充显式 capability 路由、主代理 dependency context 收束、运行时重编排接入、测试面与检查清单说明。

### 2026-05-08 — 明确对话记忆 token 预算配置来源

- 更新对话上下文记忆与压缩 PRD，明确 `conversation_memory_max_tokens` 逻辑值默认取启动配置 `config.yaml` 的 `trim_max_tokens`，运行期从已 bootstrap 的 `MAF_CONFIG_TRIM_MAX_TOKENS` 环境变量读取，业务节点不重复读取配置文件。
- 进一步澄清 `trim_max_tokens` 是本轮上下文工程总 token 上限来源，实际可用于对话记忆的预算需先扣除 system prompt、当前问题、上传摘要、上游能力结果、Skill 上下文和模型输出预留空间，不能让历史记忆直接用满窗口。
- 补充 SQLQuery 追问补全契约：上下文改写不能依赖 LLM Planner 自由改写 `user_question`，应由系统侧 memory builder / orchestration 生成可信 effective question，再通过系统 payload 注入 public capability。
- 明确记忆上下文 / effective question 必须在 workflow provider 构建 plan 前生成，LLM Planner 与 deterministic `AutoWorkflowProvider` fallback 都要使用同一份系统侧补全结果，避免 fallback 路径退化为单轮判断。
- 补充主代理最终回答去重规则：同一 `task_id` 已有 assistant history message 时，记忆构建优先使用 message，不再重复读取同任务最终 TEXT artifact。
- 明确 conversation memory 必须使用独立 `ConversationMemorySafeAllowlist`，不得直接复用主代理 dependency context allowlist，避免 `rows`、`candidate_rows`、`storage_ref`、SQL 或 schema DDL 等进入长期记忆。
- 补充 Skill 脚本边界：conversation memory 默认不得通过 `request.metadata` 原样透传给自动脚本，如需携带相关信息必须先剥离或替换为脚本专用 allowlist。
- 明确跨轮上传文件引用边界：记忆只保存 upload 脱敏摘要 / `upload_id` / 文件名 / preview 等安全 metadata，不持久化完整内容；原始内容仅在 `InMemoryUploadStore` 仍有效且权限校验通过时可继续使用。
- 补充摘要快照字段与 LLM 注入边界：新增 `account_id`、source hash、版本、safe model metadata、last error 等持久化 / 审计字段，并明确正常 Planner / 主代理 prompt 只注入带边界标注的 `history_summary`，不传存储 metadata。
- 明确当前任务 `root_message_id` 对应 user message 必须从 `ConversationMemoryContext` 历史部分排除，当前用户问题只出现在独立 current_user_message 区块，避免重复注入。
- 新增澄清 / interrupt answer 消息规则，明确补充信息应标注为当前任务 clarification message，不作为新的独立业务轮次或当前问题，也不得在摘要中覆盖原始 root message。
- 全面扩展对话记忆 PRD 的验收标准与测试计划，覆盖 effective question 系统生成、fallback 自动规划、去重、安全 allowlist、Skill 脚本隔离、跨轮上传边界、clarification 归并、摘要 metadata 不入 prompt、安全审计与分层回归命令。
- 补齐 `ConversationMemoryContext` 模型字段，新增 `root_message_id`、`current_user_message`、`resolved_user_message`、`clarification_messages` 与 `resolution_metadata`，并要求补全问题不得覆盖用户原文。
- 明确 prompt 当前问题区块必须区分 `current_user_message` 用户原文与 `resolved_user_message` 系统补全 effective question；Planner / public capability 可优先用补全结果，最终回答仍需保留用户原文边界。
- 明确对话记忆可选配置默认与覆盖规则：`recent_turns` 默认 6 个业务轮次，summary 预算由实际 memory 可用预算派生，summary LLM 默认启用但可通过 runtime / 已 bootstrap 环境变量禁用，禁用时走 fallback 不阻塞请求。

### 2026-05-07 — 将 MySQL 连接配置迁移到本地 config.yaml

- 移除 `src/mysql_engine.py` 中硬编码的真实 MySQL 连接串，改为从本地 `config.yaml` 的 `mysql_readonly.url` 或部署环境变量 `MAF_MYSQL_READONLY_URL` 读取。
- 更新 SQLQuery / MySQL 相关 README、AGENTS 与 PRD 说明，明确真实数据库地址、账号、密码不得进入 tracked 文件。
- 将 SQLQuery schema metadata 中的真实库名替换为逻辑库名，并补充 MySQL engine 配置解析测试，降低仓库泄露数据库访问权限的风险。

### 2026-05-07 — 新增 JSON/CSV 上传内存暂存并接入 Skill 脚本

- 新增 conversation 级 JSON/CSV 上传接口，登录用户可上传文件到进程内存暂存区，返回 `upload_id`、文件摘要、列名和行数 preview；上传记录按用户和 conversation 隔离，并带大小、类型、TTL 与数量约束。
- 消息提交支持 `metadata.upload_ids` 引用上传文件，主代理 prompt 只注入脱敏文件摘要，Skill 自动脚本通过 `uploaded_artifacts[].content` 获取原始 JSON/CSV 内容。
- 前端输入区新增“上传文件”入口、拖拽上传区域和暂存区文件列表，发送消息时自动携带当前暂存文件的 `upload_ids`。
- 暂存区支持按 conversation 查询与按文件删除，用户点击删除后会同步移除后端进程内存记录；若后续消息仍引用已删除或过期的 `upload_id`，后端会记录到 `missing_upload_ids` 并忽略该文件。
- 更新 Codex Skill 构建指南中脚本输入说明，补充受控上传入口下脚本可读取原始文件内容、LLM prompt 仍只接收摘要的边界。

### 2026-05-07 — 改造 mini BreedStat RCBD Skill 为项目兼容形态

- 将 `skill/mini_breedstat_rcbd_skill/SKILL.md` 改为本项目可解析的 YAML frontmatter + 中文 Skill 指令，声明 `mini-breedstat-rcbd`、中文触发词、`runtime: python` 自动脚本与 `answer` 输出契约。
- 新增 `scripts/run_rcbd.py` 作为 Python wrapper，负责本系统 JSON stdin/stdout、Rscript 查找、临时输入文件、R 脚本调用和结构化错误返回；保留原 `run_rcbd_local.R` / `render_rcbd_layout_html.R` 业务逻辑链路，不重写 RCBD 算法。
- 更新该 Skill 的 README 与 R 依赖说明，移除旧 `.codex` / Windows Rscript 路径口径；将 RCBD 核心依赖切换为 Skill 包内 `scripts/rcbd_design_core.R`，并补充兼容性回归测试覆盖项目 Skill Catalog 发现、缺输入 JSON 输出与 bundled R 核心依赖成功执行。
- RCBD Skill 的 R 执行链路已迁移到 `scripts/rcbd_design_core.R`，并删除旧 `scripts/design_Functions.R`，避免后续维护时继续引用过期核心脚本。

### 2026-05-07 — 前端品牌文案改为小奥Agent

- 将前端页面标题、顶部主标题、登录卡片与创建用户卡片中的“业务对话台”统一改为“小奥Agent”，将顶部副标题改为“AI育种助手”，并同步更新前端测试断言与 HTML title。

### 2026-05-07 — 新增 Codex Skill 构建指南

- 新增 `Codex-Skill构建指南.md`，面向 Oh-my-codex `skill-creator` 说明本系统可加载 Skill 的项目根目录 `skill/`、frontmatter、触发匹配、prompt 注入、受控 Python 脚本、Rscript wrapper、JSON IO、项目正式依赖快照、验证命令与常见错误。
- 将项目级 Skill 默认扫描目录从 `.codex/skills` 调整为仓库根目录 `skill/`，保留用户级 `~/.codex/skills` 兼容入口。
- 将仓库根目录 `skill/` 加入 `.gitignore`，作为本地 Skill 工作区使用，避免同事各自构建或调试的 Skill 包默认进入版本控制。
- 同步更新 README、开发流程索引、Phase 8 文档与主代理 Skill PRD，明确本系统兼容的是受控 Skill runtime，不复刻完整 Codex workspace / plugin / shell 能力。

### 2026-05-07 — 新增对话上下文记忆与压缩 PRD

- 新增 `docs/prd/backend/10-对话上下文记忆与压缩PRD.md`，定义 conversation 内会话延续型记忆、Planner / 主代理 prompt 注入范围、两级压缩策略、摘要持久化、安全审计与测试验收口径。
- 同步更新 PRD 总索引、后端 PRD 总览与后端开发流程索引，将对话记忆与 compression engineering 纳入后续 Phase 8.3 实施输入。

### 2026-05-05 — 调整 SQLQuery LLM 设置与复合查询拆分

- 修复“品种信息 + 基因信息”类复合数据库问题未拆分的路由判断：当问题同时包含明确的品种信息表达和基因/基因型意图时，AutoWorkflow 与 SQLQuery intent route 会拆成审定品种库与基因型库两个 SQLQuery 分支，再由主代理汇总。
- SQLQuery 内部 LLM 调用继续使用独立 runtime、非流式执行，但默认跟随主代理通用 `deep_thinking` / `main_agent_thinking_enabled` 与 `main_agent_reasoning_effort` 设置；保留显式 `sql_query_reasoning_effort` 作为兼容覆盖入口。
- SQLQuery `sql_generate` 与 `result_filtering` 只消费 LLM 的最终 answer 文本，忽略非流式返回结构中的 `reasoning_content` / `reasoning`，避免推理内容进入 SQL/JSON 业务解析。
- 补充复合路由、自动工作流、SQLQuery LLM thinking/reasoning 透传与 reasoning 内容忽略测试，并完成 SQLQuery、API、orchestration、e2e 回归。

### 2026-05-05 — 新增历史会话命名与重命名

- 复用现有 `Conversation.title` 作为“对话名称”存储位置：每轮用户消息提交后都会检查当前会话是否已有标题；若标题为空，则收集该会话下所有用户消息，异步调用主代理 LLM runtime 生成短标题；标题生成固定 `thinking=False`、`reasoning_effort="minimal"`，失败不会影响消息提交或任务执行，并会在后续仍无标题的轮次继续重试。
- 新增会话重命名 API：`PATCH /api/v1/conversations/{conversation_id}`，按当前登录用户校验 conversation 归属，标题会做去空白、非空与 60 字符上限校验。
- 前端历史会话列表新增“重命名”按钮，用户输入新名称后调用后端持久化并即时更新历史栏显示；取消或空名称不会发起重命名请求。
- 补充后端会话标题自动生成、失败后按全部用户消息重试、重命名隔离测试与前端 API / 历史栏重命名测试，并完成 API、storage、e2e、前端 Vitest 与 build 回归。

### 2026-05-05 — 加固 SQLQuery 品种综合概览召回

- SQLQuery `variety_overview` SQL 生成 Prompt 新增“独立召回再合并”约束：宽泛品种概览需要从 `*_varieties.variety_name LIKE` 独立召回审定信息，并从 `variety.variety_name LIKE` + `rice_comp` 独立召回基因型/籼粳成分；`ref_var_id` / `variety_id` 仅作为辅助关系，不能作为唯一召回条件。
- SQLQuery SQL 生成增加 `variety_overview` LLM 输出校验：当 LLM 对审定表没有各自 `variety_name LIKE` 召回而试图依赖 ID join 时，会触发 deterministic fallback，避免再次出现“只查到基因型成分、审定字段全空”的回答。
- `variety_overview` deterministic fallback 改为审定库行与基因型库行 `UNION ALL` 独立返回，并补充审定品种详情字段（品种来源、特征特性、产量表现、栽培要点、适种区域、审定意见）；不再把审定行通过不可靠 `ref_var_id` join 混入基因型成分。
- `schema_metadata.yaml` 将 `rice_varieties.ref_var_id -> variety.variety_id` 标注为弱关联/辅助富化关系，Prompt 中的 join hint 会携带说明，降低 LLM 误用连接关系的概率。
- 补充 Prompt 与 SQL 生成回归测试，并用真实 MySQL smoke 验证“查一下龙粳33的品种信息”现在返回审定库记录（`黑审稻2012007`、2012、申请者、品种来源）和独立基因型成分记录（粳稻成分 `90.33519600`）。

### 2026-05-04 — 新增登录权限系统与用户隔离历史基础

- 后端新增数据库用户、PBKDF2 密码哈希、4 位动态验证码、HttpOnly Cookie Session 登录/退出/me 接口，并将消息提交、任务查询、SSE、取消、graph、artifact、interrupt 等业务接口改为登录后访问。
- 新增 `POST /api/v1/auth/register` 创建用户路径，注册时强制用户名规范与密码策略（至少 8 位且必须同时包含字母和数字），重复用户名返回 409，注册成功后自动写入 session cookie。
- 所有会话/任务资源改为以后端 session 解析出的 `username` 做归属校验，提交消息时不再信任前端 `account_id`；补齐按当前用户过滤的历史会话与历史消息 API，并在任务完成后持久化助手文本消息用于历史恢复。
- 前端新增登录/创建用户切换页、验证码刷新、登录态恢复/退出、用户名展示、历史会话列表与历史消息加载；SSE 使用 cookie 登录态，API client 默认携带 `same-origin` credentials。
- 修复前端点击历史会话的续聊语义：即使点击的是当前已恢复的 active conversation，也会重新加载该历史消息到对话框，并在后续发送时继续使用该历史 `conversation_id`。
- 新增用户历史会话删除能力：前端历史列表提供删除入口，后端 `DELETE /api/v1/conversations/{conversation_id}` 校验当前登录用户归属；若该会话仍有未完成任务会自动取消运行中任务并终止 runtime handle，然后硬删除该 conversation 下的消息、任务、节点、边、产物、事件、中断、checkpoint 与 mailbox 业务记录，保留认证表和 append-only audit 日志。
- 移除前端输入数据库类关键词时出现的 SQLQuery 路由提示栏，并精简空对话欢迎语，避免对用户展示内部路由选择建议。
- 移除对话区下方常驻任务进程状态栏，将“准备就绪 / 正在准备数据库查询 / 任务已完成”等任务进程状态收纳到顶部栏“任务进程”点击展开浮层中。
- 更新 `AGENTS.md` 开发准则：明确当前项目不再追求“最小糊上”的临时实现，后续代码需按长期交付标准保证稳健、可维护、无冗余且逻辑闭环。
- 补充 auth storage/API 多用户隔离测试与前端登录/历史测试，并完成后端分层 unittest、前端 Vitest 与 build 回归。
- 整改 SQLQuery 数据库路由：新增配置驱动的 `QueryUnderstandingService` 统一 AutoWorkflow 与 SQLQuery intent route 判断，并为 intent route 预留受校验的可选 LLM 语义路由 seam；审定品种查询缺作物时改为多作物审定表宽查，审定信息 + 基因型信息复合问题会自动拆成两个 SQLQuery public 数据能力分支后由主代理汇总，并把 route candidate / subtask / no-crop broad / LLM router fallback 元数据写入 intent/schema 输出。

### 2026-04-30 — 完成前后端本地联调启动验证

- 使用 `scripts/run_fullstack_dev.py --frontend-port 3000 --backend-port 8000` 拉起真实后端与 Vite 前端，确认前端固定运行在 `http://127.0.0.1:3000/`，并通过 Vite proxy 转发 `/api` 到后端 `http://127.0.0.1:8000`。
- 验证 `GET /`、前端代理路径 `GET /api/v1/capabilities` 与后端直连 `GET /api/v1/capabilities` 均返回 `200 OK`，能力列表包含主代理与当时的 SQLQuery public 数据能力。
- 工作结束时已停止本地全栈开发进程，并确认 3000 / 8000 端口无监听进程残留。

### 2026-04-28 — 补充 tiktoken 依赖与 Token 计数工具

- 主代理编排内核新增运行时受控重编排闭环：`OrchestrationService` 可在节点执行结果或完成判定后调用 `RuntimeReplanner`，在 `max_replans` / `max_dynamic_nodes` 预算内校验 revised DAG、追加新节点、orphan 未执行旧节点并继续调度；新增 `task.replan_started`、`task.graph_updated`、`task.replanned`、`task.replan_rejected` 事件，避免 `REPLAN_AVAILABLE` 只记录后直接失败。
- 新增迁移前 SQLQuery runtime replanner 与 `result_filtering.satisfaction` 输出契约：当单个 SQLQuery 数据能力结果明确建议重排，且用户问题包含多作物 / 多地区并列查询时，会在运行时拆成多个 SQLQuery public 数据能力节点并由 `main_agent.respond` 汇总；编排层只负责通用 revised DAG 校验、预算与调度，不承载 SQL/schema/农业领域规则。
- 主代理 LLM runtime 收口为单实例共享：默认自动模式下，planner 高层 DAG、运行时观察/重排 advisor 与 `main_agent.respond` 最终总结通过同一个主代理 `SharedLLMRuntime` 调用；SQLQuery 内部 `sql_generate` / `result_filtering` 使用独立 SQLQuery LLM runtime，固定非流式、`thinking=disabled`，不复用主代理 LLM 实例。显式组件级 fake / override seam 保留。
- 主代理编排、运行时重排、主代理 Skill 注入说明与 SQLQuery SQL 生成 / 结果筛选的 LLM-facing Prompt 统一改写为中文表达；保留 JSON / SQL 字段名等机器契约，降低中文模型理解英文系统提示的偏差。
- `deep_thinking` / `main_agent_reasoning_effort` 现在会作用于主代理编排阶段；planner 使用同一 runtime 的 thinking 流收集推理片段，并通过 `main_agent.reasoning_delta`（`stage=orchestration_plan`）在前端可见事件中展示。
- 新增 `MainAgentRuntimeReplanner`：当节点输出的 `satisfaction` 明确未满足且仍有预算时，主代理可用同一 LLM runtime 观察当前结果并返回 public-only revised DAG；编排层继续负责 capability registry、macro expansion、DAG/预算校验和 graph update 事件。
- `MainAgentRuntimeReplanner` 的 observation prompt 增加 allowlist sanitizer 与 token budget：只传满足度、行数、route/schema、截断状态、少量 capped row sample / summary，过滤 SQL、guard token、schema DDL、完整 rows 等 capability 内部或高成本字段。

- 调整 LLM 配置读取方式：`build_api_runtime()` 在启动期将 `config.yaml` bootstrap 到 `MAF_CONFIG_*` 进程环境变量，`LLMClient`、SQLQuery `trim_max_tokens`、Planner / 主代理 / SQLQuery LLM runtime 后续均从环境读取，不再在客户端构造或节点执行阶段重复读取配置文件；显式注入 `config` dict 仍作为测试和定制 runtime seam 保留，切换配置源时会清理旧环境值，且同一 runtime 的多个 `*_config_path` 必须指向同一启动配置文件。
- 在 `multi_agent` Conda 环境中安装 `tiktoken==0.12.0`，并将其运行依赖 `regex==2026.4.4` 同步写入 `requirements.txt`。
- 将根目录临时 `get_token_num.py` 归入 `src/integrations/token_counter.py`，作为系统运行时可复用的上下文 token 数量估算辅助工具，并补充集成层回归测试。
- SQLQuery `result_filtering` 在调用 LLM 前会按 `trim_max_tokens` 从最新行开始裁剪候选数据 token 预算，避免 MySQL 原始结果过大撑爆筛选 prompt，并在输出 / audit metadata 中记录 token trim 结果。
- SQLQuery schema context 不再用规则函数预先挑选业务字段，而是在 route / table 范围内暴露全部 LLM 可见字段；`sql_generate` 由 LLM 自行选择 SELECT / WHERE 字段并添加过滤条件，`column_types_used` 缺省时由系统从 schema 推导，避免漏回填类型导致正确 SQL 被 fallback 覆盖。
- SQLQuery SQL 生成与 SQL Guard 不再默认补充或强制要求 `LIMIT`，允许只读查询返回全量匹配数据；prompt 仅要求在用户明确提出前 N 条、限制条数或分页时才生成 `LIMIT`。
- SQLQuery `result_filtering` 移除固定 `200` 行候选行硬上限；进入筛选 LLM 的 `candidate_rows` 现在完全由 `trim_max_tokens` 裁剪结果决定，不再在 token trim 后二次按行数截断。
- SQLQuery SQL 生成 Prompt 复刻 legacy `xiaoao_agent/sub_agents/sql_query_agent` 的 DDL 拼接方式：`schema_metadata.yaml` 仍作为表结构保存源，`schema_context_prepare` 会按当前 selected tables / columns 渲染 `schema_ddl`，审定品种库按作物收敛注入表结构，基因型数据库保留完整 gene schema；`sql_generate` 主路径接受 raw SQL / fenced SQL 输出并反推表字段使用情况，同时保留旧 JSON 输出兼容，`sql_guard` 表白名单优先收窄到当前 selected tables。
- 修复前端在 SQLQuery 暂停 / 补充信息恢复后最终回答消失的问题：前端不再根据任务图中的迁移前 SQLQuery 内部节点把恢复后的助手消息强制改成 SQLQuery 模式，而是始终优先展示主代理 text artifact，并把 SQLQuery 表格作为能力补充结果卡片；同时新增通用 capability artifact display 入口，避免后续新增 capability 时复现“能力结果覆盖/隐藏主代理回答”的问题。

### 2026-04-27 — 强化宽泛问题的第一性原理处理

- 主代理 Prompt 新增“第一性原理理解用户需求”约束：不假定用户知道自己要什么、该选哪个 capability 或该提供哪些参数，优先推断真实目标并给出可验证的初步答案/下一步。
- SQLQuery SQL 生成 Prompt 新增 `first_principles_need_inference` 策略，明确宽泛但仍在支持范围内的问题应优先生成概览查询，而不是直接要求用户补充库名或路径。
- 新增 SQLQuery `variety_overview` 路由与 schema profile，用于“查一下龙粳33”这类只给品种名的宽泛问题，同步覆盖审定品种库与基因型/籼粳成分信息。
- SQLQuery intent route 会将宽泛品种名查询路由到综合概览，不再在“审定品种库/基因型数据库”之间抛出冷冰冰的手动选择中断。
- SQLQuery deterministic fallback 可生成单条只读 `UNION ALL` 概览 SQL；对“龙粳/籼/稻”等水稻品种线索优先缩窄到水稻审定表 + 基因型相关表，避免跨全作物表慢扫。
- 用本地真实后端 API 验证“查一下龙粳33”可完成：任务 6 个 SQLQuery 节点全部 completed，返回 2 行，分别覆盖 `approval_variety_db` 与 `genotype_db`。
- 补充 SQLQuery intent / schema context / SQL generate / prompt builder 与主代理 Prompt 回归测试。
- 修复等待补充信息任务锁住会话的问题：新增 `GET /api/v1/tasks/{task_id}/interrupts` 与 `POST /api/v1/tasks/{task_id}/interrupts/{interrupt_id}/answer`，前端检测到 `waiting_for_input` 后展示澄清问题，并把下一次输入作为 interrupt answer 继续原任务。
- 补全 `waiting_for_input` 前端追问 UI：助手消息展示“需要补充信息”卡片、字段与可选示例，输入区显示“下一条消息会继续当前任务”的补充模式状态条，并保留显式“取消当前任务”按钮。
- 加固 interrupt 竞态：当前端已看到节点 `waiting_for_input` 但后端 open interrupt 尚未可见时，继续保持任务锁定和轮询，避免把下一条用户输入误当成新任务提交。
- 新增当前会话未完成任务列表：后端提供会话任务列表接口，前端展示未完成任务数量、任务摘要、状态、活动节点数，并提供逐项“停止任务”按钮供用户主动终止任务。
- 未完成任务列表默认折叠隐藏，只在标题栏保留数量、展开/收起和刷新入口，避免占用主对话区域。
- SQLQuery 结果表格增加横向滚动与气泡内宽度约束，避免宽表列数较多时撑出助手消息框。
- SQLQuery intent route 新增显式库名 / route_id 优先匹配：当用户补充“审定品种库”或“基因型数据库”时优先采用该路由，再回退到宽泛品种概览或 keyword 打分。
- `routing_rules.yaml` 补齐审定品种库 / 基因型数据库的库名关键词，并为审定品种库水稻 crop alias 增加“稻 / 粳 / 粳稻 / 籼稻”，使“龙粳18 + 审定品种库”这类补充信息可直接推断为水稻。
- 审定品种库单品种查询默认升级为业务详情输出：schema 裁剪保留申请者、育种者、品种来源、特征特性、产量表现、栽培要点、适种区域与审定意见等核心字段；确定性 SQL fallback 也会按详情/列表场景选择字段。
- SQLQuery 默认产品链路恢复内部 `result_summarize` 尾节点：SQLQuery 子代理可先生成可复用结果摘要，同时 `sql_execute_readonly` 继续提供表格 preview。
- SQLQuery 尾节点从 `result_summarize` 重命名并改造为 `result_filtering`：SQL 生成阶段继续用 `LIKE` 召回候选品种，尾节点通过 LLM 输出 `keep_row_indexes` 筛选真正符合用户需求的行，最终返回 `filtered_query_result` 表格；无 LLM 或 LLM 失败时保守保留候选表格并记录 fallback。
- 收紧 SQLQuery 单品种编号筛选规则：当用户查询“龙粳18”这类明确编号品种时，`result_filtering` 会只保留规范化等于“龙粳18”或“龙粳18号”的行，并剔除“龙粳1836 / 龙粳1823”等编号后继续追加数字的其他品种；该规则在无 LLM 或 LLM 误保留时也会作为后过滤生效。
- 修复 interrupt 反复追问时复用同一 `interrupt_id` 可能导致节点停在 `waiting_for_input` 但无 open interrupt 的问题；interrupt id 现在带 reason/input 指纹，避免“无限转圈”式悬挂。
- 排查并取消本地人工验证会话中遗留的 `task-7ff47608987d`：该任务停在迁移前 SQLQuery intent route 阶段的 `waiting_for_input`，旧前端未支持 interrupt answer，导致后续普通提交被 ConversationSerialGuard 以 409 busy 拒绝。
- 新增默认自动规划 provider：用户不再需要显式选择 capability；数据库/品种/审定/基因型类问题会自动展开为 `SQLQuery` 六节点宏能力，再交给 `main_agent.respond` 结合上游结果生成自然语言最终回答，普通问题仍保持单主代理路径。
- SQLQuery 默认运行链路保留末端 `result_summarize` 节点，内部 DAG 回到 intent route、schema context、SQL generate、SQL guard、readonly execute、result summarize 六节点；主代理接收 SQLQuery 上游摘要结果做最终对话式整合。
- SQLQuery SQL 生成新增品种名 LIKE 匹配约束：LLM prompt 明确禁止 `variety_name = ...`，LLM 若返回严格等值条件会触发 fallback；确定性 fallback 统一使用 `variety_name LIKE '%关键词%'`。
- 主代理 Prompt 新增“上游能力结果上下文”注入，只暴露 SQLQuery 摘要、路由、行数等安全字段，让主代理可基于 capability 结果整合回答，而不是重新猜测或忽略已完成节点。
- 前端业务对话台移除“普通对话 / SQLQuery”手动模式选择，展示为“当前模式：自动规划”，数据库类输入仅提示主代理会自动判断是否调用 SQLQuery；自动 SQLQuery 路径仍会在主代理最终回答下展示折叠的结果卡片，未完成任务列表中的空 capability 也显示为“自动规划”。
- 完成完整 LLM Planner runtime 接入：默认自动规划入口先尝试 LLM 生成 public-only 高层 DAG，再经过 public validator、macro expander 与 internal validator 后执行；planner 输出非法、引用 internal capability 或 provider 不可用时自动 fallback 到确定性 `AutoWorkflowProvider`。
- Planner prompt 现在注入当前 public capability 清单并明确数据库问题优先规划 SQLQuery 数据能力后接 `main_agent.respond`；系统会强制用真实用户问题覆盖当前 public capability 的 `user_message` / `user_question`，并在 SQL-only 或未连线主代理计划中自动追加/重连主代理 finalizer，确保主代理接收上游结果上下文。
- 新增正式的 per-capability planner payload allowlist：`CapabilityPayloadPolicy` / `PlannerPayloadPolicy` 默认 fail-closed，策略随 capability 注册进入 `CapabilityRegistry`，LLM Planner prompt 会展示每个 public capability 允许的 payload 字段；只有 capability 明确声明的字段才会从 LLM planner payload 进入执行图，系统生成字段始终覆盖 planner 字段。主代理与当时的 SQLQuery public 数据能力是首批注册策略，由各自模块声明真实用户输入注入规则。
- `build_api_runtime` 新增 planner 注入 seam（fake text generator / LLM config / factory / reasoning effort / enable 开关），`ApiRuntime` 支持异步 workflow provider，并新增 `workflow.plan_built` audit-only 事件记录 planner route、fallback 状态与原因。
- 补充 LLM Planner orchestration 与 API 集成测试，覆盖成功展开、单主代理、payload 覆盖保护、capability registry payload policy、未来 capability 自定义 allowlist、自动 finalizer、未连线主代理重连、provider 异常 fallback、internal capability fallback 与非法 JSON fallback。
- 新增 `docs/Capability接入指南.md`，明确后续新增 capability 时在 `src/capabilities/<name>/`、`src/api/runtime.py`、planner payload allowlist、macro provider、AutoWorkflowProvider、测试与文档中分别要加入什么，避免把新能力逻辑写成 SQLQuery 式特判。
- 前端对话框新增上游能力执行感知：根据 `node.started` / `node.completed` 事件在助手气泡内展示“正在执行 SQLQuery / 主代理 / 未来 capability”的当前步骤，避免长时间只显示等待回答。
- 前端 Markdown 渲染补齐 GitHub 风格基础表格解析与横向滚动样式，使主代理返回的 Markdown 表格不再作为普通段落显示。
- 补齐 SQLQuery 内部 LLM 默认装配：真实 API runtime 当时会用 `config.yaml` 创建 SQLQuery 文本生成器，并同时传给 SQL generation 与 result filtering 阶段；显式注入的 `llm_text_generator` 仍保持最高优先级，fake backend 明确关闭真实 SQLQuery LLM。
- SQLQuery SQL 生成 Prompt 与确定性 fallback 增加“X 系列 / 名字里带 X / 名称中包含 X”识别规则；例如“龙粳系列”会生成 `variety_name LIKE '%龙粳%'`，避免无 WHERE 条件只返回前 50 条无关数据。

### 2026-04-27 — 新增主代理思考控制

- 前端业务对话台在“当前模式”右侧新增“思考强度”下拉菜单（显示为“最底” / “低” / “中” / “高”，实际值为 `minimal` / `low` / `medium` / `high`，默认“中”）和“深度思考”开关。
- 提交消息时通过 metadata 独立传递 `main_agent_reasoning_effort` 与 `deep_thinking`：前者控制 reasoning effort，后者控制 LLM 请求 `extra_body.thinking.type` 为 `enabled` 或 `disabled`。
- 主代理执行器支持请求级 reasoning effort 与 thinking 开关覆盖，将 provider 返回的 `reasoning_content` 以 `main_agent.reasoning_delta` 前端事件输出，并在 LLM audit metadata 中记录实际档位和 thinking 状态。
- 调整主代理 LLM runtime 绑定，使真实 LLM client 可通过 `generate_text_with_thinking()` 同时产出 answer / reasoning 内容；保留旧的一参 fake streamer 兼容。
- 修复主代理 executor 默认 LLM fallback 仍调用 `stream_text()` 导致过滤 `reasoning_content` 的问题；已用真实 provider 探测确认字段位于 `choices[0].delta.reasoning_content` / `delta.model_extra.reasoning_content`，并用本地后端 API 验证可转发为 `main_agent.reasoning_delta`。
- 前端对话气泡新增安全 Markdown 渲染，并在助手消息中以“思考内容”框展示深度思考返回的 reasoning 内容；开启深度思考但 provider 未返回 `reasoning_content` 时也会显示占位提示。
- “思考内容”在正文开始并完成返回后默认折叠约 3 行，右上角提供展开/收起开关，内容文字统一使用灰色弱化展示。
- 补充前端 API/UI、Markdown 渲染、LLM client、主代理 executor 与 API runtime 回归测试，并通过前端测试/build 与后端相关测试。

### 2026-04-27 — 完成前端业务对话台 v1 与全栈启动脚本

- 基于 `docs/prd/frontend/00-前端业务对话台PRD.md` 新增 `docs/dev_processes/frontend/` Phase 0~5 开发过程文档，并补充 Ralph 执行版 PRD / Test Spec。
- 新增 `frontend/` React + TypeScript + Vite + Ant Design 前端业务对话台，支持普通主代理 streaming、SQLQuery 模式、任务状态、取消、capability 状态、artifact 摘要与简表预览降级展示。
- 新增前端 API/SSE client、task event reducer、SQLQuery artifact parser 与 Vitest/React Testing Library 覆盖，默认不展示 SQL / DAG / schema / audit 技术细节。
- 新增 `scripts/run_fullstack_dev.py`，默认使用仓库真实 FastAPI runtime 拉起后端和 Vite 前端，支持人工验证；如需 UI-only 验证可显式加 `--fake-backend` 使用 deterministic fake provider/数据库适配器。
- 调整 Vite build 分包策略，将 React、Ant Design、rc 组件和通用依赖拆成独立 vendor chunk，避免 Ant Design 依赖集中进入单个大 chunk。
- 更新 `README.md`、`AGENTS.md` 与 `docs/dev_processes/README.md`，记录前端工程、验证命令和全栈启动方式。

### 2026-04-27 — 补充前端业务对话台技术选型建议

- 在 `docs/prd/frontend/00-前端业务对话台PRD.md` 中新增技术选型建议与约束草案，推荐 v1 采用 `React + TypeScript + Vite + Ant Design + EventSource/SSE`。
- 明确 Next.js、Tailwind + shadcn/ui、WebSocket、Vercel AI SDK、AG-UI、Redux/Zustand 等备选方案的 v1 结论与暂不选原因。

### 2026-04-27 — 整理后端开发过程文档目录

- 将后端 Phase 0~8.2 开发过程文档统一迁移到 `docs/dev_processes/backend/`，并保留 `docs/dev_processes/README.md` 作为跨领域开发流程总入口。
- 同步更新 `README.md`、`AGENTS.md`、PRD 索引、一期计划与相关专题文档中的开发过程路径引用，避免继续指向旧的 `docs/dev_processes/Phase-*.md` 根目录路径。

### 2026-04-27 — 新增前端业务对话台 PRD 草案

- 基于当前后端 FastAPI / SSE / capability / artifact 实现事实，新增 `docs/prd/frontend/00-前端业务对话台PRD.md`，明确前端 v1 定位为内部业务用户对话台，而非研发调试台。
- 明确 v1 仅依赖当时现有 API：普通对话走 `main_agent.respond`，数据库查询走 SQLQuery public 数据能力，SQLQuery 默认展示自然语言摘要与简表预览。
- 更新 `docs/prd/frontend/README.md` 与 `docs/prd/README.md`，将前端 PRD 状态从预留改为已开始，并记录 v1 非目标与后续后端 API 增强项。

### 2026-04-27 — 同步当前实现基线的文档、PRD 与 Agent 规则

- 更新 `AGENTS.md`、`README.md`、`docs/一期核心模块边界.md`、`docs/一期开发计划.md` 与 `docs/SQLQuery-LLM版本改造方案.md`，移除“仓库仍保持最小化/架构未定”等过期表述，改为当前后端实现目录职责、PRD 入口、SQLQuery LLM 落点和后续维护口径。
- 将后端 PRD 统一迁移到 `docs/prd/backend/`，新增 `docs/prd/README.md` 与 `docs/prd/frontend/README.md`，并更新 `docs/prd/backend/00-主代理框架PRD.md` 的 backend / frontend 边界、专题索引与使用建议。
- 新增 `docs/prd/backend/07-SQLQuery-LLM增强与真实库验证.md`、`08-主代理Skill兼容与真实LLM运行时.md`、`09-高层DAG规划与SQLQuery宏能力边界.md`，把 Phase 5.5、Phase 8、Phase 8.1、Phase 8.2 已超出原 PRD 粒度的能力纳入正式 PRD。
- 同步更新 `docs/dev_processes/README.md`、`docs/LLM接入阶段建议.md`、SQLQuery prompt 模板及各阶段文档中的 PRD 路径引用，移除旧 `docs/主代理框架PRD.md` 与根级 `docs/prd/0x-*` 引用。
- 更新 `AGENTS.md` 编码风格规则，把早期“避免过早抽象”改为更短的“避免无证据提前泛化，也避免在已有稳定复用需求时继续复制粘贴”。

### 2026-04-27 — 完成 Phase 8.2 主代理真实 LLM Runtime 绑定

- 新增 `docs/dev_processes/Phase-8.2-主代理真实LLM运行时绑定与Smoke验证.md`，并在 `docs/dev_processes/README.md`、`docs/LLM接入阶段建议.md` 中补齐索引和阶段说明。
- 为主代理真实 LLM 增加 runtime 显式装配参数：`main_agent_llm_config`、`main_agent_llm_config_path`、`main_agent_llm_client_factory`、`main_agent_reasoning_effort`；保留 fake stream 最高优先级，默认测试仍不访问真实 provider。
- `LLMClient` 新增 `safe_metadata()`；主代理 `main_agent.llm_call` / `main_agent.llm_fallback` 现在记录不含 secret 的 provider metadata，并过滤 API key、prompt、base_url 等敏感 key；fallback diagnostic 只记录异常类型，不记录 provider 原始异常文本。
- 新增 `scripts/smoke_main_agent_llm.py` 作为显式手工 smoke 入口，并同步更新 `README.md` 与 `AGENTS.md`；本地 `config.yaml` smoke 已验证普通主代理消息可通过真实 provider 完成。
- 补充主代理 runtime factory、safe metadata、fallback 安全事件相关测试，并回归 `tests/integrations`、`tests/capabilities/main_agent`、`tests/api`、`tests/orchestration`。

### 2026-04-27 — 收紧 SQLQuery Prompt Schema 与 MySQL 只读适配器

- SQLQuery schema context 现在向 prompt 注入 `selected_column_details`，把 `schema_metadata.yaml` 中裁剪后的字段名、`sql_type` 与描述一起传给 LLM。
- SQL 生成 prompt 要求 LLM 回填 `column_types_used`，并在 `sql_generate` 中校验 `columns_used`、`column_types_used` 与 SQL 字段引用必须匹配裁剪后的 schema。
- 确认 `multi_agent` 环境中 `PyMySQL` 可用，并将 `PyMySQL==1.1.2` 写入 `requirements.txt`。
- 完善 `MySQLReadonlyAdapter`：支持懒加载复用 SQLAlchemy engine、注入 `engine_factory` 测试 seam，并提供同步 / 异步关闭入口释放连接池。
- 用真实只读库核对 `schema_metadata.yaml` 表/字段存在性，并通过真实 MySQL smoke 查询与 SQLQuery workflow 验证 adapter 可查到“龙粳33”；补充 adapter 集成测试覆盖 guard token、runner seam、engine 复用与 dispose。

### 2026-04-27 — 补齐 Phase 8.1 LLM Planner 前置契约

- 按 Phase 8.1 文档计划补齐 LLM Planner 前置契约：新增 `WorkflowPlanValidator`，校验 public capability、重复节点、未知依赖、环形依赖与 JSON-serializable input payload。
- 新增迁移前 `WorkflowExpander`，将高层 SQLQuery public 数据能力展开为 SQLQuery 固定内部子工作流，并正确改写上游 / 下游依赖。
- 新增 Planner 输出 JSON schema、fake LLM 输出解析 seam 与回归测试，继续保持“不实现完整 LLM Planner、不开放 SQLQuery 内部节点给 Planner”的阶段边界。
- 更新 Phase 8.1 文档状态与补齐记录，明确首轮 public SQLQuery 边界与本次 Planner 前置契约均已完成。

### 2026-04-24 — 完成 SQLQuery LLM 增强与 Phase 8 主代理首轮能力

- 建立 Phase 5.5 / Phase 8 / Phase 8.1 的专题文档与阶段索引：新增 SQLQuery LLM 增强专题、Codex Skill 兼容层专题、SQLQuery 宏能力与 LLM 动态 DAG 规划设计稿，并同步更新 `docs/dev_processes/README.md` 与 `docs/LLM接入阶段建议.md`。
- 迁移前收口 SQLQuery 公开命名与能力边界：对外保留当时的 SQLQuery public id（展示名 `SQLQuery`），默认隐藏并拒绝外部直接调用内部节点及更早旧 id。
- 完成 Phase 8 主代理首轮实现：新增 `main_agent.respond`、`MainAgentWorkflowProvider`、`CompositeExecutor` 与 workflow router；普通消息默认进入主代理，显式 SQL 查询进入固定 SQLQuery workflow。
- 落地 Codex Skill 兼容层与主代理 prompt 上下文：新增 `src/integrations/codex_skills/` 与 `src/capabilities/main_agent/`，支持解析 `SKILL.md`、识别 IO / scripts 扩展、匹配 skill、受控 Python 脚本 runner、上传 artifact 脱敏 metadata 注入，以及 `main_agent.output_delta` / `main_agent.output_final` streaming 事件。
- 完成 Phase 5.5 SQLQuery LLM 首轮 TDD 实施：当时在原 SQLQuery package 中新增 prompt builder / LLM utils，改造 SQL generation 与 result summarize 支持注入 LLM 文本生成器、结构化 JSON 输出、clarify / reject、rows preview、确定性 fallback 与 SQLQuery LLM 审计事件。
- 整理 LLM client seam：将根目录 `llm_client.py` 移入 `src/integrations/llm_client.py`，改用 `yaml.safe_load` 与初始化期配置读取，补齐 `generate_text()`、`generate_text_with_thinking()`、`stream_text()` 与 `ReasoningEffort` 范围收窄。
- 修复 interrupt/resume 相关竞态：避免 late open interrupt 覆盖 answered 状态，并在恢复调度时重置旧 workflow 节点状态、先落库 `WAITING_FOR_INPUT` 再暴露 open interrupt。
- 补充主代理、Skill 兼容层、SQLQuery LLM、observability 与 e2e 测试；新增 `tests/integrations` 与 `tests/capabilities/main_agent` 最小测试命令，并同步更新 `README.md`、`AGENTS.md`。
- 将本地 `config.yaml` 与本周工作周报草稿加入 `.gitignore`，新增 `docs/工作周报模板.md`；将 Phase 5.5 / SQLQuery 示例查询统一为“龙粳33”，并用真实只读数据库核对基础品种、基因型预览、籼粳成分与审定信息。

### 2026-04-23 — 完成一期 Phase 0~7 开发计划、实现与验收

- 产出一期计划与测试规格：新增 `.omx/plans/prd-20260423-main-agent-framework-phase1.md`、`.omx/plans/test-spec-20260423-main-agent-framework-phase1.md` 与上下文快照，并将计划转为 `docs/一期开发计划.md`、`docs/dev_processes/README.md` 和 Phase 0~7 系列文档。
- 冻结一期边界与记忆口径：收紧 Phase 4 / Phase 5 分工、拆分 Phase 6 / Phase 7，明确一期只做会话延续型记忆，不做跨任务知识沉淀；会话记忆作为主框架状态数据落 SQLite，后续同构迁移到 PostgreSQL。
- 完成 Phase 1 core：新增 `src/core/` 共享模型、状态枚举、基础错误与 contract 定义，补齐 `tests/core/`，并新增 `docs/一期核心模块边界.md`。
- 完成 Phase 2 storage：新增 `src/storage/` 与 `src/storage/sqlite/`，落地 SQLite base / session / bootstrap、ORM model、repository、async storage façade、`InterruptAnswer` 与 `StoragePort` 增量；新增 `docs/dev_processes/Phase-2-SQLite状态存储表结构草案.md` 与 `tests/storage/`。
- 完成 Phase 3 lifecycle：新增 `src/lifecycle/`，落地 task state machine、mailbox、interrupt/resume、cancellation、conversation serial guard、typed payload 基础设施与相关存储 primitive；新增 `tests/lifecycle/` 与任务上下文终止状态流转图（Markdown / PNG）。
- 完成 Phase 4 orchestration：新增 `src/orchestration/`，落地 capability / instance registry、scheduler、workflow/task plan、completion policy、strict reject backpressure、orchestration service 与所需状态查询 seam；新增 `tests/orchestration/` 并修订主代理编排能力流程图。
- 完成 Phase 5 SQLQuery MVP：当时新增 SQLQuery capability package 与 integrations，当时复用系统级 SQLQuery package 与 YAML configs 接入 intent route、schema context、SQL generate、SQL Guard、readonly execute、result summarize 六阶段 workflow；新增对应测试、SQLQuery 子代理结构图，以及 SQLQuery LLM 改造方案和 LLM 接入阶段建议图文档。
- 完成 Phase 6 API/SSE：新增 `src/api/`、FastAPI app、DTO、消息提交/任务查询/SSE/取消/任务图/产物/能力目录接口、进程内事件 broker 与 JSONL audit logger；补齐 live fan-out、late result discard、取消事件与 `tests/api/`。
- 完成 Phase 7 验收与二期评估输入：新增 `tests/e2e/`、`tests/observability/`，覆盖 happy path、guard blocked、interrupt/resume、cancel late result ignored 与 JSONL audit；新增 `docs/一期验收报告.md` 与 `docs/第二阶段评估输入.md`，并同步更新各阶段最小测试命令到 `README.md`、`AGENTS.md` 与 Phase 文档。
- 更新仓库规则：在 `AGENTS.md` 中新增开始分析、设计、编码或文档修改前必须先阅读 `CHANGELOG.md` 最近相关条目的要求。

### 2026-04-22 — 初始化主代理框架设计基线并建立仓库级设计资产

- 建立并持续收口主代理框架 PRD，明确主框架只负责任务拆解、编排、分发，具体业务能力以下层 capability 形式接入。
- 将 PRD 重构为“总览 + 专题”结构，新增 `docs/prd/` 专题文档，降低单文档耦合度并方便后续按模块做计划与实现。
- 补齐 SQLQuery 首个 MVP 相关设计资产，包括数据库结构说明、Prompt 输入模板、业务路由规则、schema 元数据与 SQL Guard 规则。
- 新增系统级 SQLQuery schema context builder 骨架与基础模型定义，用于后续按 TDD 推进 capability 落地。
- 导出当前 `multi_agent` 环境依赖到根目录 `requirements.txt`，并补充仓库规则：优先基于现有依赖实现功能。
- 新增根目录 `CHANGELOG.md`，作为仓库级开发记录入口，并在仓库规则中明确要求每日开发结束时手动补记当天工作内容。
- 收口主代理思维模式、完成判定闭环、优先级两层模型、模块划分与主框架/Capability 上下级边界等关键架构决策。
- 更新 `.gitignore`，把 `.codex/` 作为本地运行时目录忽略，不再纳入版本控制。
