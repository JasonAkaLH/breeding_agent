# 统一 Agent Loop Skill 临时完整结果与全局上下文 Compaction 实施证据

日期：2026-08-28

状态：`complete_local`；仓库实现、真实PostgreSQL/Runtime Sidecar、本地真实Skill Task和全量门禁已闭合，未构建、推送或部署镜像，不宣称`release_complete`。

## 1. Branch与检查点

- 分支：`main`
- 实施起点：`8575e6b0`
- 最终证据提交：包含本文档的`docs(agent): close transient result context rollout`提交；identity以Git历史为准，避免文档自引用hash。
- `prod_untouched=true`

| Checkpoint | Commit | 闭合范围 |
|---|---|---|
| A | `b540c19d` | Run级固定model window与90% context budget authority |
| B | `11dd0d12` | 128 KiB边界、v2 bounded receipt与private transient stage |
| C | `9d64717b` | schema-first resolver、完整candidate与total preflight |
| D | `31fe3c36` | global closed-history compaction与Provider context误差一次收敛 |
| E | `af6f6dbc` | stage/CAS recovery、final/covered/terminal cleanup与startup janitor |
| F | `7fd84a3c` | 低基数观测、legacy兼容与固定28条E2E |
| G | 本证据所在提交 | 全量门禁、真实smoke、状态与回滚证据 |

## 2. 自动门禁

### 2.1 Python

| 门禁 | 结果 |
|---|---|
| `compileall src tests` | PASS |
| Core | Ran 54，0 skip，OK |
| Storage | Ran 551，13项环境条件skip，OK |
| Lifecycle | Ran 47，0 skip，OK |
| Integrations | Ran 763，2项平台条件skip，OK |
| Orchestration | Ran 179，0 skip，OK |
| Capabilities / main agent | 17 / 17 |
| Capabilities / MCP Tool | 15 / 15 |
| Capabilities / Skill Tool | 3 / 3 |
| API | 612 / 612 |
| E2E | 9 / 9 |
| Observability | 41 / 41 |
| Checkpoint F固定门禁 | 50 / 50 |
| 相邻compaction回归 | 4 / 4 |
| 超90%受控global compaction fixture | 1 / 1 |
| 外部`bioinfo-daily`自测 | 6 / 6 |

全量API首轮暴露三个旧测试文件的自建model-edition fixture缺`trim_max_tokens`，与新Run必须从绑定model window生成budget的fail-closed合同冲突。仅为fixture补入1,024,000-token测试窗口，未给生产逻辑增加猜测fallback；相关31项和完整API 612项复验通过。

### 2.2 PostgreSQL与Runtime Sidecar

- 在本地PostgreSQL 17容器中创建经确认不存在的隔离数据库，`tests.storage.test_agent_storage_postgres_integration` 6 / 6、零skip通过，随后精确删除该测试库并复验不存在。
- Storage全量中真实Runtime Sidecar Agent repository进程集成通过。
- 统一Rust门禁中Runtime Sidecar的in-memory/SQLite/kernel/gRPC/near-50-MiB与Python-Rust Agent payload shared vectors均通过。

### 2.3 Frontend

- 24 files / 334 tests：PASS。
- TypeScript typecheck：PASS。
- production build：PASS。
- 仅有既有的单chunk超500 kB提示，本轮Frontend源码零diff。

### 2.4 Rust / Runtime Sidecar

`conda run -n multi_agent python scripts/run_rust_quality_gates.py --run`最终退出码0。首次在sandbox内运行时，功能测试与nextest 198 / 198已通过，但`cargo audit`无法在只读`~/.cargo`创建advisory DB lock；按权限规则在sandbox外重跑后整套门禁通过。覆盖fmt、Clippy、workspace tests、nextest 198 / 198、audit、deny、coverage threshold、两个30秒fuzz smoke、provenance/SBOM self-check和三个macOS PyO3 wheel。CycloneDX对既有`Proprietary` SPDX表达式的提示未导致门禁失败。

## 3. 固定28条核心E2E

- artifact-free ordinary Skill只执行1次，形成1件private stage和`skill-result-v2 transient_staged` bounded receipt，`commit.staged_artifacts=()`。
- 下一provider request包含完整28条、首尾sentinal与顶层/structured duplicate；resolved raw canonical digest与receipt raw SHA一致，receipt marker、stage ref和pending marker不在provider request。
- final准确引用第28条唯一sentinel，没有为读回结果再次调用Skill。
- total低于Run 90% limit时compaction=0；另一受控fixture证明total超限时只compact closed history，最新result sequence保持在compaction boundary之后。
- final后raw/manifest数为0，Task Artifact没有新`skill_result.json`。
- 同体量但`answer_mode=direct`产生普通业务Artifact的fixture保持`skill-result-v1 artifact_backed`，原Artifact列表和下载成功。

