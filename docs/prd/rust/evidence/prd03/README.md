# PRD03 RuntimeSidecar evidence ledger

本目录记录 `docs/prd/rust/03-DispatcherStoreEventSidecarPRD.md` 的 release / enforce / decommission 证据状态。

- `runtime_sidecar_release_gates.json` 是机器可读账本，由 `scripts/validate_prd03_runtime_sidecar_evidence.py` 校验。
- `--allow-pending` 只允许 CI / 开发分支确认“证据门禁存在且 fail-closed”；不代表生产 enforce 已获准。
- 不带 `--allow-pending` 必须在 artifact provenance、benchmark、7 天 shadow promotion、migration/DR、ops readiness 与 decommission 全部真实通过后才能返回 ready。
- 当前账本故意保持 `pending_external_production_evidence`：不得用样例、合成数据或本地 smoke 冒充生产 7 天 shadow / rollout / decommission 证据。

## 当前远端 CI 证据

- GitHub Actions `Rust quality gates` run `25948082624` 已在 commit `ed44653a9fb591da5be82366cf5f87e4458030ad` 通过。
- 该 run 覆盖 Ubuntu 22.04 x86_64 / Python 3.13 的 Rust quality gates、bounded fuzz smoke 与 PyO3 wheel smoke。
- 后续提交会在 workflow 中额外生成并上传 `maf-runtime-sidecar` Linux x86_64 release binary、SBOM、provenance 与 manifest。

## Enforce / decommission 前必须补齐

1. 部署流水线或 runtime allowlist 接收 `maf-runtime-sidecar` binary/image manifest，冻结 checksum 与 Cargo.lock digest。
2. 收集覆盖全部 PRD03 操作的 Python baseline vs Rust sidecar benchmark。
3. 收集连续 7 天、至少 1000 个 shadow 样本，且 contract mismatch / panic / crash 为 0。
4. 执行 migration / DR dry-run、backup、restore、event replay validation、rollback / roll-forward drill。
5. 执行 unavailable、protocol mismatch、queue full、deadline spike、secret / identity mismatch、migration failure、crash recovery、restore / replay drill。
6. promotion 通过后，单独执行 Python legacy write-path decommission PR；最终 rollback 只能走 sidecar artifact/deployment rollback 或 restore/replay。
