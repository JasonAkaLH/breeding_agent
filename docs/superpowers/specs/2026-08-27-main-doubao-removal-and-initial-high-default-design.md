# Main 豆包模型移除与首次对话 High 默认设计

状态：用户已批准方案 A；ready for implementation planning
日期：2026-08-27
目标分支：`main`

## 1. 背景

当前 main 开发环境的模型配置暴露五个模型，其中豆包 Seed 2.1 Pro/Turbo 在关闭
thinking 时只接受 `minimal`。前端 `reasoningEffort` 初始值同样写为 `minimal`，因此用户首次
进入空白对话时会看到“关闭深度思考 + 最低”，与产品约定不符。

用户已确认：main 移除两个豆包模型；首次空白对话默认“不开启深度思考 +
`high`”。后续点击“新建对话”必须保持现有行为，继续沿用当前的深度思考和强度设置，不重置为
初始默认。

## 2. 目标

1. main 模型列表只保留 DeepSeek V4 Flash GA、DeepSeek V4 Pro GA 和 GLM-5.2。
2. 前端冷启动的首次空白对话初始为 `deepThinking=false` 与 `reasoningEffort=high`。
3. 后续新建对话不改变用户当前的 thinking/effort 选择。
4. 保持模型切换时“当前值合法则保留，非法才回退到新状态默认值”的现有规则。

## 3. 范围

### 3.1 在范围内

- 从 Git-ignored 的 main 本地 `config.yaml` 删除：
  - `doubao-seed-2-1-pro-260628`；
  - `doubao-seed-2-1-turbo-260628`。
- 把 `frontend/src/App.tsx` 的 `reasoningEffort` 初始值从 `minimal` 改为 `high`。
- 增加首次初始化和新建对话继承行为的前端回归测试。
- 更新当前 API 文档中把豆包当作现行模型的例子，保留通用的状态能力合同。
- 重建 main 本地 backend/frontend，确认配置 API 和前端下拉只有三个模型。

### 3.2 不在范围内

- 不修改 `prod` 分支或生产配置。
- 不删除通用 `low` / `medium` reasoning effort 支持。
- 不重写历史设计、实施账本或 CHANGELOG 中的豆包实测证据。
- 不迁移、删除或改写已完成 Task 中的历史模型 metadata。
- 不修改数据库 schema、Rust/Sidecar、Provider adapter 或 reasoning stream。

## 4. 行为设计

### 4.1 模型列表

backend 继续从既有 `model_editions.options` 读取配置，不增加代码级 denylist。本地配置删除两个
选项后，`GET /api/v1/config/model-editions` 只投影剩余三个模型。客户端显式提交已移除的
model edition 时，沿用现有配置校验在 Provider 调用前拒绝。

### 4.2 首次空白对话

React state 的初始值固定为：

```text
deepThinking = false
reasoningEffort = high
```

当模型配置加载完成后，现有 `resolveEffectiveReasoningEffort` 继续做最终合法性检查。当前保留的
三个 main 模型在 thinking disabled 时都支持 `high`，因此不会回退。

### 4.3 后续新建对话

`resetConversationWorkspace` 与 `handleNewConversation` 不增加 `setDeepThinking` 或
`setReasoningEffort`。用户在上一段对话选择的 thinking/effort 会继续用于新对话。其他新建对话
行为，包括模型回到 backend default、清空消息/附件/Task state，全部保持现状。

## 5. 错误与兼容边界

- 模型配置缺失或非法时，前后端继续 fail closed，不为 `high` 新增隐式降级。
- 历史会话可继续显示已持久化的豆包任务结果，但不能再以已移除的 edition 创建新 Task。
- 本地 `config.yaml` 含敏感配置且 Git-ignored；只做定向 YAML 选项删除，不输出、暂存或提交其内容。
- `docker_cmd.md` 不读取、不修改、不跟踪。

## 6. 验收标准

1. 新的前端实例首次空白对话显示“深度思考关闭 + 思考强度高”。
2. 用户把设置改为任意其他合法组合后点击“新建对话”，thinking/effort 保持不变。
3. 配置 API 与前端模型下拉精确显示三个剩余模型，不出现两个豆包 edition。
4. 显式提交已移除的 edition 在 Provider 调用前失败。
5. 前端定向测试、API/config 定向测试、typecheck、build、文档测试和完整受影响回归通过。
6. backend/frontend 重建后健康，浏览器级实测与 API 结果一致。
7. `prod` 未变更，`config.yaml` 与 `docker_cmd.md` 仍 Git-ignored/untracked。

## 7. 回滚

- 代码回滚：把 React 初始 `reasoningEffort` 恢复为 `minimal`。
- main 本地环境回滚：从本地安全备份恢复两个豆包 `model_editions.options` 块并重建 backend。
- 回滚不修改历史 Task 或数据库。
