# Thinking 状态感知的模型 Reasoning Effort 配置设计

状态：用户已批准；implementation pending
日期：2026-08-27
分支：`main`
取代：`2026-06-25-model-reasoning-effort-design.md` 中的 reasoning-effort schema、前端关闭 thinking 时禁用强度选择及对应验证口径

## 1. 背景与证据

当前系统已按模型维护 reasoning effort 选项，但配置只用 `options[]` 隐式表达
`thinking.type=enabled` 时的支持集合，并用
`options[].allow_when_thinking_disabled` 表达 disabled 支持。该结构不对称，前端还会在
deep thinking 关闭时直接禁用强度下拉，因此无法让用户选择 provider 实际接受的
disabled-thinking effort。

2026-08-27 使用仓库当前 `config.yaml`、`LLMClient` 和 Ark endpoint 对五个模型进行了两轮真实
smoke。请求使用产品相同的显式 `thinking.type` 与 `reasoning_effort` 组合，不保留回答正文、密钥或
provider request ID：

| 模型 | `thinking=enabled` | `thinking=disabled` |
|---|---|---|
| `deepseek-v4-flash-ga-260731` | `minimal / low / medium / high / xhigh / max` 全部接受 | `minimal / low / medium / high / xhigh / max` 全部接受 |
| `deepseek-v4-pro-ga-260813` | `minimal / low / medium / high / xhigh / max` 全部接受 | `minimal / low / medium / high / xhigh / max` 全部接受 |
| `glm-5-2-260617` | `minimal / low / medium / high / xhigh / max` 全部接受 | `minimal / low / medium / high / xhigh / max` 全部接受 |
| `doubao-seed-2-1-pro-260628` | `minimal / low / medium / high` 全部接受 | 仅 `minimal` 接受；其他三档返回 HTTP 400 `InvalidParameter` |
| `doubao-seed-2-1-turbo-260628` | `minimal / low / medium / high` 全部接受 | 仅 `minimal` 接受；其他三档返回 HTTP 400 `InvalidParameter` |

两轮共覆盖 52 个组合：46 个被 provider 接受，6 个按预期被拒绝。provider 接受只能证明请求组合
合法，不能证明关闭 thinking 后各档 effort 一定产生可测量的语义差异；本设计不把兼容性 smoke
提升为质量或效果评测。

## 2. 目标

- 每个模型在配置中显式维护 `thinking=enabled` 和 `thinking=disabled` 各自支持的 effort 集合。
- 两种 thinking 状态分别维护默认 effort。
- 前端在 thinking 开启或关闭时都允许选择当前状态支持的 effort。
- 切换 thinking 状态或模型时，当前 effort 仍合法则保留，不合法才回退到新状态默认值。
- 前端、API、后端校验和 provider 请求使用同一个模型配置 authority。
- 配置错误在启动期 fail closed；非法用户组合在 provider 调用前失败。
- 提供可重复、脱敏、非默认 CI 的真实 provider 矩阵 smoke 入口。

## 3. 非目标

- 不证明不同 effort 的回答质量、token 使用量或推理深度确实不同。
- 不引入运行时 provider 自动探测或每次启动自动 smoke。
- 不根据 provider 错误自动降级 effort、自动打开 thinking 或静默改写用户选择。
- 不支持按模型配置不同的 API key、base URL 或 provider。
- 不改变模型 edition 选择、Agent capability profile、prompt、streaming 或工具调用合同。
- 不增加 enabled/disabled 两套“上次选择”状态；前端只维护一个当前 effort。
- 不修改数据库 schema、持久化消息格式、Rust sidecar、部署配置或 `prod` 分支。

## 4. 核心决策

### 4.1 选项目录与状态策略分离

每个模型只定义一次 effort 的 value/label 目录，再由 enabled/disabled 两个状态策略声明支持集合和
默认值：

```yaml
model_editions:
  options:
    - value: deepseek-v4-flash-ga-260731
      label: DeepSeek V4 Flash GA
      reasoning_efforts:
        options:
          - value: minimal
            label: 最低
          - value: low
            label: 低
          - value: medium
            label: 中
          - value: high
            label: 高
          - value: xhigh
            label: 更高
          - value: max
            label: 最高
        thinking:
          enabled:
            default: high
            supported: [minimal, low, medium, high, xhigh, max]
          disabled:
            default: minimal
            supported: [minimal, low, medium, high, xhigh, max]
```

该结构避免 enabled/disabled 重复 label，也比每个 option 上维护两个布尔字段更容易整体审查。
`options[]` 的顺序是前端展示顺序；`supported[]` 只表达集合成员资格，不改变展示顺序。

