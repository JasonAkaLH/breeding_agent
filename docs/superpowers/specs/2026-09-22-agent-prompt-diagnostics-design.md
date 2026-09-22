# 开发环境单任务 Prompt 诊断

## 已确认的范围

只观察 Agent 最终交给 SDK 的请求与对应响应，不修改 Prompt、消息、工具结果、模型参数或会话记忆，不新增数据库表或外部请求。用于区分事实约束未进入请求与模型在收到约束后仍产生无依据解释。

## 设计与实施计划

1. 在流式、非流式 `completions.create` 前检查请求，并在收到完成响应后记录对应 ID。
2. 默认关闭，且只在 `MAF_API_ENV=dev` 启用。临时控制文件携带随机 capture ID 和最多 600 秒有效期；首次符合 AgentRun 格式的请求以 exclusive create 锁定所属 Task。同任务后续采样、审批恢复和协议重试沿用该 capture，其他任务和启动模型门禁不记录。
3. 逐条匹配完整的 `MAIN_AGENT_FACTUAL_GROUNDING_LINES`，只承认 system 消息中的匹配，记录命中的消息索引、消息角色顺序、规则和 system 消息 SHA-256。考虑 SDK `extra_body.messages` 覆盖，并用实际 SDK 加 MockTransport 验证序列化结果。
4. 请求/响应以 `capture_id + request_id + attempt` 关联；响应另记 provider response ID、finish reason 和 tool call count。只输出固定元数据，不输出任何消息正文、工具参数、响应正文或认证信息。
5. 文件不存在、过期、格式错误、读写或日志失败时不阻断模型调用。诊断不会改变既有重试语义。
6. 验证默认关闭、开发环境限制、任务隔离、缺失/部分规则、错误角色、SDK 覆盖、流式/非流式、协议重试和无正文泄露；发布开发镜像后验证默认未启用。

## 开启：捕获下一次主 Agent 调用所属任务

先确认开发容器已运行包含此功能的新镜像。确保当前没有其他运行中的任务，执行下列命令后立即新建对话测试。开关无需重启；若其他用户先触发请求，会锁定他们的任务，需按日志中的 task_id 确认并重新开启。

```bash
docker exec -i breeding-agent-backend-dev python - <<'PY'
import json, os, time, uuid
from pathlib import Path
p = Path('/app/runtime/agent-prompt-diagnostic.json')
p.parent.mkdir(parents=True, exist_ok=True)
capture_id = uuid.uuid4().hex
temporary = p.with_name(f'.agent-prompt-diagnostic-{capture_id}.json')
with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
    json.dump({'capture_id': capture_id, 'expires_at': time.time() + 600}, f)
os.replace(temporary, p)
print('enabled for next task, 10 minutes; capture_id=' + capture_id)
PY
```

在新对话中输入 `lims中的总项目数`。如果出现 MCP 审批，正常批准，等待最终回答。该任务的工具前/后各轮都会被记录；后续新一轮用户提问会创建另一个 Task，需重新开启才能捕获。

## 查看与判读

```bash
docker logs --since 10m breeding-agent-backend-dev 2>&1 | grep 'agent_prompt_diagnostic'
```

找到 `agent_prompt_diagnostic.response` 中 `finish_reason=stop`、`tool_call_count=0` 的最终回答响应，再按相同 request_id 和 attempt 找到 request。六项 `grounding_rules_in_system` 均为 true 表示该次 SDK 参数中的 system 消息包含全部规则；`rule_system_message_indices` 显示实际位置。标题匹配不算完整规则匹配。

边界明确标为 `sdk_arguments`：它证明客户端交给 SDK 的消息内容，不能证明服务端或中间网关没有改写，也不能追溯开启前的历史请求。实际 HTTP 序列化通过 SDK MockTransport 测试覆盖，若仍怀疑网关需按 provider response_id 核对网关日志。

日志使用 warning 级别以确保开发容器默认日志配置可见；此事件本身不是业务异常。如果只有 request 没有 response，不应据此认定模型违反约束，需结合请求失败或流中断情况排查。

## 关闭

```bash
docker exec breeding-agent-backend-dev python -c "from pathlib import Path; Path('/app/runtime/agent-prompt-diagnostic.json').unlink(missing_ok=True)"
```

10 分钟后也会自动停止捕获。关闭停止新的请求记录，已经记录请求的在途调用仍会写出对应响应。随机 capture 对应的 `.task` 文件只保存所选任务 ID，权限 0600，不包含业务内容。
