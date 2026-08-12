# 用户级 MCP Gateway 运行手册

## 启用前置

用户级 MCP 默认关闭。启用实例必须同时配置：

- `MAF_USER_MCP_ENABLED=true`
- `MCP_CREDENTIAL_KEY_FILE=/run/secrets/mcp-credential-key`
- `MAF_USER_MCP_MAX_ACTIVE_CALLS=<正整数>`
- `MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES=<正整数>`

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
3. 启用 `MAF_USER_MCP_ENABLED` 后滚动发布；确认 sentinel 校验、health attempt 与 scope lease 指标正常。
4. 旧全局 MCP Runtime 仍保持原执行链，不把用户配置注册到 CapabilityRegistry。

## 故障与回滚

- 回滚旧版本时保留 `mcp_credential_key_validation` 表和记录；不得重建或覆盖 sentinel。
- 主密钥丢失后已有凭据不可恢复：更换服务器侧凭据并要求用户重新填写。
- DELETE 返回 202 表示仍有 health/scope lease；tombstone 已禁止新调用，协调器会在 lease 释放或过期后物理删除。
- PostgreSQL NOTIFY 仅加速本地取消；数据库 lease/version CAS 才是安全事实源。
- 临时磁盘低于水位时新调用返回 `mcp_capacity_unavailable`；已接受调用不会按结果大小截断。
