# Prompt Envelope P7 供应商缓存与观测增强实施计划

- **日期**：2026-05-29
- **源 PRD**：`docs/prd/backend/prompt-envelope/08-阶段七-供应商缓存与观测增强PRD.md`
- **Ralph context snapshot**：`.omx/context/prompt-envelope-p7-cache-observability-20260529T050429Z.md`
- **执行模式**：`$ralph + $plan`；默认 Solo Ralph，只有当实现面超出当前 touched files 或出现并行冲突时再拉 `$team`。

## 1. Requirements Summary

1. PromptEnvelope audit 必须稳定记录 `cacheable_prefix_hash`、`cacheable_prefix_tokens`、`first_dynamic_segment`，并提供统一 `prompt_render_metrics` 便于灰度聚合。
2. stable prefix 动态污染必须 fail-closed：`task_id`、`conversation_id`、`username`、current user、artifact、dependency result 等动态字段不得进入 `cache_affinity=prefix && mutability=stable` segment。
3. Provider prompt cache hint 由配置显式开启，默认关闭；provider 不支持时请求 no-op 但 safe metadata / audit 能记录 no-op 状态。
4. main-agent PromptEnvelope、P5 PromptProfile、多 messages/string/shadow 路径均要保留 audit-only、no raw prompt、no frontend SSE 行为。
5. 灰度观测字段覆盖 mode、template_version、prefix hash/token、final input budget/tokens、history budget/tokens、history compression retry、trim reasons、role fallback、provider cache hint/capability 状态。

## 2. Brownfield Evidence

- `src/orchestration/prompt_envelope.py:163-223` 已执行 final preflight，第一次失败后只允许一次 history compression retry，再失败 `final_input_over_budget` fail-closed；`final_input_token_budget = floor(trim_max_tokens * 0.75)` 位于 `src/orchestration/prompt_envelope.py:173-175`。
- `src/orchestration/prompt_envelope.py:347-365` 已生成 cacheable prefix hash/token 与 first dynamic segment；但 `_cacheable_prefix()` 在 `src/orchestration/prompt_envelope.py:567-583` 只按 stable prefix 计算 hash，缺少污染检测。
- `src/orchestration/prompt_envelope.py:593-618` 目前 segment audit metadata 只有 allowlisted history candidate 字段，符合 no-raw 基线。
- `src/capabilities/main_agent/prompt_envelope_builder.py:145-166` 当前 stable prefix 仅有稳定系统契约与下载硬约束；动态 tool profile/schema/history/tool result/current user 分别在 `src/capabilities/main_agent/prompt_envelope_builder.py:168-267` 以 dynamic/no_cache 加入。
- `src/capabilities/main_agent/prompt_envelope_builder.py:426-488` 与 `src/orchestration/prompt_profiles.py:178-240` 已输出 budget/hash/role fallback 等审计字段，但缺少统一 metrics object 与 provider cache capability/hint 状态。
- `src/integrations/llm_client.py:122-124` 解析 role/feature capabilities；`src/integrations/llm_client.py:357-368` 只写 thinking/reasoning request options，尚无 provider cache hint。
- `src/integrations/llm_runtime.py:41-67` static metadata 已支持 role capabilities，未暴露 provider cache capability。
- 主代理 audit-only 事件在 `src/capabilities/main_agent/executor.py:208-216` 与 LLM call audit payload 在 `src/capabilities/main_agent/executor.py:385-401`；前端不可见基线已有 `tests/api/test_main_agent_llm.py:119-201`。

## 3. RALPLAN-DR Summary

### Principles

1. **安全先于缓存**：prefix 稳定性不能以提前动态上下文为代价。
2. **观测不泄漏**：所有 metrics/audit 只记录 hash、计数、状态、safe capabilities。
3. **Provider 可插拔**：hint 是配置增强，不成为 correctness 依赖。
4. **兼容 P0-P6**：保持 off/shadow/string/messages、75% budget、一次 retry、role fallback 与 no-raw 行为。
5. **Fail closed**：动态污染和最终 over-budget 都必须阻断，而不是隐式降级为泄漏风险。

### Decision Drivers

