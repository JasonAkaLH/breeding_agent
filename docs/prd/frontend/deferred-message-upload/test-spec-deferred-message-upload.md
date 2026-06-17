# 发送时上传文件测试规格

- 目标 PRD：docs/prd/frontend/deferred-message-upload/01-deferred-message-upload-prd.md
- 规划来源：.omx/plans/test-spec-20260616-deferred-message-upload.md
- 日期：2026-06-16

## 1. 前端单元和组件测试

### 1.1 选择文件不立即上传

- Arrange：渲染已登录 App，准备 File materials.csv。
- Act：触发隐藏 file input change。
- Assert：api.uploadConversationFile 未调用；发送框上方显示 materials.csv；卡片状态文案为待发送或等价文案；文件 Drawer 不显示该草稿，也不显示 Skill 可用。

### 1.2 拖拽文件不立即上传

- Act：对拖拽上传区触发 dragOver 和 drop。
- Assert：拖拽态 class 仍按现有测试出现和消失；api.uploadConversationFile 未调用；发送框上方显示草稿附件；Drawer 不显示草稿附件。

### 1.3 删除草稿附件不调用后端 DELETE

- Arrange：选择文件进入草稿。
- Act：点击该附件删除按钮。
- Assert：api.deleteConversationUpload 未调用；附件从列表消失；发送按钮状态按剩余输入和附件重新计算。

### 1.4 发送普通消息时先上传再提交

- Arrange：选择一个草稿附件，输入普通消息。
- Act：点击发送。
- Assert：api.uploadConversationFile 被调用；api.submitMessage 在上传 resolve 后调用；submitMessage.metadata.upload_ids 为上传返回的 ids；成功后草稿附件清空。

### 1.5 slash soft-binding metadata 保留

- Arrange：选择草稿附件，输入 slash skill 命令。
- Assert：发送时上传；submitMessage.metadata 同时包含 upload_ids、forced_by_slash_command、slash_command、soft_skill_binding。

### 1.6 interrupt answer 上传链路

- Arrange：任务进入接受上传的 pending interrupt。
- Act：选择草稿附件后发送 upload-only answer。
- Assert：发送前未上传；点击发送后上传；submitMessage.metadata 包含 interrupt_id 和 upload_ids，必要时包含 sheet selections。

### 1.7 不接受上传的 interrupt 仍禁用上传入口

沿用现有不接受上传 interrupt 测试。断言上传按钮和隐藏 input disabled；点击发送不会上传。

### 1.8 上传失败不提交消息

- Arrange：api.uploadConversationFile reject。
- Act：点击发送。
- Assert：api.submitMessage 未调用；草稿附件仍可见，显示错误或 toast。

### 1.9 消息提交失败时 best-effort 回滚

- Arrange：上传 resolve 返回 upl-1，api.submitMessage reject。
- Assert：api.deleteConversationUpload 对 upl-1 被调用一次；草稿附件保留；用户看到失败提示。

### 1.10 部分上传失败时回滚已上传文件

- Arrange：两个草稿附件；第一个上传成功，第二个失败。
- Assert：不调用 submitMessage；对第一个返回的 upload_id 调 DELETE；两个草稿附件仍留在 UI，失败项有提示。

## 2. API Client 回归测试

若 frontend/src/api/client.ts 签名不变，则保留现有测试即可：

- uploadConversationFile 仍向 /api/v1/conversations/uploads 发送 FormData，其中包含 conversation_id 和 file。
- deleteConversationUpload 仍向 /api/v1/conversations/uploads 发送 DELETE JSON body。
- submitMessage 仍发送 JSON body，metadata 不被 client 改写丢失。

若实现为了 AbortSignal 改签名，必须新增可选参数测试，并证明旧调用方式仍通过。

## 3. 后端回归测试

后端默认不改，但执行以下回归以证明 API 与本地文件资源语义未破坏：

- python -m pytest -q tests/api/test_uploads.py tests/storage/test_conversation_file_resources.py
- python -m compileall -q src/api src/storage tests/api tests/storage

重点断言仍由现有测试覆盖：上传 API 保存文件资源并返回 upload_id；metadata.upload_ids 缺失或删除时 fail closed；删除 API 标记 deleted 并物理清理资源目录。

## 4. 前端验证命令

- cd frontend && npm test -- --run
- cd frontend && npm run typecheck
- cd frontend && npm run build
- git diff --check

## 5. 手工验收脚本

1. 打开前端，选择一个 CSV。
2. 观察后端日志或网络请求：选择后不出现 /api/v1/conversations/uploads。
3. 在发送框上方删除该附件，确认无 DELETE 请求；打开文件面板确认草稿未出现在已保存资源列表。
4. 再选择 CSV，输入用这个文件分析，点击发送。
5. 观察请求顺序：先 POST uploads，成功后 POST chat-messages。
6. 确认后端 conversation 文件目录只在发送后出现文件。
7. 对一个需要上传的 interrupt 重复步骤 1 到 5。

## 6. Architect Iteration Addendum：新增状态机断言

### 6.1 上传失败不产生幽灵消息

- 普通消息路径：api.uploadConversationFile reject 后，api.submitMessage 未调用；草稿附件仍可见；对话列表中不新增本轮 user 消息或 assistant 提交中气泡。
- Interrupt answer 路径：接受上传的 pending interrupt 中上传失败后，pending interrupt 仍保持等待；对话列表中不新增本轮补充信息 user 消息或 assistant 恢复中气泡；草稿附件仍可见。

### 6.2 回滚删除失败时刷新后端文件列表

- Arrange：上传成功返回 upl-1，api.submitMessage reject，api.deleteConversationUpload 也 reject。
- Assert：调用 listConversationUploads 或现有刷新函数；后端残留文件显示在文件面板的已保存资源区域；用户看到可删除残留资源的提示；发送框上方草稿和文件面板已保存资源不重复显示同一个附件。

### 6.3 生命周期重置

- 新建会话、切换会话、logout、login 初始化时，draftAttachments 被清空。
- 不同 conversation 之间不得复用浏览器 File 草稿对象。
