# 20260831 最新源码完整包

## 基线与边界

- 用户在知悉 128 部分 Worker 与最新源码存在差异后，明确选择对最新版本打包。
- 源码为 `5330cc0defcba9564e4b9ad33f54f480ba92244f` 加 2026-08-31 CSV 多行与引号修复及回归用例；后者尚未提交 GitHub。
- 包结构沿用最近交付的 `20260826-full-source-r1-no-business-db` 完整包，仅增量更新版本、镜像、源码与说明，不重排 `.env.example`。
- 在 128 的独立打包目录构建 API/Worker 镜像，不提交运行容器快照、不拷贝环境凭据或业务数据；所有 Worker 镜像内源码统一为本版。
- 本次仅完整打包和 Linux 包级校验，不替换运行服务、不执行数据库迁移、不重启 Doris、不做回滚演练。
- 本包不意味着 128 所有正在运行的 Worker 已升级。运行差异见 `RUNTIME_VERSION_AUDIT_20260831.md`。

## 包含内容

- 审批流自动授权：待办申请 `createTime` 的 MMDD 日期、apiAdd 目录配置、映射表和授权信息表所属库配置、importDataPermissions 单目录配置、持续监听状态及分层日志。
- Doris CSV：字段类型自定义、一键 VARCHAR(65533)、中文映射、多行单元格及引号保真修复。
- 当前源码中的查询导出与进度、帮助与架构中心、数据血缘与流水线、离线开发、SM3/SM4、恢复、数据同步、批量授权和接口编排等既有模块。
- API、通用 Worker 及 9 个独立 Worker 镜像标签；共用镜像层只导出一次。
- 启停/镜像加载脚本、配置模板、MySQL 调优脚本、Oracle21c 生命周期脚本、PRD/历史验证记录、最新源码快照与测试。

## 配置与兼容

- 环境变量结构和默认值沿用基线，仅更新镜像标签和镜像归档名。
- 系统库固定 `mysql:8.4`，兼容 `root/recovery/recovery/oracle_recovery` 历史默认值；应用继续以 MYSQL_USER 连接。
- 保留旧 Docker 兼容参数、系统 MySQL 资源基线、SM4 jar 及查询导出持久卷、Java8 UDF 编译及 Oracle 长超时配置。
- 不包含系统库数据、用户凭据、业务数据库镜像或 Oracle 客户端。MySQL8.4/Redis 镜像沿用旧包方式，不随包归档；离线安装须提前加载基础设施镜像，或配置外部 MySQL/Redis。

## 验证口径

- 本地最新源码完整回归已执行：277 passed、1 skipped、32 subtests passed。
- CSV 的 128 真实数据验证沿用同日发布记录；本次不重复创建业务数据。
- 镜像源码一致性、导入冒烟、Linux 文本/语法/配置加载、启动命令模拟和包校验结果记录在包内 `PACKAGE-CHECKS.json`。
- 本次不宣称已完成新镜像的真实部署、全模块业务回归或浏览器验收。

## 使用

1. 将部署包和同名 `.sha256` 上传目标 Linux 目录，执行 `sha256sum -c <包名>.tar.gz.sha256`。
2. `tar -xzf <包名>.tar.gz`，进入解压目录，执行 `sha256sum -c SHA256SUMS.txt`。
3. 首次部署复制 `.env.example` 为 `.env` 并填写真实配置；升级时保留原凭据、加密密钥和卷名，将本版镜像标签合并到原 `.env`，不要直接覆盖。
4. 使用已有系统库时设置 `START_LOCAL_MYSQL=false`；同理配置 Redis。升级前自行备份系统库和持久卷、确认无在途任务。
5. 执行 `sh load-images.sh`、`sh start-service.sh`、`sh status-service.sh`。
6. 访问 `http://目标服务器:8000/ui`（端口以 `.env` 为准）。完整参数与 Oracle 外部/本地模式参见 `README-DOCKER-RUN.md`。

注意：`start-service.sh` 是实际部署命令，会替换同名应用容器并执行既有系统库迁移；不应在未计划发布的服务器上仅为看包内容而运行。
