# Backend 外部运行时配置最小设计

状态：`published_main_not_deployed`；document-perfectization第二轮`100/100 Pass`
日期：2026-08-30
目标分支：`main`

## 1. 目标

正式backend镜像不得包含仓库根目录本地`config.yaml`。服务器使用
`/data/peihai/seedpilot_config_dev.yaml`作为配置authority，并在容器启动时只读挂载到现有默认路径`/app/config.yaml`。

## 2. 最小方案

采用运行时只读挂载：

1. `.dockerignore`增加根目录精确规则`/config.yaml`；
2. Dockerfile删除`COPY config.yaml ./config.yaml`；
3. `docker-compose.yml`通过必填`MAF_CONFIG_FILE_HOST`把配置以long bind syntax只读挂载到
   `/app/config.yaml`，并固定`bind.create_host_path: false`；
4. main环境的受保护`docker_cmd.md`必须在backend停止或替换前验证
   `/data/peihai/seedpilot_config_dev.yaml`是非symlink普通文件、link count为1、mode精确为`0600`且非空，再使用待发布backend
   镜像执行无输出的`bootstrap_config_env("/app/config.yaml", override=True, strict=True)`；验证成功后才把该文件
   只读挂载到`/app/config.yaml`；
5. README同步说明镜像不含配置、Compose必填变量，以及main服务器固定挂载源为
   `/data/peihai/seedpilot_config_dev.yaml`。

不采用构建期secret，因为最终镜像仍可能包含配置；不改成全环境变量注入，因为会扩大配置管理范围。

## 3. 验收

- Docker构建上下文排除根`config.yaml`，Dockerfile不再复制该文件；
- Compose缺`MAF_CONFIG_FILE_HOST`时配置渲染失败；提供路径时使用`read_only`且
  `bind.create_host_path: false`挂载到`/app/config.yaml`；
- main `docker_cmd.md`在任何backend停止/替换动作前完成上述文件identity、权限和严格配置加载预检；任一失败
  都必须停止部署，且不得输出配置正文；backend启动命令包含
  `/data/peihai/seedpilot_config_dev.yaml:/app/config.yaml:ro`等价只读bind；
- `linux/amd64` backend `0.1.25`可成功构建；
- 不挂载配置时镜像文件系统不存在`/app/config.yaml`；
- 挂载`0600`、普通且非symlink的临时外部配置后，严格bootstrap成功，隔离SQLite backend `/api-doc`返回200；
- 本轮不修改Frontend、Runtime Sidecar、业务配置加载代码、schema、依赖或`prod`，也不推送backend，除非用户后续明确要求。

## 4. 已知风险与决定

旧backend `0.1.25`推送在manifest发布前已中止，远端tag已验证不存在，但中止前已有多个layer上传，无法排除
旧`config.yaml`所在layer作为未引用blob暂留私有registry。用户此前已明确授权该未脱敏镜像推送，因此本次把该
registry范围内残留风险记录为接受；不声明“旧配置从未上传”，不扩展到凭据轮换、registry GC或仓库删除。新的
backend镜像必须以重新构建后的本地digest覆盖同一tag，并在任何后续推送前重新验证镜像内不存在
`/app/config.yaml`。

后续用户已明确授权发布三个main镜像；远端`backend-dev:0.1.25`指向重新构建且不含`/app/config.yaml`的
`sha256:c1664088e23d5879fb1dc85c898e3e2d0a9f4cde5ffe4f1640d99944982e8e34`，尚未部署。

## 5. 回滚

回滚本次tracked commit可恢复旧构建合同，但不得把旧的内嵌配置镜像重新推送或部署。外部配置文件不由仓库代码
创建、修改或删除；回滚部署命令前必须另行决定配置注入方式，不能静默恢复内嵌配置。

License Requirement：仅调整Docker构建上下文、运行时文件挂载和文档；无新增依赖或许可变化。
