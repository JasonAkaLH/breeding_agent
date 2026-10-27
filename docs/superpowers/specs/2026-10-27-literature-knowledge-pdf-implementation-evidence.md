# 文献知识库双格式实施证据

- 日期：2026-10-27；分支 `main`；开始基线 `bcece50`，工作区原为干净。
- 状态：`implemented_local_sqlite_verified_model_acceptance_partial`。仓库接入与本地验证完成；真实模型稳定性/引用质量仍有失败，不能宣布 AC-27、NFR-05 或整份 PRD 全部验收通过。
- 依据：[接续计划](2026-10-27-literature-knowledge-pdf-implementation-plan.md)、[PRD 24](../../prd/backend/24-文献知识库原生CapabilityPRD.md)。旧 Markdown 历史结果仍见[原证据](2026-10-16-literature-knowledge-implementation-evidence.md)，不将其扩展为 PDF 证据。
- 用户执行调整：本次以 SQLite 完成数据库实测，替代真实 PostgreSQL；PostgreSQL 只复验 v13 manifest/reconciliation 合同，未连接实际 PostgreSQL。
- 未发布镜像、未部署、未操作应用/生产数据库、未读取受保护的 `docker_cmd/` 文件，未修改原始文献目录。

## 1. 源码检查点与存储边界

| 检查点 | Commit | 范围 |
|---|---|---|
| A | `450ba1d` | 固定解析依赖、逐页解析、私有双格式文件仓、nullable 解析列、旧 SQLite 增列、PostgreSQL v13 合同。 |
| B | `082e892` | PDF 导入与正文就绪、claim/CAS、人工审核、四种替换、双文件清理恢复及旧 Markdown 兼容。 |
| C～E | `1cbded9` | 原件/正文 API、管理页面、正文组合版本、实际分页/压缩、评测脚本与 SQL 不存全文回归。 |

**全文只存在本地文件中，不存在任何 SQL 表中。** PDF 原件为私有 `.pdf` 文件，提取正文为私有 `.md` 文件，库目录下生成 `index.md`。Markdown 上传直接读取原件，不复制第二份全文。

SQL 继续保存既有书目元数据/检索简述、库归属、授权、处理及清理状态；`body_ref` / `parsed_body_ref` 只是相对文件引用，两个 SHA 字段只是校验值。SQL 不存 PDF 二进制、解析全文或文件内容副本。AgentItem 仅保存小引用；实际模型和压缩请求按当前授权/版本从本地文件装载文字。`test_knowledge_flow` 同时证明正文进入模型请求、SQLite 全库逻辑 dump 与日志不含合成正文标记。

两张表各增加 nullable TEXT `parsed_body_ref`、`parsed_body_sha256`。SQLite 从真实缺列且已有文献、草稿、共享及选库数据的临时旧结构开始，重复 bootstrap 后旧行仍可维护。PostgreSQL manifest 为 `maf.postgresql_fresh_runtime_schema.v13`；静态 reconciliation 只增加这四列，保留既有 operator-only 边界和判重约束。

原件 SHA 继续承担同库跨表判重。Markdown 正文版本保留原 SHA；PDF 正文版本为 `SHA256("knowledge-pdf-text-v1\0" + source_sha256 + "\0" + parsed_body_sha256)`，不充当下载哈希。任一 SHA 变化均使旧游标/引用失效，未改变引用 schema 或 Rust wire。

## 2. 自动验证

所有命令使用项目 `multi_agent` 环境。下表为实际读取过输出的结果；不同模块分批执行，未把环境错误或 skip 当作通过。

