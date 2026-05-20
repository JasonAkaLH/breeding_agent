# Slash Skill Command PRD Set

本目录记录前端业务对话台 slash command 能力的分阶段 PRD。总设计来源为：

- `docs/superpowers/specs/2026-05-20-frontend-slash-skill-command-design.md`

## 拆分原则

Slash command 能力分成两个独立交付面：

1. **前端 Slash Skill Command MVP**：让用户能通过 `/` picker 或 `/skill args` 显式强制调用 public Skill，并保证上传文件 metadata 与结构化 `capability_id` 同时提交。
2. **Pending Skill Context Continuation**：当强制调用的信息不足且 Skill 无法自行多轮补全时，后端持久化待补全上下文，并在下一轮普通输入中继续同一个 Skill。

这样前端 MVP 可以独立上线，不被更复杂的后端持久化语义阻塞。

## PRD 列表

- [01 - Frontend Slash Skill Command MVP](01-frontend-slash-skill-command-mvp.md)
- [02 - Pending Skill Context Continuation](02-pending-skill-context-continuation.md)

## 推荐实施顺序

1. 先实施 PRD 01，完成 slash 强制调用主路径。
2. 再实施 PRD 02，补齐信息不足后的跨轮续接语义。

## 不在当前专题范围

- 通用 command framework。
- `/clear`、`/new`、`/help` 等内置命令。
- 富文本 token composer。
- 前端 Skill 参数校验。
