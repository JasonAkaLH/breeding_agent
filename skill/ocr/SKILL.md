---
name: ocr
description: >-
  识别图片、截图、扫描件或 PDF 中的文字，并按用户需要整理为 Markdown、JSON 或简短摘要。适用于 OCR、图片识别、识别图片文字、提取扫描件文本、PDF 识别、文档 OCR、图片转文字、解析扫描件、把上传 PDF 转成 Markdown、询问可上传文件类型或输出格式等场景。
---

# OCR 文档识别

## 总纲

使用此 Skill 识别图片、截图、扫描件或 PDF 中的文字，并按用户需要整理为 Markdown、JSON 或简短摘要。

平台执行事实源由 `skill.contract.yaml` 和当前 selected input schema 决定；用户可见文件类型、输出格式和错误表达必须优先从 references 读取。

## 工作流

1. 确认用户提供了 OCR 文件或可访问路径。
2. 如果没有上传 artifact，也没有可用文件内容，只问用户上传图片/PDF；不要把缺文件伪装成 OCR 服务失败。
3. 让平台执行层完成上传、识别、轮询、结果读取和清理。
4. 成功时优先展示 Markdown 文本；用户需要结构化结果时再提供 JSON 摘要。
5. 失败时基于平台返回的错误码、阶段、是否可重试和状态给出可操作建议。

## 资源导航

- `references/usage.md`：总体流程、输入优先级和失败处理原则。
- `references/supported-files.md`：支持的文件类型和上传注意事项。
- `references/output-formats.md`：Markdown/JSON/both 输出口径。

## 边界

- 不暴露 token、鉴权头、远端服务地址、内部配置、本机绝对路径或运行目录。
- 不把 OCR 服务当长期文件存储；结果读取和清理由平台执行层处理。
- 不要求用户手动调用底层服务或轮询 job。
- 不编造 OCR 内容；不确定的识别内容应标记为不确定或建议用户提供更清晰文件。