| 检查 | 实际结果 |
|---|---|
| 起始 Markdown 定向基线：files/repository/library_service/routes/results | 43 项通过。 |
| 最终解析/文件/schema 定向组（含旧 SQLite、v13 reconciliation） | 49 项通过。 |
| 最终模型预算、PDF/服务、knowledge results、PDF E2E 和 evaluate 定向组 | 51 项通过。 |
| 最终 Markdown BOM 预览字节/SHA 补测（routes/service/developer_docs） | 37 项通过；先以原始 BOM 丢失红测复现，再改为返回原始 Markdown 字节，与响应 SHA 一致。 |
| `discover -s tests/storage` | 590 项，16 skip，其余通过；skip 包含缺真实 PostgreSQL DSN 的模块。 |
| `discover -s tests/lifecycle` | 48 项通过。 |
| `discover -s tests/orchestration` | 208 项通过。 |
| `discover -s tests/capabilities` | 36 项通过。 |
| `discover -s tests/api` | 673 项通过。 |
| `discover -s tests/e2e` | 21 项通过；包含真实 Sidecar off/shadow/enforce 重启范围恢复。 |
| `discover -s tests/observability` | 41 项通过。 |
| `discover -s tests/core` | 54 项，3 项既有失败；见下文隔离基线复现。 |
| `discover -s tests/integrations` | 835 项，1 项既有失败、2 skip；其余通过。 |
| 前端完整 `npm --prefix frontend test -- --run` | 27 个文件、379 项通过。 |
| 前端 typecheck / build | 通过；构建保留既有大 chunk 提示。 |
| API guide Python 合同及 evaluate CLI | 13 项通过。 |
| `NODE_PATH="$PWD/frontend/node_modules" node --test tests/api/api_doc_ui.test.cjs` | 16 项通过；59 个接口操作、26 个知识库操作及显式原件下载。 |
| `scripts/update_api_doc_contract.py --check`、修改文件 Ruff、`git diff --check` | 通过。 |

新增聚焦验证包括：扫描/混合/加密/损坏与普通空白页、页界及首尾、限额含等值边界、解析日志隔离；PDF/Markdown 独立文件与删除标记；候选登记后写盘失败重试、元数据失败人工完善、迟到解析/写入、丢弃/删除、取消与 claim 接管；四种格式替换；原件/正文哈希和权限；旧草稿 ID 关闭；原件变化但文字相同、解析 SHA 变化、旧 Markdown 版本；真实解析函数进入实际模型请求、连续分页、压缩、历史答案独立与 SQL 不存全文。

真实模型先暴露了分页预算吃满剩余窗口、随后压缩摘要无法放入的问题。新增 `test_pdf_pages_leave_room_for_compaction_with_matching_token_counters` 在修复前稳定失败，修复后通过。分页现在给后续摘要和推进预留半数剩余上下文，仍受每次 50,000-token 和 durable payload 131,072-byte 上限约束；没有扩大模型窗口或修改 Rust。

### 环境与既有失败

受限环境的回环端口/分词访问曾使 Sidecar、API 与两项 memory 测试失败；授权后重跑相关层，最终 storage/lifecycle/orchestration/API/E2E 均通过。不能将首次受限运行写为产品失败或最终成功结果。

以下失败在仓库外 `git archive bcece50` 的隔离副本以同一环境重新运行并复现，未修改其无关合同：

- `test_persistence_port_contracts.PersistencePortContractsTest.test_aggregate_is_thin_and_preserves_exact_inherited_surface`。
- 同类 `test_narrow_ports_own_one_exact_disjoint_partition`。
- `test_public_contract_compatibility.PublicContractCompatibilityTest.test_storage_port_has_exact_async_method_signatures`。
- `test_mcp_client.MCPClientTests.test_client_rejects_negotiated_version_incompatible_with_transport_family`。

新增 PDF 提示词一度使 2000-token 模型预算回归失败；已精简知识库提示词，并在同一测试及完整 API 673 项中验证通过，未修改预算语义或该测试。

## 3. 真实文献的本地解析与规模

只读使用用户提供的 `documents_lib/文献`、`documents_lib/公司`，原件不变；报告不含原始文件名、正文、问题、答案、凭据或宿主路径。

