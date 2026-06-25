# Per-Model Reasoning Effort Configuration Design

状态：approved design; implementation pending
日期：2026-06-25

## 背景

当前系统把 `reasoning_effort` 当成全局枚举处理：后端类型和解析逻辑固定为 `minimal / high / max`，前端“思考强度”下拉也固定展示同一组选项。这只适合 DeepSeek 系列；新接入的豆包 Seed 2.1 模型实际使用 `minimal / low / medium / high`，并且当 `thinking.type == disabled` 时只有部分 effort 能发送。

2026-06-25 对 Ark endpoint 的 live smoke test 结果显示，`doubao-seed-2-1-pro-260628` 与 `doubao-seed-2-1-turbo-260628` 的组合规则一致：

- `thinking.type == enabled` 或省略时：`minimal / low / medium / high` 均可发送。
- `thinking.type == disabled` 时：只允许省略 effort 或发送 `minimal`。
- `thinking.type == disabled` 搭配 `low / medium / high` 会返回 `Invalid combination of reasoning_effort and thinking type`。

因此模型配置需要表达两层信息：

1. 每个模型自己的 `reasoning_effort` 枚举及前端展示 label。
2. 每个枚举值在 `thinking.type == disabled` 时是否允许发送。

## 目标

- 让每个 `model_edition` 维护自己的 reasoning-effort 选项集合。
- 让前端根据当前模型动态展示该模型专属的“思考强度”选项和中文 label。
- 让配置声明每个 effort 是否可在 `thinking.type == disabled` 时发送。
- 在前端与后端都避免非法组合打到 provider。
- 保持旧配置可运行；缺少 per-model reasoning 配置时使用 legacy fallback，不阻断启动。

## 非目标

- 不在本次设计中支持按模型配置不同 `api_key` / `base_url`。
- 不改变 `model_edition` 的选择机制和 `trim_max_tokens` 的按模型解析机制。
- 不引入 provider 自动探测；模型能力仍由配置声明，live smoke test 只作为初始配置依据。
- 不要求前端在“深度思考”关闭时允许用户选择不合法 effort；关闭时系统应自动选择 disabled-safe effort。

## 推荐配置 schema

在 `model_editions.options[]` 的每个模型内新增 `reasoning_efforts`：

```yaml
model_editions:
  default: deepseek-v4-flash-260425
  options:
    - value: deepseek-v4-flash-260425
      label: DeepSeek V4 Flash
      trim_max_tokens: 1024000
      reasoning_efforts:
        default: minimal
        disabled_default: minimal
        options:
          - value: minimal
            label: 最低
            allow_when_thinking_disabled: true
          - value: high
            label: 高
            allow_when_thinking_disabled: false
          - value: max
            label: 最高
            allow_when_thinking_disabled: false

    - value: doubao-seed-2-1-pro-260628
      label: 豆包Seed 2.1 Pro
      trim_max_tokens: 256000
      reasoning_efforts:
        default: minimal
        disabled_default: minimal
        options:
          - value: minimal
            label: 最低
            allow_when_thinking_disabled: true
          - value: low
            label: 低
            allow_when_thinking_disabled: false
          - value: medium
            label: 中
            allow_when_thinking_disabled: false
          - value: high
            label: 高
            allow_when_thinking_disabled: false
```

字段含义：

- `reasoning_efforts.default`：该模型在 deep thinking 打开且当前选择无效时使用的默认 effort。
- `reasoning_efforts.disabled_default`：`thinking.type == disabled` 且当前 effort 不允许发送时使用的兜底 effort。
- `reasoning_efforts.options[].value`：实际发送给 provider 的字符串，不做全局枚举限制。
- `reasoning_efforts.options[].label`：前端展示文案，支持中文。
- `reasoning_efforts.options[].allow_when_thinking_disabled`：当请求会发送 `thinking.type: disabled` 时，该 effort 是否允许随请求发送。

`disabled_default` 必须指向 `options[]` 中存在且 `allow_when_thinking_disabled: true` 的项。若配置缺失或非法，运行时按“第一个允许 disabled 的项”兜底；仍找不到时，后端不发送 `reasoning_effort`。

## 后端设计

### 1. 配置模型

扩展 `src/integrations/model_editions.py`：

- 新增 `ReasoningEffortOption`：`value: str`、`label: str`、`allow_when_thinking_disabled: bool`。
- 新增 `ReasoningEffortConfig`：`default: str | None`、`disabled_default: str | None`、`options: tuple[ReasoningEffortOption, ...]`。
- 扩展 `ModelEditionOption`，增加 `reasoning_efforts: ReasoningEffortConfig | None`。
- 解析 `reasoning_efforts.options`，去重规则沿用 model edition：同一个模型内相同 `value` 只保留第一项。
- `label` 缺失时回退到 `value`。
- `allow_when_thinking_disabled` 缺失时默认 `false`，避免误把未知 provider effort 发到 disabled thinking。

为了兼容旧配置，若某个模型缺少 `reasoning_efforts`：

