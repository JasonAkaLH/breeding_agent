# 发送时上传文件 PRD

- 状态：已实施，待最终合并
- 日期：2026-06-16
- 规划来源：.omx/plans/prd-20260616-deferred-message-upload.md
- 共识交接：.omx/plans/handoff-20260616-deferred-message-upload.md
- 范围：前端对话台文件选择和拖拽后的暂存、取消、发送时上传，以及与现有后端 conversation file resource API 的兼容衔接。
- 非目标：不新增 API；不改变现有上传、删除、消息提交 API 的路径、HTTP method 或必需参数；不实现跨刷新保留未发送文件；不改后端本地资源文件系统持久化模型。

## 1. 背景

当前文件选择或拖拽后，前端立即调用 POST /api/v1/conversations/uploads。根据 docs/prd/backend/20-对话文件本地资源文件系统PRD.md，该 API 会立刻把文件保存到本地 conversation 文件目录、写入 DB、重写 index.md，并返回可提交到 metadata.upload_ids 的 upload_id。

这导致用户在发送消息前只是附加文件，实际已经把文件交给后端保存和解析；此时点击删除等价于删除后端资源，而不是取消一个尚未发送的附件。

## 2. 目标

1. 用户通过拖拽或上传文件按钮选择文件后，文件先进入浏览器端草稿附件列表。
2. 草稿附件可在发送前删除，删除不调用后端。
3. 只有用户发送携带该文件的消息时，前端才调用现有上传 API；上传成功后再调用现有消息提交 API 并传递 metadata.upload_ids。
4. 后端保持当前 API 与本地资源文件系统行为：上传 API 仍负责保存、解析、建索引；消息提交仍只消费 upload_id。
5. 普通消息和 interrupt answer 的附件提交流程保持一致。

## 3. RALPLAN-DR Summary

### Principles

1. API 兼容优先：不新增 endpoint，不改变现有 method、path、必需参数。
2. 概念分层清晰：浏览器草稿附件与后端 conversation file resource 必须分离。
3. 失败不隐瞒：上传或提交失败时保留可恢复状态，并提示用户。
4. 最小后端改动：后端现有上传、解析、绑定语义作为稳定边界。
5. 测试锁定行为：先用前端回归测试证明选择不上传、发送才上传。

### Decision Drivers

1. 用户明确要求发送前不落后端、不存本地。
2. 现有后端 API 已能在发送时上传并返回 upload_id。
3. 现有 UI 状态 pendingUploads 混合了草稿和已保存资源，需要拆分以避免语义错误。

### Viable Options

#### Option A：前端草稿附件加发送时顺序调用现有 API，推荐

优点：不新增 API；后端几乎不动；符合用户要求；可用前端测试强约束。
缺点：上传和消息提交不是服务端原子事务；需要 best-effort 回滚。

#### Option B：新增 multipart chat endpoint 一次性提交文本和文件，不选

优点：更接近原子提交，服务端可统一事务和回滚。
缺点：违反不加入新的 API；改动后端面更大；需要重写客户端协议。

#### Option C：选择时上传到后端临时区，发送后转正，不选

优点：可保留预览解析。
缺点：违反先不要发送到后端存到本地；需要新临时态或 API 语义变化。

## 4. 用户体验

### 4.1 选择和拖拽

- 文件被选择或拖入发送框后，发送框上方立即显示一张待发送文件卡；右侧文件面板在发送前不显示草稿附件。
- 文件卡展示文件名、大小、基础类型或扩展名；在发送前不显示后端解析出的行数、列名或 Skill 可用。
- 拖拽提示文案从释放文件以上传到当前对话调整为释放文件以附加到下一条消息。
- 发送框上方展示待发送附件状态；右侧文件面板空状态区分当前还没有已保存文件，发送带附件的消息后文件才会保存为当前对话资源并供 Skill 使用。

### 4.2 取消

- 发送前点击删除，只从浏览器草稿列表移除该文件。
- 不调用 DELETE /api/v1/conversations/uploads。
- 已经保存在后端的历史 conversation 文件仍可使用现有删除行为；本 PRD 不重做历史资源管理体验。

