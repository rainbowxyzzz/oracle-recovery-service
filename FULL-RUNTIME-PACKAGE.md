# 完整运行包说明

本包用于离线部署当前数据库恢复与清理系统。

## 本包包含

- `oracle-recovery-service-api:latest`
- `oracle-recovery-service-worker:latest`
- 系统元数据库镜像：`mysql:latest`
- 系统队列镜像：`redis:latest`
- 当前项目源码快照
- `.env`
- `docker-compose.yml`
- `run-with-docker.sh`
- `stop-with-docker.sh`
- `status-with-docker.sh`
- 三大数据库目标服务辅助脚本
- `config/` 配置目录
- `docs/` 文档目录
- `ORACLE导出导入/` 参考项目目录

## 本包不包含

- Oracle 业务数据库镜像和数据
- SQL Server 业务数据库镜像和数据
- MySQL 恢复目标业务库镜像和数据
- Doris 业务数据库镜像、二进制安装包和数据
- Docker volume 中的历史运行数据

默认配置会启动系统本身、系统 MySQL、Redis；不会自动启动业务数据库目标容器。
业务数据库可以在页面的数据连接中配置，也可以在目标服务器预先准备好镜像或已有服务后，再按需要启用对应脚本。

## 启动

```bash
unzip oracle-recovery-service-deploy-*-full-runtime-no-business-db.zip
cd oracle-recovery-service-deploy-*-full-runtime-no-business-db
chmod +x *.sh
./run-with-docker.sh
```

访问：

```text
http://服务器IP:8000/ui
```

默认管理员：

```text
admin / admin123
```

上线后请立即修改管理员密码和 `.env` 中的 `SECRET_KEY`。
