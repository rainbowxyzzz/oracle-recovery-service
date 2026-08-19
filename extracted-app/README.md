# Oracle Recovery Service

Production-oriented microservice for intelligent Oracle Data Pump (DMP) recovery across versions (11g/12c → 19c).

## Features

- REST API to trigger recovery from remote directories (SSH)
- Celery workers for long-running `impdp` jobs
- Policy tree: log → parfile → filename → SQLFILE probe → trial import → ORA auto-correction
- Never reads `.dmp` binary content
- Batch import for multiple volume groups in one directory

## 部署方式

| 方式 | 命令 | 说明 |
|------|------|------|
| **纯 Python（推荐调试）** | `./install-python.sh` → `./start-python.sh` | **远程 MySQL + 本机 Redis**，见 **[DEPLOY-PYTHON.md](DEPLOY-PYTHON.md)** |
| Docker 全量 | `./install.sh` | 可选；会询问确认 |

**本机无需 Oracle**。DMP 在 B 机 Docker 内时见 **[DEPLOY.md](DEPLOY.md)**。

```bash
chmod +x install-python.sh start-python.sh
./install-python.sh --with-redis-docker  # 可选：仅用容器起本机 Redis
./start-python.sh
```

## 本地开发

```bash
cp .env.example .env
docker compose up -d postgres redis
pip install -e ".[dev]"
python scripts/init_db.py
uvicorn recovery_service.main:app --reload
celery -A recovery_service.workers.celery_app:celery_app worker -l info
```

## API example

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "remote_host": "10.0.0.10",
    "remote_user": "oracle",
    "remote_password": "secret",
    "remote_directory": "/backup/exp",
    "target_connection": "dbhost:1521/ORCLPDB1",
    "target_admin_user": "system",
    "target_admin_password": "manager",
    "options": {
      "auto_confirm": false,
      "remap_schema": ["OLDSCHEMA:NEWSCHEMA"]
    }
  }'
```

## Requirements

- Worker host must have `impdp` compatible with target DB
- Target DB must have `DIRECTORY` (e.g. `DATA_PUMP_DIR`) pointing at dump file location
- Network: Worker → SSH (remote files), Worker → Oracle (1521)

## Project layout

See `src/recovery_service/engine/` for parsers and policy tree; `config/ora_dictionary.yaml` for ORA rules.
