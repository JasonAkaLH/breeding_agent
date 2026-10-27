# 文献知识库双格式完整交付实施计划

## 1. 状态、依据与范围

- 日期：2026-10-27。
- 状态：`accepted_local_sqlite`。已按本计划实施，检查点 A `450ba1d`、B `082e892`、C～E `1cbded9`，接续引用/租约修复 `101e7e2`、严格完整阅读 `fe0115b`；最终代码、实际回归与真实模型结果见[PDF 实施证据](2026-10-27-literature-knowledge-pdf-implementation-evidence.md)。未部署。
- 执行调整（2026-10-27 用户确认）：本次数据库实测使用 SQLite 替代真实 PostgreSQL；PostgreSQL 保留 v13 manifest/reconciliation 静态合同，不把 SQLite 结果宣称为 PostgreSQL 事务结果。
- 存储澄清（2026-10-27 用户再次确认）：原件、解析 Markdown 和 index.md 均在本地私有文件目录；SQL 不保存 PDF 二进制或解析全文，只保存既有元数据、权限、状态、相对文件引用与哈希。工具持久化小引用，发送给 LLM 时才从文件装载文字。
- 产品依据：[PRD 24](../../prd/backend/24-文献知识库原生CapabilityPRD.md)，包含用户已确认的 Markdown / PDF 双格式输入和“扫描版暂不处理”。
- 代码基线：`main@9aba682`。当前工作区另有本轮形成的 PRD、原实施计划、证据范围说明、文档索引和 CHANGELOG 修订；实施时保留这些修改并重新检查 HEAD。
- 接续关系：[原实施计划](2026-10-16-literature-knowledge-implementation-plan.md)的 0～F 已完成 Markdown 仓库接入；原 G 的真实环境验收未闭合。本文是新增 PDF 和剩余整体验收的执行入口，原计划第 8 节保留设计摘要，不再维护第二套开发顺序。
- 既有证据：[2026-10-16 验证记录](2026-10-16-literature-knowledge-implementation-evidence.md)只证明当时执行的 Markdown/替身/Sidecar 检查，不证明 PDF 或真实文献质量。

本计划一次覆盖新增 PDF 能力和原 PRD 的完整交付要求。复用已实现的建库、审核、共享、会话选库、原生工具、分页与压缩；只修改双格式所需接入点并复验既有行为。

本次不增加 OCR、图片语义识别、向量检索、LlamaIndex、PageIndex、团队管理、共享编辑者、独立问答 Agent 或文件转换服务。不读取受保护的本地部署文件，不导入应用/生产数据库，不修改用户的原始文献目录。

## 2. 现有代码与必要改动

