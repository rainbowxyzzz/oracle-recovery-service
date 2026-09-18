# Oracle 21c EE 容器生命周期

该目录用于在老版本 Docker 环境中检查、创建、启动和初始化两个 Oracle 21c Enterprise Edition（企业版）恢复目标：保留 `ZHS16GBK` 中文字符集主实例，并保障独立的 `AL32UTF8` 通用字符集实例。

## 启动模式

```text
ORACLE21C_MODE=auto
```

发现已有容器、已有镜像或 `ORACLE21C_IMAGE_TAR` 时自动管理 21c；三者都不存在时记录并跳过。

```text
ORACLE21C_MODE=container
```

强制使用本地 21c。没有容器时必须存在精确镜像，或通过 `ORACLE21C_IMAGE_TAR` 加载镜像。

```text
ORACLE21C_MODE=external
```

跳过本地 21c 管理。

## 默认持久目录

```text
/data/oracle-recovery/oracle21c/oradata     -> /opt/oracle/oradata
/data/oracle-recovery/oracle21c/dmp         -> /opt/oracle/recovery_dmp
/data/oracle-recovery/oracle21c/tablespaces -> /opt/oracle/recovery_tablespaces
```

已有容器会读取并校验实际挂载。只有显式配置了单项宿主机路径且与已有容器不一致时，脚本才会停止，避免误用数据目录。

UTF 实例默认使用独立目录和端口：

```text
/data/oracle-recovery/oracle21c-utf8/oradata     -> /opt/oracle/oradata
/data/oracle-recovery/oracle21c/dmp              -> /opt/oracle/recovery_dmp（与主实例共享输入目录）
/data/oracle-recovery/oracle21c-utf8/tablespaces -> /opt/oracle/recovery_tablespaces
宿主机端口 1523 -> 容器端口 1521
```

`package-start-oracle21c.sh` 会依次启动主实例和 UTF 实例。UTF 容器不存在时，脚本使用相同镜像自动创建；已停止时自动启动；初始化阶段会查询并核验真实数据库字符集，字符集不匹配时停止，不会删除或重建已有数据目录。

## 文件

- `start-oracle21c.sh`：按模式执行完整生命周期。
- `start-oracle21c-utf8.sh`：将 UTF 实例配置映射到通用生命周期脚本。
- `package-start-oracle21c.sh`：应用包双实例启动入口。
- `deploy.sh`：检查 Docker、容器、镜像、镜像包和挂载，创建或启动容器。
- `initialize.sh`：等待 PDB `READ WRITE`，创建 DIRECTORY（目录对象）、授权并验证 SYSTEM/PDB、数据库字符集和 SQLPlus 21c（命令行客户端）。
- `oracle21c.env.example`：独立使用时的配置模板。

## 应用包内使用

应用 Docker Run 包的 `start-service.sh` 会自动调用根目录的 `start-oracle21c.sh`。也可以单独执行：

```sh
sh ./start-oracle21c.sh
```

应用包不包含 Oracle 业务镜像。离线环境应先加载镜像，或配置：

```sh
ORACLE21C_IMAGE_TAR=/absolute/path/to/oracle21c-image.tar
```

## 独立使用

```sh
cp oracle21c.env.example oracle21c.env
chmod 600 oracle21c.env
vi oracle21c.env
ORACLE21C_ENV_FILE=$PWD/oracle21c.env sh ./start-oracle21c.sh
```

默认不限制容器总内存，`shm-size` 为 `1g`。大型生产实例应根据服务器资源显式配置 `ORACLE21C_MEMORY_LIMIT`、`ORACLE21C_SHM_SIZE` 和可选 DBCA 参数。

## 幂等初始化

初始化会重复创建以下 DIRECTORY 并向 SYSTEM 授权，不会删除业务表：

```text
RECOVERY_DMP_DIR=/opt/oracle/recovery_dmp
RECOVERY_TABLESPACE_DIR=/opt/oracle/recovery_tablespaces
```

## 验证

```sh
docker ps --filter name=oracle-recovery-oracle21c-ee
docker inspect oracle-recovery-oracle21c-ee --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
docker exec oracle-recovery-oracle21c-ee sh -c 'export ORACLE_HOME=/opt/oracle/product/21c/dbhome_1; $ORACLE_HOME/bin/sqlplus -V'
```
