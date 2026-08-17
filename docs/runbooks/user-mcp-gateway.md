# 用户级 MCP Gateway 运行手册

## 启用前置

用户级 MCP 默认关闭。新部署使用阶段三 canonical 开关，不再使用旧布尔路由开关推导 `enforce`：

- `MCP_USER_SCOPED_GATEWAY_ENABLED=true`
- `MCP_ROUTING_MODE=shadow|enforce`
- `MCP_LEGACY_GLOBAL_RUNTIME_ENABLED=true|false`
- `MAF_MASTER_KEY_FILE=/run/secrets/maf-master.key`
- `MAF_USER_MCP_MAX_ACTIVE_CALLS=<正整数>`
- `MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES=<正整数>`

`MAF_USER_MCP_ENABLED` 和 `MAF_USER_MCP_ROUTING_ENABLED` 仅保留兼容边界；它们与 canonical 配置冲突时启动会 fail closed，`MAF_USER_MCP_ROUTING_ENABLED=true` 且缺少 `MCP_ROUTING_MODE` 也会拒绝启动。完整的模式组合、实例准入、证据、迁移与回滚操作见 [用户级 MCP Phase 3 灰度与下线手册](./user-mcp-phase3-rollout.md)。

根密钥文件是一行 canonical Base64，解码后恰好 32 bytes，权限为 `0400` 或 `0600`；应用不会生成、覆盖或轮换该文件。所有实例必须挂载同一份固定根密钥。根密钥不直接参与业务加密，而是为 MCP credential、MCP recovery、Auth token、MCP audit reference 和 key validation 派生互相隔离的领域子密钥。启动时应用会原子 create-or-verify `maf_master_key_validation` sentinel，文件缺失、权限/长度不合法、数据库不可用或 sentinel 无法解密都会阻止 Ready。

Endpoint 策略：

- 任意公网 HTTP/HTTPS Endpoint 均可配置；公网 HTTP 记录 `plaintext_http`，携带认证时同时记录 `credential_over_plaintext_http` 安全布尔值。
- 私网、回环、链路本地、云元数据、多播、保留和未指定地址始终拒绝，不提供管理员域名/CIDR 白名单例外。
- DNS rebinding、实际连接 IP 偏离、跨 Origin 重定向和 HTTPS 降级继续失败关闭。

可选配置：
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

- 首次部署候选失败时不得让旧代码读取新领域密文；保留诊断副本后回退代码并重建空开发 SQLite。任何数据库删除或替换都需要针对精确目标单独批准。
- 主密钥丢失后已有凭据不可恢复：更换服务器侧凭据并要求用户重新填写。
- DELETE 返回 202 表示仍有 health/scope lease；tombstone 已禁止新调用，协调器会在 lease 释放或过期后物理删除。
- PostgreSQL NOTIFY 仅加速本地取消；数据库 lease/version CAS 才是安全事实源。
- 临时磁盘低于水位时新调用返回 `mcp_capacity_unavailable`；已接受调用不会按结果大小截断。
- 旗标回滚只影响新 Task；在途 Task 保持创建时固化的路径，普通调用状态不明时收敛为 `unknown`，不换链路重放。
- 任何删除旧 Runtime 的操作都必须等待生产 CP-7 D2 独立观察窗通过；关闭 legacy assembly 与物理删除不得在同一步完成。
