# Conversation 文件资源重构设计

日期：2026-06-16

## 背景

当前上传链路已经支持前端把文件上传到对话，并通过 `metadata.upload_ids` 传给后端。后端现状是：上传内容先进入 `InMemoryUploadStore`，提交消息时解析成 `uploaded_artifacts` 给主代理看摘要、`skill_artifacts` 给 Skill 脚本看原始 `content` 或 `content_base64`。为了保证 interrupt/resume，系统已有 `task_input_attachment` ledger，会把任务使用过的上传内容保存到任务附件里。

新的目标不是 RAG，而是把对话文件作为 Skill 脚本可操作的真实文件资源。Skill 应该能在运行时拿到受控工作区里的文件路径和资源 manifest，通过脚本读取文件本体来处理数据并生成结果。

## 约束

- 不破坏现有前端 API。
  - 保留 `POST /api/v1/conversations/uploads`。
  - 保留 `GET /api/v1/conversations/{conversation_id}/uploads`。
  - 保留删除上传接口。
  - 保留 `metadata.upload_ids` 提交方式。
  - 响应可以增加可忽略字段，但现有字段语义不变。
- 本地文件保存不按 hash 分层。
- 图片文件不自动生成 LLM 描述，也不在上传阶段自动 OCR。
- PDF 可以生成描述，可复用 OCR 或文本抽取能力。
- 现有 Skill 不能被一次性打断；旧 `content` / `content_base64` 输入必须兼容。
- 新版设计必须同步更新 `breeding-skill-builder`，让后续新建/迁移 Skill 遵守文件资源契约。

## 目标架构

```text
前端现有上传 API
  -> 后端持久 conversation 文件资源
  -> conversation 文件索引
  -> Skill run workspace
  -> resource_manifest.json / resource_index.md
  -> Skill 脚本读取真实文件
  -> 输出文件继续进入现有 artifact 管理
```

当前的临时 upload 语义升级为 conversation-scoped file resource。`upload_id` 对前端仍是上传 ID，对后端则同时作为对话文件资源 ID。

## 文件存储布局

本地文件保存采用 conversation 直观目录，不按 hash 分层：

```text
runtime/conversation_files/
  <conversation_id>/
    index.md
    <upload_id>/
      original
      description.json      # 非图片文件可有
      extracted.txt         # 可选
      ocr.md                # PDF 可选
```

说明：

- `original` 保存原始 bytes。
- 原始文件名只进入 DB / index，不参与真实文件名，避免路径注入和重名冲突。
- `sha256` 仍计算并记录，用于完整性校验，不用于目录分层。
- 删除整个 conversation 时可以清理对应目录。

## 数据模型

新增或演进 conversation 文件资源记录，逻辑字段如下：

```text
conversation_file_resource
- file_id / upload_id
- conversation_id
- username
- original_filename
- content_type
- file_type
- size_bytes
- sha256
- storage_key              # 例如 <conversation_id>/<upload_id>/original
- preview
- description
- description_status       # not_required | pending | ready | failed
- status                   # uploaded | ready | deleted 等
- created_at
- updated_at
```

DB 是结构化事实源，文件系统保存原始文件和可选缓存。真实路径不进前端响应，也不直接暴露给主代理 prompt。

## Conversation `index.md`

每个 conversation 目录维护一个人类可读、模型可读的物化索引：

```md
# Conversation Files Index

conversation_id: conv-123
updated_at: 2026-06-16T...

## upl-abc123 — sales.xlsx

- 原始文件名: sales.xlsx
- 类型: spreadsheet
- MIME: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
- 大小: 12345 bytes
- SHA256: ...
- 相对路径: upl-abc123/original
- 状态: ready
- 描述状态: ready

### 文件描述

这是一份销售数据表，包含日期、客户、产品、金额等字段。

### 结构

- Sheets: Sheet1
- Columns: date, customer, product, amount
- Rows: 1200
```

定位：

- DB 仍是事实源，负责权限、状态、查询、并发更新。
- `index.md` 由后端从 DB / description 生成。
- Skill 运行时复制为 workspace 内的 `resource_index.md`，不暴露真实持久目录。
- 不把 `index.md` 作为唯一真源。

图片文件在 `index.md` 中仍登记，但描述固定为：

```md
图片文件不自动生成描述。如需识别图片文字，请调用 OCR Skill。
```

## 文件描述策略

| 文件类型 | 策略 |
| --- | --- |
| CSV / Excel | 基于 sheet、表头、行列数、前 N 行抽样生成结构化描述 |
| JSON | 基于顶层结构、key、样例对象生成描述 |
| TXT | 基于前 N tokens 生成描述 |
| VCF / VCF.GZ | 基于 header、样本数量、variant 样例生成描述 |
| PDF | 可通过 OCR 或文本抽取后生成描述 |
| 图片 PNG/JPG/JPEG | 不生成描述，`description_status = not_required` |

