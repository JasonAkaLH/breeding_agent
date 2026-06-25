# Per-Model Reasoning Effort Configuration Design

状态：perfectized design; implementation pending
日期：2026-06-25

## 背景

当前系统把 `reasoning_effort` 当成全局枚举处理：后端类型和解析逻辑固定为 `minimal / high / max`，前端“思考强度”下拉也固定展示同一组选项。这只适合 DeepSeek 系列；新接入的豆包 Seed 2.1 模型实际使用 `minimal / low / medium / high`，并且当 `thinking.type == disabled` 时只有部分 effort 能发送。

2026-06-25 对 Ark endpoint 的 live smoke test 结果显示，`doubao-seed-2-1-pro-260628` 与 `doubao-seed-2-1-turbo-260628` 的组合规则一致：

- `thinking.type == enabled` 或省略时：`minimal / low / medium / high` 均可发送。
- `thinking.type == disabled` 时：只允许省略 effort 或发送 `minimal`。
- `thinking.type == disabled` 搭配 `low / medium / high` 会返回 `Invalid combination of reasoning_effort and thinking type`。

当前产品主路径由 UI 的 deep thinking 状态决定并发送明确的 `thinking.type: enabled | disabled`，不依赖 provider 省略 `thinking.type` 时的默认行为。smoke test 中的“省略”只作为 provider 兼容性证据。

因此模型配置必须表达两层信息：

1. 每个模型自己的 `reasoning_effort` 枚举及前端展示 label。
2. 每个枚举值在 `thinking.type == disabled` 时是否允许发送。

## 目标

- 每个 `model_edition` 必须维护自己的 reasoning-effort 选项集合。
- 前端必须根据当前模型动态展示该模型专属的“思考强度”选项和中文 label。
- 配置必须声明每个 effort 是否可在 `thinking.type == disabled` 时发送。
- 前端必须根据配置决定是否显示“深度思考”开关、是否允许关闭 thinking、以及切换模型/切换 thinking 时的 effective effort。
- 后端必须基于显式传入的当前模型配置做最终校验，拒绝非法 effort 或非法 `thinking.type + reasoning_effort` 组合。
- 移除全局固定 reasoning-effort 枚举和内部 hard-coded `max` 语义；所有路径都按前端请求参数和当前模型配置解析。

## 非目标

- 不在本次设计中支持按模型配置不同 `api_key` / `base_url`。
- 不改变 `model_edition` 的选择机制和 `trim_max_tokens` 的按模型解析机制。
- 不引入 provider 自动探测；模型能力仍由配置声明，live smoke test 只作为初始配置依据。
- 不做后端自动降级或静默兜底；非法请求必须显式失败。
- 不设计 `reasoning_effort: None` 主路径；只要 provider 支持 `reasoning_effort`，前端和后端都必须提供当前模型合法的字符串值。

## 产品决策

1. **配置必填**：对显式配置在 `model_editions.options[]` 中的每个模型，`reasoning_efforts.options[]` 必须存在且合法。缺失或非法时启动阶段失败。
2. **无 disabled-safe effort 的模型强制开启 thinking**：如果某模型的所有 reasoning efforts 都不允许 `thinking.type == disabled`，前端不显示可关闭的“深度思考”开关，改为显示只读状态“深度思考：已开启”，并固定提交 `deepThinking=true`。
3. **关闭 thinking 时前端修正状态**：如果当前模型允许关闭 thinking，但当前 effort 不允许 disabled，用户关闭“深度思考”时前端自动切到该模型的 disabled-safe 默认 effort。
4. **非法组合后端拒绝**：历史客户端或手写 API 提交非法 effort、未知 effort、或 disabled-thinking 不允许的 effort 时，后端返回明确错误，不自动改写为默认值，也不强制打开 thinking。
5. **审计记录请求值与实际值**：成功调用记录后端校验通过并实际发送的 `reasoning_effort`；拒绝请求时记录 requested value 和拒绝原因。不再记录 `reasoning_effort_adjusted=true`，因为系统不做自动降级。

## 推荐配置 schema

在 `model_editions.options[]` 的每个模型内新增必填 `reasoning_efforts`：

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