### 4.3 发送

点击发送后，若存在草稿附件：

1. 锁定当前输入和附件快照。
2. 逐个调用现有 api.uploadConversationFile，参数仍是 conversationId 和 file。
3. 收集返回的 upload_id。
4. 调用现有 api.submitMessage，把 metadata.upload_ids 与 slash metadata 或 interrupt metadata 合并。
5. 消息提交成功后清空草稿附件。

若无草稿附件，消息提交行为保持不变。

## 5. 本地代码设计

### 5.1 数据模型拆分

当前 pendingUploads 类型是 UploadFileResponse 数组，同时代表后端已保存文件和下一条消息附件。实施时应拆分概念：

- DraftAttachment：发送前浏览器内附件，包含 localId、File 对象、filename、contentType、sizeBytes、status、errorMessage。
- conversationUploads：后端已保存的 conversation 文件列表。也可以保留现有列表语义但重命名，避免把草稿误认为后端资源。

为降低改动风险，优先在 frontend/src/App.tsx 内定义 DraftAttachment，不改 frontend/src/api/types.ts 的后端 DTO。

### 5.2 选择与拖拽入口

- handleUploadFile 改为 handleAttachFile。
- 校验 authUser、conversationId、canUploadInCurrentComposer 和 file。
- 不调用 api.uploadConversationFile。
- 将 File 和基础元数据加入 draftAttachments。
- 清空隐藏 input value，允许再次选择同名文件。
- handleUploadDrop 继续读取 event.dataTransfer.files 的第一个文件，但调用本地 attach 函数。
- 当前 input 未启用 multiple，本 PRD 不要求一次选择多个；但多次选择可累积多个草稿附件。

### 5.3 删除入口

- 对 DraftAttachment 的删除只过滤本地 draftAttachments。
- 对已经保存在后端的 conversation 文件，继续使用 api.deleteConversationUpload。
- 待发送草稿附件只展示在发送框上方；右侧文件面板只展示已经保存在后端的 conversation 文件资源，并继续使用现有后端删除行为，不再提供上传入口。

### 5.4 发送编排

新增前端辅助函数 uploadDraftAttachments，输入 conversationId 和草稿附件列表，输出 UploadFileResponse 列表，内部按现有 api.uploadConversationFile 顺序上传。

普通消息 handleSubmit：

1. 计算 slash intent、conversation、content 等现有逻辑保持不变。
2. 推荐在提交前 snapshot draftAttachments，然后设置 submitting 或 uploading 状态。
3. 若 snapshot 非空，调用现有上传 API 并收集 upload ids。
4. 调用现有 api.submitMessage，metadata 合并顺序为 upload_ids 加 forced 或 slash metadata，避免丢失 soft binding。
5. 成功后清空草稿附件；失败时按第 6 节处理。

Interrupt answer handleInterruptAnswer：

- 若 interruptAcceptsUpload 为 true，则使用同一上传辅助函数，然后调用现有 interruptSubmitMetadata。
- 若当前 interrupt 不接受上传，则保持禁止 attach 和发送 upload-only 的现有约束。

### 5.5 API 兼容

保持以下接口不变：

- POST /api/v1/conversations/uploads：仍接收 form conversation_id 和 file，仍返回 UploadFileResponse。
- GET /api/v1/conversations/{conversation_id}/uploads：仍列后端 active 文件。
- DELETE /api/v1/conversations/uploads：仍删除后端已保存资源。
- POST /api/v1/conversations/chat-messages：仍通过 metadata.upload_ids 绑定文件。

后端无需新增 endpoint，也无需改变参数。后端测试作为回归保障，而不是实现重点。

## 6. 错误与一致性

### 6.1 上传失败

- 不调用 submitMessage。
- 草稿附件保留在列表中，标记 failed 并展示错误。
- 用户可删除或重试发送。

### 6.2 部分上传成功

- 若多个草稿附件中某个上传失败，不提交消息。
- 对本次已经上传成功的文件，best-effort 调用现有 DELETE API 回滚。
- 若回滚失败，展示提示：部分文件已保存到当前对话，可在文件面板删除。

