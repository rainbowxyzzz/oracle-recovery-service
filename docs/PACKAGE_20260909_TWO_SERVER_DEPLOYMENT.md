# 2026-09-09 双服务器离线交付记录

> 2026-09-10 补充：部署过程中发现 B 服务器实际存在未启动的 Docker 17.03、旧升级脚本存在 Docker 启动就绪竞态、A 归档包含旧 Oracle TEMP 初始化脚本，以及 OpenMetadata 同步配置与 OIDC（开放身份连接协议）统一认证配置容易混淆。完整操作、修复和验收步骤统一维护在 [双服务器离线部署、升级与排障操作手册](DUAL_SERVER_OFFLINE_DEPLOYMENT_GUIDE_20260910.md)。本文仅保留交付记录。

## 部署边界

- A 服务器：Docker 17.03，运行数据恢复与治理业务系统、系统 MySQL 8.4 和 Redis 7；使用 Docker Run，不依赖 Docker Compose。
- B 服务器：Docker 20.10+，只运行 OpenMetadata 1.13.0、其独立元数据库和 Elasticsearch 9.3.0；使用 Docker Compose 2.24.7。
- 两台服务器通过 B 的宿主机 `8585` 端口通信，不共享 Docker 网络，不跨服务器控制容器。

## 交付件

1. `oracle-recovery-business-docker17-offline-20260909.tar.gz`
   - 复用已验证的 `20260909-data-automation-reliable-dispatch-r1` API/Worker 镜像，未重新构建。
   - 补齐系统 MySQL 8.4、Redis 7-alpine 离线镜像。
   - `.env.example` 明确提供 B 服务器 OpenMetadata 地址、Token 和同步开关。
2. `openmetadata-docker20-1.13.0-standalone-20260909.tar.gz`
   - 只包含 OpenMetadata Server/DB 1.13.0、Elasticsearch 9.3.0、Docker Compose 2.24.7 和独立启停/备份/校验脚本。
   - 删除原同机方案中的外部平台 Docker 网络依赖。
   - 补充 `install-docker-20.10.24-compose-2.24.7-fresh.sh`，专用于 B 裸机安装；不再用升级脚本检查一个尚不存在的 Docker 服务。

## 2026-09-10 现场修正

- B 服务器后来确认并非真正裸机，而是已安装 Docker 17.03、服务最初未启动。该环境必须使用修正版升级脚本 `install-docker-20.10.24-compose-2.24.7.sh`；`fresh` 新装脚本只用于无 Docker 安装且数据目录为空的服务器。
- 修正版升级脚本会等待 Docker 服务端可用并核验服务端版本，避免 `systemd`（Linux 服务管理器）刚返回启动成功时 Socket（本地通信套接字）尚未就绪导致误回滚。
- B 当前推荐归档为 `openmetadata-docker20-1.13.0-standalone-20260910-fresh-r1.tar.gz`。
- A 当前原始归档内的 Oracle 21c TEMP 初始化脚本仍为旧版。使用该归档时，启动前必须执行 `fix-server-a-oracle21c-temp-limit.sh`；下次完整打包必须直接纳入修正版。
- 只修改 `OPENMETADATA_*` 不会启用统一登录；A 服务器还必须配置并启用 `OIDC_*`，修改 `.env` 后通过 `start-service.sh` 重建容器。

## 启动顺序

1. 先在 B 解压、配置并执行 `sh ./start-openmetadata.sh`。
2. 在 OpenMetadata 创建服务端 API Token。
3. 在 A 的 `.env` 中填写 `OPENMETADATA_URL=http://B服务器IP:8585`、API 地址和 Token，启用同步。
4. 在 A 执行 `sh ./start-service.sh`。
5. 从 A 检查 OpenMetadata 连接状态并执行一条隔离的元数据/血缘推送冒烟验证。

## 验证边界

本次仅打包，不修改 A、B 或 128 服务器。业务镜像复用既有验证结果；新脚本执行 Shell 语法、UTF-8 无 BOM、LF 换行、环境文件加载、Compose 配置和归档校验。由于本机 Docker daemon 不可达，不声明完成真实容器启动验证。
