# Skill 输出文件 Artifact 与下载 PRD

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- **范围**：后端 / Agent Skill 兼容层 / 文件型 Artifact / 下载鉴权 / 前后端契约
- **文档状态**：草案（用于补充细节与后续实现规划）
- **日期**：2026-05-08
- **关联模块**：`src/integrations/agent_skills/`、`src/capabilities/main_agent/`、`src/api/`、`src/storage/`、`frontend/`

## 1. 背景

当前 Skill 自动脚本执行链路已经支持：

- 主代理匹配 `SKILL.md`；
- 按 `parameters` / `input_parameters` 解析业务参数；
- 把上传文件原文通过受控 `uploaded_artifacts` 传给脚本；
- 脚本通过 stdout 返回 JSON object；
- 主代理把脚本 JSON 输出注入最终 prompt。

但脚本运行在 `SkillScriptRunner` 创建的临时目录中，执行结束后临时目录会被删除。若 Skill 生成 HTML、CSV、XLSX、PDF、图片等文件，当前只能把小文件内容 inline 到 stdout JSON；大文件只能返回“已生成 / 未生成”的摘要，文件本体无法被平台持久化和下载。

以 `mini-breedstat-rcbd` 为例，RCBD 设计可能生成：

- 田间布局 HTML；
- 田间记录 CSV / JSON；
- 后续可能扩展的 XLSX / PDF 报告。

这些产物不适合全部塞进 LLM prompt，也不应通过脚本暴露服务器本地路径。因此需要平台级、统一、受控的 Skill 输出文件 artifact 与下载机制。

## 2. 目标

1. **统一文件产出口**：Skill 脚本可声明并生成输出文件，由平台收集为 managed artifact。
2. **安全下载**：用户通过统一 API 下载文件，必须校验当前账号拥有对应 conversation / task 权限。
3. **不泄露服务器路径**：前端、LLM prompt、audit 与 stdout 均不得暴露真实本地绝对路径。
4. **LLM 上下文安全**：输出文件内容默认不进入主代理 prompt；prompt 只看到文件名称、类型、大小、摘要和下载提示。
5. **可观测与可回归**：文件收集、拒绝、替换删除、下载、越权等行为都有可测试的契约和审计事件。
6. **可迁移存储**：v1 可先使用本地受控文件存储；未来迁移对象存储 / PostgreSQL metadata 时保持 API 与逻辑同构。

## 3. 非目标

- 不允许 Skill 自己开 HTTP 服务或自定义下载接口。
- 不允许用户下载 Skill 返回的任意本地路径。
- 不把 HTML / CSV / PDF 等文件全文注入 LLM prompt。
- 不在 v1 实现在线预览、协同编辑或文件版本管理；v1 只提供下载。
- 不把上传文件暂存机制与输出文件持久化混为一套；输入上传仍由 upload store 管理，输出文件由 artifact store 管理。
- 不支持脚本运行后继续访问已删除的临时工作目录。

## 4. 用户场景

### 4.1 RCBD 布局 HTML 下载

用户上传材料 CSV，并要求生成随机区组设计。

Skill 脚本完成 RCBD 设计后生成 `rcbd_layout.html`。主代理回答中说明“田间布局预览已生成”，前端统一附件卡片展示可下载文件。用户点击下载，浏览器通过统一下载 API 获取 HTML 文件。

### 4.2 田间记录 CSV 或 XLSX 下载

Skill 生成田间记录表。主代理不在聊天正文中展示完整大表，而是展示摘要：材料数、plot 数、区组数、seed，并提供 CSV 或 XLSX 下载入口。

若脚本同时生成多个格式，v1 不要求 Skill 自行取舍；平台应把多个合法输出文件打包成一个 `.zip`，并把该 zip 作为唯一可下载输出文件。

### 4.3 多文件产出的 v1 处理

一个 Skill 可能在临时目录内同时生成：

- `summary.json`
- `fieldbook.csv`
- `layout.html`