### 4.2 单一新 schema，不保留静默兼容

旧字段 `reasoning_efforts.default`、`reasoning_efforts.disabled_default` 和
`options[].allow_when_thinking_disabled` 不再属于合法生产配置。迁移必须同步修改根配置、fixtures、
DTO、前端和文档；新解析器发现旧字段或新旧混用时启动失败，不生成 legacy fallback。

### 4.3 状态切换只在必要时回退

- 当前 effort 属于新状态 `supported`：保留。
- 当前 effort 不属于新状态 `supported`：切换到新状态 `default`。
- 若模型完全不支持 disabled thinking，前端强制 enabled，并按相同规则保留或回退 effort。
- 后端不执行前端式自动修正；显式非法 API 请求必须失败。

## 5. 配置合同

### 5.1 数据结构

后端配置模型收敛为四个职责单一的值对象：

- `ReasoningEffortOption(value: str, label: str)`：effort 目录项。
- `ReasoningEffortStatePolicy(default: str | None, supported: tuple[str, ...])`：单一 thinking 状态策略。
- `ReasoningEffortThinkingPolicy(enabled, disabled)`：两种状态的必填组合。
- `ReasoningEffortConfig(options, thinking)`：模型级完整配置。

`ReasoningEffortConfig` 提供以下无 I/O 查询：

- `policy_for(thinking_enabled: bool)`；
- `supported_values(thinking_enabled: bool)`；
- `supports(value: str, thinking_enabled: bool)`；
- `default_for(thinking_enabled: bool)`。

### 5.2 启动校验

每个显式 model edition 必须通过以下校验：

1. `reasoning_efforts.options`、`thinking.enabled`、`thinking.disabled` 全部存在。
2. `options` 非空，value 非空且不重复；label 缺失时仍可安全回退到 value。
3. enabled/disabled 的 `supported` 各自不能包含空值或重复值。
4. `supported` 的每个值必须存在于公共 `options`。
5. 公共 `options` 的每个值必须至少被一个 thinking 状态引用，禁止孤儿目录项。
6. enabled 的 `supported` 必须非空，`default` 必须非空且属于 enabled `supported`。
7. disabled 的 `supported` 可以为空；为空时 `default` 必须为 `null`。
8. disabled 的 `supported` 非空时，`default` 必须非空且属于 disabled `supported`。
9. 旧 schema 字段或新旧字段混用必须报配置错误。

没有 disabled 支持项是合法模型能力，不是配置错误；该模型必须在产品层强制开启 thinking。

### 5.3 五个当前模型的初始配置

- DeepSeek V4 Flash GA、DeepSeek V4 Pro GA、GLM-5.2：enabled/disabled 均支持目录中的六档。
- 豆包 Seed 2.1 Pro、Turbo：enabled 支持四档，disabled 仅支持 `minimal`。
- 保持当前产品默认：所有模型 enabled 默认 `high`，disabled 默认 `minimal`。

## 6. 后端设计

### 6.1 API 投影

`GET /api/v1/config/model-editions` 使用 snake_case 返回与配置同构的只读能力投影：

```json
{
  "value": "doubao-seed-2-1-pro-260628",
  "label": "豆包Seed 2.1 Pro",
  "reasoning_efforts": {
    "options": [
      {"value": "minimal", "label": "最低"},
      {"value": "low", "label": "低"},
      {"value": "medium", "label": "中"},
      {"value": "high", "label": "高"}
    ],
    "thinking": {
      "enabled": {
        "default": "high",
        "supported": ["minimal", "low", "medium", "high"]
      },
      "disabled": {
        "default": "minimal",
        "supported": ["minimal"]
      }
    }
  }
}
```

后端只返回已经通过启动校验的配置，不在 DTO 序列化阶段补值或过滤错误。

### 6.2 请求解析

`resolve_llm_request_options()` 继续接收显式 model reasoning registry，并执行：

1. 解析 `model_edition` 和 `deep_thinking`。
2. 选择 enabled 或 disabled 状态策略。
3. 未提供 `main_agent_reasoning_effort` 时使用该状态 `default`。
4. 显式 effort 必须属于该状态 `supported`，否则返回 validation error。
5. disabled `supported=[]` 时拒绝 `deep_thinking=false`。
6. 合法值原样进入 `LLMClient`，不改写为全局默认。

所有 Main Agent、Agent Loop、title、memory、Skill input 及其他复用 reasoning registry 的路径继续使用同一
resolver 和当前 runtime 注入的配置，禁止重新读取仓库根配置或引入分支专用默认值。

