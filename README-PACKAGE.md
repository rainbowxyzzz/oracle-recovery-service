# Oracle Recovery Service Docker Package

## Contents

- `oracle-recovery-service-images.tar`: prebuilt linux/amd64 service images
- `docker-compose.yml`: runtime compose file using the prebuilt images
- `.env`: runtime environment template
- `config/`: service YAML configuration
- `oracle-client-empty/`: placeholder mount path for Oracle client
- `export-service-images.sh`: exports only the business API/Worker images
- `start-oracle19c.sh`: creates or starts the built-in Oracle19c target container
- `recreate-oracle19c-zhs16gbk.sh`: recreates Oracle19c with `ZHS16GBK`
- `status-oracle19c.sh`: checks the Oracle19c target container
- `start-sqlserver.sh`: creates or starts the built-in SQL Server target container
- `start-mysql-restore.sh`: creates or starts the independent MySQL restore target container

## Load Service Images

```bash
docker load -i oracle-recovery-service-images.tar
```

The service image package should contain the business runtime images, mainly:

- `oracle-recovery-service-api:latest`
- `oracle-recovery-service-worker:latest`

Database images are intentionally decoupled from the business package. On an
offline intranet server, either configure existing database services, or preload
database images separately before running the target startup scripts.

To rebuild the business image package:

```bash
chmod +x export-service-images.sh
./export-service-images.sh
```

## Start

Review `.env` first, especially passwords, `SECRET_KEY`, and Oracle settings.

Oracle19c is not included in `oracle-recovery-service-images.tar`. The server can
use an existing Oracle service, an existing Oracle container, or a local Oracle
image. By default the image id is:

```text
53661f3d548e
```

The startup script uses `/data/oracle-recovery/oracle19c` for Oracle external
data, DMP files, and tablespace files. Existing Oracle containers are kept:

- running container: keep it
- stopped container: start it
- missing container: create it from `ORACLE_IMAGE`
- missing configured image: search local images by `ORACLE_IMAGE_PREFIXES`
- missing service, container, and image: fail with a clear "image not found" error

To use an existing external Oracle service instead of a local container:

```text
ORACLE_TARGET_MODE=external
ORACLE_TARGET_HOST=192.168.1.5
ORACLE_HOST_PORT=1521
```

Default Oracle character set:

```text
ORACLE_CHARACTERSET=ZHS16GBK
```

Oracle character set is fixed when the database is created. If an existing
database was initialized with `AL16UTF8`, changing `.env` is not enough. Recreate
the Oracle target database with:

```bash
chmod +x recreate-oracle19c-zhs16gbk.sh start-oracle19c.sh status-oracle19c.sh
./recreate-oracle19c-zhs16gbk.sh
./status-oracle19c.sh
```

The recreate script intentionally removes the old Oracle container and deletes
`ORACLE_ORADATA_HOST_PATH`; DMP and tablespace external directories are kept. The
status output should include `NLS_CHARACTERSET ZHS16GBK`.

With Docker Compose:

```bash
docker compose up -d
```

Without Docker Compose:

```bash
chmod +x run-with-docker.sh stop-with-docker.sh status-with-docker.sh
./run-with-docker.sh
```

On older Docker hosts such as Docker 17.03 without `iptables`/`firewalld`, keep:

```text
NO_IPTABLES_MODE=auto
```

The startup script then avoids Docker port publishing rules. API is exposed
through host networking on `API_PORT`; target database containers are accessed
with `docker exec` and do not need published host ports.

Open:

```text
http://127.0.0.1:8000/ui
```

The UI keeps historical task records after refresh. Open a task detail to see:

- source DMP directory and target Oracle PDB connection
- generated schema/user, tablespace, datafile, Oracle DIRECTORY
- every recovery step
- full `imp` / `impdp` stdout, stderr, retry output, and Oracle import log content

API docs are still available:

```text
http://127.0.0.1:8000/docs
```

## Oracle impdp

The default web flow executes `imp`/`impdp` inside the built-in Oracle Docker
container. The worker image itself does not contain an Oracle client.

Submit an embedded Oracle task with:

```bash
curl -X POST http://SERVER:8000/api/v1/tasks/embedded-oracle \
  -H "X-API-Key: YOUR_SECRET_KEY_IF_PRODUCTION" \
  -H "Content-Type: application/json" \
  -d @professional-task.example.json
```

The request contains only:

- `source`: server A, where DMP/log/par files are stored
- optional `oracle_password`: Oracle host SSH password when it is not set in `.env`
- optional `oracle_host`: set it to the same IP as `source.host` when DMP files
  and Oracle are on the same server, so the service uses remote `cp` instead of
  SFTP streaming.

Oracle target connection, PDB, paths, and passwords come from `.env`.

## MySQL Restore Target

MySQL restore uses a separate container and never restores into the service
metadata database container `oracle-recovery-mysql`.

Defaults:

```text
MYSQL_RESTORE_CONTAINER_NAME=mysql-recovery-target
MYSQL_RESTORE_BASE_HOST_PATH=/data/mysql-recovery
MYSQL_RESTORE_BACKUP_HOST_PATH=/data/mysql-recovery/backup
MYSQL_RESTORE_DATA_HOST_PATH=/data/mysql-recovery/data
MYSQL_RESTORE_IMAGE_PREFIXES=mysql
```

To use an existing external MySQL restore service:

```text
MYSQL_RESTORE_TARGET_MODE=external
MYSQL_RESTORE_TARGET_HOST=192.168.1.5
MYSQL_RESTORE_HOST_PORT=3306
```

Supported backup files in the selected source directory:

- `.sql`
- `.sql.gz`
- `.sql.zip` / `.zip` containing SQL files

The MySQL flow drops and recreates the target database by default when the same
name exists. You can set a target database name in the web UI; otherwise it is
derived from the backup filename.

## SQL Server Target

SQL Server follows the same decoupled runtime rule:

- existing configured container: keep or start it
- no configured container: discover by default port `1433` or name prefixes
- no container: create from `SQLSERVER_IMAGE`
- missing configured image: search local images by `SQLSERVER_IMAGE_PREFIXES`
- missing service, container, and image: fail with a clear "image not found" error

To use an existing external SQL Server service:

```text
SQLSERVER_TARGET_MODE=external
SQLSERVER_TARGET_HOST=192.168.1.5
SQLSERVER_HOST_PORT=1433
```

## Service MySQL and Redis

The business service can use local MySQL/Redis containers or configured existing
services. Defaults keep the current behavior:

```text
SERVICE_MYSQL_MODE=auto
MYSQL_HOST=mysql
MYSQL_SERVICE_IMAGE=mysql:8.4
SERVICE_REDIS_MODE=auto
REDIS_HOST=redis
```

If `MYSQL_HOST` or `REDIS_HOST` is changed to an existing service address and the
mode remains `auto`, `run-with-docker.sh` will not start the local service
container.

The service metadata database must use `mysql:8.4`. Do not use `mysql:latest`,
`mysql:8`, or MySQL 9.x for the service metadata container. A data directory
created by MySQL 9.x cannot be started by MySQL 8.4 without rebuilding or
migrating the metadata database.
