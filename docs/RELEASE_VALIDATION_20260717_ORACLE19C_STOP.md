# Oracle 19c 导入停止能力发布验证

更新时间：2026-07-17  
验证环境：`192.168.150.128`  
目标容器：`oracle-recovery-oracle19c`  
范围边界：未修改、未重启、未进入 `oracle-recovery-oracle21c-ee`

## 1. 发布内容

- 排队任务继续使用“取消”。
- 运行中的 Oracle 19c 自动导入任务新增“停止导入”。
- 停止中任务新增“强制终止”。
- Data Pump 探测、正式导入和回退导入使用任务级唯一 `JOB_NAME`。
- 普通停止执行 `STOP_JOB=IMMEDIATE`，强制终止执行 `KILL_JOB`。
- Worker 重新读取持久化停止请求，并将主动停止收口为 `cancelled`。
- 停止原因、时间、Job 名称和执行结果写入任务事件及审计。

## 2. 自动测试

- Oracle 自动导入韧性与停止专项测试：`16/16` 通过。
- Worker 重复投递与停止状态收口测试：`4/4` 通过。
- Python 编译检查通过。
- 前端 JavaScript 语法检查通过。

## 3. Oracle 19c 实机验证

验证数据库返回：

```text
Oracle Database 19c Standard Edition 2
Release 19.0.0.0.0
Version 19.3.0.0.0
```

独立创建 `CODEX_STOP_*` 测试 schema、表空间和 300000 行约 146.4MB 数据，验证结果：

```text
JOB_EXECUTING
STOP_JOB_OK
START_JOB_OK
KILL_JOB_OK
ORACLE19C_STOP_VALIDATION_OK
```

停止后确认 Job 状态为 `NOT RUNNING`；恢复后重新进入 `EXECUTING`；强制终止后 `DBA_DATAPUMP_JOBS` 中 Job 数量为 0。

清理结果：

```text
USERS=0
TBS=0
JOBS=0
DIRS=0
DUMP_CLEAN
```

## 4. Chrome 验证

真实 Chrome 验证了最大化、左半屏、右半屏和普通窗口：

- “停止导入”提交 `force=false` 和停止原因。
- 状态切换为 `stopping` 后显示“强制终止”。
- “强制终止”提交 `force=true`。
- 页面无横向溢出、无控制台错误、无 `pageerror`。

窗口与页面尺寸：

| 状态 | 外部窗口 | 页面视口 |
|---|---:|---:|
| 最大化 | 1920×1032 | 2133×1050 |
| 左半屏 | 960×1032 | 1048×1041 |
| 右半屏 | 960×1032 | 1048×1041 |
| 普通窗口 | 1280×820 | 1404×805 |

## 5. 发布状态

- API 健康检查通过。
- MySQL 停止控制字段迁移完成。
- API 与 Worker 日志未发现异常。
- 热更新备份：`/root/oracle-recovery-hotfix-backups/20260717-110840-oracle19c-stop`
