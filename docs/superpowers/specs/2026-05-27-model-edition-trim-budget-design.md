# Model Edition Trim Budget Design

状态：approved for implementation
日期：2026-05-27

## 目标

`trim_max_tokens` 不再作为生产主路径的全局常量，而是绑定到每个 `model_edition`。用户每次提交选择模型版本后，后端使用该模型版本在 `config.yaml` 中声明的上下文裁剪预算。

## 决策

采用 `model_editions.options[]` 内联配置：

```yaml
model_editions:
  default: deepseek-v4-flash-260425
  options:
    - value: deepseek-v4-flash-260425
      label: DeepSeek V4 Flash
      trim_max_tokens: 1024000
    - value: deepseek-v4-pro-260425
      label: DeepSeek V4 Pro
      trim_max_tokens: 1024000
```

换算规则：这里的 `K` 按十进制千计，`1024K = 1024 * 1000 = 1024000 tokens`。

## 运行时规则

1. 前端只提交 `model_edition`，不提交 `trim_max_tokens`。
2. 后端校验 `model_edition` 必须来自配置 allowlist。
3. LLM runtime 为选中模型创建 client 时，把该模型的 `trim_max_tokens` 覆盖到运行时 config。
4. Conversation memory / prompt trim 使用同一个按模型解析后的预算。
5. 生产配置不再使用顶层 `trim_max_tokens`；每个可选模型必须在 `model_editions.options[]` 中声明自己的 `trim_max_tokens`。
6. 若某个模型缺少 `trim_max_tokens`，运行时维持既有默认裁剪行为；不得从生产顶层配置继承。

## 验收

- 配置解析能读取每个 model option 的 `trim_max_tokens`。
- `config_with_model_edition()` 会把选中模型的 trim 写入派生 config。
- 用户选择不同模型时，LLM client / conversation memory 使用对应预算。
- API 文档说明该字段由服务端配置维护，前端不得传入。
- 不提交 `config.yaml` 或任何密钥。