| 入口 | 已核对的当前行为 | 本次必要改动 |
|---|---|---|
| `src/capabilities/knowledge/service.py` | `_upload` 只接受 Markdown；处理、预览、确认和读取都将 `body_ref` 解释为 UTF-8 原文。 | 分开原件读取与阅读正文，接入后台 PDF 解析，补齐解析门禁和双文件恢复。 |
| `src/capabilities/knowledge/extraction.py` | 按完整 Markdown 分段提取元数据，允许元数据失败后人工完善。 | 继续只消费 Markdown；不把 PDF 二进制交给现有提取器，不生成虚假的页标题作为论文标题。 |
| `src/storage/knowledge_files.py` | 受控引用只允许 `.md`，`put_body` 一律解码 UTF-8；原子写入、库锁、哈希及删除标记已存在。 | 明确支持 `.pdf` 原件与 `.md` 阅读正文，保留全部文件安全及清理约束。 |
| `src/storage/sqlalchemy_models.py`、`knowledge_repository.py` | 原件身份用 `sha256` 判重；草稿/文献各只有一个 `body_ref`；确认、替换与清理已有事务和 CAS。 | 增加 PDF 解析正文引用与哈希，成对发布并登记全部旧引用；同库原件判重逻辑保持。 |
| `src/storage/sqlite/bootstrap.py` | 已有 pre-create additive migration；`create_all` 不会补现有表的列。 | 添加知识库两张表的可空列兼容升级，验证带旧数据的数据库。 |
| `src/state/postgres/runtime_schema.py`、`schema_reconciler.py` | 当前 manifest 为 v12，由共享模型生成列；已有安全增列和 operator-only 漂移边界。 | 将 manifest 推进为 v13，验证新增列可安全 reconciliation，保留其他表的漂移规则。 |
| `src/api/routes/knowledge.py` | `/content` 直接调用 `download_document`，其内容目前同时是原件和阅读正文。 | `/content` 固定为 Markdown，新增已入库原件和草稿原件下载接口。 |
| `frontend/src/api/client.ts`、`knowledgeTypes.ts`、`KnowledgeSettingsPanel.tsx` | 上传和替换控件只接受 Markdown；下载统一走 `/content?download=true`。 | 支持双格式，分开原件下载、解析正文预览和 Markdown 导出，显示解析失败。 |
| `src/orchestration/agent_loop/knowledge_results.py`、`knowledge/registration.py` | 正文版本直接取 `read_document` 的 `sha256`；引用 schema、正文装载和预算已闭合。 | 明确正文版本与下载哈希的区别；Markdown 保持原版本值，PDF 版本绑定原件与解析正文。 |
| `scripts/evaluate_knowledge.py` | corpus 清单只允许 `.md`，在临时 SQLite/文件根执行提取、确认和实际 Agent 链路。 | 扩展 `.pdf` 清单与双格式导入结果，保持路径限制、隔离环境和低敏报告。 |

新增源码只需一个小型本地 PDF 解析模块，建议为 `src/capabilities/knowledge/pdf.py`；不抽象通用解析器注册表。相关状态与事务继续由已有 service/repository 拥有。

## 3. 实施时固定的数据与接口语义

### 3.1 原件、阅读正文与三个哈希用途

解析全文不写入任何 SQL 表。文件存储是正文唯一持久来源，`index.md` 是按修订生成的文件投影；数据库仅记录控制状态与文件位置。在 `KnowledgeImportRow` 和 `KnowledgeDocumentRow` 各增加两个可空字段：`parsed_body_ref`、`parsed_body_sha256`。原 `body_ref`、`sha256` 保持上传原件含义。字段命名是本计划的实施选择，不增加用户配置。

| 对象 | Markdown 上传 | PDF 上传 |
|---|---|---|
| `body_ref`、`sha256` | 原始 Markdown 及其原始字节 SHA-256。 | 原始 PDF 及其原始字节 SHA-256。 |
| `parsed_body_ref`、`parsed_body_sha256` | 均为空，直接阅读原件，不复制第二份全文。 | 解析出的 Markdown 及其字节 SHA-256；解析未完成时不可作为可读正文。 |
| 原件下载校验 | 原 `sha256`。 | 原 `sha256`。 |
| Markdown 下载校验 | 原 `sha256`，BOM/CRLF 等原样保留。 | `parsed_body_sha256`，不能使用 PDF 的哈希。 |
| Agent 正文版本 | 保持现有值：原 `sha256`，兼容持久化旧引用。 | 对原件哈希和解析正文哈希计算确定性的组合 SHA-256，仍为 64 字符。 |

PDF 版本可采用 `SHA256("knowledge-pdf-text-v1\0" + source_sha256 + "\0" + parsed_body_sha256)`，集中在一个 helper 中生成。它只用作可变资源版本，不冒充任一下载文件的哈希。

`read_document` 返回实际阅读正文的内容哈希和独立 `version`；`load_knowledge_resource` 使用 `version`。Markdown 的版本和值保持不变。现有 `knowledge_reference.v1`、游标形状和三个工具参数不扩展；不迁移或重写历史 AgentItem。

同库判重始终比较上传原件原始字节的 SHA-256，覆盖文献、未丢弃草稿、同批、重试和并发。标题、DOI、转换文字相同均不触发合并；跨库文献仍独立。

