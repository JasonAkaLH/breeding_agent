# MAF 统一根密钥与领域子密钥设计

日期：2026-08-14

状态：已批准，待实施

适用范围：`main` 分支、首次开发环境部署、CP7-A 单机 Docker Compose

## 1. 目标

部署只维护一个固定根密钥。根密钥不直接参与 AES、HMAC 或其他业务密码学操作；所有业务用途都通过带版本、不可混用的领域标签派生独立子密钥。

本设计同时满足：

- 容器重建、服务重启和重复部署不会改变任何子密钥；
- 用户 MCP 凭据不因部署重新配置；
- 登录 token 可以独立刷新，不影响 MCP 凭据；
- 一个领域的密文、摘要或子密钥不能在另一个领域复用；
- 数据库和镜像都不保存根密钥或派生子密钥。

当前尚未进行首次部署，因此不保留旧 `MCP_CREDENTIAL_KEY_FILE` 密文格式，不实现旧密钥迁移、双读或回退。

## 2. 明确不做

- 不为每个用户创建独立数据加密密钥表。
- 不把根密钥、根密钥 Hash、派生子密钥或可逆密钥材料写入数据库。
- 不直接复用根密钥执行 AES-GCM、token HMAC 或审计 HMAC。
- 不支持运行时根密钥轮换、双密钥窗口或历史密文重加密。
- 不把 LLM provider key、用户 MCP token 等外部服务凭据视为根密钥；它们仍是被加密的数据。
- 不修改 CP7-B 退役授权门。

## 3. 唯一部署密钥

宿主机只提供：

```text
MAF_MASTER_KEY_FILE_HOST=/absolute/path/to/maf-master.key
```

Compose 只读挂载为：

```text
MAF_MASTER_KEY_FILE=/run/secrets/maf-master.key
```

密钥文件契约：

- UTF-8/ASCII 单行标准 Base64；
- 解码后精确 32 bytes；
- 只允许一个可选末尾换行；
- 拒绝 Hex、自动补位、空白折叠、符号链接和非普通文件；
- 开发环境权限只接受 `0400` 或 `0600`，生产合同为精确 `0400`；
- 不进入 Git、Docker build context、镜像、普通配置、日志或审计 payload；
- 启动后不得自动生成、覆盖或修改该文件。

旧入口 `MCP_CREDENTIAL_KEY_FILE_HOST`、`MCP_CREDENTIAL_KEY_FILE` 和 `MAF_AUTH_TOKEN_HASH_SECRET` 不再构成密钥权威。首次部署直接拒绝这些旧入口，避免不同实例使用不同密钥来源。

## 4. 派生算法

统一使用 HKDF-SHA256：

```text
child_key = HKDF-SHA256(
  input_key_material = root_key,
  salt = fixed application salt,
  info = exact domain label,
  length = 32 bytes,
)
```

固定 application salt 为：

```text
maf/master-key-domain-derivation/v1
```

`info` 必须使用下表的精确 ASCII 标签。标签大小写敏感，不允许调用方自定义、拼接用户输入或省略版本。

| 领域 | 精确标签 | 用途 |
|---|---|---|
| MCP credential | `maf/mcp-credential-aes-gcm/v1` | 用户 MCP Server API key、bearer token、静态认证 Header 的 AES-256-GCM 加密 |
| MCP recovery | `maf/mcp-recovery-aes-gcm/v1` | MCP request state、远端 Task ID 与恢复私有状态的 AES-256-GCM 加密 |
| Auth token | `maf/auth-token-hmac-sha256/v1` | 登录 token 的 HMAC-SHA256 存储指纹 |
| MCP audit reference | `maf/mcp-audit-reference-hmac/v1` | owner、调用与 rollout 审计安全引用 |
| Key validation | `maf/key-validation-aes-gcm/v1` | 数据库 sentinel 的 AES-256-GCM create-or-verify |

派生器只能通过闭合枚举返回上述子密钥，不能提供任意字符串派生接口。子密钥只保存在持有它的最小运行时对象中，不暴露到 API、repr、日志或错误信息。

## 5. 组件边界

### 5.1 `MasterKeyDeriver`

负责一次安全读取根密钥、校验文件合同并按闭合领域派生子密钥。加载完成后，其他组件不得重新读取密钥文件。

### 5.2 MCP credential cipher

只接收 MCP credential 子密钥。AAD 继续绑定：

```text
owner_user_id + server_id + encryption_version
```

用户凭据继续存储在现有 owner-scoped Server 存储边界中：

- `credential_ciphertext`
- `credential_nonce`
- `encryption_version`
- `credential_updated_at`

查询 API 仍只返回 `credential_configured`，不返回明文、密文、Nonce 或摘要。

### 5.3 MCP recovery cipher

request state、远端 Task ID 和其他恢复私有数据改用 MCP recovery 子密钥。credential 密文不得由 recovery cipher 解密，反之亦然。

### 5.4 登录 token

`UsernameTokenService` 只接收 Auth token 子密钥，并用它计算 HMAC-SHA256。用户刷新 token 时只生成新 token、更新数据库 HMAC 和认证 generation，不修改根密钥或子密钥。

相同根密钥重启后，尚未刷新或注销的登录 token 保持有效。变更根密钥不属于本阶段支持的 token 刷新操作。

### 5.5 审计安全引用

