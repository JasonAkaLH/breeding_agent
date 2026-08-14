# MAF 统一根密钥与领域子密钥设计

日期：2026-08-14

状态：已实施，自动验收通过，待人工验收

适用范围：`main` 分支、首次开发环境部署、CP7-A 单机 Docker Compose 的在线应用运行时

## 1. 目标

在线应用运行时只维护一个固定根密钥。根密钥不直接参与 AES、HMAC 或其他业务密码学操作；所有在线运行时用途都通过带版本、不可混用的领域标签派生独立子密钥。

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
- 不把 production rollout evidence attestation、artifact provenance、operator/validator keyring 或 migration evidence signing key 纳入在线根密钥。这些离线信任密钥继续独立保存，且不得挂载给 backend。
- 不修改 CP7-B 退役授权门。

### 2.1 受影响系统与当前证据

| 系统 | 当前状态 | 本设计要求 |
|---|---|---|
| 用户 MCP credential | `CredentialCipher` 使用一个 AES key 加密用户凭据 | 改用 MCP credential 领域子密钥 |
| MCP recovery | 与 credential 共用同一个 `CredentialCipher` | 改用独立 MCP recovery 领域子密钥 |
| 登录 token | `UsernameTokenService` 使用独立文本 secret；缺省时每进程随机生成 | 改用稳定 Auth token 领域子密钥 |
| MCP audit/shadow digest | 从 credential key 二次派生 | 改用独立 MCP audit reference 领域子密钥 |
| 密钥 sentinel | `mcp_credential_key_validation` 只验证旧 credential key | 改为 `maf_master_key_validation`，使用 Key validation 领域子密钥 |
| Compose | 单独挂载 MCP credential key | 只挂载一个在线 runtime 根密钥 |
| 离线 rollout/provenance | 使用独立 attestation/signing key 或 keyring | 保持独立，不进入本设计派生树 |

现有 `cryptography==46.0.7` 已提供 AES-GCM 与 HKDF-SHA256；本设计不新增密码学依赖。

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
- 编码后文件精确为 44 bytes，或带一个末尾换行时为 45 bytes；读取不得超过 45 bytes；
- 拒绝 Hex、自动补位、空白折叠、任一路径组件为符号链接、非普通文件和硬链接数不等于 `1`；
- 开发 Compose 权限只接受 `0400` 或 `0600`；宿主 UID 到容器 UID 的映射具有平台差异，因此本阶段不以 owner UID 作为信任判断；
- 逐组件以 `O_DIRECTORY|O_NOFOLLOW` 打开父目录，最终文件以 `O_RDONLY|O_CLOEXEC|O_NOFOLLOW` 打开；
- 读取前后 `fstat` 的 device、inode、size、mtime 和 ctime 必须一致，路径最终组件仍须指向同一 device/inode；
- 不进入 Git、Docker build context、镜像、普通配置、日志或审计 payload；
- 启动后不得自动生成、覆盖或修改该文件。

### 3.1 闭合密钥权威

| 入口 | 合同 |
|---|---|
| `MAF_MASTER_KEY_FILE_HOST` | 仅供 Compose 在宿主机解析；缺失时 `docker compose config` 失败 |
| `MAF_MASTER_KEY_FILE` | backend 唯一文件权威，精确为 `/run/secrets/maf-master.key` |
| `MCP_CREDENTIAL_KEY_FILE_HOST` | 环境中只要存在即拒绝，包括空字符串 |
| `MCP_CREDENTIAL_KEY_FILE` | 环境中只要存在即拒绝，包括空字符串 |
| `MAF_AUTH_TOKEN_HASH_SECRET` | 环境中只要存在即拒绝，包括空字符串 |
| `MAF_AUTH_TOKEN_HASH_SECRET_REQUIRED` | 环境中只要存在即拒绝，包括空字符串 |
| `build_api_runtime(auth_token_hash_secret=...)` | 从公开 runtime 构造签名删除，不保留别名或回退 |
| `build_api_runtime(auth_token_hash_secret_required=...)` | 从公开 runtime 构造签名删除 |
| `build_api_runtime(user_mcp_credential_key_file=...)` | 替换为唯一的 `master_key_file` 参数 |
| 测试密钥注入 | 只允许显式 `master_key_bytes` 测试 seam；与文件参数互斥，不读取环境变量 |
| legacy migration CLI | `--credential-key-file` 删除并替换为 `--master-key-file`；CLI 必须用同一派生器取得 MCP credential 子密钥，不保留旧参数别名 |