### 3.2 本地 PDF 解析

采用 `pypdf` 本地读取文字层；实施时核对并固定经测试的版本到 `requirements.txt`，记录 BSD-3-Clause 许可证。不安装 OCR 或另一个模型 SDK。

- 接受 `.md`、`.markdown`、`.pdf`，大小沿用当前单文件 20 MiB 上限；后端验证扩展名和实际内容，不信任 MIME 或前端 `accept`。
- PDF 结构读取、页遍历和文字提取放在现有处理 worker 的线程执行；LLM 元数据提取仍复用现有 `KnowledgeMetadataExtractor`。
- 逐页保留提取出的文字及顺序，加入 `<!-- PDF page: N -->` 页界，不凭空重建标题层级、表格含义或公式。不让模型改写解析全文，也不截掉页眉页脚后遗漏有效信息。
- 用真实中英文、多栏样本验证并选定固定提取模式，记录阅读顺序的已知局限；不按每个请求增加复杂的解析策略选择。
- 全文无有效文字、检测到扫描/不可读页、损坏、加密不可读，均明确失败。真正空白页保留页界；无法区分扫描与纯图形页时按无法提取正文提示，不跳过后声称完整。
- 不承诺自动识别所有伪文字层或文字质量问题，最终由所有者对照原件审核；不对扫描分类准确率作未经实测的保证。
- 对页数、展开内容和单页内容流设置必要的内部上限，采用命名常量并用边界测试固定；超过上限失败，不静默裁切，也不新增用户设置。上限在样本验证时确定并记录。

图片内部信息、扫描内容和原页面视觉语义不属于解析正文。提示词、工具说明与审核页面均需避免把“文字正文读完”表述为“全部 PDF 视觉内容已识别”。