### 6.3 Provider 边界

`LLMClient` 保持现有 payload 语义：

```json
{
  "thinking": {"type": "enabled|disabled"},
  "reasoning_effort": "<validated effort>"
}
```

只有 provider capability 显式声明不支持 thinking 或 reasoning effort 时才沿用既有省略规则。本设计不以
`None` 表达普通产品路径。

## 7. 前端设计

### 7.1 类型与有效性

前端类型与 API 新结构一一对应。配置有效性检查至少确认：目录非空、两个状态存在、当前状态默认值属于
支持集合，以及支持值可映射到目录项。后端正常情况下已经保证这些条件；前端校验用于防止版本错配或
损坏响应造成错误提交。

### 7.2 动态强度选项

前端按公共 `options[]` 顺序过滤当前状态 `supported`，生成思考强度下拉选项：

- thinking enabled：展示 enabled 支持项。
- thinking disabled：展示 disabled 支持项。
- 不再因为 `effectiveDeepThinking=false` 禁用 Select。
- 仅在任务交互锁、配置非法或当前状态没有支持项时禁用 Select。

豆包关闭 thinking 后仍可操作下拉，但只有 `minimal`；DeepSeek/GLM 关闭后可选择全部真实接受的档位。

### 7.3 切换行为

切换 thinking：

- 新状态支持当前 effort：保持当前值。
- 新状态不支持当前 effort：使用新状态 default。
- disabled 无支持项：不提供可关闭开关，显示“深度思考：已开启”。

切换模型：

1. 判断新模型能否维持当前 thinking 状态；不能时强制 enabled。
2. 当前 effort 在最终状态仍受支持则保留。
3. 否则使用最终状态 default。

前端继续只持有一个 `reasoningEffort` state，不记忆两个状态各自的历史选择。

### 7.4 提交

App 提交界面当前实际显示的 `modelEdition`、`effectiveDeepThinking` 和
`effectiveReasoningEffort`。API client 只负责映射到请求 metadata，不推断状态策略、不写死 `minimal`，也不
生成旧 schema fallback。后端仍是最终校验 authority。

## 8. 数据流

1. 服务启动时解析并严格校验五个模型的 reasoning effort 配置。
2. API 把同一已校验结构投影给前端。
3. 前端根据所选模型和 thinking 状态过滤强度目录。
4. 用户切换 thinking、模型或 effort；前端只在当前值变得非法时使用新状态默认值。
5. 提交携带当前模型、thinking 状态和有效 effort。
6. 后端使用同一 registry 重新验证组合，非法请求在 provider 调用前失败。
7. LLM client 向 provider 发送显式 thinking 与已校验 effort。
8. provider 若拒绝配置声明为合法的组合，按现有 provider error 路径失败并触发配置复核，不自动重放或降级。

## 9. 错误处理与审计

- 配置错误：启动失败，错误指出模型、状态、字段和值，但不包含密钥、base URL query 或请求正文。
- API 非法组合：返回 HTTP 400，指出模型、`enabled|disabled` 状态和 requested effort。
- 前端配置错误：显示模型配置错误，禁用发送，不生成猜测值。
- provider 合同漂移：保留稳定的 provider error 分类；日志和审计只记录安全的模型、状态、effort、HTTP 状态和
  provider error code。
- 不记录 smoke 回答正文、密钥或 provider request ID；不把 provider 接受误记为效果验证。

成功调用继续记录 validated/effective effort 与 thinking 状态。拒绝请求记录 requested effort 和拒绝原因，
不记录 `reasoning_effort_adjusted=true`，因为后端不自动调整显式请求。

## 10. 迁移策略

这是前后端锁步的配置/API contract 变更，必须在一个范围清晰的实现中同步完成：

1. 先以新测试冻结 parser、resolver 和前端切换行为。
2. 替换后端配置 dataclass、parser、validator 和 helpers。
3. 同步迁移根 `config.yaml` 与所有测试 fixtures。
4. 更新 API DTO 和 model-editions 投影。
5. 更新前端类型、动态过滤、开关与模型切换逻辑。
6. 更新 API/developer docs、旧设计状态、`docs/AGENTS.md` 和 `CHANGELOG.md`。
7. 运行自动化门禁后，用真实 smoke 脚本复测 52 个组合。

不保留旧配置 reader、旧 API response union 或前端双 schema decoder。部署必须使用同一提交构建的前后端和
配置；若外部客户端依赖旧 model-editions schema，应在部署前作为独立协调事项完成升级，而不是在本仓库
加入永久兼容层。

