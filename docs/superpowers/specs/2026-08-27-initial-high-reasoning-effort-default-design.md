# 首次空白对话 High 思考强度默认设计

状态：`implemented`；用户已批准
日期：2026-08-27
目标分支：`main`
替代范围：本文档是当前唯一实施依据；不实施同日“Main 豆包模型移除与首次对话 High
默认设计”中的模型移除内容。

## 1. 目标

新的前端 App 实例首次进入空白对话时，必须初始为：

```text
deepThinking = false
reasoningEffort = high
```

后续点击“新建对话”必须保持现有行为：沿用用户当前的 thinking/effort 选择，不重置为首次
初始值。

## 2. 实施边界

- 只把 `frontend/src/App.tsx` 中 `reasoningEffort` 的 React 初始值从 `minimal` 改为
  `high`。
- `deepThinking` 初始值继续为 `false`。
- `resetConversationWorkspace` 与 `handleNewConversation` 不增加 thinking/effort 重置。
- 现有模型切换规则不变：当前 effort 在新模型/状态下合法则保留，非法才回退到对应
  policy default。
- 这是前端初始值，不修改直接 API 调用在省略 effort 时使用的后端配置默认值。

## 3. 明确不修改

- 不移除豆包 Seed 2.1 Pro/Turbo，不修改 `config.yaml`。
- 不修改任何模型的 enabled/disabled supported/default 配置。
- 不修改 backend、Provider、reasoning stream、数据库、Rust/Sidecar 或 `prod`。
- 不增加依赖、网络请求、持久化字段、DOM 控件或日志内容。

## 4. 合法性与兼容

模型配置加载后，现有 `resolveEffectiveReasoningEffort` 继续检查 `high` 是否属于当前
thinking policy 的 supported 集合。当前 main 默认 DeepSeek 模型在 thinking disabled 时支持
`high`，所以首次空白对话保持 `high`。若未来 backend default 改为 disabled 下不支持 `high`
的模型，前端按现有规则回退到该 policy default，不发送非法组合。

## 5. 验收标准

1. 全新 App mount 在默认 DeepSeek 配置加载后，功能菜单显示深度思考关闭、思考强度为“高”。
2. 首条普通聊天提交 `deepThinking=false` 与 `reasoningEffort=high`。
3. 用户把当前设置改为另一个合法组合后点击“新建对话”，thinking/effort 保持不变。
4. 切换到不支持当前 effort 的模型/状态时，现有合法回退行为仍通过。
5. `frontend/src/App.test.tsx` 定向回归、前端全量测试、typecheck 与 production build 通过。
6. 重建 frontend 后，浏览器级实测与自动测试一致；backend 无需重建。
7. 仓库工作树干净，`config.yaml`、`docker_cmd.md` 仍 Git-ignored/untracked，`prod` 未更新。

## 6. 回滚

把 `reasoningEffort` React 初始值恢复为 `minimal`，重建 frontend。回滚不修改对话、Task、模型
配置或数据库。
