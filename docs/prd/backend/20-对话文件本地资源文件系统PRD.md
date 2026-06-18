# 对话文件本地资源文件系统 PRD

- **状态**：MVP 已落地，本文记录 2026-06-16 的实现口径与后续约束
- **范围**：前端对话上传文件进入后端后的本地保存、索引、删除、Skill 运行时挂载
- **非目标**：不是 RAG；不做向量索引；不把文件内容切片进检索库

## 1. 背景与目标

用户在对话中上传的文件，需要成为该对话可复用的本地文件资源。Skill 执行时应拿到真实文件副本，由脚本读取并处理数据，再产出结果或 artifact。

本次改造保留现有前端 API 与 `metadata.upload_ids` 语义，只把后端的临时上传语义升级为 conversation-scoped file resource：

```text
上传 API
  -> 本地 conversation 文件目录
  -> DB 记录权限、状态、元数据
  -> index.md 物化索引
  -> Skill workspace/input 文件副本
  -> resource_manifest.json / resource_index.md
```

## 2. 核心设计原则

1. **文件本体在本地文件系统**：原始 bytes 保存为普通文件，Skill 脚本处理真实文件，不依赖 RAG。
2. **DB 是事实源，index.md 是投影**：DB 负责权限、状态、分页、并发更新；`index.md` 便于人和模型理解，可由 DB 重建。
3. **文件挂靠 conversation**：每个文件属于上传它的 `conversation_id` 与 `username`。
4. **前端 API 兼容**：上传、列表、删除和提交消息的旧字段不删；只新增可忽略字段。
5. **旧 Skill 兼容，新 Skill 走 manifest**：旧 Skill 可继续读 `content` / `content_base64`；新版 Skill 应优先读 `resource_manifest_path` 中的 `files[].mount_path`。
6. **图片不自动描述**：图片只登记元数据；需要识别文字时由用户显式调用 OCR Skill。

## 3. 本地存储布局

默认路径跟随 runtime 数据库目录：

```text
<runtime-db-parent>/conversation_files/
  <conversation_id>/
    index.md
    <upload_id>/
      original
      description.json   # 非图片文件可有
```

实现要点：

- 不按 hash 分层。
- `conversation_id` 和 `upload_id` 写入路径前会做安全编码，禁止空段、`.`、`..`、绝对路径和路径穿越。
- 原始文件名不参与持久路径，只保存在 DB / API / index 中。
- `storage_key` 形如 `<safe_conversation_id>/<safe_upload_id>/original`，是后端内部 key，不暴露真实绝对路径。
- 单个文件删除时会物理删除 `<upload_id>/` 资源目录。
- 删除整个 conversation 时会物理删除 `<conversation_id>/` 文件目录。

## 4. 数据模型

新增 conversation 文件资源记录：

```text
conversation_file_resource
- file_id                 # 当前等同 upload_id
- conversation_id
- username
- original_filename
- content_type
- file_type
- size_bytes
- sha256
- storage_key
- preview                 # 表头、行数、sheet 等上传预览
- description_status      # ready | not_required | failed | pending
- description_summary
- description_ref
- status                  # active | deleted
- normalized_filename
- normalized_content_type
- requires_sheet_selection
- selected_sheet
- created_at
- updated_at
```

为什么仍需要 DB：

- 校验文件是否属于当前用户和当前对话。
- 支持 `GET /uploads` 默认只列 active 文件，未来可分页管理大量文件。
- 标记删除状态，避免已删除文件再次作为 `upload_id` 被绑定到任务。
- 让 `index.md` 可重建，而不是把 Markdown 当唯一事实源。

## 5. API 兼容口径

### 上传

```http
POST /api/v1/conversations/uploads
form: conversation_id, file
```

行为：

- 校验 conversation 归属。
- 复用现有上传类型和大小限制。
- 保存原始文件到本地 conversation 文件目录。
- 写入 `conversation_file_resource`。
- 重写该 conversation 的 `index.md`。
- 返回旧字段，并新增可忽略字段：
  - `status`
  - `description_status`

### 列表

```http
GET /api/v1/conversations/{conversation_id}/uploads?limit=&cursor=&include_deleted=
```

行为：

- 默认只返回 `active` 文件。
- `limit` / `cursor` 为数量不限场景预留分页能力。
- `include_deleted=true` 主要用于调试/管理，不是普通前端默认路径。

### 删除单个文件

```http
DELETE /api/v1/conversations/uploads
body: { conversation_id, upload_id }
```

行为：

- DB 标记 `status = deleted`。
- 物理删除本地 `<conversation_id>/<upload_id>/` 目录。
- 重写 `index.md`，索引中保留删除记录，路径显示为“文件本体已物理删除”。
- 已复制到运行中 Skill workspace 的文件副本不受影响。

## 6. index.md 物化索引

每个 conversation 目录维护一个 `index.md`：

```md
# Conversation Files Index

conversation_id: conv-123
updated_at: 2026-06-16T...

## upl-abc — data.xlsx

- 原始文件名: data.xlsx
- 类型: spreadsheet
- MIME: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
- 大小: 12345 bytes
- SHA256: ...
- 相对路径: upl-abc/original
- 状态: active
- 描述状态: ready

### 文件描述

这是一份结构化数据文件，行数约为 120，列包括: ...
```