描述失败不阻塞文件使用。文件仍可进入 Skill 工作区，`description_status` 标记为 `failed`，`index.md` 记录失败状态。

## OCR Skill 关系

现有 `ocr` Skill 继续作为用户显式文档识别能力：

- 图片上传阶段不自动 OCR。
- 用户需要识别图片文字时，由对话流程调用 OCR Skill。
- PDF 描述生成可以复用 OCR 或文本抽取能力，但失败不阻塞 PDF 作为文件资源使用。
- OCR 输出仍作为 Skill 结果 artifact，不作为图片上传文件的默认 description。

## Skill 运行时工作区

每次 Skill 脚本执行创建临时工作区：

```text
skill-run-xxxx/
  input/
    upl-abc123__sales.xlsx
    upl-def456__config.json
  outputs/
  resource_manifest.json
  resource_index.md
```

平台在执行前：

1. 根据本次任务的 `upload_ids` 或 task input ledger 找到 conversation 文件资源。
2. 复制文件到 `input/`。
3. 写 `resource_manifest.json`。
4. 复制 conversation `index.md` 为 `resource_index.md`。
5. 注入环境变量和 payload 字段。

推荐第一阶段使用复制，不用软链接，避免脚本修改 input 影响原始文件，并兼容未来 sandbox。

## Resource manifest

示例：

```json
{
  "version": 1,
  "conversation_id": "conv-1",
  "task_id": "task-1",
  "node_id": "node-1",
  "input_dir": "/tmp/skill-run/input",
  "output_dir": "/tmp/skill-run/outputs",
  "conversation_index_path": "/tmp/skill-run/resource_index.md",
  "files": [
    {
      "upload_id": "upl-abc123",
      "filename": "sales.xlsx",
      "mount_path": "/tmp/skill-run/input/upl-abc123__sales.xlsx",
      "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "file_type": "spreadsheet",
      "size_bytes": 12345,
      "sha256": "...",
      "preview": {},
      "description": {},
      "relative_source_path": "upl-abc123/original"
    }
  ]
}
```

真实持久路径不进入 manifest；`mount_path` 只指向临时 workspace。

## Skill payload 双轨兼容

平台新增字段：

```json
{
  "resource_manifest_path": "/tmp/skill-run/resource_manifest.json",
  "conversation_index_path": "/tmp/skill-run/resource_index.md",
  "input_dir": "/tmp/skill-run/input"
}
```

同时保留旧兼容字段：

```json
{
  "uploaded_artifacts": [
    {
      "upload_id": "upl-1",
      "filename": "materials.csv",
      "mount_path": "/tmp/skill-run/input/upl-1__materials.csv",
      "content": "ped_id,..."
    }
  ]
}
```

不改旧 Skill 时，它们不会自动读取 manifest 或 `mount_path`，仍会读 `content` / `content_base64`。平台负责从持久文件资源或 workspace 文件临时构造这些兼容字段，保证旧 Skill 不依赖内存 upload store。

兼容字段策略：

| 文件类型 | 旧兼容字段 |
| --- | --- |
| CSV / JSON / TXT / 单 sheet Excel 已选 sheet | `content` |
| PDF / 图片 / VCF / VCF.GZ / 其他二进制 | `content_base64` |
| 多 sheet Excel 未选 sheet | 不填 `content`，继续触发 sheet selection |

长期推荐新 Skill 优先读取 `resource_manifest_path` 和 `files[].mount_path`。

## `task_input_attachment` 职责调整

现有 `task_input_attachment` 保留，但职责从“保存一份任务上传内容”调整为“记录某个 task 使用了哪些 conversation file”。

目标形态：

- ledger 引用 `file_id` / `storage_key` / 资源元数据。
- resume 时从持久 conversation 文件资源重新构造 manifest 和兼容字段。
- 不再需要长期在 `source_payload` 或 `skill_artifact` 中保存整份 base64。

迁移期间可以保留旧字段，逐步减少对内联 base64 的依赖。

## 删除语义

第一阶段建议：

- 用户删除上传：从 conversation 文件列表移除或标记 `deleted`。
- 已经被任务使用过的文件不影响已完成任务记录。
- 物理文件优先软删除或延迟清理，避免正在运行的 Skill 被删除打断。
- 删除 conversation 时清理对应 conversation 文件目录。

## 分阶段迁移

### Phase 1：持久文件资源层

- 新增后端 conversation file store / index 服务。
- 上传后落盘。
- 写 DB 记录。
- 更新 `index.md`。
- 保留现有 upload API response shape。
- 保留 `content` / `content_base64` 兼容字段。