技术依据：[pypdf 文字提取、布局模式及限制](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[上游许可证](https://github.com/py-pdf/pypdf/blob/main/LICENSE)，已于 2026-10-27 核对。

### 3.3 持久状态、发布与清理顺序

继续使用现有 `pending → processing → awaiting_review → publishing → published`，失败为 `failed`、丢弃为 `discarded`；不新增独立 PDF job 表或第二套调度器。解析与元数据提取均归属同一导入记录。

1. 上传仍先登记原件引用，再落盘并验证原件哈希，最后进入 `pending`；PDF 原件使用 `.pdf`，不能假存为 `.md`。
2. 领取现有 claim 并续租。Markdown 直接读取原件；PDF 先检查是否已有有效解析正文，没有时读取原件并在内存逐页解析。
3. 得到完整候选 Markdown 后，在短事务内检查库、草稿状态、原件哈希、claim 与 revision，成对登记候选解析引用及哈希；旧候选引用如需替换，同时进入 `cleanup_refs`。
4. 使用登记后的新 revision 写解析正文，原子落盘并重读校验。写前、写后均不能绕过失效 claim、删除标记和草稿状态；继续提取时使用更新后的记录，不能沿用领取时的旧 revision。
5. 阅读正文准备好后才调用全文元数据提取。成功进入待确认；元数据失败但正文有效时允许人工完善，解析失败或正文缺失时禁止审核确认。`edit_import` 也不得把未解析 PDF 直接变为可确认草稿。
6. 确认在同一数据库事务中绑定原件、解析正文和元数据，推进索引修订；替换时登记旧原件和旧解析正文。完成索引发布、清理和校验后才标记完整成功。
7. 丢弃或删除先关闭读取，再清理当前/候选原件、解析正文和临时文件。草稿预览和原件下载不读取已发布或已丢弃的旧导入，防止利用旧 import ID 访问替换后的旧全文。

上述过程不在数据库事务中执行解析、长文件 I/O 或模型调用。后台线程取消不被当作已终止全部底层工作，迟到结果必须通过状态、claim、revision 和删除标记被拒绝。

| 中断位置 | 恢复要求 |
|---|---|
| 解析完成、尚未登记引用 | 只有内存结果；失去 claim 后不能登记或写盘。 |
| 引用已登记、正文未写完 | 恢复能定位该引用；重新生成或明确失败，不能确认缺失正文；临时文件可清理。 |
| 正文已写、元数据提取失败 | 原件与正文保留作审核依据；允许重试或人工补元数据，不重复产生全文副本。 |
| 解析过程中被丢弃、删除或另一 worker 接管 | 旧执行不能复活草稿、覆盖新候选、清除新版本或留下未知全文。 |
| 原件/正文指针切换后崩溃或清理失败 | 已切换旧引用立即不可新读；恢复继续索引和双文件清理，未完成不报成功。 |

替换支持 MD→MD、MD→PDF、PDF→MD、PDF→PDF；文献 ID 不变。已输出答案与引用始终保持普通聊天文本，不建立文献生命周期关联。

### 3.4 数据库兼容与回退边界

- 两张表各增两个 nullable TEXT 列，无新增表、共享角色、枚举或判重唯一键；应用层对解析引用与哈希成对验证。
- SQLite 在 `bootstrap_sqlite_database` 的 `create_all` 前做幂等增列。旧 Markdown 行保持解析字段为空；测试必须从真实缺列、已有文献/草稿/共享/选库的旧结构开始，重复 bootstrap 后继续读取和维护。
- PostgreSQL 由共享模型生成新 manifest，版本 v12→v13；验证 reconciliation 只提出这四列的安全增列，不改变知识库判重约束，也不放宽其他表的 operator-only 规则。
- 使用隔离 PostgreSQL 验证带旧行的增列、重复启动、并发去重、确认和替换；缺隔离 DSN 时明确记录，不能将静态 schema 测试当真实事务证据。
- 回退前停止知识库写入/处理并收敛活动知识库 Run。旧代码会将 PDF 当 Markdown，不能未经验证回退到旧 writer；优先前向修复，不删除新增列或文献文件来回退，也不通过恢复历史正文处理普通代码回退。

## 4. API、页面与 Agent 接口

### 4.1 HTTP 合同

| 路径 | 内容与校验 | 权限 |
|---|---|---|
| `POST .../libraries/{library_id}/imports` | 混合上传，逐项返回 pending/重复/失败；支持现有 replacement_document_id。 | owner。 |
| `GET .../imports/{import_id}/content` | 预览就绪的阅读 Markdown；解析未完成返回明确不可用，不返回 PDF 二进制。 | owner，仅有效草稿。 |
| `GET .../imports/{import_id}/source` | 下载草稿原件；PDF 为 application/pdf，Markdown 为 text/markdown。 | owner，仅有效草稿；published/discarded 不作为历史原件下载入口。 |
| `GET .../documents/{document_id}/content` | 预览/导出当前 Markdown；沿用 `download=true`，原 Markdown 字节及行为不变。 | 当前 owner/reader。 |
| `GET .../documents/{document_id}/source` | 下载当前上传原件，保留原始字节。 | 当前 owner/reader。 |

所有路径沿用身份、库归属、当前状态和授权检查；读取后复验版本/状态。两种下载都设置 `Cache-Control: no-store`、正确媒体类型、经过安全编码的文件名及对应文件真实 `X-Content-SHA256`。PDF 原件的名字保持 `.pdf`，解析正文导出使用 `.md`。

保留 service 的原件下载语义，新增明确的阅读正文下载方法；`read_document`、`preview_import` 和 `/content` 使用阅读正文路径。新增草稿原件方法复用 `_import` 的 owner 校验及草稿状态门禁，不从模型接收物理路径。

列表/草稿可增加只读的 `source_format`、`text_ready` 展示字段，不暴露物理引用。就绪状态必须来自有效的处理记录与内容校验；前端的按钮状态不能替代后端确认时重读与哈希校验。

错误至少区分不支持格式、PDF 损坏/不可读、无可用文字、扫描页暂不支持、解析正文尚未就绪、超过限制和存储不可用；具体代码在本阶段定稿并以测试固定。日志只记状态、计数和耗时，不输出正文、底层异常文本或凭据；解析适配层也需处理 `pypdf` 内部警告，不能让其绕过这条约束。

### 4.2 管理页面

- 继续从账户设置进入，保留“我的文献库 / 共享给我”和 MCP 设置。
- 批量上传与替换控件接受双格式，文案改为“上传文献”；显示格式与逐项实际状态。上传接受不等于解析成功或已入库。
- PDF 草稿提供“下载原 PDF”“预览提取正文”，明确只处理文字层；解析未完成/失败时禁止确认，显示可理解的原因。
- 元数据提取失败而正文就绪时保留人工编辑与批量确认能力；解析失败则提供重试、丢弃或改传可读文件，不能以手填字段绕过。
- 已入库 PDF 提供“下载原 PDF”和“导出 Markdown”；Markdown 文献保持原来的原文预览/下载，不产生重复文件操作。
- 替换确认说明将清理旧原件和旧解析正文。现有轮询、账号/会话切换防迟到响应、只读权限和选库保存顺序保持。
- 客户端继续使用带认证的请求与 Blob 下载，不把私有文件放入公开 URL，不引入 PDF 编辑器。

### 4.3 原生工具与模型上下文

固定保留 `knowledge.list_libraries`、`knowledge.read_index`、`knowledge.read_document`，类型继续为 `knowledge`，不通过 Skill/MCP 包装 PDF 解析。

- 更新工具描述及必要提示词，说明正文为 Markdown 上传原文或 PDF 提取文字，索引简述仍不能代替全文证据。
- 先按库描述选库，再读索引、正文；当前轮的固定范围、撤权、部分/全部库失效规则保持。
- 正文和索引继续只持久化小引用；普通模型请求与压缩请求均装载实际文字、按实际 token 计数，沿用 payload 和工具业务结果预算。
- 无游标新读取得到当前正文；旧 PDF 游标/持久引用在原件或解析正文变化后返回 changed/unavailable，不能读取旧文件或混读新正文。
- 历史 Markdown 引用的版本计算保持原值；不修改引用 schema 或 Sidecar wire，不重写历史答案。

## 5. 开发阶段与检查点

每阶段先添加证明新行为的聚焦回归，再实施并验证；通过后按仓库规范保存范围明确的 Git 检查点。阶段是开发顺序，不是产品版本。以下命令与测试均为执行计划，本次未运行功能测试。

### 阶段 0：基线与合成夹具

1. 检查当前分支、HEAD、用户差异和已有知识库测试，记录既有失败/skip 的实际原因。
2. 增加小型合成 PDF helper；基础红测用标准库构造最小文字 PDF 字节。阶段 A 固定解析依赖后补齐多页文字、普通空白页、图片/扫描页、混合页、损坏和加密夹具，不为测试增加未声明的 ReportLab 依赖。
3. 建立 PDF 上传当前失败、原件/正文读取区分及双文件清理的红测；不提交私有真实文献。

出口：能稳定证明当前代码的格式缺口，已有 Markdown 基线已记录。夹具建议放在 `tests/capabilities/knowledge/pdf_fixtures.py`，解析回归放在 `test_pdf.py`。

### 阶段 A：解析、文件仓与兼容 schema

1. 固定 `pypdf` 版本和依赖；实现小型逐页 PDF→Markdown 函数及明确错误，不调用模型或网络转换。
2. 扩展 `KnowledgeFileStore` 的受控扩展名，原件和解析正文均沿用私有权限、哈希、原子写入和删除标记。
3. 增加两表字段、SQLite 缺列升级、PostgreSQL v13 manifest/reconciliation 合同；旧行保持可读。

验证：解析首尾及页界、扫描/空白区分和失败不截断；PDF 二进制不解码为 UTF-8；同目录原件与正文可分别删除；路径/符号链接/硬链接约束保持；真实旧结构增列及幂等。

主要文件：`requirements.txt`、`knowledge/pdf.py`、`storage/knowledge_files.py`、`sqlalchemy_models.py`、`sqlite/bootstrap.py`、`state/postgres/runtime_schema.py`；只在合同测试证明必要时修改 `schema_reconciler.py`。

### 阶段 B：导入、人工审核和双文件恢复

1. 扩展 `_upload` 原件验证，保持原始 SHA-256 和跨表判重。
2. 在 `process_pending` 接入解析，增加成对引用登记的 claim/CAS；使用新 revision 推进后续提取。
3. 统一正文就绪校验，接入预览、元数据手工完善和单篇/批量确认；文件读取/哈希校验在短事务外完成，底层 `publish_import` 在事务内复验字段成对、处理状态与 revision，拒绝直接发布未解析 PDF。
4. 扩展 `publish_import`、`_discard`、`delete_document`、`delete_library` 和恢复清理，成对处理原件与解析正文；保留 `cleanup_refs` 作为未完成操作的唯一清理账本。

验证：AC-30～AC-33；同库并发原件判重、跨库独立；四种格式替换；解析登记/写盘/提取/发布各中断点；丢弃、删除、claim 接管及手工编辑竞态；重启后没有旧原件、旧解析正文或未知全文副本。

主要测试：`test_library_service.py`、`test_knowledge_repository.py`、`test_knowledge_files.py`。同一 repository 合同扩展到真实 PostgreSQL 模块；环境不足单列。

### 阶段 C：原件与正文 API

1. 分开原件下载与 Markdown 阅读方法，增加两个 `/source` 路由；保留旧 Markdown `/content` 合同。
2. 同步安全公开字段、错误、媒体类型、文件名和各自哈希；前后均校验当前权限和状态。
3. 覆盖旧 import ID 的读取门禁，避免发布/替换后通过草稿接口访问旧文件。
4. 同步 API guide、前端调用示例及 `docs/api/CHANGELOG.md`，使用既有生成器更新 OpenAPI 快照。

验证：混合批次部分失败；PDF 原件与 Markdown 字节/哈希各自一致；草稿 owner 与已入库 reader；跨库 ID、撤权、删除及发布未完成；源文件名不会改变下载类型或暴露宿主路径。

主要文件：`src/api/routes/knowledge.py`、必要的 `knowledge_dto.py`、领域 service；测试沿用 `tests/api/test_knowledge_routes.py` 和 developer docs 合同。

### 阶段 D：管理页面与客户端

1. 扩展 client/types，明确“原件下载”与现有“Markdown 导出”的方法，不通过文件名猜请求路径。
2. 更新上传、替换、预览、格式标记、错误与确认按钮状态；保留批量逐项结果。
3. 对 PDF 与 Markdown、owner 与 reader、正文未就绪与元数据失败分别测试。

验证：`knowledgeClient.test.ts`、`KnowledgeSettingsPanel.test.tsx`；选库 hook/App 竞态回归、完整前端测试、typecheck、build。无需重写账户设置、选库或消息生命周期。

### 阶段 E：Agent 链路与评测入口

1. 实现正文版本 helper，分开资源 `version` 与阅读/下载 SHA；接入 `knowledge_results.py` 的装载与重验。
2. 更新工具说明与现有知识库提示词；保持三个动作、固定范围与只读权限。
3. 增加从真实 PDF 解析函数经管理确认进入实际模型请求的端到端测试；模型替身根据工具正文首尾细节回答，验证先索引、后正文。
4. 覆盖长 PDF 解析正文分页、强制上下文压缩、旧游标/引用、撤权及替换；检查没有在 AgentItem/Artifact/日志另存原文。
5. 扩展 `evaluate_knowledge.py` 的双格式 manifest 和导入结果处理；重复原件返回现有文献/草稿时不能假设一定返回新 import。保留相对路径限制、显式 live、临时数据库和低敏报告。

验证：`test_knowledge_results.py`、`test_knowledge_flow.py`、`test_evaluate_knowledge.py`；复验 `test_knowledge_scope_recovery.py` 的 off/shadow/enforce 真实 Sidecar 范围恢复，不因 PDF 扩展 Rust 合同。

### 阶段 F：真实素材、全量回归与交接

1. 使用用户提供的 `documents_lib/文献`、`documents_lib/公司` 中有代表性的 PDF 只读副本，覆盖中文、英文、多栏、长文和失败样本，核对首尾文字、页序与局限。
2. 构建明确预期文献、正文事实和至少一项必然压缩的问题集；覆盖多主题、相近标题、缺字段、跨库重复、空库和每库几百篇目标规模。已有 Markdown 可共同参与，不把数据归档当作已经入库。
3. 用已授权模型配置在隔离库执行真实问答，记录选库/选文献、完整覆盖、引用、延迟、调用量与可得 token；对照正文核验引用语义。没有模型返回的 usage 要明确记为不可得。
4. 使用隔离 PostgreSQL 完成升级及并发回归；重跑相关后端层、完整前端和适用 Sidecar 测试。若旧失败仍存在，引用本次基线复现，不顺手修改无关合同。
5. 更新验证记录、runbook、相关 AGENTS 和 CHANGELOG，逐项标明新旧范围的实际结果与缺口，记录固定依赖版本、schema 变更及回退边界。

出口：管理、双格式入库/审核、共享、选库、只读工具、清理恢复、真实环境及 AC-01～AC-33 全部有实际证据后才可宣称完整验收。任何缺口保留具体状态，不用合成测试或 skip 替代。镜像发布、开发/生产数据库升级和部署不在本次计划编写或自动验收执行范围内。

## 6. 全部需求与验收覆盖

| 原 PRD 范围 | 本次实施/复验 | 验收 |
|---|---|---|
| FR-01 管理入口、FR-02 角色 | D 保留入口与 owner/reader；C 同步原件权限。 | AC-01、AC-02、AC-14 |
| FR-03 上传、全文提取 | A/B 实现双格式，E/F 验证实际正文进入提取和问答。 | AC-03、AC-06、AC-26、AC-30、AC-31、AC-32 |
| FR-04 人工审核 | B/C/D 保留单篇/批量审核并增加解析门禁。 | AC-04、AC-05、AC-30、AC-31 |
| FR-05 原始字节判重 | B 保持同库跨表事务判重与跨库独立。 | AC-07、AC-08、AC-09、AC-32 |
| FR-06 替换、FR-07 清理 | B 实现双文件切换、丢弃、删除与恢复。 | AC-10、AC-11、AC-26、AC-33 |
| FR-08 数据职责、FR-09 索引 | A/B/C 保留原件字节、当前投影和跨会话持久性。 | AC-02、AC-06、AC-12、AC-30 |
| FR-10 共享、FR-11 授权/团队预留 | C/E 对原件与正文使用同一授权，保留团队 resolver 接口。 | AC-13、AC-14、AC-15、AC-16、AC-28 |
| FR-12 会话选择、FR-13 本轮固定、FR-14 失效 | D/E 复验选库保存及 Sidecar 恢复，无新增选库逻辑。 | AC-17、AC-18、AC-19 |
| FR-15 原生能力、FR-16 索引后全文 | E 验证独立 knowledge 与 PDF 正文真实请求链。 | AC-20、AC-21、AC-30 |
| FR-17 长文、版本、预算与压缩 | E 覆盖实际正文、连续分页、两种哈希变化与压缩。 | AC-22、AC-23、AC-28、AC-29、AC-33 |
| FR-18 引用与历史答案独立 | E/F 对照事实、书目和删除前后的既有消息。 | AC-24、AC-25、AC-29 |
| 整体平台回归与真实文献验收 | F 汇总实际执行结果，不把旧证据扩展到新格式。 | AC-27 |

| 非功能需求 | 覆盖 |
|---|---|
| NFR-01 异步及效率 | B 复用后台 claim，解析在工作线程，查询不重新解析整篇 PDF，普通聊天不等待入库。 |
| NFR-02 预算 | E 连续分页与实际请求/压缩计数，保留 131,072-byte payload 和 50,000-token 工具业务结果上限。 |
| NFR-03 可观察与隐私 | B/C/D 真实处理/失败/清理状态，F 检查日志及报告不保存全文、完整问答或凭据。 |
| NFR-04 持久化 | A/B/E 覆盖旧库升级、重启、共享、选库和正文文件恢复。 |
| NFR-05 真实育种评测 | F 使用授权真实文献和预期证据问题集，不预先承诺准确率或性能。 |

## 7. 验证命令与环境

以下保留实施验证命令；实际执行计数、环境边界和失败复现以同日实施证据为准。

```bash
# 每阶段先选择相关模块；新增 parser 的测试模块在阶段 0 创建。
conda run -n multi_agent python -m unittest tests.storage.test_knowledge_files tests.storage.test_knowledge_repository tests.storage.test_postgres_runtime_schema_manifest tests.storage.test_postgres_schema_reconciler
conda run -n multi_agent python -m unittest tests.capabilities.knowledge.test_pdf tests.capabilities.knowledge.test_extraction tests.capabilities.knowledge.test_library_service
conda run -n multi_agent python -m unittest tests.api.test_knowledge_routes tests.api.test_knowledge_selection tests.api.test_developer_docs
conda run -n multi_agent python -m unittest tests.orchestration.test_knowledge_results tests.e2e.test_knowledge_flow tests.e2e.test_knowledge_scope_recovery tests.scripts.test_evaluate_knowledge
conda run -n multi_agent python -m unittest -v tests.storage.test_knowledge_postgres_integration
conda run -n multi_agent python scripts/update_api_doc_contract.py --check
npm --prefix frontend test -- --run src/api/knowledgeClient.test.ts src/components/KnowledgeSettingsPanel.test.tsx src/hooks/useKnowledgeSelection.test.tsx src/App.test.tsx
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
git diff --check
```

最终后端分层 discover 和 Ruff 沿用[原计划第 6 节](2026-10-16-literature-knowledge-implementation-plan.md#6-验证命令与执行方式)，增加本次 parser 和修改文件；需要验证完整已确认范围，不能只运行 PDF 新测试。检查无需 Rust 改动时复用既有 Sidecar binary；Rust 合同若确实需要修改，先说明偏差，门禁仍走 `scripts/run_rust_quality_gates.py`。

PostgreSQL 仅使用 `MAF_POSTGRES_KNOWLEDGE_TEST_DSN` 或隔离测试 helper 接受的 `MAF_POSTGRES_TEST_DSN`；真实增列演练使用可丢弃的旧结构测试库，不对共享测试库或生产库删列来制造旧版本。禁止读取 `docker_cmd/` 寻找配置。

真实文献评测先校验 manifest，再使用已有脚本的显式 `--live`。不把 corpus、问题、答案或完整模型请求提交到 Git；报告仅记录低敏标识、数量、检查结果和指标。既有 2026-10-16 环境缺口需重新核查，不能假设现已可用。

## 8. 完成与交接

交接必须包含：固定源码检查点、依赖版本、schema v13 与旧 Markdown 兼容结果、AC-01～AC-33 证据索引、实际测试结果/skip/既有失败、真实文献解析与模型评测结果，以及发布/回退注意事项。新增入口和关键约束同步对应目录 AGENTS；文档状态、Future Work 和 CHANGELOG 同步。

代码实现、真实验收、发布和部署分别记录。PDF 解析成功不等于审核入库；代码/替身测试通过不等于真实模型质量通过；真实环境缺口不构成缩减已确认需求的理由。

License Requirement：实施固定 `pypdf==6.21.2`（BSD-3-Clause），用于本地文字层解析；不引入 OCR、模型 SDK 或新的常驻服务。
