# 2026-08-04 审计整改发布验证记录

## 1. 验证范围

本次承接 `FULL_SYSTEM_FUNCTION_AUDIT_20260801.md` 和 `API_ORCHESTRATION_BUTTON_UI_AUDIT_20260801.md` 中已确认的问题，采用“本地源码开发、128 热更新验证”的方式，未打包、未替换业务镜像。

覆盖内容：

- F-01：离线开发画布拖动阈值。
- F-02：data-platform 工作流通过 Celery `data_platform` 队列执行。
- F-03：`data-sync`、`doris-sql` 与 `data-platform` 路由边界。
- F-04：Worker 使用通用 `WORKER_CONCURRENCY`，不串用 SM3 并发变量。
- F-05：128 系统元数据库从 MySQL 9.3 迁移到 MySQL 8.4。
- F-06：批量授权权限基线字段和回收保护。
- 接口编排中心的消息生命周期、面板互斥、节点单击/拖动分离、自动排布和按钮闭环。

## 2. 备份与发布

源码及 PRD 修改前封存：

```text
.codex-backup/20260801-audit-remediation-prechange/source-and-prd-prechange.tgz
SHA256 1A0CC6FBEEE8B0A900F52E0E3ACF2093892089064284D79291FA2C18508F18F2
```

128 热更新备份：

```text
/root/codex-backups/20260804-audit-remediation-predeploy
```

128 发布目录：

```text
/root/codex-release/20260804-audit-remediation-r1
```

本次没有重建镜像；当前 API/Worker 镜像沿用 128 已验证镜像，代码通过容器内文件热更新发布。

## 3. MySQL 8.4 迁移后核验

迁移方式为逻辑导出、新建独立 8.4 数据卷、导入和核心表行数/表清单比对，旧容器和旧卷保留为回滚点。

| 项目 | 结果 |
|---|---|
| 当前容器 | `oracle-recovery-mysql` running |
| 镜像 | `mysql:8.4` |
| 实际版本 | `8.4.11` |
| 当前卷 | `oracle_recovery_mysql_data_84_20260804` |
| 旧容器 | `oracle-recovery-mysql-93-backup-20260804`，保留 |
| 旧卷 | `oracle_recovery_mysql_data`，保留 |
| API 健康 | HTTP 200，MySQL 连接成功 |

8 项资源参数全部符合项目规则：

```text
sort_buffer_size         16777216
join_buffer_size          4194304
read_buffer_size          1048576
read_rnd_buffer_size      4194304
tmp_table_size           268435456
max_heap_table_size      268435456
max_allowed_packet       268435456
innodb_buffer_pool_size  536870912
```

## 4. 队列和工作流闭环

迁移后重新创建隔离流程并通过真实 API/Worker 链路验证：

```text
workflow  5c9d88c5-08c7-4326-bac5-2b538f7886ab
version   f664add7-e210-45b1-a2a1-def51519039a
run       f5be097c-e5ca-4844-9fef-549e8e73e8dd
status    succeeded
node      succeeded
```

Worker 日志确认真实接收并执行 `data_platform.workflow_run`；运行结束后 `data_platform` 队列为 0，隔离工作流已清理。

最终队列深度全部为 0：

```text
celery oracle_restore doris_sm3 doris_sm4 doris_sql
data_sync data_platform resource_provisioning api_orchestration
```

最终数据库无 `queued/running` 残留：

```text
data_platform_workflow_runs  0
data_platform_component_runs 0
api_orchestration_runs       0
```

## 5. 128 服务核验

API、Oracle、SM4、SM3、SQL、data-sync、data-platform、resource-provisioning、api-orchestration Worker 均为 running，重启次数为 0。最终复核近 30 分钟未发现新增 `Traceback`、`Exception`、`Access denied`、`Out of sort memory`、`can't start new thread` 或应用级错误日志。

批量授权权限基线字段在 `batch_auth_grant_users` 中完整存在：

```text
privilege_existed_before
granted_by_this_batch
revoke_decision
revoke_decision_reason
checked_before_grant_at
checked_before_revoke_at
revoked_at
```

## 6. 可见 Chrome 验收

使用专用可见 Chrome CDP 会话，硬刷新禁用缓存后验收，避免复用热更新前旧标签页。

| 窗口状态 | outer | inner | DPR | 页面级溢出 |
|---|---:|---:|---:|---|
| 最大化 | `1920x1032` | `1920x945` | 1 | 无 |
| 左半屏 | `960x1032` | `944x937` | 1 | 无 |
| 右半屏 | `960x1032` | `944x937` | 1 | 无 |
| 还原 | `1280x820` | `1264x725` | 1 | 无 |

接口编排中心真实闭环：

- 新建流程默认只显示 Start/End 画布，节点工作台关闭。
- 节点库、运行 Input、节点工作台互斥。
- 节点按下阶段不打开工作台；完整单击后打开工作台。
- 节点拖动 30px 后只移动节点，不误开工作台，状态变为未保存。
- 自动排布提示位于消息区域，不遮挡画布工具栏，约 4 秒自动消失。
- 连接器保存 HTTP 201，真实测试 HTTP 200；停用后发送被阻止；重新启用、搜索和删除闭环通过。
- SQL API 保存 HTTP 201，真实 MySQL 参数化查询 HTTP 200 并返回入参；停用、重新启用、搜索和删除闭环通过。
- 流程保存 HTTP 201、发布 HTTP 200、运行 HTTP 202；运行中心显示 `succeeded` 和 Start/End 节点日志。

离线开发和数据变化画布：

- 3px 轻微移动不修改节点坐标。
- 超过 5px 才进入拖动并更新临时坐标。
- 离线开发画布发生 `pointercancel` 后恢复未保存的临时位置。
- 两个画布在还原窗口中页面级无横向溢出。

截图目录：

```text
artifacts/128-releases/20260804-audit-remediation/chrome/
```

其中包含最大化、左半屏、右半屏、还原窗口下的连接器、流程设计、SQL API、运行中心，以及离线开发和数据变化画布截图。

Chrome 正向操作复核后：

```text
pageerror = 0
unexpected console error = 0
```

本轮主动验证停用资产时产生的两个 HTTP 400 是预期业务拒绝响应；清空错误采集后，连接器健康检查和 SQL API 正向查询均无控制台错误。

## 7. 自动化测试

前轮本地验证结果：

```text
专项测试：51 passed
完整回归：192 passed, 1 skipped
Python compileall：通过
内联 JavaScript node --check：通过
```

## 8. 结论与遗留边界

本轮 F-01 至 F-06 和接口编排按钮级 UI 整改均已完成并在 128 验证通过；MySQL 8.4 迁移后的应用、队列、日志和权限基线均正常。未生成部署包，未替换镜像，未执行回滚演练。

接口编排高级认证、失败节点原位续跑、并行/汇聚、数组、人工确认、补偿和 Webhook 等仍属于 PRD 后续分期能力，不应被本报告宣称为已完成。