Runtime、CLI 和测试不得直接接收领域子密钥。除闭合测试 seam 外，所有领域子密钥只能由本进程从唯一根密钥派生。

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

负责一次安全读取根密钥、校验文件合同并按闭合领域派生子密钥。公开 API 只接受 `MasterKeyDomain` 闭合枚举，不接受自由字符串标签。加载完成后，其他组件不得重新读取密钥文件。

生产装配必须一次性派生本进程需要的全部子密钥，然后把每个子密钥交给其唯一类型。Python 无法保证不可变 `bytes` 的确定性内存清零；本设计只保证根密钥和子密钥不会进入持久化、序列化、日志、异常文本或公开属性，不宣称进程内存可验证零化。

### 5.2 类型隔离

实现必须提供下列互不继承、互不接受其他领域对象的类型：

| 类型 | 唯一输入 | 唯一职责 |
|---|---|---|
| `MCPCredentialCipher` | MCP credential 子密钥 | 用户 MCP credential AES-GCM 加解密 |
| `MCPRecoveryCipher` | MCP recovery 子密钥 | request state、远端 Task ID 和恢复私有状态 AES-GCM 加解密 |
| `AuthTokenHasher` | Auth token 子密钥 | 登录 token HMAC-SHA256 |
| `MCPAuditReferenceSigner` | MCP audit reference 子密钥 | owner/call/shadow/rollout 安全引用 HMAC-SHA256 |
| `MasterKeySentinelCipher` | Key validation 子密钥 | sentinel create-or-verify |

不得保留可同时执行 credential、recovery、audit 和 sentinel 的通用 `CredentialCipher(key)`，也不得提供公开 `derive(label: str)` 或 `cipher_for(name: str)` 接口。Runtime 通过显式命名参数装配这些类型；类型错接必须在构造阶段失败，不能等到真实密文解密时才发现。

### 5.3 MCP credential cipher

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

### 5.4 MCP recovery cipher

request state、远端 Task ID 和其他恢复私有数据改用 MCP recovery 子密钥。credential 密文不得由 recovery cipher 解密，反之亦然。

### 5.5 登录 token

`UsernameTokenService` 只接收 `AuthTokenHasher`，不再接受自由文本 `secret` 或自行生成进程随机 pepper。用户刷新 token 时只生成新 token、更新数据库 HMAC 和认证 generation，不修改根密钥或子密钥。

相同根密钥重启后，尚未刷新或注销的登录 token 保持有效。变更根密钥不属于本阶段支持的 token 刷新操作。

### 5.6 审计安全引用

审计 owner/call、shadow catalog digest 和 migration target credential digest 只使用 `MCPAuditReferenceSigner`，不再从 MCP credential key 二次派生，也不共享 credential cipher 实例。

### 5.7 密钥 sentinel

数据库使用新的单例表 `maf_master_key_validation` 保存 create-or-verify sentinel，并使用 `MasterKeySentinelCipher`。权威 schema 精确为：

| 字段 | 合同 |
|---|---|
| `singleton_key` | integer primary key，固定为 `1`，数据库 CHECK 拒绝其他值 |
| `validation_nonce` | 12-byte AES-GCM Nonce |
| `validation_ciphertext` | 固定验证明文的 AES-GCM 密文与 tag，不含根密钥摘要 |
| `derivation_version` | integer，首版固定为 `1` |
| `created_at` | 非空 UTC 时间 |

SQLite 和 PostgreSQL fresh schema 都必须创建同一逻辑约束。当前尚未部署，因此 runtime 不再使用旧 `mcp_credential_key_validation`；fresh schema 不创建旧表，已有本地开发数据库中遗留的旧表可以保留为不可达孤儿表，但不得读取、迁移、写入或作为回退。

并发首次启动采用 create-or-get：SQLite 在写事务中执行 insert-or-ignore 后读取唯一 winner；PostgreSQL 使用 `INSERT ... ON CONFLICT DO NOTHING` 后读取同一行。胜出或落败实例都必须用本次派生的 Key validation 子密钥验证 winner；不能因为自身 insert 未执行而跳过验证。

启动顺序固定为：

1. 安全读取根密钥；
2. 派生闭合领域子密钥；
3. 初始化数据库连接；
4. 原子创建或验证 sentinel；
5. sentinel 成功后才构造认证和 MCP 请求服务；
6. sentinel 成功后才允许 runtime 取得 Ready 和接收请求。

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

`MAF_MASTER_KEY_FILE_HOST` 不得进入 backend environment；它只参与 Compose bind mount 展开。backend environment 只能看到固定容器路径 `MAF_MASTER_KEY_FILE`。