## 4. 本地真实`bioinfo-daily` smoke

查询2026-08-21至2026-08-28，`max_results=30`。不记录用户正文、文章正文、标题、查询响应、绝对路径或凭据。

### 4.1 外部Skill直连

- 实际article count：30；PubMed raw result count：36。
- date field：EDAT；fallback used：false。
- `pubmed_results.json`：188,067 bytes；SHA-256 `4729f27dd759f820a2fcd2a95ba554c2e96b81a1d6bc55772e1bb04a228fbd3f`。

### 4.2 本地统一Agent Task

- Task ID：`task-7572227d4687`；Run ID：`agent-run:task-7572227d4687`；final status：`completed`。
- Tool call ID：`call-86b5e3897a6c-0`；Node ID：`agent-node:task-7572227d4687:57ca65dd9417f7cfd75b9c82`；Tool call count：1。
- 实际article count：30；model-only full raw：307,329 bytes；SHA-256 `ba351376380fe42d9f2816930c5b18dd7ad1bccacc4a4f5234c3be8c570392b3`。末条记录SHA-256为`7d6c7e84bbf4652dab9b20a1ad7bfc59829b359b1cec7a8a645b0c61dc1c609a`，final精确引用该末条的PMID，但本证据不记录PMID或正文。
- pinned model window：1,024,000 tokens；90% limit：921,600。
- 完整结果采样前preflight：required=76,628，history=0，transient=74,773，tool=1,087，total=77,715，decision=`fits`，compaction count=0。
- projection=`transient_staged`；stage count=1；`execution.artifacts=()`由空`artifact_refs`、projection分流和`agent.result_projected.artifact_count=0`闭合证明。
- final后stage/manifest file count=0；Task Artifact count=1（final output）；raw `skill_result.json` count=0。
- audit、Message和Conversation history的raw/title/full-result marker泄漏扫描为0；stage ref、private path和raw未进入持久化消费面。

真实Task不为制造超限重复调用外部Skill。超90%场景由受控本地fixture单独证明global compaction，其中两个call各执行一次，compaction request=1，latest result不在covered range。

## 5. Recovery、清理与泄漏边界

- stage写成但outcome CAS前崩溃、CAS response loss、receipt后/sample前崩溃：同一identity复验/提交，Skill不重放。
- stage缺失、symlink、size/SHA/owner漂移：typed unavailable，provider和Skill均不重跑。
- compaction成功但commit前崩溃保留旧boundary和stage；commit后cleanup前崩溃由startup janitor按authority清理。
- final/failed/cancelled/covered authority提交后best-effort cleanup；cleanup失败不反转authority，24小时janitor收敛。
- 无manifest raw因owner不明fail-safe保留，不猜测删除。
- Metric label只使用closed preflight/compaction/transient outcome，拒绝Task/Run/call/stage ref、capability ID、路径、正文和Tool参数。

## 6. 发布与回滚

- 本轮没有构建、推送、重启或部署新镜像；已运行的本地容器不作为本轮发布证据。
- 发布必须backend与Runtime Sidecar成对；Frontend wire/UI本轮无变更。
- 回滚前停止新submission，统计并等待所有v2 Run final、terminal或被committed compaction覆盖。
- 在新版本中完成在途v2 recovery/cleanup；旧版本不得接管v2 receipt。
- 确认无in-flight v2后成对回滚backend/Sidecar。回滚后新大Skill结果恢复v1 `artifact_backed` safe mode，历史v1继续可读，v2 AgentItem/stage不做破坏性清理。

## 7. 已知非阻断缺口

1. 本机为macOS；统一Rust门禁生成macOS wheel，未执行Ubuntu 22.04 / `manylinux_2_35` wheel smoke，因此不宣称发布完成。
2. Storage全量仍有13项环境条件skip，Integrations有2项平台条件skip；本目标要求的PostgreSQL Agent repository已用隔离PostgreSQL 17单独6 / 6零skip闭合。
3. 未执行生产部署、真实租户MCP或浏览器发布验收；与本设计的Skill消费/持久化边界无关，但阻止宣称`release_complete`。

## 8. 最终判定

`repository_implementation=complete`，`local_runtime_smoke=complete`，`release_acceptance=not_claimed`，`deployment_performed=false`，`prod_untouched=true`。

License Requirement：复用现有Python/SQLAlchemy/SQLite/PostgreSQL、Rust/Runtime Sidecar opaque AgentItem、provider token counter、Agent projector/context/compaction、LocalArtifactFileStore、Frontend与Skill runtime；无新增第三方依赖或许可类型。