v1 不把它们分别登记为多个 downloadable artifacts。平台应逐个校验这些文件，并把多个合法文件打包成一个平台生成的 `.zip` artifact；前端仍只展示这一个 zip 下载入口。脚本仍可把关键指标汇总进 `answer` / `summary`。

### 4.4 越权访问拦截

用户 A 不能通过猜测 `artifact_id` 下载用户 B 的 Skill 输出文件。下载接口必须复用 task / conversation owner 校验。

## 5. 总体设计原则

1. **平台统一托管**：Skill 只负责在受控目录写文件并在 stdout JSON 中声明；文件收集、持久化、下载 URL、鉴权由平台负责。
2. **相对路径契约**：Skill 只能声明相对于受控输出目录的路径，例如 `outputs/rcbd_layout.html`；拒绝绝对路径、`..`、symlink 和目录逃逸。
3. **文件内容不进 prompt**：主代理 prompt 只注入可读摘要，不注入完整文件内容。
4. **下载口与文件系统解耦**：API 返回 `artifact_id` / `download_url`，不返回真实本地路径。
5. **替换式输出缓存**：v1 不设置应用层单文件大小上限；但同一输出缓存作用域内只保留 1 个当前 Skill 输出文件，新文件生成后必须删除旧文件并保存新文件；若单次 Skill 产出多个合法文件，平台打包为 1 个 zip 输出文件。
6. **失败不阻断主回答**：文件收集失败不应导致整个 `main_agent.respond` 失败；应在脚本结果中保留结构化 warning，并让主代理说明文件未能保存。

## 6. Skill 输出文件契约

### 6.1 运行目录

`SkillScriptRunner` 执行脚本时应创建受控输出目录：

```text
<temporary skill cwd>/outputs/
```

并向脚本提供安全环境变量：

```text
MAF_SKILL_OUTPUT_DIR=<temporary skill cwd>/outputs
```

脚本可任选以下方式写文件：

- 写入相对路径 `outputs/<filename>`；
- 读取 `MAF_SKILL_OUTPUT_DIR` 后写入该目录。

环境变量只指向临时输出目录，不包含 secret、真实部署路径或账号信息。

### 6.2 stdout JSON 声明

脚本 stdout 仍必须是 JSON object。若生成文件，应增加 `output_files`：

```json
{
  "answer": "RCBD 设计已完成，已生成布局预览文件。",
  "output_files": [
    {
      "path": "outputs/rcbd_layout.html",
      "filename": "rcbd_layout.html",
      "mime_type": "text/html",
      "label": "田间布局预览",
      "summary": "RCBD 田间布局 HTML，可下载后在浏览器打开。"
    }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `path` | 是 | 相对输出路径；必须位于受控 `outputs/` 目录下 |
| `filename` | 否 | 面向用户的下载文件名；缺省取 `path` basename |
| `mime_type` | 否 | 脚本声明的 MIME；平台仍需按扩展名 / 内容做保守校验 |
| `label` | 否 | 前端展示名称 |
| `summary` | 否 | 面向主代理和前端的短摘要，不得包含文件全文 |

### 6.3 Skill manifest 可选声明

v1 不强制 Skill 在 `SKILL.md` 中声明 `outputs.files`。默认安全基线是平台全局文件类型 allowlist；Skill manifest 的 `outputs.files` 是可选收紧项，用于比全局默认 allowlist 更严格地校验：

```yaml
outputs:
  required:
    - answer
  files:
    - name: layout_html
      required: false
      extensions: [.html]
      mime_types: [text/html]
      label: 田间布局预览
    - name: fieldbook_csv
      required: false
      extensions: [.csv]
      mime_types: [text/csv]
      label: 田间记录表