```python
minimal / high / max
```

作为 legacy fallback，其中 `minimal` 允许 disabled，`high/max` 不允许 disabled。这保持当前 DeepSeek 行为和旧测试语义。

### 2. API payload

扩展 `/api/v1/config/model-editions` 返回：

```json
{
  "default_model_edition": "deepseek-v4-flash-260425",
  "options": [
    {
      "value": "doubao-seed-2-1-pro-260628",
      "label": "豆包Seed 2.1 Pro",
      "reasoning_efforts": {
        "default": "minimal",
        "disabled_default": "minimal",
        "options": [
          {
            "value": "minimal",
            "label": "最低",
            "allow_when_thinking_disabled": true
          },
          {
            "value": "low",
            "label": "低",
            "allow_when_thinking_disabled": false
          }
        ]
      }
    }
  ]
}
```

API 不需要暴露 `trim_max_tokens`；该字段仍只在后端运行时使用。

### 3. 请求解析与组合约束

`src/integrations/llm_request_options.py` 需要从全局 `Literal` 改为字符串型、模型感知解析：

- `ReasoningEffort = str` 或移除固定 `Literal`。
- `LLMRequestOptions.reasoning_effort` 改成 `str | None`。
- `resolve_llm_request_options()` 增加 `model_edition_config` 或可读取当前 bootstrapped config，并根据 `model_edition` 找到对应 reasoning config。
- 当 `thinking_enabled == true`：
  - 若 metadata 提供的 `main_agent_reasoning_effort` 属于该模型 options，则使用它。
  - 否则使用该模型 `default`。
- 当 `thinking_enabled == false`：
  - 若 metadata effort 属于该模型 options 且该 option 允许 disabled，则使用它。
  - 否则使用 `disabled_default`。
  - 若 `disabled_default` 不合法，则使用第一个允许 disabled 的 option。
  - 若没有任何 disabled-safe option，则返回 `None`，后续 client 不发送 `reasoning_effort`。

`src/integrations/llm_client.py` 的 `_provider_request_options()` 应只在 `reasoning_effort is not None` 时加入请求字段。这样能安全覆盖少数 provider 不接受 disabled-thinking effort 的情况。

### 4. 审计 metadata

现有 `llm_call` 事件和 `safe_metadata()` 里已经记录 `reasoning_effort`。实现时应确保记录的是“实际发送或决定发送”的 effort，而不是用户原始选择。若发生降级，可额外记录：

```json
{
  "requested_reasoning_effort": "medium",
  "reasoning_effort": "minimal",
  "reasoning_effort_adjusted": true,
  "reasoning_effort_adjustment_reason": "not_allowed_when_thinking_disabled"
}
```

该审计字段不是前端首期必须展示的 UI，但测试应能断言非法组合不会被静默伪装成原始选择。

## 前端设计

### 1. 类型扩展

`frontend/src/api/types.ts` 增加：

```ts
export interface ReasoningEffortOption {
  value: string;
  label: string;
  allow_when_thinking_disabled: boolean;
}

export interface ReasoningEffortConfig {
  default: string | null;
  disabled_default: string | null;
  options: ReasoningEffortOption[];
}

export interface ModelEditionOption {
  value: ModelEdition;
  label: string;
  reasoning_efforts?: ReasoningEffortConfig | null;
}
```

`ReasoningEffort` 类型从固定 union 改成 `string`，因为它由模型配置决定。

### 2. 动态选项

`App.tsx` 不再使用全局 `REASONING_EFFORT_OPTIONS`。改为根据当前 `modelEdition` 查找 model option：

- `reasoningEffortOptions = selectedModel.reasoning_efforts.options`
- 若缺失，使用 legacy fallback：`minimal / high / max`
- AntD `Select` 的 `options` 使用配置里的 `label/value`

### 3. 切换模型时的状态修正

当用户切换模型后：

1. 如果当前 `reasoningEffort` 不属于新模型的 options，切到新模型 `default`。
2. 如果当前 deep thinking 关闭，并且当前 effort 不允许 disabled，切到 `disabled_default` 或第一个 disabled-safe option。
3. 如果没有 disabled-safe option，提交时不带 `reasoningEffort`。

这样可以避免用户从 DeepSeek 的 `max` 切到豆包后仍提交 `max`。

### 4. 切换 deep thinking 时的状态修正

当用户关闭“深度思考”时：

- 如果当前 effort 不允许 disabled，则自动切换到 disabled-safe effort，通常是 `minimal`。
- Select 可以继续 disabled，显示实际会提交的 disabled-safe effort。

当用户打开“深度思考”时：

- 保留当前 effort；若它不属于该模型 options，则切到模型 `default`。

### 5. 提交 payload

前端提交前计算 `effectiveReasoningEffort`：

- deep thinking 开：当前 effort 若属于模型 options，否则模型 default。
- deep thinking 关：当前 effort 若允许 disabled，否则 disabled-safe fallback。
- fallback 仍为空时，不提交 `reasoningEffort`。

