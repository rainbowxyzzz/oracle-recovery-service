# Oracle 导出日志辅助专项导入真实验证报告

## 1. 验证范围

验证“Oracle 导出目录同时存在 DMP 和 expdp 日志”时的专项导入流程，包括日志解析、DMP 分卷绑定、直读零复制、源缺口门禁、日志归档和任务结果状态。

验证环境使用 Oracle 19c 代替 Oracle 11g，导出和导入参数均采用 Oracle 11g 兼容语法。该结果不等同于 Oracle 11g 实机认证。

## 2. 环境与夹具

| 项目 | 值 |
|---|---|
| 验证主机 | `192.168.150.128` |
| Docker | `1.13.1` |
| Oracle | 19c，PDB `ORCLPDB1` |
| Oracle 容器 | `oracle-recovery-oracle19c` |
| 共享宿主机目录 | `/data/oracle-recovery/oracle19c/dmp` |
| 共享容器目录 | `/opt/oracle/recovery_dmp` |
| 源 schema | `LOGASSIST_SRC` |
| 源表 | `T_LOG_ASSIST` |
| 数据量 | 1000 行 |
| DMP | `codex_logassist_20260716_01.dmp` |
| 导出日志 | `codex_logassist_20260716.log` |
| 导出命令 | `expdp ... DIRECTORY=RECOVERY_DMP_DIR DUMPFILE=codex_logassist_20260716_%U.dmp LOGFILE=codex_logassist_20260716.log TABLES=LOGASSIST_SRC.T_LOG_ASSIST EXCLUDE=STATISTICS METRICS=YES` |

## 3. 自动化回归测试

API 和 Worker 容器分别执行完整测试集：

```text
Ran 71 tests in approximately 1 second
OK (skipped=1)
```

跳过项为容器内没有 Node.js 的画布布局行为测试，与本次 Oracle 功能无关。新增回归测试覆盖 SFTP `exit()` 同步关闭行为，防止日志读取成功后被关闭异常误判为解析失败。

## 4. 真实 E2E 结果

| 场景 | 预期 | 实际 |
|---|---|---|
| 正常日志 + DMP 直读 | 专项导入成功 | 通过，`oracle_export_log_assisted=true` |
| 日志与分卷精确绑定 | `state=exact` | 通过 |
| 直读不复制文件 | `zero_copy_dump=true`、`copied_files=[]` | 通过 |
| 目标数据校验 | 目标表 1000 行 | 通过 |
| 日志下载 | ZIP 包含 `source_export/codex_logassist_20260716.log` | 通过，HTTP 200 |
| 日志声明缺少一个源对象，未授权正式导入 | 阻断 | 通过，任务失败 |
| 同一缺口 dry-run | 生成计划并带告警 | 通过，`succeeded_with_warnings` |
| 同一缺口显式授权正式导入 | 允许继续但带告警 | 通过，`succeeded_with_warnings` |
| 授权导入后的目标数据 | 已导出的表仍为 1000 行 | 通过 |

真实任务编号：

```text
clean_task        = 98794251-1b23-4ae9-9d56-0d1295ee7d71
blocked_gap_task  = 48674f59-1e08-4c22-83dd-74b14615f16e
dry_run_gap_task  = 0b542910-1791-4832-b635-df2710cc1409
accepted_gap_task = 1e1213cf-7d26-480a-840a-15af7e9265a3
```

## 5. 问题与修复

首轮真实验证发现 `AsyncSSHClient.close()` 对 `asyncssh.SFTPClient.exit()` 使用了 `await`。该方法在当前 AsyncSSH 版本中同步返回 `None`，导致日志内容虽然已读取，却被上层报告为 `read_or_parse_failed`。修复为同步调用，并新增回归测试后重发验证。

另发现验证脚本自身的两个问题：API 详情字段已嵌套在 `task` 下，以及 ZIP 内归档路径没有前导斜杠。两项均只影响测试脚本读取和断言，不属于产品代码；修正后重新执行完整 E2E。

## 6. 清理与服务复核

已删除：

- `LOGASSIST_SRC`
- `CODEX_LOGASSIST_20260716_SET_2`
- `TBS_CODEX_LOGASSIST_20260716_S`
- `/data/oracle-recovery/oracle19c/dmp/codex_logassist_20260716*`

清理脚本输出 `ORACLE_LOGASSIST_CLEANUP_OK`。复核结果：API health 为 `ok`，Worker Celery `ping` 返回 `pong`，Oracle 容器状态为 `healthy`。

## 7. 结论

本次新增专项流程在当前 Oracle 19c 验证环境和现有系统能力范围内通过。它能够利用可信的 expdp 导出日志补充导入计划和缺口判断，但仍以实际 DMP 探测和导入结果为最终依据；对 Oracle 11g 的结论应限定为“使用 11g 兼容语法在 19c 上验证”，后续仍需 Oracle 11g 实机补充认证。