### Phase 2：Skill workspace manifest

- 改造 Skill runner / execution service 创建 `input/`。
- 复制本次使用文件到 workspace。
- 生成 `resource_manifest.json` 和 `resource_index.md`。
- payload 新增 `resource_manifest_path`、`conversation_index_path`、`input_dir`。
- `uploaded_artifacts[]` 新增 `mount_path`。

### Phase 3：description 后台化

- 上传时同步生成基础 preview。
- 非图片文件进入描述任务。
- PDF 可接 OCR / 文本抽取。
- 图片标记 `description_status = not_required`。
- 失败不阻塞文件使用。

### Phase 4：Skill 逐步迁移

- 更新 Skill 构建指南和 `breeding-skill-builder`。
- 新 Skill 默认使用 manifest / mount_path。
- 选 1-2 个典型 Skill 作为迁移示例。
- 后续再考虑大文件停止内联 base64。

## `breeding-skill-builder` 更新要求

新版文件资源机制落地时，必须同步修改现有：

```text
.codex/skills/breeding-skill-builder/
```

要求：

1. 更新 `SKILL.md`
   - 增加新版 Skill 文件输入原则。
   - 明确新 Skill 优先读取 `resource_manifest_path`。
   - 明确新 Skill 优先使用 manifest 中的 `files[].mount_path`。
   - 明确 `uploaded_artifacts[].content` / `content_base64` 是兼容层，不是新主接口。

2. 更新 templates
   - Python 脚本模板示范读取：
     ```python
     manifest_path = payload.get("resource_manifest_path")
     ```
   - 从 manifest 读取 `files[].mount_path` 并打开真实文件。

3. 更新 checklist
   - 检查 Skill 是否支持 manifest / mount_path。
   - 检查是否避免让 LLM 生成本地路径。
   - 检查是否不暴露真实持久存储路径。
   - 检查 artifact/file 字段是否来自受控 upload / artifact source。

4. 更新 migration guide
   - 旧 Skill 可继续读 `content` / `content_base64`。
   - 新版优先 manifest。
   - 大文件和二进制处理应迁移到真实文件路径。

5. 同步 `Skill构建指南.md`
   - 若指南仍强调 `uploaded_artifacts[].content`，改为新版约定。

验收：

- 新建 Skill 模板默认使用 manifest / mount_path。
- 旧 Skill 迁移说明清楚。
- 文档不再鼓励把文件内容内联进 payload 作为主方案。
- `breeding-skill-builder` 的 quick validate 通过。

## 测试策略

### 上传 API

- 现有上传测试继续通过。
- 上传后本地文件存在。
- DB 记录包含 storage key / file resource 元数据。
- `index.md` 创建并包含 upload_id、文件名、相对路径。
- 图片文件 `description_status = not_required`。
- PDF 文件 `description_status` 可为 `pending` / `ready` / `failed`，但文件可用。

### Conversation 文件列表

- `GET /uploads` 旧字段不变。
- 新字段可选且可被旧前端忽略。
- 删除后列表和状态符合设计。

### Skill 执行

- 新测试 Skill 能读取 `resource_manifest_path`。
- 新测试 Skill 能从 `uploaded_artifacts[].mount_path` 打开文件。
- 旧测试 Skill 不改代码仍能读取 `content` / `content_base64`。
- Skill 修改 workspace input 文件不影响原始文件。

### Interrupt / resume

- 初始消息上传文件后进入 interrupt。
- 用户补参数恢复后，旧 Skill 和新 Skill 都能拿到文件。
- 不依赖内存 upload store TTL。

### 安全

- API 不返回真实持久路径。
- `mount_path` 只出现在 Skill runtime 内部 payload。
- path traversal 文件名被拒绝或安全化。
- 删除 conversation 后清理对应目录。

### 文档与 builder

- `Skill构建指南.md` 包含新版文件资源契约。
- `breeding-skill-builder` 模板、checklist、migration 指南同步更新。
- 相关 skill quick validate 通过。

## 验收标准

1. 前端不改也能上传、提交、删除文件。
2. 上传文件真实落盘到 `runtime/conversation_files/<conversation_id>/<upload_id>/original`。
3. 每个 conversation 有 `index.md`。
4. Skill 脚本可以通过 manifest / mount_path 操作真实文件。
5. 旧 Skill 不改代码仍可运行。
6. 旧兼容字段由持久文件资源构造，不依赖内存 TTL。
7. 图片不生成描述。
8. PDF 可以生成描述。
9. 上传文件跨 interrupt / resume 可用。
10. `breeding-skill-builder` 和 `Skill构建指南.md` 已按新版约定更新。
