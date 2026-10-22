# 2026-10-09 A 服务器完整包 R3 发布验证

## 验证模式与范围

- 模式：模式二（发布到 128 并验证），未执行回滚演练。
- 目标服务器：`192.168.150.128`。
- 包版本：`20261009-sm4-partial-encryption-oracle-startup-r3`。
- 应用镜像继续复用已验证的 `20261009-sm4-partial-encryption-r1`，本次只修正包级版本清单和 Oracle 启动脚本。

## 发布前保护

- 发布前确认业务任务状态中无 `queued`、`running` 或 `stopping` 任务。
- 系统 MySQL 备份：`/data/codex-release/20261009-sm4-partial-oracle-startup-r2/backup-20261009-184707/oracle_recovery.sql.gz`。
- 原 `.env` 备份：`/data/codex-release/20261009-sm4-partial-oracle-startup-r2/backup-20261009-184707/current.env`。
- 原 UTF Oracle 数据没有直接删除，已移动至可恢复目录：`/data/codex-release/20261009-sm4-partial-oracle-startup-r2/backup-20261009-184707/utf-oracle-data-before-fresh-create`。
- 主 Oracle 容器和主库数据目录未清理。

## 实际验证过程

1. 清理旧 UTF Oracle 容器，并确认新的 UTF `oradata`、`tablespaces` 目录为空。
2. 使用 R2 脚本首次创建时，发现旧 `.env` 没有 `ORACLE21C_PASSWORD`，脚本在创建容器前安全失败。
3. R3 增加兼容逻辑：旧 `.env` 未声明 Oracle 密码时，读取现有主 Oracle 容器的 `ORACLE_PWD` 供 UTF Oracle 使用；密码不写回 `.env`、不输出到日志。
4. 再次从空目录运行包内 `start-oracle21c.sh`，完成 UTF Oracle DBCA 初始化、PDB 打开、字符集检查、目录对象和 TEMP 文件初始化。
5. 运行完整 `start-service.sh`，由包内 `package-images.env` 选择应用镜像，完成迁移并重建 API 与九个 Worker。
6. 检查健康接口、Celery 节点、容器日志、Oracle 字符集和 MySQL 运行参数。

## 验证结果

- 交付包：`artifacts/oracle-recovery-business-docker17-offline-20261009-sm4-partial-encryption-oracle-startup-r3.tar.gz`。
- SHA256：`7e1a984060bb1a6ef787674c536d02c8bc52ad6d1b0271b570c1978110724d8e`。
- 服务器端包校验、Linux 解压、包内校验和、LF/UTF-8 无 BOM、Shell 语法与预检全部通过。
- 现有 `.env` 中 `API_IMAGE`、`APP_IMAGE_TAR`、`SM4_WORKER_IMAGE`、`UTF_ORACLE21C_MODE` 均不存在，服务仍按包清单启动到正确镜像，证明升级不再要求手工写入这些版本字段。
- API 和九个 Worker 均启动；Celery `inspect ping` 返回 `9 nodes online`。
- `/api/v1/health` 返回 HTTP 200，MySQL 状态为成功；`/ui` 返回 HTTP 200。
- 主 Oracle：容器 `healthy`，`ORCLPDB1` 为 `READ WRITE`，字符集 `ZHS16GBK`，NCHAR 字符集 `AL16UTF16`。
- UTF Oracle：容器 `healthy`，`ORCLPDBUTF8` 为 `READ WRITE`，字符集 `AL32UTF8`，NCHAR 字符集 `AL16UTF16`；20GB TEMP 文件存在并启用自动扩展。
- MySQL 实际运行参数满足包基线：16MB 排序缓冲、256MB 临时表和包大小、512MB InnoDB buffer pool。
- 管理员登录、`/auth/me`、SM4 密钥列表、SM4 任务定义和函数部署查询均通过；页面资源包含证件、姓名、电话、地址四类局部加密规则。
- 本地完整回归：`388 passed, 1 skipped`；`git diff --check` 通过。

## 非阻塞观察

- 数据库迁移成功后，Python 进程退出阶段出现一次 `Event loop is closed` 的 aiomysql 析构警告，没有影响迁移结果或服务启动。
- API 日志中的审批自动监听任务仍在请求既有外部地址 `http://23.47.200.117/api/market/login`，对方返回 HTTP 400 非 JSON，属于已有审批自动监听配置问题；API、SM4 和 Oracle 链路不受影响。
- 本次没有执行回滚演练；原 UTF Oracle 数据仍保留在备份目录，可供人工恢复或比对。