- 目录结构检查共 747 个小写 `.pdf`，其中 66 个超过既定 20 MiB，2 个结构读取失败。该计数不是全部文件的成功解析率，也未将资料目录直接当作已入库。
- 分散抽取 14 份：8 份完整提取、3 份因原件超过 20 MiB 拒绝、3 份因非空页不可提取文字拒绝。中文、英文和多栏均有实际样本；成功样本 8～17 页。
- 从尺寸合格文件中另选最长三份：740 页和 727 页样本因不可提取页失败；739 页样本完整提取 2,678,370 字符，约 7.866 秒（该次检查值）。
- 使用 Poppler 对照英文多栏首页及中文手册末页，核对首尾文字、页序与文字层边界。中文手册截图中的表格内容不进入提取正文；英文连字符断行、阅读顺序和图片/公式语义不保证自动还原。
- 独立临时 SQLite 本地规模探针完成 300 篇真实 PDF 的解析、确认和索引，遇到 31 个失败样本并明确丢弃；300 个原件 SHA 无重复。耗时 195.391 秒；索引 58,886 字符，读取约 0.008 秒；单篇正文读取约 0.002 秒。

**规模探针使用固定元数据替身，不是 300 篇真实模型问答或元数据质量评测。** 这些延迟是单次本机检查值，不是性能承诺。

## 4. 已授权真实模型评测与实际缺口

用户明确批准将抽样英文作物基因组综述、中文公司手册正文与问题发送到既有配置的 `https://ark.cn-beijing.volces.com`，使用现有凭据。仅在内存将测试上下文窗口调整为 12,000/16,000；未复制含凭据配置到临时文件。每轮使用独立临时 SQLite/私有文件根，不导入应用库；模型请求和完整回答不进入 Git 或本报告。

问题集包含三库（研究、公司资料、空库）、三份双格式输入、三个问题；覆盖指定库、索引后正文、中文末页事实、英文长文完整覆盖/必然压缩、缺书目字段和空库。该小问题集不代表数百篇、多主题、相近标题的完整真实模型质量验收。

| 轮次 | 条件与实际结果 |
|---|---|
| R1 | 12,000 窗口：手册完成且正文事实/书目标题符合预期，但缺 `[1]`；英文长文压缩后 `agent_context_required_segments_too_large`；空库完成。 |
| R2 | 16,000 窗口：手册 `[1]` 与事实通过；长文仍因相同预算问题失败，证明单纯扩大测试窗口未解决根因。 |
| R3 | 修复分页预算后：三问均完成，英文全文连续覆盖并实际压缩 3 次，预期事实和书目标题正确；两篇答案均缺约定的数字引用格式。 |
| R4 | 明确系统引用格式后：手册事实/书目/`[1]` 通过；长文已完整读取并压缩 3 次，但最终以 `execution_crash` 结束；空库完成。 |
| R5 | 复验在导入阶段以 `ValueError` 中止，未取得完整结果。安全输出未保存底层异常正文，精确原因未闭合，不将其记录为通过。 |

R3 的可得指标（不使用失败轮替代）：元数据模型 6 次调用、32.935 秒，文字适配器无 usage，明确记为不可得。问答/压缩总计 17 次模型返回：手册 4 次、12.009 秒、10,118 prompt / 796 completion tokens；研究 11 次（含 3 次压缩）、60.505 秒、72,661 / 3,748 tokens；空库 2 次、4.725 秒、2,892 / 243 tokens。全轮约 110.180 秒。该轮问答 usage 均由模型返回；没有推算元数据调用 token。

对照正文检查了中文末页的两个操作事实，以及英文文章工具/局限和书目；发现部分答案对阅读覆盖范围的表述不精确。数字引用格式、最终回答稳定性、失败原因、更多真实主题和规模问题集仍需继续验证，不能以替身结果或一次成功读取关闭 AC-24/AC-27/AC-29/NFR-05 的真实质量部分。

首次 live 动作曾被自动审批拒绝（资料/端点授权和凭据副本风险）。随后用户批准具体资料/端点，并改为内存配置才执行；没有绕过拒绝或创建配置副本。

## 5. AC-01～AC-33 索引

“本地通过”指实际自动化/文件检查，不等于真实模型质量或部署验收。