- `reasoning_efforts.default`：该模型在 deep thinking 打开且当前选择无效时的默认 effort。必须指向 `options[]` 中存在的项。
- `reasoning_efforts.disabled_default`：该模型在 `thinking.type == disabled` 时的默认 effort。若模型存在任何 disabled-safe option，则必须指向 `options[]` 中存在且 `allow_when_thinking_disabled: true` 的项。若模型没有 disabled-safe option，该字段可以省略或为 `null`。
- `reasoning_efforts.options[].value`：实际发送给 provider 的字符串，不做全局枚举限制。
- `reasoning_efforts.options[].label`：前端展示文案，支持中文。
- `reasoning_efforts.options[].allow_when_thinking_disabled`：当请求会发送 `thinking.type: disabled` 时，该 effort 是否允许随请求发送。

## 后端设计

### 1. 配置模型与启动校验

扩展 `src/integrations/model_editions.py`：

- 新增 `ReasoningEffortOption`：`value: str`、`label: str`、`allow_when_thinking_disabled: bool`。
- 新增 `ReasoningEffortConfig`：`default: str`、`disabled_default: str | None`、`options: tuple[ReasoningEffortOption, ...]`。
- 扩展 `ModelEditionOption`，增加必填 `reasoning_efforts: ReasoningEffortConfig`。
- 解析 `reasoning_efforts.options`，同一个模型内相同 `value` 只保留第一项；后续重复项应作为配置问题进入启动校验错误，避免部署者误以为后面的值生效。
- `label` 缺失时回退到 `value`。
- `allow_when_thinking_disabled` 缺失时默认 `false`，避免误把未知 provider effort 发到 disabled thinking。

启动阶段必须校验：

- 每个 `model_editions.options[]` 模型都有 `reasoning_efforts.options[]`，且 options 非空。
- 每个 option 有非空 `value`。
- `reasoning_efforts.default` 指向存在的 option；若缺失，则启动失败。
- 如果存在至少一个 `allow_when_thinking_disabled: true` 的 option，`disabled_default` 必须存在且指向其中一个 disabled-safe option。
- 如果不存在 disabled-safe option，配置合法，但该模型在前端必须强制 thinking enabled。

旧的 legacy fallback（`minimal / high / max`）不得作为产品主路径；仅可在低层测试 helper 中显式构造，不能在生产配置缺失时自动补齐。

### 2. 显式传入配置/registry

`reasoning_effort` resolver 不得在内部直接 `load_config()` 读取全局 bootstrapped config。实现必须由调用链显式传入当前 runtime 实际使用的配置或预解析 registry：

- `ApiRuntime` 初始化时基于 `_model_edition_config` 解析并持有 model reasoning registry。
- `SharedLLMRuntime` 使用 injected config 时必须使用 injected config 中的 registry；使用 environment config 时使用 bootstrapped environment config 对应的 registry。
- `MainAgentExecutor`、planner generator、conversation title generator、skill input generator、conversation memory builder、resolution generator 等所有调用 `resolve_llm_request_options()` 或 `resolve_llm_reasoning_effort()` 的路径，都必须接收并传递同一个 registry。
- 测试注入 config 时，resolver 必须使用测试 config，而不是仓库根 `config.yaml`。

该约束是阻止多 runtime / injected config 场景误读全局配置的硬要求。

### 3. API payload

扩展 `/api/v1/config/model-editions` 返回，字段保持现有 API 风格的 snake_case：

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

### 4. 请求解析与组合约束

`src/integrations/llm_request_options.py` 需要从全局 `Literal` 改为模型感知字符串解析：

- `ReasoningEffort = str` 或移除固定 `Literal`。
- `LLMRequestOptions.reasoning_effort` 保持合法字符串，不设计 `None` 主路径。
- `resolve_llm_request_options()` 必须接收显式传入的 model reasoning registry，并根据 `model_edition` 找到对应 reasoning config。
- 若请求未携带 `model_edition`，使用当前 runtime 的 default model edition 找到对应 config。
- 当 `thinking_enabled == true`：
  - metadata 提供的 `main_agent_reasoning_effort` 必须属于该模型 options；否则返回 validation error。
  - metadata 未提供时，使用该模型 `default`。
- 当 `thinking_enabled == false`：
  - 当前模型必须至少有一个 disabled-safe option；否则返回 validation error，提示该模型必须开启 deep thinking。
  - metadata 提供的 effort 必须属于该模型 options 且该 option 允许 disabled；否则返回 validation error。
  - metadata 未提供时，使用 `disabled_default`。

`src/integrations/llm_client.py` 的 provider capability 逻辑保持现状：只有当 provider config 声明 `supports_reasoning_effort: false` 时才不发送 `reasoning_effort`。本功能主路径不通过 `None` 表达省略。

### 5. 清理 hard-coded reasoning effort