## 7. 首次部署与数据行为

本项目尚未部署，首次部署直接创建新 sentinel 和新密文，不执行旧格式迁移。

部署后：

- 固定根密钥文件必须和数据库分开备份；
- 丢失根密钥后，已存 MCP 凭据和恢复私有数据不可恢复；
- 更换容器、镜像或 Compose project 不得改变根密钥文件；
- 登录 token 的正常刷新不要求修改根密钥；
- 未经单独设计和批准，不得更换根密钥或领域标签版本。

### 7.1 根密钥泄露与恢复边界

单一在线根密钥泄露意味着五个在线领域的机密性或真实性都必须视为失效。当前阶段不提供不停机轮换或自动重加密。处置合同为：

1. 停止 backend，阻断新的登录和 MCP 调用；
2. 撤销所有登录 token；
3. 废弃已存 MCP credential 和 recovery 私有状态；
4. 生成新的根密钥并重建 `maf_master_key_validation` sentinel；
5. 用户重新登录并重新填写 MCP credential；
6. 在重新验收前不得恢复 Ready。

离线 rollout/provenance/operator 信任密钥不由该根密钥派生，因此在线根密钥泄露不自动使既有离线签名信任链失效。反之，离线签名密钥泄露也不得获得在线 credential 解密能力。

## 8. 错误边界

所有密钥错误只暴露固定安全错误码，不包含路径以外的文件内容、Base64 文本、子密钥、密文或解密异常细节：

- `maf_master_key_file_missing`
- `maf_master_key_file_unavailable`
- `maf_master_key_file_invalid_type`
- `maf_master_key_file_invalid_permissions`
- `maf_master_key_file_invalid_format`
- `maf_master_key_invalid_length`
- `maf_master_key_legacy_authority_configured`
- `maf_master_key_validation_unavailable`
- `maf_master_key_mismatch`
- `maf_key_domain_invalid`
- 现有领域解密错误继续使用各自的安全错误码。

`validation_unavailable` 只表示数据库读写或 create-or-get 失败；已有 sentinel 无法由当前 Key validation 子密钥解密、格式错误或 derivation version 不匹配统一返回 `maf_master_key_mismatch`。错误不得回显数据库异常、Nonce 或密文。

## 9. 自动验收

### 9.1 派生单元测试

- 相同根密钥、salt 和标签稳定得到相同 32-byte 子密钥；
- 任意两个领域的子密钥不同；
- 不同根密钥的所有子密钥不同；
- 未知标签无法通过公开 API 派生；
- 根密钥对象和子密钥对象的 `repr` 不包含密钥材料。
- 运行时公开 API 不存在自由字符串派生入口。

### 9.2 密码学隔离测试

- credential 密文只能由 credential 子密钥解密；
- recovery 密文只能由 recovery 子密钥解密；
- credential/recovery 密文互换必须失败；
- Auth token 子密钥不能验证审计 HMAC，反之亦然；
- AES-GCM Nonce 继续保持每次写入唯一。
- 将任一领域类型接到其他领域构造参数时必须在构造阶段失败。

### 9.3 启动与重启测试

- 同一根密钥重启后可以验证 sentinel、解密用户 MCP 凭据并验证既有登录 token；
- 更换根密钥后 sentinel 校验失败，backend 不得 Ready；
- 缺少、权限错误、格式错误或长度错误的根密钥文件均 fail closed；
- 任一旧密钥环境变量存在时 fail closed；
- 任一旧 runtime 构造参数或旧 migration CLI 参数均被拒绝；
- 根密钥只读取一次，启动后替换路径内容不影响当前进程且不得静默重载。
- 两个实例并发首次启动时只能产生一个 sentinel，且两者都验证同一 winner 后才能 Ready。
- SQLite 关闭并重开后，同根密钥可验证 sentinel、登录 token、credential 和 recovery 密文。
- PostgreSQL fresh schema/仓库合同与 SQLite 保持静态等价；CP7-A 是单机 SQLite 开发部署，真实 PostgreSQL 执行和生产 rollout 不作为本阶段验收条件。

### 9.4 Compose 合同测试

- 只存在 `MAF_MASTER_KEY_FILE_HOST` 一个宿主密钥入口；
- 缺少该变量时 `docker compose config` 失败；
- backend 只读挂载固定容器路径；
- Sidecar 和 frontend 不得到该挂载；
- backend environment 不包含宿主文件路径；
- README 不再要求旧密钥变量。

### 9.5 文件信任测试

