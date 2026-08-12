# 用户级 MCP Gateway 运行手册

## 启用前置

用户级 MCP 默认关闭。新部署使用阶段三 canonical 开关，不再使用旧布尔路由开关推导 `enforce`：

- `MCP_USER_SCOPED_GATEWAY_ENABLED=true`
- `MCP_ROUTING_MODE=shadow|enforce`
- `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=true|false`
- `MCP_CREDENTIAL_KEY_FILE=/run/secrets/mcp-credential-key`
- `MAF_USER_MCP_MAX_ACTIVE_CALLS=<正整数>`
- `MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES=<正整数>`

`MAF_USER_MCP_ENABLED` 和 `MAF_USER_MCP_ROUTING_ENABLED` 仅保留兼容边界；它们与 canonical 配置冲突时启动会 fail closed，`MAF_USER_MCP_ROUTING_ENABLED=true` 且缺少 `MCP_ROUTING_MODE` 也会拒绝启动。完整的模式组合、实例准入、证据、迁移与回滚操作见 [用户级 MCP Phase 3 灰度与下线手册](./user-mcp-phase3-rollout.md)。

主密钥文件是一行标准 Base64，解码后恰好 32 bytes，生产权限为 `0400`；应用不会生成、覆盖或轮换该文件。所有实例必须挂载同一份密钥。启动时应用会原子 create-or-verify 数据库 sentinel，密钥缺失、权限/长度不合法、数据库不可用或 sentinel 无法解密都会阻止 Ready。

可选配置：

- `MAF_USER_MCP_ALLOWLIST_DOMAINS`：逗号分隔的企业域名。
- `MAF_USER_MCP_ALLOWLIST_CIDRS`：逗号分隔、严格格式的企业 CIDR。
- `MAF_USER_MCP_TEMPORARY_RESULT_ROOT`：任务临时结果根目录。
- `MAF_USER_MCP_MEMORY_RESULT_THRESHOLD_BYTES`：内存切换临时文件的阈值，默认 1 MiB；它不是结果上限。
- `MAF_USER_MCP_ORPHAN_SAFE_AGE_SECONDS`：重启清理孤儿任务目录的安全年龄，默认 3600 秒。

## 发布顺序

1. 先发布向后兼容的新增数据库表。
2. 为全部实例挂载相同的只读主密钥文件，并显式配置容量门禁。
3. 先以 canonical `shadow` 组合滚动发布；确认 sentinel 校验、rollout instance admission、health attempt 与 scope lease 正常。
4. 旧全局 MCP Runtime 仍保持原执行链，不把用户配置注册到 CapabilityRegistry。
5. 仅在 Phase 3 证据门禁和审批通过后扩大 `enforce`；不得把本地回归通过等同于生产 CP-7 完成。

## 故障与回滚

- 回滚旧版本时保留 `mcp_credential_key_validation` 表和记录；不得重建或覆盖 sentinel。
- 主密钥丢失后已有凭据不可恢复：更换服务器侧凭据并要求用户重新填写。
- DELETE 返回 202 表示仍有 health/scope lease；tombstone 已禁止新调用，协调器会在 lease 释放或过期后物理删除。
- PostgreSQL NOTIFY 仅加速本地取消；数据库 lease/version CAS 才是安全事实源。
- 临时磁盘低于水位时新调用返回 `mcp_capacity_unavailable`；已接受调用不会按结果大小截断。
- 旗标回滚只影响新 Task；在途 Task 保持创建时固化的路径，普通调用状态不明时收敛为 `unknown`，不换链路重放。
- 任何删除旧 Runtime 的操作都必须等待生产 CP-7 D2 独立观察窗通过；关闭 legacy assembly 与物理删除不得在同一步完成。
