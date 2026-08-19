# Release Validation 2026-07-28 Microservice Phase 1

## Scope

This validation covers the first phase of business microservice separation:

- `APP_SERVICE_MODE` route slicing while keeping `monolith` compatibility.
- `WORKER_SERVICE_MODE` / `WORKER_QUEUES` business queue selection.
- Data sync manual runs submitted to the `data_sync` queue with persisted component run logs.
- Oracle restore, SM4, SM3, Doris SQL and data platform legacy API smoke checks.

No Docker Run package was generated in this validation.

## Environment

- Server: `192.168.150.128`
- API container: `oracle-recovery-api`
- Worker container: `oracle-recovery-worker`
- Baseline running image tag: `20260725-data-sync-component-runs-r7`
- Validation date: 2026-07-28

## Source Snapshot

Pre-change source snapshot:

```text
artifacts/source-snapshots/oracle-recovery-source-snapshot-20260727-231605.zip
SHA256: 3B9A7BD18FCD67886F938B4352325458F07C5D2277E18B433F35B2217CD71066
```

128 hot-update backup:

```text
/tmp/oracle-recovery-hotupdate-backup-20260727-2355
```

## Automated Validation

Executed inside a one-off 128 container with the uploaded source tree:

| Check | Result |
|---|---|
| `python -m compileall -q src tests` | Passed |
| `test_microservice_modes.py` | Passed, 7 tests |
| `test_data_platform_component_tasks.py` | Passed, 13 tests |
| `test_data_sync.py` | Passed, 15 tests |
| `test_sm4_task_versioning.py` | Passed, 2 tests |
| `test_oracle_auto_import_resilience.py` | Passed, 19 tests |
| `scripts/worker-entrypoint.sh` queue mode check | Passed |

## Real Data Sync Queue Validation

Created isolated Doris validation database:

```text
CODEX_MICROSVC_20260728
```

Created source table:

```text
CODEX_MICROSVC_20260728.SRC_QUEUE_CHECK
```

Created data sync task:

```text
codex_microservice_data_sync_queue_20260728
node_id: 06d3b9d0-8836-4073-a7f9-c8e3a8ba4c50
```

Run result through monolith worker:

```text
component_run_id: 780b3c0d-acb5-4910-977a-3c3563653d45
status: succeeded
message: 数据同步完成：成功 1 张表，失败 0 张表，写入 3 行。
target row count: 3
```

Verified persisted table run and SQL logs:

- `data_platform_component_runs.status = succeeded`
- `data_platform_component_run_tables.status = succeeded`
- `data_platform_component_run_logs` contains `create_table` SQL.
- `data_platform_component_run_logs` contains `insert_select` SQL.

## Independent Worker Validation

Temporarily committed the hot-updated worker container into:

```text
oracle-recovery-service-worker:microservice-hotupdate-20260728-test
```

Stopped the monolith worker and started:

```text
oracle-recovery-worker-data-sync-test
WORKER_SERVICE_MODE=data-sync
```

Worker startup confirmed only this queue:

```text
queues=data_sync
```

Executed the same isolated data sync task:

```text
component_run_id: afffdd52-7ba4-49d2-b9d8-f3c35a360816
status: succeeded
target row count: 3
```

Worker log confirmed consumption:

```text
Task data_platform.component_task_run[bcc600c6-3d5a-41e7-b541-dc66dbde21fe] received
Task data_platform.component_task_run[...] succeeded
```

The temporary worker was removed and `oracle-recovery-worker` was restored to monolith mode.

## Legacy API Smoke Checks

| Module | Endpoint | Result |
|---|---|---|
| Oracle restore | `GET /api/v1/tasks` | HTTP 200 |
| SM4 | `GET /api/v1/doris-encryption/batches` | HTTP 200 |
| SM3 | `GET /api/v1/doris-sm3/queue` | HTTP 200 |
| Doris SQL | `POST /api/v1/doris-sql-etl/doris/execute` | HTTP 200, `SELECT COUNT(*)` returned `3` |
| Data platform | `GET /api/v1/data-platform/nodes` | HTTP 200 |

SM3 queue status after correction:

```json
{"queue_name":"doris_sm3","pending_count":0,"active_worker_count":1}
```

## Issues Found And Fixed During Validation