1. 防止上下文爆掉或动态信息进入 cacheable prefix。
2. 让生产灰度能按 template/mode/provider/prefix hash 聚合对比。
3. 不绑定未知 provider 专有 API，避免未来迁移成本。

### Viable Options

- **Option A — Core detector + shared metrics + config-driven provider hint（选择）**
  - Pros：一次在 renderer 层覆盖 P1/P2/P5/P6；main-agent/profile 只消费 safe payload；provider hint 默认 off 且 no-op 明确。
  - Cons：需要 touching core/profile/client/runtime/test 多处。
- **Option B — 只在 main-agent builder 约束 stable segment**
  - Pros：改动较小。
  - Cons：无法覆盖 P5 profiles / generic renderer 调用；P7-FR-2 易漏。
- **Option C — 直接接入单一 vendor cache API**
  - Pros：可能更快看到某一 provider 的 cache 命中。
  - Cons：违反非范围，不可移植，且真实 provider 行为不可作为 correctness 依赖。

### Invalidation rationale

B 无法覆盖所有 PromptEnvelope 调用边界；C 违反“provider cache 作为可配置增强、不依赖单一 provider”的 PRD 边界。因此采用 A。

## 4. ADR

- **Decision**：在 `prompt_envelope` core 增加 stable-prefix pollution detector 与 metrics helper；在 main-agent/profile audit payload 中输出统一 `prompt_render_metrics`；在 LLM client/runtime 增加 provider cache capability + request hint 开关与 safe metadata。
- **Drivers**：P7-FR-1~5、no-raw、安全 fail-closed、provider 兼容性。
- **Alternatives considered**：builder-only guard、vendor-specific cache API、只做 audit 不做 fail-closed。
- **Why chosen**：core detector 覆盖最完整；metrics helper 避免 audit 字段散落；config-driven hint 可以不绑定 vendor。
- **Consequences**：新增测试覆盖 renderer、profile、main-agent、LLM client/runtime；不新增依赖/DB schema；provider hint 的真实命中率仍需生产侧按 hash 聚合观察。
- **Follow-ups**：若未来选定 provider-specific cache API，应另开 PRD 定义请求形态、命中率指标、回滚与供应商差异测试。

## 5. Implementation Steps

1. **TDD：core renderer**
   - 在 `tests/orchestration/test_prompt_envelope.py` 添加：stable prefix hash 在用户/history/tool result 变化时稳定；stable prefix segment 的 metadata/name/content 动态字段污染 fail-closed；audit metrics/no-raw 序列化断言。
   - 实现 `PromptPrefixPollutionAudit` 或等价 safe details，新增 `_detect_prefix_pollution()`，在 `_cacheable_prefix()` 前/内 fail-closed。
   - 新增 `prompt_render_metrics_from_audit()`，统一生成灰度指标，不包含 raw prompt。

2. **TDD：PromptProfile / main-agent audit payload**
   - 在 `tests/orchestration/test_prompt_profiles.py` 与 `tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py` 断言 `prompt_render_metrics` 包含 final budget/tokens、history retry、trim reasons、role fallback count、prefix 信息，且 no raw。
   - 更新 `prompt_profiles.py` / `prompt_envelope_builder.py` 输出 metrics，并把 metrics 纳入 llm_call payload。
   - 对 shadow render failure 保持 safe error details。

3. **TDD：provider cache capability/hint**
   - 在 `tests/integrations/test_llm_client.py` 添加默认关闭、不支持 no-op、支持+开启时写 request `extra_body` hint 的 fake tests；safe metadata 只记录 capability/status/hint keys，不记录 raw endpoint/key。
   - 在 `tests/integrations/test_llm_runtime.py` 添加 static metadata 透传 provider cache capability 的测试。
   - 实现 `_resolve_provider_cache_capabilities()`、request option merge、`last_provider_cache_hint_status` / safe metadata。

4. **Main-agent / API audit-only 验证**
   - 保持 `main_agent.prompt_envelope_rendered` 与 `main_agent.prompt_profile_rendered` 为 audit-only；如 stream metadata 提供 provider cache capabilities，则 prompt audit / llm call audit 能看到 safe provider cache 状态。
   - 不修改前端 SSE 契约。

