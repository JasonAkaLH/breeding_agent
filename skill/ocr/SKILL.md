---
name: ocr
capability_id: skill.ocr
display_name: OCR 文档识别
description: 调用远端 OCR MCP 服务识别图片或 PDF；当用户要求 OCR、识别图片文字、提取扫描件文本、解析图片/PDF 为 Markdown 或 JSON 时使用。
triggers:
  - OCR识别
  - 图片识别
  - 识别图片文字
  - 提取图片文字
  - 扫描件识别
  - PDF识别
  - 文档OCR
  - 图片转文字
  - 解析扫描件
execution:
  mode: python_subprocess
  answer_mode: requires_finalizer
parameters:
  file_path:
    type: string
    required: false
    aliases: [文件路径, 图片路径, PDF路径, path, file]
  output_format:
    type: string
    required: false
    default: markdown
    enum: [markdown, json, both]
    aliases: [输出格式, 返回格式, format]
outputs:
  required:
    - answer
scripts:
  - name: run_ocr
    path: scripts/run_ocr.py
    runtime: python
    auto_run: true
    timeout_seconds: 3900
    inputs:
      required:
        - query
    outputs:
      required:
        - answer
---

# OCR

## Use when

- 用户要识别图片、截图、扫描件或 PDF 中的文字。
- 用户要把 OCR 结果整理成 Markdown、JSON 或简短文字摘要。
- 用户提供了本地文件路径，或通过前端上传了图片/PDF artifact。

## Workflow

1. 优先使用自动脚本 `run_ocr` 调用远端 OCR MCP。
2. 输入优先级：`file_path` 参数 > 脚本运行时 `uploaded_artifacts` 中的真实文件内容；图片/PDF 上传应通过项目 `upload_ids` 机制进入脚本专用 artifact 通道。
3. OCR 服务地址、token、超时和轮询间隔从 Skill 自己的本地配置文件 `skill/ocr/config.yaml` 读取；该文件与项目根目录 `config.yaml` 完全分开，且被 git ignore，禁止提交真实连接信息。
4. 脚本内部完成：上传文件到 `/uploads` → MCP `initialize` → `start_parse_job` → 等待 `get_parse_job` 终态 → 读取结果 → `ack_parse_job` 清理。
5. 对用户只呈现一次 OCR 操作的结果；不要要求用户手动轮询 job。
6. 如果脚本返回 `ok: false`，直接说明失败原因和可操作修复建议。

## Error handling

- 失败时读取脚本 stdout JSON 的 `error`、`error_code`、`stage`、`error_type`、`retriable` 和 `status` 字段，不要只说“调用失败”。
- HTTP 非 2xx、JSON-RPC 顶层 `error`、`tools/call` 的 `isError: true`、以及 `get_parse_job` 返回 `failed` / `cancelled` / `expired` / `gone` 都会保留错误码和错误信息；其中 `get_parse_job` 轮询中的短暂 HTTP 408/429/5xx 会在总超时内自动重试。
- `queued`、`running`、`cancelling`、`RESULT_NOT_READY` 不是最终失败；脚本会继续等待到成功、终态失败或超时。
- 成功时优先读取 `markdown` / `answer`，需要结构化结果时读取 `structured_result`；拿到 `result_receipt` 后脚本会调用 `ack_parse_job` 清理服务端临时数据。
- 长任务排查时可临时在 `skill/ocr/config.yaml` 中设置 `debug_progress: true`；脚本只向 stderr 输出脱敏进度，stdout 仍保持单个 JSON object。

## Output

- 默认用中文回答。
- 默认优先返回 OCR Markdown 文本。
- 用户要求结构化数据时，返回 JSON 摘要或说明脚本产出的 `structured_result`。
- 必须保留服务返回的明确错误码/错误信息，不要编造 OCR 内容。

## Boundaries

- 不要在回答中暴露 `auth_token` 或完整鉴权头。
- 更换远端服务时只改 `skill/ocr/config.yaml`，不要把真实地址、token 或鉴权头提交到仓库。
- 不要把 OCR MCP 当长期文件存储；成功拿到结果后应由脚本调用 `ack_parse_job`。
- 如果没有文件路径、没有上传 artifact、也没有可用文件内容，只问一个关键问题：请用户上传图片/PDF 或提供可访问的本地文件路径；脚本必须按 `Skill构建指南.md` 返回 structured `missing_input`（`ok: false`、`is_error: true`、`error.type: missing_input`、`missing: ["file_path"]`），不要把缺文件伪装成 OCR 服务失败。
- 大 PDF 或远端处理超时时，返回已有 `job_id` 和错误说明，不要假装已完成。