实现必须清理所有产品路径中的 hard-coded effort，尤其是：

- `frontend/src/api/client.ts` 中 deepThinking=false 时强制 `minimal` 的逻辑。
- `src/api/runtime.py` 中 `_INTERRUPT_TURN_LLM_METADATA = {"deep_thinking": True, "main_agent_reasoning_effort": "max"}`。
- `planner_reasoning_effort`、`main_agent_reasoning_effort`、interrupt turn、conversation memory summary/resolution、skill input generator、conversation title generator、main agent executor 中所有默认/固定值。
- 测试夹具中表达产品默认值的 `max` 断言；测试可以显式构造 DeepSeek config 验证 `max`，但不能把 `max` 作为跨模型默认。

所有路径必须按前端传入的 `model_edition`、`deep_thinking`、`main_agent_reasoning_effort` 和当前模型配置解析。内部发起的 LLM 调用如果没有用户显式 effort，也必须使用当前模型配置的 `default` 或 `disabled_default`，不得写死全局值。

### 6. 审计 metadata 与错误信息

成功 LLM call 事件和 runtime metadata 应记录：

```json
{
  "model_edition": "doubao-seed-2-1-pro-260628",
  "thinking_enabled": true,
  "requested_reasoning_effort": "medium",
  "reasoning_effort": "medium"
}
```

非法请求失败时，错误日志或事件应记录：

```json
{
  "model_edition": "doubao-seed-2-1-pro-260628",
  "thinking_enabled": false,
  "requested_reasoning_effort": "high",
  "reasoning_effort_error": "not_allowed_when_thinking_disabled"
}
```

错误应返回可理解的信息，例如：当前模型 `doubao-seed-2-1-pro-260628` 不允许在 deep thinking 关闭时使用 `high`。系统不记录 `reasoning_effort_adjusted=true`，因为不会自动降级。

## 前端设计

### 1. 类型扩展

`frontend/src/api/types.ts` 增加：

```ts
export type ReasoningEffort = string;

export interface ReasoningEffortOption {
  value: string;
  label: string;
  allow_when_thinking_disabled: boolean;
}

export interface ReasoningEffortConfig {
  default: string;
  disabled_default: string | null;
  options: ReasoningEffortOption[];
}

export interface ModelEditionOption {
  value: ModelEdition;
  label: string;
  reasoning_efforts: ReasoningEffortConfig;
}
```

`ReasoningEffort` 类型从固定 union 改成 `string`，因为它由模型配置决定。

### 2. 动态选项

`App.tsx` 不再使用全局 `REASONING_EFFORT_OPTIONS`。改为根据当前 `modelEdition` 查找 model option：

- `reasoningEffortOptions = selectedModel.reasoning_efforts.options`
- AntD `Select` 的 `options` 使用配置里的 `label/value`
- 如果 API 返回模型缺少 `reasoning_efforts`，前端应视为配置错误并禁用提交，而不是生成 legacy fallback。

### 3. 深度思考开关显示规则

前端根据当前模型是否存在 disabled-safe option 决定展示：

- 若至少一个 option `allow_when_thinking_disabled: true`：显示正常“深度思考”开关。
- 若没有任何 disabled-safe option：隐藏可交互开关，显示只读状态文案“深度思考：已开启”，并固定 `deepThinking=true`。

该规则必须随模型切换实时更新。

### 4. 切换模型时的状态修正

当用户切换模型后：

1. 如果新模型没有 disabled-safe option，强制 `deepThinking=true`。
2. 如果当前 `reasoningEffort` 不属于新模型的 options，切到新模型 `default`。
3. 如果当前 deep thinking 关闭，并且当前 effort 不允许 disabled，切到 `disabled_default`。
4. 如果配置声称存在 disabled-safe option 但缺失合法 `disabled_default`，前端禁用提交并显示配置错误；该情况正常应在后端启动阶段失败。

这样可以避免用户从 DeepSeek 的 `max` 切到豆包后仍提交 `max`。

### 5. 切换 deep thinking 时的状态修正

当用户关闭“深度思考”时：

- 如果当前 effort 不允许 disabled，则自动切换到该模型的 `disabled_default`。
- Select 可以继续 disabled，显示实际会提交的 disabled-safe effort。

当用户打开“深度思考”时：

- 保留当前 effort；若它不属于该模型 options，则切到模型 `default`。

### 6. 提交 payload 与 API client 职责

前端 App 负责计算并传入 effective reasoning effort：

