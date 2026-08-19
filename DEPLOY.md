# 部署说明（跨主机 + Oracle 在 Docker 内）

## 典型架构

```text
  A 机 (本服务)                 B 机 (DMP 服务器)              C 机 (可选)
  ─────────────                 ─────────────────              ───────────
  API + Worker                  /data/dump/*.dmp               目标 Oracle 19c
  无需安装 Oracle        SSH →  Docker[oracle]  ──impdp──►   :1521
```

- **A**：只跑 `install.sh` 起的 Docker 服务（或 venv）
- **B**：放 DMP；`impdp` 在 B 上的 **Docker 容器里**执行（通过 SSH + `docker exec`）
- **C**：导入目标库，API 里填 `target_connection`（容器内需能访问 C 的 1521）

---

## 一键启动（A 机）

```bash
chmod +x install.sh && sudo ./install.sh
```

**A 机不需要 Oracle。**

---

## 分步配置（推荐调试流程）

在浏览器打开 `http://A机IP:8000/docs`，找到 **setup** 分组，或使用脚本：

```bash
export RECOVERY_API=http://127.0.0.1:8000
export RECOVERY_API_KEY=<install.sh 输出的 SECRET_KEY>
chmod +x scripts/configure-wizard.sh
./scripts/configure-wizard.sh
```

### 检测顺序

| 步骤 | API | 你需要提供 |
|------|-----|------------|
| 1 | `POST /api/v1/setup/check/ssh` | B 机 IP、SSH 端口、账号密码 |
| 2 | `POST /api/v1/setup/check/dmp-files` | 宿主机 DMP 目录 `dmp_host_path` |
| 3 | `POST /api/v1/setup/check/docker` | `docker_container` 容器名 |
| 4 | `POST /api/v1/setup/check/container-path` | `dmp_container_path` 容器内路径 |
| 5 | `POST /api/v1/setup/check/impdp` | 同上（验证容器内 impdp） |
| 6 | `POST /api/v1/setup/check/target-db` | C 机连接串、system 密码 |

一次跑完全部：`POST /api/v1/setup/check/all`

配置模板：`GET /api/v1/setup/template`

---

## 路径怎么填？

在 **B 机**上执行：

```bash
# 宿主机路径（给 dmp_host_path / remote_directory）
ls /data/oracle/dump

# 容器名
docker ps

# 容器内路径（给 dmp_container_path）
docker exec -it oracle19c bash -c 'echo $ORACLE_HOME; ls $(dirname $(find / -name DATA_PUMP_DIR 2>/dev/null | head -1)) 2>/dev/null'
# 或查 Oracle:
# SELECT directory_path FROM dba_directories WHERE directory_name='DATA_PUMP_DIR';
```

**宿主机目录必须通过 volume 挂进容器**，且 `dmp_container_path` 与 `DIRECTORY` 的 `directory_path` 一致。

示例：

| 配置项 | 示例值 |
|--------|--------|
| `dmp_host_path` | `/data/oracle/dump` |
| `docker_container` | `oracle19c` |
| `dmp_container_path` | `/opt/oracle/admin/ORCL/dpdump` |
| `oracle_directory` | `DATA_PUMP_DIR` |

---

## 提交恢复任务

```json
POST /api/v1/tasks
{
  "remote_host": "192.168.1.50",
  "remote_port": 22,
  "remote_user": "root",
  "remote_password": "***",
  "remote_directory": "/data/oracle/dump",
  "target_connection": "192.168.1.100:1521/ORCLPDB1",
  "target_admin_user": "system",
  "target_admin_password": "***",
  "options": {
    "auto_confirm": false,
    "execution": {
      "mode": "remote_docker",
      "docker_container": "oracle19c",
      "dmp_host_path": "/data/oracle/dump",
      "dmp_container_path": "/opt/oracle/admin/ORCL/dpdump",
      "oracle_directory": "DATA_PUMP_DIR",
      "oracle_home_in_container": "/opt/oracle/product/19c/dbhome_1"
    }
  }
}
```

---

## 网络要求

| 来源 | 目标 | 端口 |
|------|------|------|
| A (Worker) | B | 22 SSH |
| B (Oracle 容器) | C (目标库) | 1521 |
| A (可选检测) | C | 1521 |

---

## Docker Compose 部署 Oracle 时

若用 `docker compose` 而非单容器名：

```json
"execution": {
  "docker_compose_dir": "/opt/oracle",
  "docker_compose_service": "oracle",
  "docker_container": "oracle"
}
```

（`docker_container` 仍填 service 名或容器名，与 compose 环境一致即可。）

---

## 常见问题

**Q: A 机没有 impdp 能跑吗？**  
能。impdp 在 B 机容器内执行。

**Q: 检测 SSH 成功但 container-path 失败？**  
检查 volume 挂载，宿主机 `dmp_host_path` 与容器 `dmp_container_path` 是否同一挂载。

**Q: impdp 连不上目标库？**  
在 B 机容器内测试：`docker exec -it oracle19c bash -c "tnsping ..."` 或 sqlplus 连 C 机。
