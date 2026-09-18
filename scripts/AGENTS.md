# scripts/AGENTS.md

本文件用于快速定位维护与验证脚本。除非本文件另有说明，继续遵守仓库根目录 `AGENTS.md`。

## 目录速览

- `check_docker_cmd_policy.sh`：校验本地 `docker_cmd/` 目录及旧根路径 `docker_cmd.md` 的 Git 忽略和未跟踪保护。
- `run_fullstack_dev.py`：本地拉起前后端用于人工验证。
- `smoke_main_agent_llm.py`、`smoke_model_reasoning_matrix.py`、`smoke_mcp_server_config.py`、`smoke_user_mcp_soft_binding.py`：真实 provider / MCP smoke脚本；reasoning matrix默认只输出脱敏plan，必须显式`--live`才探测全部model × thinking state × effort且不记录回答/凭据/request ID；user-scoped discovery smoke创建并清理隔离owner-bound Task，不绕过Gateway Task ownership。
- `run_rust_quality_gates.py`、`run_rust_coverage_thresholds.py`：Rust quality / coverage gate脚本；Skill policy与MCP protocol fuzz smoke为独立gate。
- `rust_artifact_provenance.py`：Rust artifact SBOM / provenance / manifest 辅助。
- `stage_user_mcp_cp7_inputs.py`：把阶段闭合的 8/9 项 CP7 本地输入安全、no-clobber 地暂存到空卷并生成脱敏 canonical receipt。
- `prd_evidence.py`、`validate_prd*.py`：PRD evidence ledger 与 fail-closed 验证脚本。
- `postgresql_state_migration.py`、`postgresql_state_cutover.py`、`validate_postgresql_state_platform_runtime.py`：PostgreSQL state platform 迁移、切换与验证。
- `conversation_delete_ops.py`：会话删除维护操作。
- `validate_user_mcp_phase3_evidence.py`：离线验证 Phase 3 closed-schema evidence、digest/provenance、production HMAC attestation 和 stage gate。
- `validate_unified_agent_loop_evidence.py`：统一Agent Loop Phase 0～7 handoff evidence validator；Phase 0双向校验active PRD、旧测试和九类执行/恢复入口，Phase 5/6/7按到期规则拒绝缺失或未closed的readiness、DAG删除和破坏性迁移证据。
- `validate_project_skill_bundle.py`：对外部Project Skill root计算或校验确定性`sha256:<64-lower-hex>` bundle digest；拒绝symlink/特殊文件/容量/deadline边界，输出不含正文或绝对路径的safe JSON。
- `initialize_mcp_first_production.py`：新用户 MCP 功能的一次性 PostgreSQL 管理员准入；`check` 只读，`apply` 绑定预检摘要并在锁内原子登记首次 evidence/approval/activation，不执行 DDL，不进入普通服务启动。
- `postgres/user_mcp_first_enablement_constraints.sql`：显式升级五项 evidence CHECK；兼容原 CI/production 记录，不初始化 MCP。
- `control_user_mcp_rollout.py`：向 canonical rollout ledger 追加 approval、activation、block/resolution 与严格降暴露回滚；PostgreSQL 的 `append-block` 只用 evaluator DSN，其他子命令只用 operator DSN，SQLite 仅限 local/CI。
- `produce_user_mcp_shadow_evidence.py`：使用 snapshot producer 身份从 canonical PostgreSQL durable sample/metric/drill ledger 派生、签封并写入 production rollout evidence；不接受 caller-authored production payload。
- `migrate_legacy_mcp_config.py`：legacy MCP inventory、HMAC consumer 引用/集合 digest、consumer × capability obligation 和 informational health dry-run；standalone safe apply 默认实时重验完整 catalog/capability/consumer 连续性，并根据 state runtime config 向 canonical PostgreSQL 或 local/CI SQLite 原子批量写入确定性新 ID、canonical config/加密凭据和 migration provenance；CLI 不接受 PostgreSQL DSN、不做 schema bootstrap，也不提供 Endpoint 域名/CIDR allowlist 参数，公网 HTTP/HTTPS 与私网拒绝规则和用户 API 一致。
- `migrate_mcp_dispatch_aggregate.py`：MCP dispatch聚合schema的closed report与受控apply；同文件private helper唯一拥有operator DSN guard、PostgreSQL engine lifecycle和rejected JSON emitter，report/apply环境读取顺序保持不同；apply必须携带上一份报告的64位SHA，SQLite只迁移无业务行的legacy表并先创建`0600`完整备份、在独占事务中切换且记录migration row，PostgreSQL使用operator DSN环境变量、advisory/table lock和`NOT VALID → VALIDATE`约束接管；含业务行或authority漂移必须退出3，不自动猜测映射。
- `migrate_unified_agent_loop_schema.py`：统一Agent Loop Phase 7的单实例closed schema operator；绑定tested commit/tree、canonical SHA及仓库外不可变receipt链，按SQLite→PostgreSQL→Sidecar执行apply，partial prefix只能精确续签或三backend restore-all。
- `migrate_runtime_sidecar_submission_authority.py`：Checkpoint A专用离线Submission Authority迁移CLI；生成closed inventory/report，调用Rust stdin importer并签封v2 evidence。apply必须在canonical real path共同lifetime writer fence内完成备份、import/finalize和destination复验；backup/receipt/evidence使用同目录secure temp、fsync与no-clobber原子发布，不提供在线import RPC。
- `postgres/user_mcp_rollout_permissions.sql`：Phase 3 PostgreSQL rollout ledger 的无凭据最小权限模板，分离 app/snapshot/CI/evaluator/operator/validator NOLOGIN 角色、DB-derived production evidence prepare/finalize、正值安全红线原子 block、SECURITY DEFINER write API、gate-scope 串行和 append-only history 保护。
- `postgres/user_mcp_legacy_migration_permissions.sql`：legacy MCP canonical PostgreSQL 迁移的专用 NOLOGIN/LOGIN 边界与 SECURITY DEFINER 原子 apply/snapshot API；禁止 migrator 直接获得用户 MCP 基表 DML/SELECT 权限。
