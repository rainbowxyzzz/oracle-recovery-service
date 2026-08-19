# Release Validation: 20260715 Oracle Logs and SM4 Coverage

## Scope

Release tag: `20260715-oracle-logs-sm4-coverage`

This release includes Oracle auto-import observability and probe hardening, historical log credential masking, SM4 per-database function deployment state and JAR recovery, and offline development schedule management.

Business database images are not included.

## Runtime Baseline

- Validation host: `192.168.150.128`
- Docker server/client: `1.13.1` (older than the target `docker-ce 17.03`)
- Application Python: `3.10.20`
- Oracle auto-import host Python: `3.7.9` at `/usr/local/bin/python3`
- Oracle validation database: Oracle 19c, PDB `ORCLPDB1`
- Image platform: `linux/amd64`

## Automated Regression

The complete shared source tree was synchronized to both the API and Worker containers before image commit.

- API container: 25/25 unit tests passed.
- Worker container: 25/25 unit tests passed.
- Total executions: 50 passed, 0 failed.
- Full Python source, scripts, and tests passed `compileall`.
- Inline UI JavaScript passed `node --check`.

Coverage includes offline task tree/schedule behavior, Doris DDL replication policy, Oracle DIRECTORY naming and error classification, plan/log credential masking, SM4 per-database key resolution, SM4 JAR recovery, and production revision freezing.

## Oracle End-to-End Validation

Task: `6006d6d5-9808-42da-a91e-0fa4c90c087b`

- Import submitted through the production task API.
- Source dump: `py37_e2e.dmp`.
- Source schema: `SRC_PY37_E2E`.
- Target schema: `PY37_E2E_260715_1312`.
- Task result: `succeeded`.
- Validation result: 1 table and 2 objects.
- Source/target hard row-count comparison:
  - `T_E2E_ORACLE_IMPORT`: source 3, target 3.
- Task events: 97.
- Archived log files: 8.
- Timeline events in archive: 73.
- `run.log`: 45,644 bytes with command begin/end, output boundaries, return codes, and durations.
- The complete downloaded archive was scanned and did not contain the Oracle administrator password.
- New `plan.json` contains no executable clear-text command arrays and retains masked command descriptions only.

Historical task `adbc61fe-e568-4c5d-a5be-659d40936ad7` was also downloaded through the new endpoint. Its old clear-text Oracle login was masked in the generated ZIP without modifying the historical source artifact.

## Timeout Validation

The application's own SSH command runner executed `sleep 125` against the Oracle host with a 180-second operation timeout.

- Elapsed: 125.3 seconds.
- Return code: 0.
- Result marker: `LONG_TIMEOUT_OK`.

This verifies that the release does not retain a fixed 120-second command timeout for the tested path.

## SM4 Validation

- Missing historical JAR recovery, pre-DDL key validation, and offline batch provenance are covered by regression tests.
- API and Worker use the shared persistent volume `oracle_recovery_sm4_jars` at `/app/data/sm4-jars`.
- Java UDF compilation remains constrained to Java 8 (`--release 8`, with the existing source/target fallback).
- Earlier live validation on the same release source deleted and rebuilt a stored JAR and confirmed Java class major version 52.
- A new real Doris function deployment was not repeated during this packaging run because the configured Doris endpoint was not listening.

## Image Validation

- API image imported successfully and exposed the Oracle log download route.
- Worker image imported Oracle auto-import and SM4 JAR recovery modules successfully.
- A temporary Worker container started through its default entrypoint and reached Celery `ready`.
- Final application images are `linux/amd64`.

## Frontend Note

Desktop and 390px SM4 database-tree visual checks were completed during the preceding live release validation. This packaging run revalidated JavaScript syntax and live API/UI availability. A repeated local Playwright run was unavailable because the bundled Node runtime did not contain `playwright-core`; no uncontrolled dependency was installed for the release.

## Docker Run Package Validation

The final package was assembled and validated on Docker `1.13.1`, which is older than the target Docker `17.03` baseline.

- Image archive checksum and `gzip -t` passed.
- Docker save metadata contains only the API and Worker application images.
- No Oracle, SQL Server, Doris, or MySQL restore-target image is included.
- All packaged shell, environment, SQL, YAML, Markdown, and text files were checked for UTF-8 BOM and CRLF/CR; none were found.
- `sh -n` passed for `start-service.sh`, `load-images.sh`, `status-service.sh`, and `stop-service.sh`.
- `.env.example` was sourced successfully and validated for MySQL 8.4, legacy `recovery/recovery` credentials, Oracle timeout values, and persistent SM4 JAR storage.
- Forbidden `--pull`, `--mount`, `host-gateway`, and Compose dependencies were not found in runtime scripts.
- The final package loaded both images and ran `start-service.sh` successfully.
- Database migration completed, API health returned `ok`, Worker returned Celery `pong`, and both containers used the package image tag.
- API and Worker mounted the same `oracle_recovery_sm4_jars` volume.
- The packaged service returned the Oracle log manifest for the successful end-to-end task after restart.

The validation host retained pre-existing `mysql:latest` and `redis:latest` containers, so the startup script reused them instead of replacing their persistent data. The package default remains `mysql:8.4` and `redis:7-alpine`. Migration emitted a non-fatal `aiomysql` connection finalizer warning after reporting `Database initialized`; the migration exit code, schema check, API startup, and service health checks all passed.