| AC | 本次证据与状态 |
|---|---|
| 01 | 前端账户入口/App、管理组件及原生工具 catalog；本地通过。 |
| 02 | repository/service 库维护、选库/会话生命周期与旧 SQLite 数据保留；本地通过。 |
| 03 | extraction 分段全覆盖、PDF E2E 首尾事实；真实元数据多轮有失败，质量未整体关闭。 |
| 04 | service/routes/组件正文就绪和确认前索引门禁；本地通过。 |
| 05 | batch_confirm、组件逐项结果及元数据失败人工完善；本地通过。 |
| 06 | files/service/API 原件与正文分别按真实字节哈希、MD BOM/CRLF；本地通过。 |
| 07 | repository 跨表并发、PDF 异名/同批/重试/并发；SQLite 本地通过。 |
| 08 | 相同解析文字而原件不同仍独立，版本不同；本地通过。 |
| 09 | 跨库文件独立，删除/重传不影响另一库；本地通过。 |
| 10 | MD→MD、MD→PDF、PDF→MD、PDF→PDF 同 ID 替换和双文件删除；本地通过。 |
| 11 | 删除先关闭读取、清理失败/重启恢复、库独立；本地通过。 |
| 12 | 索引修订 CAS、失败拒绝旧投影、300 篇索引；本地通过。 |
| 13 | 已有用户共享、幂等授权；本地通过。 |
| 14 | PDF source/content owner/reader、草稿只 owner、直接写拒绝；本地通过。 |
| 15 | ACL/撤权覆盖 source/content 和 Agent 实时装载；本地通过。 |
| 16 | access 主体 resolver 合同与 API 拒绝未支持团队；本地通过。 |
| 17 | knowledge-selection API/hook/App 保存与切换竞态；本地通过。 |
| 18 | 固定范围及真实 Sidecar off/shadow/enforce 重启、不扩 wire；本地通过。 |
| 19 | 指定库部分/全部失效、不得回退其他库；本地通过，真实空库检查有结果。 |
| 20 | 固定三个原生 knowledge 工具与既有 Invocation lifecycle；本地通过。 |
| 21 | 实际 PDF parser→审核→索引→正文→模型请求；替身完整通过，真实模型有已读全文事实样例。 |
| 22 | 长索引/长 PDF 连续范围与压缩、739 页解析；本地通过，真实长文曾完整读取。 |
| 23 | 原件/解析 SHA、旧游标/引用 changed、实时重验；本地通过。 |
| 24 | 替身引用合同通过；真实引用格式与稳定性仍部分失败，未整体关闭。 |
| 25 | Markdown/PDF 历史答案在删除后不变；本地通过。 |
| 26 | 元数据失败可人工审核、解析失败不可绕过，发布/写盘/并发恢复；本地通过。 |
| 27 | SQLite 替代 PostgreSQL 已获用户确认；当前本地层大部分通过、4 项既有失败，真实质量仍有缺口；未关闭整体验收。 |
| 28 | 同参新读不复用旧成功、撤权/替换复验；本地通过。 |
| 29 | 一致 token counter 红绿测试与真实压缩 3 次；真实最终引用质量仍未整体关闭。 |
| 30 | 中英文/多页 PDF 解析、审核、原件哈希、实际模型正文；本地与样本检查通过，完整真实质量仍受上述缺口限制。 |
| 31 | 空白、扫描/混合、损坏、加密和限额；明确失败且不可手工绕过；本地通过。 |
| 32 | 混合批次部分失败、原件判重、跨格式不合并；本地通过。 |
| 33 | 四种替换、候选中断、迟到工作、双文件清理及旧引用失效；本地通过。 |

NFR-01/02/03/04 分别由后台线程及 claim、实际分页/压缩预算、日志/SQL 全文隔离、旧库升级和恢复覆盖。NFR-05 仅有上述小集实测，不宣称整体质量达标。

## 6. 发布与回退

原件与正文的私有文件根必须持久化并与 SQL 状态一致备份。旧程序把 PDF `body_ref` 当 Markdown，回退前应停写、停止处理并收敛活动 Run，优先前向修复；不删除新增列、原件或解析正文进行普通代码回退。当前只完成本地源码和验证，不执行部署。

License Requirement：唯一新增依赖为 `pypdf==6.21.2`，BSD-3-Clause。固定 plain 文字提取和默认容错读取；上限见 runbook。上游依据：[文字提取及限制](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[许可证](https://github.com/py-pdf/pypdf/blob/main/LICENSE)。不安装 OCR、ReportLab、模型 SDK 或新的常驻服务。