生成规则：

- 从 DB 中读取当前 conversation 的资源记录生成。
- atomic replace 写入，避免半截文件。
- active 文件显示相对路径；deleted 文件显示“文件本体已物理删除”。
- 图片描述固定为“图片文件不自动生成描述。如需识别图片文字，请调用 OCR Skill。”

## 7. 文件描述策略

当前 MVP 使用同步基础描述，不阻塞文件作为 Skill 输入：

| 类型 | 当前描述策略 |
| --- | --- |
| CSV / JSON / Excel | 使用 preview 的行数、列名、sheet 信息生成结构描述 |
| TXT | 使用行数、字符数和开头片段生成摘要 |
| PDF | 记录基础 PDF 描述；后续可接文本抽取/OCR adapter |
| VCF / VCF.GZ | 说明这是变异数据文件，脚本可读取原始文件解析 |
| 图片 | `description_status = not_required`，不自动 OCR |

后续如果接入 LLM 文件总结，应保持：

- 失败不阻塞上传成功。
- 太长文件只抽取开头/结构摘要。
- 不把图片上传阶段变成自动 OCR。
- PDF 可优先复用受控文本抽取或 OCR adapter。

## 8. Skill 运行时工作区

Skill 执行前，平台会把本次任务绑定的 conversation 文件复制到临时 workspace：

```text
skill-run-xxxx/
  input/
    upl-abc__data.xlsx
  outputs/
  resource_manifest.json
  resource_index.md
```

注入 payload 字段：

```json
{
  "resource_manifest_path": "/tmp/skill-run/resource_manifest.json",
  "conversation_index_path": "/tmp/skill-run/resource_index.md",
  "input_dir": "/tmp/skill-run/input"
}
```

`resource_manifest.json` 中每个文件包含：

```json
{
  "upload_id": "upl-abc",
  "filename": "data.xlsx",
  "mount_path": "/tmp/skill-run/input/upl-abc__data.xlsx",
  "content_type": "...",
  "file_type": "spreadsheet",
  "size_bytes": 12345,
  "sha256": "...",
  "preview": {},
  "relative_source_path": "upl-abc/original"
}
```

安全边界：

- Skill 只看到 workspace 内的副本路径。
- manifest 不暴露持久存储绝对路径。
- 脚本修改 `input/` 文件不会影响原始上传文件。

## 9. 旧 Skill 与新 Skill

旧 Skill：

- 不改代码仍可通过 `uploaded_artifacts[].content` 或 `content_base64` 工作。
- 平台会从持久文件重新构造这些兼容字段。

新 Skill：

- 应优先读取 `resource_manifest_path`。
- 从 `files[].mount_path` 打开真实文件。
- 只在兼容旧逻辑时读取 `uploaded_artifacts[].content` / `content_base64`。

本次同步更新范围：

- `.codex/skills/breeding-skill-builder/`
- `.codex/skills/breeding-skill-builder/SKILL.md`
- `.codex/skills/breeding-skill-builder/references/checklist.md`
- `.codex/skills/breeding-skill-builder/references/migration.md`
- `.codex/skills/breeding-skill-builder/references/templates.md`

## 10. 删除与清理语义

| 动作 | DB | 本地文件 |
| --- | --- | --- |
| 单文件删除 | `status = deleted` | 删除该 upload 资源目录 |
| conversation 删除 | conversation 相关记录物理删除/标记删除流程处理 | 删除整个 conversation 文件目录 |
| 已运行任务查看历史 | 任务附件 ledger 保留使用记录 | 已复制到历史 workspace / artifact 的内容不回写原始文件 |

注意：单文件删除不会回收已经生成的结果 artifact；artifact 属于任务输出生命周期。

## 11. 当前验证基线

后端：

- `tests/api/test_uploads.py`
- `tests/storage/test_conversation_file_resources.py`
- `tests/integrations/agent_skills/test_resource_manifest_workspace.py`
- `tests/integrations/agent_skills/test_artifact_context.py`

前端：

- 上传、列表、删除和提交 `upload_ids` 的 UI 回归测试已适配右侧文件 Drawer。

最近一次验证：

```text
python -m pytest -q tests/api/test_uploads.py tests/storage/test_conversation_file_resources.py
python -m compileall -q src/api src/storage tests/api tests/storage
cd frontend && npm run typecheck
cd frontend && npm test -- --run
cd frontend && npm run build
git diff --check
```

## 12. 后续待办

1. 接入真正的 LLM / OCR adapter 文件描述流水线，并保持失败不阻塞。
2. 为大文件与大量文件增加更明确的 Skill 挂载预算：最大文件数、总字节数、复制超时。
3. 降低旧兼容层的 base64 内联依赖，推动新 Skill 全面迁移到 manifest。
4. 为 conversation 文件目录提供管理/修复命令：重建 `index.md`、校验 sha256、清理 orphan 目录。
5. 如果生产切到 PostgreSQL，必须保证 schema、bootstrap、repository 与 SQLite 同等支持 `conversation_file_resource`。