后端仍做最终校验，前端逻辑只负责体验和减少无效请求。

## 数据流

1. 后端启动时从 `config.yaml` 解析 model editions 和 reasoning-effort configs。
2. 前端登录后调用 `/api/v1/config/model-editions`，拿到模型列表和每个模型的 reasoning 配置。
3. 用户选择模型，前端渲染该模型专属的 reasoning effort options。
4. 用户提交消息，payload 带 `model_edition`、`deepThinking`、`reasoningEffort`。
5. 后端根据选中模型重新解析 effective reasoning effort，应用 disabled-thinking 组合约束。
6. LLM client 只发送 provider 允许的 `thinking` 与 `reasoning_effort` 组合。

## 初始配置建议

基于现有产品行为和 2026-06-25 live smoke test：

- DeepSeek 系列：
  - options：`minimal / high / max`
  - `minimal.allow_when_thinking_disabled = true`
  - `high/max.allow_when_thinking_disabled = false`
- 豆包 Seed 2.1 Pro / Turbo：
  - options：`minimal / low / medium / high`
  - `minimal.allow_when_thinking_disabled = true`
  - `low/medium/high.allow_when_thinking_disabled = false`

若后续 provider 文档或 live smoke test 证明 DeepSeek 的 disabled-thinking 规则不同，只需改配置，不应再改代码枚举。

## 错误处理

- 配置 option 缺少 `value`：忽略该 option。
- 配置 option 缺少 `label`：回退到 `value`。
- `default` 不在 options 中：回退到第一个 option。
- `disabled_default` 不在 options 中或不允许 disabled：回退到第一个允许 disabled 的 option。
- 请求 effort 不属于当前模型：后端降级到模型 default 或 disabled-safe default。
- 当前模型不存在：沿用现有 `validate_model_edition()` 的 unsupported model 错误。
- provider 仍返回组合错误：按现有 LLM provider error 路径暴露，但这是配置缺失，需要更新对应模型配置。

## 测试计划

### 后端单元测试

- `model_edition_options()` 能解析每个模型的 `reasoning_efforts`。
- 中文 label 能原样返回。
- 缺少 per-model reasoning config 时生成 legacy fallback。
- `disabled_default` 非法时回退到第一个 disabled-safe option。
- 请求 `thinking=false + doubao/high` 时解析为 `minimal`，不会返回 `high`。
- 请求 `thinking=true + doubao/medium` 时保留 `medium`。
- 请求 `thinking=true + deepseek/max` 时保留 `max`。
- 请求 `thinking=true + doubao/max` 时回退到豆包 default。
- 没有 disabled-safe option 时 `reasoning_effort` 为 `None`，client 不发送该字段。

### API 测试

- `/api/v1/config/model-editions` 返回 `reasoning_efforts`。
- 旧测试仍通过：只断言 `value/label` 的调用不应因新增字段破坏。

### 前端测试

- 模型为 DeepSeek 时显示 `minimal/high/max` 对应 label。
- 模型切到豆包时显示 `minimal/low/medium/high`，并移除 `max`。
- 从 DeepSeek `max` 切到豆包时自动修正到豆包 default。
- deep thinking 关闭时，豆包 `high` 自动修正为 `minimal`。
- 提交时发送 effective effort，而不是已经不合法的旧 state。

### 可选 live smoke

保留一个本地脚本或 runbook，用真实 Ark key 验证豆包组合矩阵。该测试不进入 CI，不提交密钥，不把 provider request id 作为断言。

## 验收标准

- 前端“思考强度”选项随模型变化，不再使用全局固定枚举。
- 豆包模型不会发送 `thinking.type=disabled + low/medium/high`。
- DeepSeek 模型仍可使用 `minimal/high/max`。
- 后端允许新模型未来声明任意 provider-specific effort 字符串，无需代码改枚举。
- 所有非法 effort 或非法 disabled-thinking 组合都能在进入 provider 前被修正或省略。
- 单元测试、API 测试、前端测试覆盖主要配置和切换路径。

## 实施顺序建议

1. 扩展后端 dataclass、parser 和 legacy fallback。
2. 扩展 API DTO 与 payload。
3. 扩展 request option 解析和 LLM client 的可选 `reasoning_effort` 发送。
4. 更新 `config.yaml` 的四个当前模型 reasoning 配置。
5. 更新前端类型、动态下拉和状态修正逻辑。
6. 补齐后端/API/前端测试。
7. 运行 targeted backend tests 与 frontend tests。

## 设计取舍

- 选择把 `allow_when_thinking_disabled` 放在每个 option 上，而不是维护一个单独的 unsupported list。这样前端和后端都能围绕同一个 option object 做展示、校验和 fallback，减少双表不同步。
- 选择保留 legacy fallback，而不是要求所有部署立即补配置。这样本次变更可以平滑合入，同时生产配置仍应显式声明当前模型能力。
- 选择后端做最终兜底，而不是只依赖前端禁用。这样历史客户端、测试脚本或手写 API 请求都不会把非法组合直接打到 provider。