```

规则：

- 未声明 `outputs.files` 的 Skill：按平台全局 allowlist 校验输出文件。
- 已声明 `outputs.files` 的 Skill：先满足全局 allowlist，再满足 manifest 中声明的扩展名 / MIME 约束。
- manifest 只能收紧全局安全限制，不能放宽全局拒绝项；例如全局拒绝的源 `.zip` 不能通过 manifest 重新允许。
- manifest 可以列出多个“可能产物类型”；当单次执行实际生成多个合法输出文件时，平台应把它们打包为 1 个 zip artifact，运行时仍只保存 1 个当前输出文件。

## 7. 后端收集与存储流程

### 7.1 执行流程

1. `SkillScriptRunner` 创建临时 cwd 与 `outputs/`。
2. 脚本写入输出文件，并在 stdout JSON 的 `output_files` 中声明相对路径。
3. Runner 解析 stdout JSON 后，按 `output_files` 找到候选文件。
4. Runner / 主代理执行层校验候选文件：
   - 路径必须相对；
   - 必须位于受控输出目录；
   - 不允许 symlink；
   - 必须是普通文件；
   - 扩展名、MIME 合法；
   - 候选文件可为多个，但每个候选都必须独立通过路径、类型与文件安全校验。
5. 若合法输出文件只有 1 个，平台直接把该文件复制 / 写入 managed artifact store。
6. 若合法输出文件大于 1 个，平台生成一个 `.zip` 文件，把全部合法输出文件打包进该 zip，并把 zip 作为本次唯一 persisted output artifact；不为 zip 内每个文件分别生成 downloadable artifact。
7. 若部分候选文件不合法，平台记录 `skill.output_file_rejected`，但只要仍存在至少 1 个合法输出文件，主任务不应失败。
8. 平台先将本次合法输出文件写入 managed artifact store，并在旧 active 输出被隐藏前持久化本次新 artifact metadata。
9. 如存在旧文件，平台先将旧 artifact metadata 标记为 `superseded` / 不可下载，再删除旧文件正文；旧文件正文删除失败不得继续暴露旧下载入口，应记录内部诊断并保持新 artifact 可用。
10. 若新 artifact metadata 持久化或旧 metadata 替换失败，本次新输出不得暴露为 active artifact，应回滚 / 标记本次新 artifact 为不可下载并保留旧 active 输出。
11. 主代理 `script_results` 中只返回 managed artifact 摘要和 `download_url`，不返回临时路径。

### 7.2 输出缓存作用域

v1 输出文件缓存作用域按以下键确定：

```text
account_id + conversation_id
```

也就是说，同一用户在同一会话中当前只保留 1 个可下载的 Skill 输出文件。后续如果需要多个 Skill 并存，可扩展为：

```text
account_id + conversation_id + skill_name
```

但 v1 不做该扩展，避免前端临时文件区和后端 artifact 生命周期过早复杂化。

替换规则：

- 新 Skill 输出文件保存成功后，旧 active 输出文件必须被删除；
- 旧 artifact metadata 可保留为审计 tombstone，但不得继续出现在普通“当前可下载文件”列表中；
- 若用户访问旧 `artifact_id` 的下载地址，对外统一返回 `404 Not Found`，避免暴露历史文件曾存在；内部 audit 应记录 `artifact.download_gone`，必要时同时保留 `superseded_by_artifact_id` 供审计追踪；
- 删除旧文件失败时，新文件不得静默暴露两个 active 输出；应记录 `skill.output_file_rejected` 或 `artifact.evict_failed`，并按实现选择“保留旧文件、拒绝新文件”或“新文件保存后立即修复索引”，但必须有测试覆盖。

### 7.3 建议内部结果模型

当前 `SkillScriptRunner.run()` 返回 `dict`。为避免把文件收集逻辑硬塞进 stdout JSON，后续实现可引入内部模型：

```python
SkillScriptRunResult(
    output: dict[str, Any],
    output_files: tuple[CollectedSkillOutputFile, ...],
    diagnostics: tuple[str, ...],
)
```

其中 `CollectedSkillOutputFile` 至少包含：

- `source_path`：临时目录内路径，仅内部使用；
- `relative_path`
- `filename`
- `mime_type`
- `size_bytes`
- `label`
- `summary`
- `sha256`

对外仍只暴露 managed artifact metadata。

当合法输出文件超过 1 个时，`CollectedSkillOutputFile` 表示被打包前的源文件集合；最终对外 artifact metadata 表示平台生成的 zip 文件。

### 7.4 Artifact metadata

建议文件型 artifact 在存储层记录：

| 字段 | 说明 |
|---|---|
| `artifact_id` | 平台生成，不由 Skill 提供 |
| `task_id` | 所属 task |
| `conversation_id` | 可冗余记录，便于鉴权和清理 |
| `producer_node_id` | 产出节点 |
| `artifact_type` | `file` |
| `storage_key` / `storage_ref` | 内部 opaque key，不是绝对路径 |
| `filename` | 下载文件名 |
| `mime_type` | 下载 Content-Type |
| `size_bytes` | 文件大小 |
| `sha256` | 内容 hash，用于完整性校验和审计 |
| `summary` | 短摘要 |
| `source_kind` | `skill_output` |
| `skill_name` | 产出 Skill |
| `entrypoint` | 产出脚本 |
| `source_file_count` | 原始合法输出文件数量；单文件为 `1`，平台打包 zip 时大于 `1` |
| `archive_format` | 非打包产物为空；平台打包产物为 `zip` |
| `retention_status` | `active` / `superseded` / `deleted` |
| `superseded_by_artifact_id` | 被新输出顶替时记录新 artifact，可为空 |
| `created_at` | 创建时间 |
| `is_complete` | 是否完整 |

若短期不扩 SQLite schema，可将部分 metadata 放入受控 JSON summary；但长期实现应补显式字段，避免前端和下载接口解析自然语言 summary。

### 7.5 Artifact store

v1 推荐新增本地 managed artifact store：

```text
<runtime data dir>/artifacts/<account-or-conversation-shard>/<artifact_id>/<filename>
```

要求：

- 存储路径只由平台生成；
- 不使用 Skill 提供的目录名作为真实路径；
- 文件名只作为下载展示名，需 sanitize；
- 数据库只保存 opaque storage key；
- 同一输出缓存作用域内，新输出文件保存时应删除旧输出文件；
- 删除 conversation 时应级联删除当前输出文件和 tombstone metadata；
- 未来可替换为对象存储，API 不变。

### 7.6 多文件 zip 打包规则

当单次 Skill 执行产生多个合法源输出文件时，平台负责生成 zip：

- 使用 Python 标准库 `zipfile`，不新增第三方依赖；
- zip 文件由平台生成并写入 managed artifact store，Skill 不提供 zip 文件本体；
- zip 下载文件名由平台生成，例如 `<skill_name>_outputs.zip` 或 `<task_id>_outputs.zip`，并进行文件名 sanitize；
- zip entry name 来源于每个源文件相对于 `outputs/` 的路径；
- zip entry name 必须统一为 POSIX `/` 分隔，不允许空路径、绝对路径、`.`、`..`、Windows drive path 或 UNC path；
- zip entry name 归一化后如发生重复，应拒绝冲突文件并记录 `duplicate_archive_entry`，不得在 zip 内静默覆盖；
- zip 内只包含通过校验的普通文件，不包含目录、symlink、hardlink 或被拒绝的文件；
- zip metadata 对外记录 `source_file_count`、`archive_format=zip`，并在 `summary` 中说明包含的文件数量和主要文件名。

## 8. API 契约

### 8.1 Artifact 列表

现有：

```http
GET /api/v1/tasks/{task_id}/artifacts
```

应扩展文件型 artifact 响应字段：

```json
{
  "artifact_id": "art_...",
  "producer_node_id": "main_agent.respond",
  "artifact_type": "file",
  "filename": "rcbd_layout.html",
  "mime_type": "text/html",
  "size_bytes": 18342,
  "summary": "RCBD 田间布局 HTML",
  "download_url": "/api/v1/artifacts/art_.../download",
  "source_file_count": 1,
  "archive_format": null,
  "retention_status": "active",
  "is_complete": true,
  "created_at": "2026-05-08T..."
}
```

兼容要求：

- 不向前端返回真实 `storage_ref` / 本地路径；
- 普通任务 artifact 列表默认只返回 `active` 的当前输出文件；`superseded` / `deleted` tombstone 仅用于审计或调试接口；
- 多文件输出时仍只返回 1 个 artifact，`mime_type=application/zip`、`archive_format=zip`、`source_file_count>1`；
- 若历史 `storage_ref` 字段仍保留，应对文件型 artifact 返回 opaque key 或空值，并引导前端使用 `download_url`。

### 8.2 下载接口

新增统一下载接口：

```http
GET /api/v1/artifacts/{artifact_id}/download
```

行为：

- 必须登录；
- 根据 `artifact_id` 找到 task / conversation / account；
- 复用 `require_task_owner` 或等价 owner 校验；
- 返回文件流；
- 设置：
  - `Content-Type`
  - `Content-Length`
  - `Content-Disposition: attachment; filename="..."`
  - `X-Content-Type-Options: nosniff`

HTML 文件 v1 固定按 attachment 下载，不直接 inline 预览，也不提供站内 sandbox 预览入口。若未来做在线预览，应另开 v2 PRD，并使用 sandbox iframe / CSP / 预览域隔离单独设计。

### 8.3 事件与审计

新增 audit-only 事件：

- `skill.output_file_collected`
- `skill.output_file_rejected`
- `artifact.downloaded`
- `artifact.download_denied`

事件 payload 允许包含：

- `artifact_id`
- `skill_name`
- `entrypoint`
- `filename`
- `mime_type`
- `size_bytes`
- `sha256`
- `reason`

禁止包含：

- 文件正文；
- 临时目录路径；
- managed store 真实绝对路径；
- 上传文件原文；
- provider prompt。

## 9. 安全边界

### 9.1 路径安全

必须拒绝：

- 绝对路径；
- `..`；
- symlink；
- hardlink 到输出目录外文件；
- 目录；
- 空文件名或控制字符文件名；
- Windows drive path / UNC path。

### 9.2 类型、数量与留存限制

v1 默认限制：

- 不设置应用层单文件大小上限；
- 单次脚本执行可声明多个源输出文件，但每个源文件都必须独立通过路径、类型与文件安全校验；
- 平台最终只持久化 1 个 downloadable artifact：合法源文件为 1 个时保存原文件，合法源文件大于 1 个时由平台生成 1 个 zip artifact；
- 同一 `account_id + conversation_id` 输出缓存作用域最多保留 1 个 active 输出文件；
- 新输出文件顶替旧输出文件，旧文件正文必须删除。

不设置单文件大小上限并不意味着文件正文可以进入 prompt；无论文件多小，输出文件内容默认都不得注入主代理 prompt。

仍需限制允许的文件类型。默认允许扩展名：

- `.txt`
- `.md`
- `.json`
- `.csv`
- `.tsv`
- `.html`
- `.pdf`
- `.xlsx`
- `.png`
- `.jpg`
- `.jpeg`

默认拒绝：

- 可执行脚本：`.sh`、`.bash`、`.py`、`.js`、`.exe`、`.bat`、`.cmd`；
- Skill 直接声明的源压缩包：`.zip`、`.tar`、`.gz`，除非后续有专门安全审计；
- 未知二进制文件。

平台为多个合法源输出文件自动生成的 `.zip` 不属于“Skill 直接声明的源压缩包”，v1 允许作为唯一 persisted output artifact，MIME 使用 `application/zip`。

文件类型 allowlist 后续可通过已 bootstrap 的 runtime 环境变量或显式 runtime config 覆盖，业务节点执行阶段不得重复读取 `config.yaml`。

Skill manifest 的 `outputs.files` 不替代全局 allowlist：未声明时使用全局 allowlist；声明时只能在全局 allowlist 之上收紧。

### 9.3 HTML 安全

HTML 文件 v1 只作为下载附件，不作为站内可信页面直接渲染。

v1 明确不支持站内 sandbox 预览，因此不新增 preview API、不在前端渲染 HTML、不把 HTML 内容注入 React DOM 或 iframe。

若未来支持预览，必须另行设计：

- 使用 sandbox iframe；
- 禁止同源脚本访问主应用；
- 设置 CSP；
- 不允许 HTML 访问用户 cookie / localStorage；
- 预览域名与主应用域名隔离。

### 9.4 LLM 与 prompt 边界

主代理 prompt 只能看到：

```json
{
  "filename": "rcbd_layout.html",
  "mime_type": "text/html",
  "size_bytes": 18342,
  "summary": "RCBD 田间布局 HTML，可下载。"
}
```

不得注入：

- HTML / CSV / PDF 文件全文；
- 本地路径；
- artifact store key；
- signed URL；
- 下载鉴权 token。

## 10. 前端展示口径

前端 v1 可在任务完成后通过 artifacts API 展示当前可下载输出文件。

建议 UI：

- 在主代理回答下方展示统一“生成文件”附件卡片；
- 当前文件显示：
  - label / filename；
  - 类型；
  - 下载按钮；
  - 简短 summary。
- 下载按钮直接请求 `download_url`。
- 若当前文件是平台生成的 zip，应显示为一个下载项，并用 summary 说明包含多个源输出文件。
- v1 同一会话只展示当前最新的一个 Skill 输出文件；新输出生成后，旧输出应从当前附件区域移除或标记为“已被新文件替换”。
- v1 不做 Skill 类型定制结果卡片；Skill 只能通过 `label` / `summary` 影响统一附件卡片的展示文案，不能要求前端按 Skill 类型渲染专属 UI。未来如需 RCBD 专属预览或业务卡片，应另开前端 PRD。

若 `skill.output_file_rejected` 发生，前端不必默认展示错误细节；主代理回答或调试信息可说明“文件数量超限 / 类型不支持 / 旧文件替换失败，未保存为下载文件”。

## 11. 与现有能力的关系

### 11.1 与上传文件

上传文件是用户输入，当前生命周期由 conversation file resource / upload store 兼容层管理；Skill 输出文件是系统产物，生命周期由 artifact store 管理。两者都必须按 account / conversation / task 隔离，但不能混用存储和 API。

本 PRD 的“同一缓存作用域只保留 1 个当前文件”仅约束 Skill 输出文件。输入侧文件机制以 `docs/prd/backend/20-对话文件本地资源文件系统PRD.md` 为准：上传文件保存为 conversation-scoped 本地资源，Skill 运行时通过 workspace manifest / mount path 读取真实文件，旧 `uploaded_artifacts` 字段只作为兼容层。

### 11.2 与 conversation memory

Conversation memory 只可保存输出文件的脱敏 metadata，例如文件名、类型、摘要和 artifact_id；不得保存完整文件内容。

后续用户说“继续看刚才那个布局文件”时，系统可基于 memory 识别曾生成过文件，但下载和读取仍必须通过 artifact owner 校验。

### 11.3 与 `main_agent.respond`

主代理负责：

- 执行 Skill；
- 接收脚本结构化输出；
- 将文件 artifact 摘要加入回答上下文；
- 告知用户可下载文件。

主代理不负责读取文件全文或直接拼接下载链接 token。

## 12. 验收标准

### 12.1 后端行为

- Skill 写入 `outputs/rcbd_layout.html` 并在 stdout `output_files` 声明后，平台生成 file artifact。
- Skill 声明多个合法 `output_files` 时，平台生成 1 个 zip artifact，zip 内包含全部合法源输出文件。
- 临时输出目录删除后，用户仍可通过 download API 下载文件。
- stdout 中声明绝对路径、`..`、symlink 或不存在文件时，平台拒绝收集，并记录 audit 事件。
- stdout 中部分 `output_files` 不合法时，平台拒绝不合法文件并记录诊断；只要仍存在合法输出文件，主任务不失败。
- Skill 直接声明 `.zip` / `.tar` / `.gz` 作为源输出文件时，v1 默认拒绝；平台为多个合法源文件生成的 zip 允许下载。
- 新输出文件保存成功后，同一 `account_id + conversation_id` 下旧 active 输出文件被删除并不可继续下载。
- 不允许扩展名 / MIME 时，平台拒绝收集对应文件，不导致主任务失败。
- 下载接口必须校验登录态和 task owner；跨账号下载失败。
- API 响应不暴露服务器绝对路径。
- 主代理 prompt / audit 不包含文件正文。

### 12.2 前端行为

- 任务完成后可展示当前生成文件。
- 前端使用统一附件卡片展示文件；不同 Skill 不触发专属文件 UI。
- 用户点击下载按钮可下载文件。
- 无文件时不显示空文件区域。
- 下载失败时展示可理解错误，例如“无权访问或文件不存在”。

### 12.3 兼容性

- 未声明 `output_files` 的现有 Skill 行为不变。
- 当前 stdout JSON 输出契约继续有效。
- 现有 text / json artifacts 列表接口不被破坏。
- 本地 `mini-breedstat-rcbd` 可迁移为输出 HTML file artifact，而不是 inline 大 HTML。

## 13. 测试计划

### 13.1 单元测试

- `SkillScriptRunner` 创建输出目录并传入 `MAF_SKILL_OUTPUT_DIR`。
- 收集合法相对路径文件。
- 拒绝绝对路径。
- 拒绝 `..`。
- 拒绝 symlink。
- 拒绝目录。
- 多个合法 `output_files` 时生成 1 个 zip artifact，并校验 zip entry name 不包含绝对路径、`..` 或重复覆盖。
- 多个 `output_files` 中存在非法文件时，拒绝非法文件并只打包合法文件。
- Skill 直接声明的源 `.zip` 默认被拒绝，平台自动生成的 zip 允许作为 managed artifact。
- 同一会话已有 active 输出文件时，新文件保存会顶替旧文件。
- 旧文件被顶替后，旧下载地址对外统一返回 `404`，内部 audit 记录 `artifact.download_gone`。
- 不设置应用层单文件大小限制，但仍验证文件不进入 prompt。
- MIME / 扩展名不匹配时按保守策略拒绝或降级。

### 13.2 集成测试

- `main_agent.respond` 执行 Skill 后保存 file artifact。
- 多文件输出时 `script_results` / artifacts API 只暴露 1 个 zip artifact 摘要。
- `script_results` 中返回 managed artifact 摘要，不返回临时路径。
- 主代理 prompt 中只出现文件摘要，不出现文件正文。
- Skill 输出文件收集失败时，任务仍完成并记录诊断。
- 新输出文件生成时清理同一输出缓存作用域下的旧文件。
- 删除 conversation 时清理 artifact metadata 与文件。

### 13.3 API 测试

- `GET /api/v1/tasks/{task_id}/artifacts` 返回下载 metadata。
- `GET /api/v1/artifacts/{artifact_id}/download` 可下载当前用户文件。
- 未登录下载失败。
- 跨账号下载失败。
- 不存在 artifact 下载失败。
- HTML 下载响应使用 attachment 与 `nosniff`。

### 13.4 E2E / 手工验证

- RCBD Skill 生成 HTML 布局文件，前端可下载。
- RCBD Skill 同时生成 HTML 与 CSV 时，前端下载 1 个平台生成的 zip。
- 下载后的 HTML 可本地打开查看布局。
- 大 HTML 不进入主代理回答正文。

建议回归命令：

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
cd frontend && npm test -- --run
```

## 14. 实施阶段建议

### Phase A：后端最小闭环

- Runner 创建输出目录；
- 解析 stdout `output_files`；
- 本地 artifact store；
- SQLite artifact metadata 扩展；
- 下载 API；
- 主代理脚本结果返回 downloadable artifact 摘要；
- 后端测试。

### Phase B：前端展示

- 任务 artifact 列表组件；
- 下载按钮；
- 错误态；
- 数据查询 Skill / Skill 结果卡片统一复用文件附件卡片，不为 Skill 输出文件引入专属 UI。

### Phase C：RCBD Skill 迁移

- `mini-breedstat-rcbd` 不再 inline 大 HTML；
- 将 layout HTML 声明为 `output_files`；
- 可选增加 fieldbook CSV 输出；
- 补充真实 Skill 兼容测试。

### Phase D：存储演进

- artifact store 抽象支持对象存储；
- retention / 清理策略；
- 下载审计报表。

## 15. 待补充问题

当前 v1 产品边界已确认，暂无阻塞本 PRD 实施的待补充问题。