- 拒绝中间路径符号链接、最终符号链接、目录、FIFO 和硬链接；
- 拒绝超过 45 bytes、非 canonical Base64、额外空白和错误权限；
- 读取前后 inode、size 或时间身份变化时 fail closed；
- 文件只读取一次，派生完成后各业务组件不再访问路径。

### 9.6 工具与文档一致性

- legacy migration CLI 只接受 `--master-key-file` 并派生 credential/audit 子密钥；
- MCP credential PRD、Gateway runbook、Phase 3 rollout runbook、CP7-A lean design、README、Compose 和 CHANGELOG 使用同一变量名与领域定义；
- 离线 attestation/keyring/provenance 配置仍保持独立，且文档不得称其为在线根密钥子密钥。

## 10. 人工验收

1. 以安全随机源生成一次 32-byte 根密钥并写成 canonical Base64 文件；例如执行 `umask 177`、`openssl rand -base64 32 > /absolute/path/to/maf-master.key`，再将文件权限固定为 `0400` 或 `0600`。
2. 用户登录并配置一个需要 token 的 MCP Server。
3. 完成一次 MCP 调用。
4. 重启 backend 和 Runtime Sidecar，不修改根密钥文件。
5. 验证原登录状态仍有效，原 MCP Server 无需重新配置且可以再次调用。
6. 刷新登录 token，验证 MCP 配置和调用不受影响。
7. 停止服务后使用错误根密钥启动，验证 backend 无法 Ready。
8. 恢复原根密钥，验证服务和已有 MCP 凭据恢复可用。

## 11. 依赖与修改边界

| 边界 | 必须完成的修改 |
|---|---|
| 密钥加载/派生 | 新增闭合 `MasterKeyDomain`、安全文件读取和 HKDF-SHA256 派生器 |
| MCP credential/recovery/audit | 拆分当前多职责 `CredentialCipher`，更新所有调用方和 legacy migration 工具 |
| Auth | `UsernameTokenService` 改为只接收 `AuthTokenHasher`，删除文本 secret 和随机 pepper 路径 |
| Storage | 新增 `MAFMasterKeyValidation` contract、SQLite/PostgreSQL row/repository/schema，停用旧 sentinel contract |
| Runtime | 唯一 master key 装配、旧入口拒绝、sentinel-before-service/Ready |
| Deployment | Compose/README 改为单一根密钥文件挂载 |
| 文档 | 同步 MCP credential PRD、Gateway/Phase 3 runbook、CP7-A lean design、CHANGELOG |
| Tests | 更新旧 fixture，增加领域隔离、并发、重启、文件 hostile、Compose 和 CLI 合同测试 |

直接单元测试可以通过 `master_key_bytes` seam 使用固定测试根密钥，但业务代码、集成测试和部署测试必须覆盖真实文件加载。测试密钥只能存在于测试进程和临时权限目录，不得加入 Compose 或示例生产配置。

## 12. 风险与已确认假设

| 项目 | 结论 |
|---|---|
| 首次部署 | 已由项目负责人确认；不存在需要保留的已部署 MCP 密文或旧 sentinel |
| 在线根密钥范围 | 已确认只覆盖在线应用运行时；离线签名与信任密钥独立 |
| 根密钥轮换 | 本阶段不支持；任何轮换必须另立迁移设计 |
| 泄露影响 | 五个在线领域全部视为失效，按第 7.1 节停机恢复 |
| 内存零化 | Python 不保证确定性零化；以最小对象持有、禁止序列化与进程退出回收为边界 |
| 开发文件 owner | Docker bind mount UID 映射跨平台不稳定，本阶段不以 owner UID 判定；以固定路径、mode、nofollow、nlink 和 inode 稳定性判定 |
| PostgreSQL 范围 | 保持 fresh DDL、repository contract 与静态测试一致；真实 PostgreSQL 和生产 rollout 不在 CP7-A 单机 SQLite 验收范围内 |

## 13. 完成定义

同时满足以下条件才算实施完成：

- Compose 只要求一个根密钥文件；
- 根密钥不直接用于业务 AES 或 HMAC；
- 五个领域使用闭合、版本化、互不相同的子密钥；
- 离线签名/信任密钥保持独立且不挂载给 backend；
- 用户 MCP 凭据继续加密入库并按 owner/server 隔离；
- 登录 token 刷新不影响 MCP 凭据；
- 同根密钥重启保持登录与 MCP 数据可用；
- 错根密钥或旧密钥入口全部 fail closed；
- 并发 sentinel、文件信任、跨领域误接线和重启恢复测试通过；
- 自动验收通过；
- CP7-B 仍停在人工授权门。