审计 owner/call 安全引用只使用 MCP audit reference 子密钥，不再从 MCP credential key 二次派生，也不共享 credential cipher 实例。

### 5.6 密钥 sentinel

数据库使用新的单例表 `maf_master_key_validation` 保存 create-or-verify sentinel，并使用 Key validation 子密钥。sentinel 只保存随机 Nonce、固定验证明文的密文和格式版本。当前尚未部署，因此 fresh schema 和 runtime 不再使用旧 `mcp_credential_key_validation`；本设计不读取、迁移或回退旧表。

启动顺序固定为：

1. 安全读取根密钥；
2. 派生闭合领域子密钥；
3. 初始化数据库连接；
4. 原子创建或验证 sentinel；
5. sentinel 成功后才允许 runtime 取得 Ready。

根密钥错误、文件缺失、文件合同不满足、sentinel 缺失且数据库不可写或 sentinel 解密失败时全部 fail closed。

## 6. Docker Compose

backend 只接受一个密钥挂载：

```yaml
environment:
  MAF_MASTER_KEY_FILE: /run/secrets/maf-master.key
volumes:
  - ${MAF_MASTER_KEY_FILE_HOST:?MAF_MASTER_KEY_FILE_HOST is required}:/run/secrets/maf-master.key:ro
```

Compose 不再声明：

```text
MCP_CREDENTIAL_KEY_FILE
MCP_CREDENTIAL_KEY_FILE_HOST
MAF_AUTH_TOKEN_HASH_SECRET
```

Runtime Sidecar 和 frontend 不挂载根密钥。只有 backend 读取该文件。

## 7. 首次部署与数据行为

本项目尚未部署，首次部署直接创建新 sentinel 和新密文，不执行旧格式迁移。

部署后：

- 固定根密钥文件必须和数据库分开备份；
- 丢失根密钥后，已存 MCP 凭据和恢复私有数据不可恢复；
- 更换容器、镜像或 Compose project 不得改变根密钥文件；
- 登录 token 的正常刷新不要求修改根密钥；
- 未经单独设计和批准，不得更换根密钥或领域标签版本。

## 8. 错误边界

所有密钥错误只暴露固定安全错误码，不包含路径以外的文件内容、Base64 文本、子密钥、密文或解密异常细节：

- `maf_master_key_file_missing`
- `maf_master_key_file_unavailable`
- `maf_master_key_file_invalid_type`
- `maf_master_key_file_invalid_permissions`
- `maf_master_key_file_invalid_format`
- `maf_master_key_invalid_length`
- `maf_master_key_legacy_authority_configured`
- `maf_master_key_validation_failed`
- `maf_key_domain_invalid`
- 现有领域解密错误继续使用各自的安全错误码。

## 9. 自动验收

### 9.1 派生单元测试

- 相同根密钥、salt 和标签稳定得到相同 32-byte 子密钥；
- 任意两个领域的子密钥不同；
- 不同根密钥的所有子密钥不同；
- 未知标签无法通过公开 API 派生；
- 根密钥对象和子密钥对象的 `repr` 不包含密钥材料。

### 9.2 密码学隔离测试

- credential 密文只能由 credential 子密钥解密；
- recovery 密文只能由 recovery 子密钥解密；
- credential/recovery 密文互换必须失败；
- Auth token 子密钥不能验证审计 HMAC，反之亦然；
- AES-GCM Nonce 继续保持每次写入唯一。

### 9.3 启动与重启测试

- 同一根密钥重启后可以验证 sentinel、解密用户 MCP 凭据并验证既有登录 token；
- 更换根密钥后 sentinel 校验失败，backend 不得 Ready；
- 缺少、权限错误、格式错误或长度错误的根密钥文件均 fail closed；
- 任一旧密钥环境变量存在时 fail closed；
- 根密钥只读取一次，启动后替换路径内容不影响当前进程且不得静默重载。

### 9.4 Compose 合同测试

- 只存在 `MAF_MASTER_KEY_FILE_HOST` 一个宿主密钥入口；
- 缺少该变量时 `docker compose config` 失败；
- backend 只读挂载固定容器路径；
- Sidecar 和 frontend 不得到该挂载；
- README 不再要求旧密钥变量。

## 10. 人工验收

1. 生成一次 32-byte 随机根密钥文件并启动三服务 Compose。
2. 用户登录并配置一个需要 token 的 MCP Server。
3. 完成一次 MCP 调用。
4. 重启 backend 和 Runtime Sidecar，不修改根密钥文件。
5. 验证原登录状态仍有效，原 MCP Server 无需重新配置且可以再次调用。
6. 刷新登录 token，验证 MCP 配置和调用不受影响。
7. 停止服务后使用错误根密钥启动，验证 backend 无法 Ready。
8. 恢复原根密钥，验证服务和已有 MCP 凭据恢复可用。

## 11. 完成定义

同时满足以下条件才算实施完成：

- Compose 只要求一个根密钥文件；
- 根密钥不直接用于业务 AES 或 HMAC；
- 五个领域使用闭合、版本化、互不相同的子密钥；
- 用户 MCP 凭据继续加密入库并按 owner/server 隔离；
- 登录 token 刷新不影响 MCP 凭据；
- 同根密钥重启保持登录与 MCP 数据可用；
- 错根密钥或旧密钥入口全部 fail closed；
- 自动验收通过；
- CP7-B 仍停在人工授权门。