5. **文档与变更记录**
   - 在 P7 PRD 补充 rollout 建议：shadow 观察 -> string 小流量 -> messages 小流量 -> provider cache hint 小流量，以及回滚/观测口径。
   - 更新 `CHANGELOG.md` Unreleased。

6. **Verification / Ralph gates**
   - 运行 targeted tests：`tests/orchestration/test_prompt_envelope.py`、`tests/orchestration/test_prompt_profiles.py`、`tests/integrations/test_llm_client.py`、`tests/integrations/test_llm_runtime.py`、`tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py`、`tests/api/test_main_agent_llm.py`。
   - 运行相关 discover：`tests/orchestration`、`tests/integrations`、`tests/capabilities/main_agent`、`tests/api`（如时间允许，至少受影响文件 targeted + 关键 discover）。
   - `python -m compileall src tests` 或 targeted `py_compile`；`git diff --check`。
   - Architect verification；changed-file deslop；post-deslop re-run tests。

## 6. Acceptance Criteria

- [ ] `cacheable_prefix_hash` 在相同 stable prefix、不同 user/history/tool result 下完全一致；stable prefix 内容改变时变化。
- [ ] stable prefix segment 出现动态字段污染时 `PromptEnvelopeRenderError.reason == "stable_prefix_dynamic_pollution"`，错误 details 不含 raw prompt。
- [ ] `prompt_render_metrics` 覆盖 prefix hash/tokens、first dynamic segment、final input budget/tokens、history budget/tokens、preflight retry、history compression retry、trim reason summary、role fallback count。
- [ ] provider cache hint 默认关闭；unsupported provider no-op；supported+enabled 才向 request options 写配置驱动 hint。
- [ ] safe metadata / audit payload 不含 raw prompt、api_key、base_url、secret、DSN、token。
- [ ] main-agent / profile render events 仍是 audit-only，不出现在 frontend SSE。
- [ ] 所有 targeted regression 通过；License Requirement 说明无依赖/许可变更。

## 7. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| 过度 content 扫描误伤稳定系统说明 | 只检测 stable prefix 的动态 segment name/security_role、metadata key、机器字段标记（如 `task_id` / `conversation_id` / `artifact_id` / `dependency_result`），避免泛自然语言误杀。 |
| provider hint shape 不统一 | 支持配置驱动 `provider_cache_capabilities.prompt_cache_hint`，默认使用 safe generic marker；unsupported/default disabled no-op。 |
| audit payload 增长 | metrics 保持小对象；segment audit 仍只含 hash/token/trim/safe metadata。 |
| 运行时伪造 provider capability | capability 只影响 provider request hint/audit，不影响安全排序或 correctness。 |

## 8. Available-Agent-Types Roster / Staffing Guidance

- `executor`（medium）：默认实现与测试修改。
- `test-engineer`（medium）：如 targeted regression 面扩大，可并行补 test review。
- `architect`（high）：Ralph 结束前强制复核安全/观测/provider 兼容性。
- `code-simplifier` / `ai-slop-cleaner`：changed-file scope deslop。
- `$team` launch hint（仅必要时）：`$team --prompt "Implement Prompt Envelope P7 from .omx/plans/prd-20260529-prompt-envelope-p7-cache-observability.md" --workers 3`；Team verification path 必须回传 core/client/main-agent/API tests 与 no-raw audit evidence。
- Ralph fallback：本任务已由用户显式指定 `$ralph`，因此采用单-owner 持续验证闭环，不默认切到 Ultragoal。

## 9. Verification Commands

```bash
conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope tests.orchestration.test_prompt_profiles tests.integrations.test_llm_client tests.integrations.test_llm_runtime tests.capabilities.main_agent.test_main_agent_workflow_and_executor tests.api.test_main_agent_llm
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m compileall src tests
python -m compileall src tests
python - <<'PY'
# optional payload no-raw scanner in tests is preferred; command left as smoke hook.
PY
git diff --check
```

## 10. Plan Changelog

- 2026-05-29：创建 P7 Ralph implementation plan；按 PRD/当前代码证据确定 core detector + metrics + config-driven provider hint 方案。