- deep thinking 开：当前 effort 必须属于模型 options；否则使用模型 `default`。
- deep thinking 关：当前 effort 必须允许 disabled；否则使用 `disabled_default`。
- 如果模型没有 disabled-safe option，App 必须强制 deep thinking 开，不得提交 disabled-thinking payload。

`frontend/src/api/client.ts` 不得在 `deepThinking=false` 时强制写入 `minimal`，也不得自行兜底。API client 只负责把 App 传入的 `reasoningEffort` 透传到 metadata：

- 如果 App 提供 `reasoningEffort`，写入 `main_agent_reasoning_effort`。
- 如果 App 未提供，省略 `main_agent_reasoning_effort`，由后端根据模型配置和 thinking 状态决定默认值或返回校验错误。

后端仍做最终校验，前端逻辑只负责体验和减少无效请求。

## 数据流

1. 后端启动时从当前 runtime config 解析 model editions 和 reasoning-effort configs；配置缺失或非法则启动失败。
2. 前端登录后调用 `/api/v1/config/model-editions`，拿到模型列表和每个模型的 reasoning 配置。
3. 用户选择模型，前端渲染该模型专属的 reasoning effort options，并根据 disabled-safe option 决定是否显示可关闭的 deep thinking 开关。
4. 用户提交消息，payload 带 `model_edition`、`deepThinking`、以及 App 计算的 effective `reasoningEffort`。
5. 后端使用显式传入的当前模型 registry 重新校验 model edition、thinking 状态和 reasoning effort。
6. 合法请求进入 LLM runtime；非法请求在进入 provider 前返回错误。
7. LLM client 发送 provider 允许的 `thinking` 与 `reasoning_effort` 组合。

## 初始配置建议

基于现有产品行为和 2026-06-25 live smoke test：

- DeepSeek 系列：
  - options：`minimal / high / max`
  - `default: minimal`
  - `disabled_default: minimal`
  - `minimal.allow_when_thinking_disabled = true`
  - `high/max.allow_when_thinking_disabled = false`
- 豆包 Seed 2.1 Pro / Turbo：
  - options：`minimal / low / medium / high`
  - `default: minimal`
  - `disabled_default: minimal`
  - `minimal.allow_when_thinking_disabled = true`
  - `low/medium/high.allow_when_thinking_disabled = false`

若后续 provider 文档或 live smoke test 证明 DeepSeek 的 disabled-thinking 规则不同，只需改配置，不应再改代码枚举。

## 错误处理

- 模型配置缺少 `reasoning_efforts.options[]`：启动失败。
- 配置 option 缺少 `value`：启动失败。
- 配置 option 缺少 `label`：回退到 `value`。
- `default` 缺失或不在 options 中：启动失败。
- 存在 disabled-safe option 但 `disabled_default` 缺失、不在 options 中、或不允许 disabled：启动失败。
- 请求 effort 不属于当前模型：后端返回 validation error。
- 请求 `thinking=false` 且该模型没有 disabled-safe option：后端返回 validation error，要求开启 deep thinking。
- 请求 `thinking=false` 且 effort 不允许 disabled：后端返回 validation error。
- 当前模型不存在：沿用现有 `validate_model_edition()` 的 unsupported model 错误。
- provider 仍返回组合错误：按现有 LLM provider error 路径暴露；这是配置缺失或 provider 行为变化，需要更新对应模型配置并补 smoke evidence。

## 文档与开发者说明

实现必须同步更新 API/developer docs，说明：

- `reasoning_effort` 是按模型配置下发，不存在全局固定枚举。
- `/api/v1/config/model-editions` 返回每个模型的 `reasoning_efforts`。
- `allow_when_thinking_disabled` 的语义。
- 当前模型没有 disabled-safe effort 时，前端必须强制 `deepThinking=true`。
- 非法 `model_edition + deep_thinking + reasoning_effort` 组合由后端返回 validation error。

现有 `tests/api/test_developer_docs.py` 已覆盖部分开发文档内容，实施时应同步调整或新增断言。

## 测试计划

### 后端单元测试

