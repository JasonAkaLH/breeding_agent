# Backend 外部运行时配置最小设计

状态：`approved`；待实施
日期：2026-08-30
目标分支：`main`

## 1. 目标

正式backend镜像不得包含仓库根目录本地`config.yaml`。服务器使用
`/data/peihai/config.yaml`作为配置authority，并在容器启动时只读挂载到现有默认路径`/app/config.yaml`。

## 2. 最小方案

采用运行时只读挂载：

1. `.dockerignore`增加根目录精确规则`/config.yaml`；
2. Dockerfile删除`COPY config.yaml ./config.yaml`；
3. `docker-compose.yml`通过必填宿主路径变量把配置只读挂载到`/app/config.yaml`；
4. README同步说明镜像不含配置，以及服务器挂载源为`/data/peihai/config.yaml`。

不采用构建期secret，因为最终镜像仍可能包含配置；不改成全环境变量注入，因为会扩大配置管理范围。

## 3. 验收

- Docker构建上下文排除根`config.yaml`，Dockerfile不再复制该文件；
- Compose配置把宿主配置以只读文件挂载到`/app/config.yaml`；
- `linux/amd64` backend `0.1.25`可成功构建；
- 不挂载配置时镜像文件系统不存在`/app/config.yaml`；
- 挂载权限合格的临时外部配置后，隔离SQLite backend `/api-doc`返回200；
- 本轮不修改Frontend、Runtime Sidecar、业务配置加载代码、schema、依赖或`prod`，也不推送backend，除非用户后续明确要求。

## 4. 回滚

回滚本次tracked commit可恢复旧构建合同；镜像库中已存在的tag不受影响。外部配置文件不由仓库代码创建、修改或删除。

License Requirement：仅调整Docker构建上下文、运行时文件挂载和文档；无新增依赖或许可变化。