### 6.3 消息提交失败

- 不清空草稿附件，允许用户修改后重试。
- 对本次发送刚上传成功的文件，best-effort 调用现有 DELETE API，避免 orphan conversation 文件。
- 若删除失败，展示可操作提示，不隐藏失败事实。

### 6.4 取消和停止

- 停止仍针对已创建 task 的执行取消；在 task 创建前的上传阶段，没有后端 task 可取消。
- 本 PRD 不要求新增上传中止 API。若不修改 api.uploadConversationFile 签名，则浏览器 fetch 不能被当前 UI 停止按钮中止；这是可接受 MVP 限制。
- 如后续要支持中止 fetch，可在前端 client 内部引入可选 AbortSignal，但本轮不推荐，因为用户要求尽量不改 API 动作和参数。

## 7. 验收标准

1. 文件选择后，api.uploadConversationFile 未被调用；发送框上方显示待发送附件，右侧文件面板不显示该草稿。
2. 发送前删除附件，api.deleteConversationUpload 未被调用；附件从 UI 消失。
3. 发送带附件消息时，调用顺序为上传 API 成功后再调用消息提交 API。
4. 消息提交的 metadata.upload_ids 来自发送时上传返回的 upload ids，并保留 slash soft-binding metadata。
5. interrupt answer 接受上传时，发送时上传并把 ids 放入 metadata.upload_ids；不接受上传时仍禁用上传入口。
6. 上传失败时不提交消息，草稿附件仍可见并可删除或重试。
7. 消息提交失败时执行 best-effort 删除刚上传文件；删除失败时有提示。
8. 后端上传、列表、删除、消息解析接口路径和参数保持不变；现有后端上传测试继续通过。

## 8. 实施边界

- 优先只改前端状态和编排：frontend/src/App.tsx 与相关前端测试。
- 后端代码默认不改；只有发现现有接口无法支持发送时上传时，才做兼容性修复，且不得新增 API。
- 不把未发送文件存入 LocalStorage 或 IndexedDB。
- 不改变 conversation file resource 的持久化目录、DB schema 或 index.md 生成规则。

## 9. 后续事项

- 如用户需要刷新页面后恢复未发送附件，再单独评估 IndexedDB 草稿缓存。
- 如用户需要上传进度条或取消上传，再单独评估 AbortController 与可观测进度方案。
- 如用户需要真正原子提交，可另立 PRD 评估 multipart chat endpoint；当前明确不做。

## 10. Architect Iteration Addendum：前端状态机补强

### 10.1 乐观消息创建规则

- 普通消息和 interrupt answer 都采用先完成草稿附件上传，再创建本地 user 和 assistant 乐观消息的顺序。
- 如果草稿附件上传失败，则不创建新的对话气泡，不调用 submitMessage，不改变 currentTaskId，也不进入 task submitting 状态。
- 如果实现因局部改动保留了先创建气泡的结构，则必须在上传失败时删除该轮新增的 user 和 assistant 气泡，并恢复输入框内容；两条路径只能选择一种，推荐前者。
- 这条规则同时适用于普通 handleSubmit 和 handleInterruptAnswer，避免用户看到并未提交到后端的幽灵消息。

### 10.2 回滚失败后的后端资源可见性

- 如果上传成功但消息提交失败，系统仍先 best-effort 调用现有 DELETE API 回滚刚上传资源。
- 如果 best-effort 删除失败，必须调用现有 listConversationUploads 或等价刷新函数，让该后端资源出现在文件面板的已保存资源区域，并使用现有后端删除按钮删除。
- 为避免同一个文件同时显示为草稿和已保存资源，刷新成功后该轮已上传成功且未能回滚的草稿项应标记为已保存残留或从草稿列表移除，并通过提示解释文件已经保存到当前对话。

### 10.3 生命周期重置

- login、logout、新建会话、切换会话等现有会清空上传状态的生命周期，也必须清空 draftAttachments。
- 浏览器 File 草稿对象不得跨 conversation 复用。
