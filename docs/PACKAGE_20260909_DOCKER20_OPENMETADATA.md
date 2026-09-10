# 20260909 Docker 20 + OpenMetadata 完整离线包

## 模式与边界

- 模式：完整打包，不发布、不替换 128 服务。
- 目标：全新 Linux x86_64 服务器，Docker Engine 20.10+，使用 Docker Compose 2.2.3+。
- 新版部署基线独立于 Docker 17.03 历史 Docker Run 包；旧包和旧脚本未被覆盖。
- 业务镜像直接复用 `20260909-data-automation-reliable-dispatch-r1`，归档 SHA-256 仍为 `dd9f92f0f258b06050282b6d8357573883c189a116806a3a4f446700e06836e5`，没有重新构建。

## 包含内容

- 业务 API 与九类 Worker 镜像归档。
- `mysql:8.4`、`redis:7-alpine` Linux amd64 离线镜像。
- `openmetadata/server:1.13.0`、`openmetadata/db:1.13.0`、`elasticsearch:9.3.0` Linux amd64 离线镜像。
- Docker Compose 2.24.7 Linux x86_64 二进制。
- OpenMetadata 轻量推送模式编排：独立元数据库、独立搜索持久卷、关闭 Airflow/Pipeline Service Client。
- 一键加载、启动、停止、状态、备份和包校验脚本。

## 不包含内容

- Oracle、Doris、SQL Server、MySQL 恢复目标库等业务数据库镜像。
- OpenMetadata Ingestion/Airflow。
- Keycloak 服务端镜像、生产 OIDC 客户端密钥、OpenMetadata API Token 和业务数据。

## 验证

- 五个新增镜像归档均通过 `crane validate --tarball` 逐层完整性检查。
- OpenMetadata、MySQL 和 Redis 的代理传输摘要已在下载前与 Docker Hub 官方 Linux amd64 摘要匹配；Elasticsearch 直接从官方仓库导出。
- Docker Compose 配置能够完整解析；确认仅公开 8585，8586 绑定回环地址，OpenMetadata MySQL 和 Elasticsearch 不映射宿主机端口。
- 全部 Shell 脚本通过语法检查，文本文件检查 UTF-8 无 BOM 与 LF 换行。
- 128 SSH 端口在打包时超时，本地 Docker Desktop daemon 不可用，因此不宣称完成容器启动或真实 OpenMetadata/API 闭环；本次验收范围为离线镜像与包级验证。