- `model_edition_options()` 能解析每个模型的 `reasoning_efforts`。
- 中文 label 能原样返回。
- 缺少 `reasoning_efforts.options[]` 时配置校验失败。
- `default` 非法时配置校验失败。
- 存在 disabled-safe option 但 `disabled_default` 非法时配置校验失败。
- 没有 disabled-safe option 的模型配置合法，并标记为必须强制 thinking enabled。
- 请求 `thinking=false + doubao/high` 时返回 validation error，不会降级为 `minimal`。
- 请求 `thinking=true + doubao/medium` 时保留 `medium`。
- 请求 `thinking=true + deepseek/max` 时保留 `max`。
- 请求 `thinking=true + doubao/max` 时返回 validation error。
- resolver 在 injected config 测试中使用显式传入 registry，不读取仓库根 `config.yaml`。
- 内部 interrupt turn、planner/main defaults、conversation memory、skill input、conversation title 等路径不再 hard-code `max` 或跨模型默认 effort。

### API 测试

- `/api/v1/config/model-editions` 返回 `reasoning_efforts`，字段为 snake_case。
- 配置非法时 runtime 启动失败或构造 runtime 失败。
- 非法请求组合返回明确 validation error，且 provider client 未被调用。

### 前端测试

- 模型为 DeepSeek 时显示 `minimal/high/max` 对应 label。
- 模型切到豆包时显示 `minimal/low/medium/high`，并移除 `max`。
- 从 DeepSeek `max` 切到豆包时自动修正到豆包 default。
- deep thinking 关闭时，豆包 `high` 自动修正为 `minimal`。
- 模型没有任何 disabled-safe option 时，不显示可交互开关，显示“深度思考：已开启”，并提交 `deepThinking=true`。
- `frontend/src/api/client.ts` 不再在 `deepThinking=false` 时硬写 `minimal`；只透传 App 提供的 effective effort。
- 提交时发送 effective effort，而不是已经不合法的旧 state。

### 文档测试

- API/developer docs 说明 per-model reasoning efforts、`allow_when_thinking_disabled`、前端强制 thinking enabled 规则和后端 validation error。
- `tests/api/test_developer_docs.py` 或同等测试覆盖文档更新。

### 可选 live smoke

保留一个本地脚本或 runbook，用真实 Ark key 验证豆包组合矩阵。该测试不进入 CI，不提交密钥，不把 provider request id 作为断言。

## 验收标准

- 每个配置模型都显式声明 `reasoning_efforts`，缺失或非法配置会在启动阶段失败。
- 前端“思考强度”选项随模型变化，不再使用全局固定枚举。
- 当前模型没有 disabled-safe effort 时，前端显示“深度思考：已开启”只读状态并固定提交 `deepThinking=true`。
- 豆包模型不会发送 `thinking.type=disabled + low/medium/high`；此类历史/手写请求在后端被拒绝。
- DeepSeek 模型仍可在 thinking enabled 时使用 `minimal/high/max`。
- 后端允许新模型未来声明任意 provider-specific effort 字符串，无需代码改枚举。
- 所有 hard-coded reasoning effort 产品路径已清理，所有 LLM 路径都按前端参数和当前模型配置解析。
- API/developer docs 与测试同步更新。
- 单元测试、API 测试、前端测试覆盖主要配置、切换、校验和错误路径。

## 实施顺序建议

1. 扩展后端 dataclass、parser、registry 和启动校验。
2. 更新 `config.yaml` 的四个当前模型 reasoning 配置。
3. 扩展 API DTO 与 `/api/v1/config/model-editions` payload。
4. 改造 request option 解析为显式 registry 输入，并清理所有 hard-coded effort。
5. 更新 LLM runtime / main agent / planner / memory / interrupt / skill input / title generator 调用链，确保传入同一 registry。
6. 更新前端类型、动态下拉、深度思考只读状态、模型切换和 deep thinking 切换修正逻辑。
7. 更新 API/developer docs。
8. 补齐后端/API/前端/文档测试。
9. 运行 targeted backend tests、frontend tests、developer docs tests。

## 设计取舍

- 选择把 `allow_when_thinking_disabled` 放在每个 option 上，而不是维护一个单独的 unsupported list。这样前端和后端都能围绕同一个 option object 做展示、校验和状态修正，减少双表不同步。
- 选择严格要求每个模型显式配置 `reasoning_efforts`，而不是保留生产 legacy fallback。这样新模型不会无意继承 DeepSeek 的 `minimal/high/max`。
- 选择前端在无 disabled-safe option 时强制 thinking enabled，而不是允许用户关闭后靠后端兜底。这样 UI 展示与实际 provider 请求一致。
- 选择后端拒绝非法组合，而不是自动降级或强制打开 thinking。这样不会违背用户显式选择，也能尽早暴露客户端或配置错误。
- 选择显式传入当前 runtime registry，而不是 resolver 内部读取全局 config。这样 injected config、测试 config、多 runtime 场景都能保持一致。