1. Formal 128 hot update initially missed `services/data_sync.py`.
   - Symptom: queued data sync run failed with `execute_data_sync() got an unexpected keyword argument 'event_hook'`.
   - Fix: hot-updated `services/data_sync.py` into API and Worker containers.
   - Result: data sync queue run succeeded and table-level logs persisted.

2. SM3 queue status still read the default `celery` queue.
   - Symptom: `GET /api/v1/doris-sm3/queue` returned `queue_name: celery`.
   - Fix: changed status lookup to `settings.celery_sm3_queue`.
   - Result: endpoint returns `queue_name: doris_sm3`.

## Final Runtime State

After validation:

- `oracle-recovery-api`: Up
- `oracle-recovery-worker`: Up
- Worker mode: `monolith`
- Health endpoint: `/api/v1/health` returns `status=ok`
- Temporary `oracle-recovery-worker-data-sync-test`: removed

## Notes For Packaging

- This validation did not generate a Docker Run package.
- A later package must include all hot-updated files, especially:
  - `services/data_sync.py`
  - `services/data_platform.py`
  - `services/doris_sm3_mapping.py`
  - `workers/celery_app.py`
  - `workers/tasks/data_platform_component.py`
  - `scripts/worker-entrypoint.sh`
- Package validation must still follow `docs/PACKAGING_MUST_READ.md` and old Docker 17.03 constraints.

## Formal 128 Deployment - R1

The phase-one runtime was formally deployed on 2026-07-28 after the earlier
hot-update validation. This section supersedes the earlier `Final Runtime State`
for the current 128 environment.

Release tag:

```text
20260728-microservice-phase1-r1
```

Source archive:

```text
/opt/oracle-recovery/releases/20260728-microservice-phase1-r1/microservice-phase1-source-20260728-microservice-phase1-r1.tar.gz
SHA256: 8a4dbc9de0adbbb254caee65947e546e33b8133d2959d4bb5151d9b2a3e63fe1
```

Running containers:

| Container | Image | Service mode / queue |
|---|---|---|
| `oracle-recovery-api` | `oracle-recovery-service-api:20260728-microservice-phase1-r1` | `APP_SERVICE_MODE=monolith` |
| `oracle-recovery-worker-oracle` | `oracle-recovery-service-worker-oracle:20260728-microservice-phase1-r1` | `oracle-restore` / `oracle_restore` |
| `oracle-recovery-worker-sm4` | `oracle-recovery-service-worker-sm4:20260728-microservice-phase1-r1` | `sm4` / `doris_sm4` |
| `oracle-recovery-worker-sm3` | `oracle-recovery-service-worker-sm3:20260728-microservice-phase1-r1` | `sm3` / `doris_sm3` |
| `oracle-recovery-worker-sql` | `oracle-recovery-service-worker-sql:20260728-microservice-phase1-r1` | `doris-sql` / `doris_sql` |
| `oracle-recovery-worker-data-sync` | `oracle-recovery-service-worker-data-sync:20260728-microservice-phase1-r1` | `data-sync` / `data_sync` |
| `oracle-recovery-worker-data-platform` | `oracle-recovery-service-worker-data-platform:20260728-microservice-phase1-r1` | `data-platform` / `data_platform` |

The six business image tags point to the same verified worker image content by
design, while the container service mode and queue provide runtime isolation.

Deployment validation:

| Check | Result |
|---|---|
| Candidate image compile and targeted unit suite | Passed twice, 56 tests per image run |
| API health | HTTP 200, MySQL connection OK |
| Legacy API smoke endpoints | HTTP 200 for auth, restore, SM4, SM3, Doris SQL and data platform |
| Worker startup | Six workers ready, concurrency 1, one business queue each |
| Real data sync after deployment | Succeeded, one table and three rows through `data_sync` worker |
| Visible Chrome data sync run and log | Succeeded, three rows shown in the run log |
| Chrome console and page errors | None |

Visible Chrome dimensions:

| Window state | Outer | Inner | Page overflow |
|---|---:|---:|---|
| Maximized | `1920x1032` | `1920x945` | No |
| Left half | `960x1032` | `944x937` | No |
| Right half | `960x1032` | `944x937` | No |
| Restored | `1280x850` | `1264x755` | No |

The first formal switch attempt passed all image tests but rolled back because
the deployment script checked the last worker before Celery had finished its
startup. The readiness check was corrected to wait up to 120 seconds per worker;
the second switch completed successfully. The previous API and monolith Worker
containers remain stopped under timestamped `pre-20260728-microservice-phase1-r1`
names for rollback.
