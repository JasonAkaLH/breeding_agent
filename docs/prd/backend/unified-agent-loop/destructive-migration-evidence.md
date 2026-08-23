# Phase 7 破坏性迁移证据

- **日期**：2026-08-23
- **适用分支/环境**：`main`；本地受控开发环境，不是`prod`
- **证据状态**：open
- **P7-A检查点**：`restore_proof_complete`；P7-B/P7-C pending
- **tested commit/tree**：`7109f5085076f635cf30b6fc16beec2505cc25c6` / `5d2062707705702a014ba72d644a84c30a4dcde6`
- **当前决策**：AL-P7-01闭合，允许进入P7-B；physical schema/proto尚未删除，Phase 7不得标记`complete`。
- **保密边界**：本文不记录DSN、credential、业务正文、仓库外绝对路径或可公开下载的backup引用。

## 1. P7-A Operator 与状态机

`src/storage/agent_schema_migration.py`和`scripts/migrate_unified_agent_loop_schema.py`提供单实例closed operator。P7-A开放
`report`、`backup`、`restore-check`、`restore-all`，`apply`在P7-B实现前固定以
`agent_schema_apply_not_available_before_p7b`拒绝。状态保存在仓库外权限`0700`的private root；receipt按
`reported -> backed_up -> restore_verified`追加为`0600`不可变文件，以前驱receipt SHA绑定。相同input digest的
`backup`和`restore-check`实际重复调用返回同一SHA，未覆盖文件或追加receipt。

Operator代码检查点：

- `36752d2`：report/backup/restore-check/restore-all、manifest/receipt、文件安全与失败回归；
- `e43b2da`：补上恢复文件启动真实Rust Sidecar的Version/Compatibility readiness，前一轮仅SQLite可读的结果不作为
  P7-A gate authority。
- `7109f50`：exact retry返回既有receipt前重新校验report ref与backup file identity/digest；前一轮r2证据被本轮r3
  commit/tree和全新backup set替代。

## 2. Canonical report 与 receipt chain

| 项目 | SHA / 状态 |
|---|---|
| report | `sha256:da8002c7acda659846d1f550c739a381f84f8871e85774396cc5bbb1583a3adb`；blockers为空 |
| `reported` receipt | `sha256:bf3b77a3601dfed0b79ba3688e3ce146ee8f3b3f0e67c6419d6bd8be0f9a82f5` |
| `backed_up` receipt | `sha256:08fe369d8200f1928eb93d36a0e094defe265be498b906032836701d152cd687` |
| `restore_verified` receipt | `sha256:1430e8a05ee7ebc43fa702eb164d93c089056aedce093251cac6e3bc2fc606b9` |
| backup set | `backup-set-da8002c7acda6598` / `sha256:99a2534b24dbe85bb2e34ee55dedb80f95535e0abceb6acf7994dad85008193a` |
| pre-migration schema version | SQLite/PostgreSQL/Sidecar均为`pre-p7` |

Report只保存schema metadata、待删DAG对象、全表行数以及Agent表计数/数据digest，不保存业务值。Receipt中的
`backend_readiness`、`agent_storage`、`task_history`和`artifact_event`均为`true`。根`docker_cmd.md`仅验证
exists/ignored/untracked，未读取、移动、打包、跟踪或修改。

## 3. 仓库外 backup set

持久backup root和backup-set目录均为`0700`。所有普通文件均为owner单链接`0600`；manifest只含下列相对restore ref：

| Backend | 相对ref | 字节数 | SHA-256 |
|---|---|---:|---|
| SQLite | `sqlite.backup` | 27,832,320 | `sha256:477f78c64e1873c05e3b2ca93c6e43dacea897063eb14780875cfb1c6169fab7` |
| PostgreSQL | `postgres.dump` | 188,401 | `sha256:ceb572657cce2e6473ff5a8a638f155a56800e460f4f5df0e457e7217cb87052` |
| Runtime Sidecar | `sidecar.backup` | 204,800 | `sha256:7a0882d899db3f862741c8262e5789feb4352b22f5f216f6fffe9db79ac1badd` |

SQLite和Sidecar使用online backup API；PostgreSQL使用17版custom-format dump。写入采用O_EXCL/no-clobber、file与directory
fsync，并在restore前复验mode、owner、link count、size和digest。该backup set至少保留到P7-C全部通过且用户明确结束
rollback窗口；隔离restore临时目标不是唯一备份。

## 4. 实际隔离恢复结果

- SQLite：恢复到全新隔离文件，`integrity_check=ok`；schema digest、全表行数、Agent表计数/数据digest与report一致；Task
  history、Artifact和Event均包含在全表对照中。
- PostgreSQL：custom dump恢复到同一专用PostgreSQL 17实例内的全新隔离数据库；以repeatable-read read-only inventory
  复验schema、全表行数和Agent digest，结果与source report一致。未使用源数据库作为restore目标。
- Runtime Sidecar：online backup恢复到全新隔离文件；除SQLite integrity/schema/数据digest一致外，operator实际启动受审
  `maf-runtime-sidecar` binary并完成Version和Compatibility readiness，随后受控停止该进程。
- 应用层old-DAG-not-readable：`tests.e2e.test_agent_loop_cutover`和`tests.api.test_agent_task_projection`证明`/graph`
  返回`edges=[]`且注入的TaskEdge reader一旦被调用即失败；本轮与operator/真实PG/Sidecar门禁合并运行75项，零skip、零失败。
- Exact retry：在已存在backup set和非空restore临时目标上重复同SHA `backup`/`restore-check`，返回原backup-set SHA和
  `restore_verified` receipt，未重跑restore或覆盖文件。

## 5. 自动回归与失败语义

计划指定的P7-A门禁在真实PostgreSQL环境运行：

```text
tests.scripts.test_migrate_unified_agent_loop_schema
tests.storage.test_agent_storage_postgres_integration
tests.storage.test_rust_runtime_sidecar_contract
```

结果为62项通过、零skip；加入old-DAG application smoke后为75项通过、零skip。Operator 10项回归覆盖report/data drift、
expected SHA不匹配、已存在冲突backup set、symlink/hard-link/mode/owner漂移、file fsync失败、PostgreSQL/Sidecar部分失败、
restore指向原state、单实例锁、stdout脱敏、非法apply、duplicate exact retry及retry前重新验证输入漂移。Ruff检查及
`git diff --check`通过。

## 6. Rollback 与后续门禁

P7-B开始后若任一backend停在partial prefix，禁止继续apply或启动混合schema binary，只能使用本backup set按
Sidecar -> PostgreSQL -> SQLite顺序执行`restore-all`，并以正常revert恢复对应Phase 6代码。`restore-all`的三backend顺序、
部分失败不前进receipt和exact retry已由operator回归覆盖；本轮没有失败的destructive apply，因此未覆盖原开发数据执行
rollback命令。

P7-A完成不代表Phase 7完成。当前仍待：P7-B三backend physical migration与proto/storage删除；P7-C完整Backend、Frontend、
Rust、static/docs、NFR/FR映射，以及受控真实MCP smoke或用户书面waiver。任一缺失时本文保持open，`prod`不在本证据范围。