## 11. 真实 Provider Smoke

新增显式手工脚本 `scripts/smoke_model_reasoning_matrix.py`：

- 默认读取显式 `--config`，必须通过显式 live 标志才发送真实请求。
- 对每个模型执行 enabled/disabled × 公共目录全部 effort，而不只测试配置声明支持的组合。
- 每次请求使用短提示、禁用 SDK 自动重试并接受可配置超时。
- 输出脱敏 JSON：model、state、effort、expected support、accepted/rejected、HTTP status、稳定 error code。
- 不输出回答、密钥、base URL、请求头或 request ID。
- observed acceptance 与配置 expectation 不一致时返回非零退出码。
- 脚本不进入默认 CI，不在服务启动或请求路径运行，不把短暂网络失败自动解释为能力不支持。

首次实现验收必须再次运行完整 52 组合矩阵。自动化 fake 测试与真实 provider smoke 分开报告。

## 12. 测试计划

### 12.1 后端单元测试

- 新 schema 五模型解析及中文 label 保留。
- options 空值/重复、supported 空值/重复、未知引用、孤儿 option 均启动失败。
- enabled 空支持或非法 default 启动失败。
- disabled 空支持 + null default 合法；其他 default 组合按规则失败。
- 旧 schema 与新旧混用启动失败。
- enabled/disabled 默认解析正确。
- 显式 effort 只按当前状态支持集合验证。
- disabled 不受支持的模型拒绝关闭 thinking。
- injected registry 不读取根配置。

### 12.2 API 测试

- model-editions 返回新 snake_case 结构。
- DeepSeek/GLM disabled 的六档请求均通过后端校验。
- 豆包 disabled 仅 minimal 通过；low/medium/high 返回 HTTP 400 且 provider 未调用。
- 两类模型 enabled 的全部配置档位均通过。
- 缺省 effort 分别使用 enabled/disabled default。

### 12.3 前端测试

- enabled/disabled 分别展示当前状态支持项，顺序来自公共目录。
- thinking 关闭时 Select 保持可操作。
- 合法当前 effort 在开关切换后保留。
- 豆包 enabled high 切到 disabled 时回退 minimal。
- DeepSeek/GLM enabled high 切到 disabled 时保留 high。
- 模型切换按最终 thinking 状态保留或回退。
- disabled 无支持项时强制 enabled 并显示只读状态。
- 配置非法时显示错误并禁用提交。
- API client 精确透传 App 的 effective effort。

### 12.4 文档与回归

- API 文档说明新 schema、状态默认值、支持集合和 400 错误。
- 开发者文档说明 live smoke 非 CI、非效果评测。
- 运行定向 Integrations/API/Frontend 测试，再运行受影响全域测试、前端 typecheck/build 和 diff check。

## 13. 验收标准

- 五个当前模型均以新 schema 显式维护 enabled/disabled 支持集合和默认值。
- 前端在两种状态下都允许从当前支持集合选择 effort。
- 切换状态或模型时合法值保留，只有非法值才回退。
- 后端在 provider 调用前拒绝当前状态不支持的 effort。
- 豆包 disabled 仍只允许 minimal；DeepSeek/GLM disabled 可选择已验证的六档。
- 旧 schema 无生产 fallback，非法配置启动失败。
- 52 组合真实 smoke 与配置 expectation 一致，报告无敏感信息。
- 自动化测试、前端 typecheck/build、文档测试及 diff check 通过。
- `docker_cmd.md` 保持存在、Git-ignored 且未被读取、跟踪、移动或修改。

## 14. 影响范围

预计只涉及：

- `config.yaml`；
- `src/integrations/model_editions.py`、`llm_request_options.py` 及直接使用其 DTO/helper 的路径；
- `src/api/dto.py`、`src/api/runtime.py` 的 model-editions 投影；
- `frontend/src/api/types.ts`、`frontend/src/App.tsx` 及对应 client/tests；
- reasoning effort 相关 fixtures、API/Integrations/Frontend 测试；
- 新增脱敏 smoke 脚本及脚本测试；
- API/developer docs、旧设计状态、索引和变更日志。

不应顺手重构无关 LLM runtime、App 状态、模型选择、Agent Loop、存储、Rust 或部署代码。

## 15. License Requirement

设计仅复用现有 Python、OpenAI-compatible SDK、FastAPI/Pydantic、React/TypeScript 和 YAML 配置机制。
实现不需要新增第三方依赖或许可变更。
