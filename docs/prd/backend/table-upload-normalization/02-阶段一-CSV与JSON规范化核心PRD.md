# 阶段一：CSV 与 JSON 规范化核心 PRD

- **父总纲**：`docs/prd/backend/table-upload-normalization/00-表格上传编码兼容与表头规范化总纲PRD.md`
- **状态**：待实施
- **前置阶段**：阶段零测试基线完成且默认 API 回归全绿
- **实施范围**：`src/api/table_upload_normalizer.py`、`src/api/upload_store.py`、`src/api/dto.py`、`src/api/routes/uploads.py`、`src/api/runtime.py`、`src/integrations/agent_skills/execution.py`、相关测试

## 1. 目标

建立系统级表格上传规范化核心，先解决 CSV / JSON 的多编码解码和技术性表头污染，并从第一批生产改动开始落实 prompt-safe 摘要上限与执行专用 artifact 隔离。

## 2. 本阶段范围

### 2.1 In scope

1. 新增窄模块 `src/api/table_upload_normalizer.py`，只负责文本表格 bytes -> 规范化内容 / 预览摘要 / 清洗审计摘要。
2. CSV / JSON 使用确定性编码候选：`utf-8-sig`、`utf-8`、`gb18030`、`gbk`（可由 `gb18030` 覆盖）、`big5`、`shift_jis`、`cp932`。
3. 表头 / JSON key 技术清洗：NFKC、BOM、零宽字符、不可见控制字符、全角空格、首尾 strip、外层成对引号。
4. CSV dialect 支持逗号、Tab、分号输入，输出统一为逗号分隔 UTF-8 CSV 文本。
5. JSON 顶层 object / 顶层 object array key 清洗；嵌套 object 不递归改写。
6. `UploadedFileRecord.content_text` 兼容承载“执行用规范化文本”，并在注释 / 命名上明确不等同原始文本。
7. `uploaded_artifacts` 按父总纲 7.4 裁剪；`skill_artifacts` 只在执行专用通道携带规范化 `content`。
8. 执行适配层 allowlist 增加本阶段所需的安全字段，不放开 prompt-safe 完整内容字段。

### 2.2 Out of scope

1. 不支持 `.xlsx` / `.xls` 解析。
2. 不新增 `openpyxl` / `xlrd`。
3. 不创建 sheet selection interrupt。
4. 不做业务列名映射，不读取 `SKILL.md` aliases 处理文件内部列名。
5. 不修改 `skill/**`。

## 3. 功能需求

| ID | 需求 |
|---|---|
| P1-FR-01 | CSV / JSON 只有完整解码成功才可接受，不得使用 `errors="ignore"` 或 `errors="replace"`。 |
| P1-FR-02 | 清洗后任一表头为空或重复时必须 400 fail closed，不生成 upload record。 |
| P1-FR-03 | `source_encoding`、`original_columns`、`column_normalizations`、count 与 truncated 标记必须进入 preview 摘要。 |
| P1-FR-04 | 宽表 preview 摘要必须按 50 列 / 50 条清洗记录上限裁剪，执行内容不受摘要裁剪影响。 |
| P1-FR-05 | `sha256` 仍基于原始 bytes，不能改为规范化文本 hash。 |
| P1-FR-06 | 主代理 prompt、conversation memory、普通 audit、SSE 不得出现完整 CSV / JSON 内容。 |
| P1-FR-07 | 当前 image / PDF 上传行为不变。 |

## 4. 验收标准

| ID | 验收标准 |
|---|---|
| P1-AC-01 | 上传 `﻿"ped_id",hyb_check,set` CSV 后，preview columns 与执行 content 第一行均为 `ped_id,hyb_check,set`。 |
| P1-AC-02 | GB18030 / GBK / Big5 / Shift-JIS CSV 可完整解码时上传成功，并记录 `source_encoding`。 |
| P1-AC-03 | 表头清洗后为空或重复时返回 400，upload store 中没有新增 record。 |
| P1-AC-04 | JSON 顶层 key 清洗后冲突时返回 400；嵌套 object 不递归改写。 |
| P1-AC-05 | `uploaded_artifacts` 不包含 `content` / `content_base64`，`skill_artifacts` 包含规范化 `content`。 |
| P1-AC-06 | 现有 CSV / JSON / image / PDF 上传、列表、删除、权限隔离行为不回归。 |

## 5. 测试计划

```bash
conda run -n multi_agent python -m unittest tests.api.test_uploads
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_main_agent_workflow_and_executor
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_artifact_context
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_execution
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

## 6. 完成门禁

- 阶段一不得新增 Python 依赖。
- no-raw prompt / audit / SSE 断言必须随本阶段一起通过。
- License Requirement：无依赖 / 许可变更，未触发 cargo-deny 风险。
