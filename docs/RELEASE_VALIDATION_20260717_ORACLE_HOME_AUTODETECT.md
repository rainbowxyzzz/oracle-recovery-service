# Oracle 19c/21c ORACLE_HOME 自动探测发布验证

## 问题

Oracle 21c 自动导入在 PDB 前置检查阶段失败：

```text
Error 6 initializing SQL*Plus
SP2-0667: Message file sp1<lang>.msb not found
SP2-0750: You may need to set ORACLE_HOME
```

任务配置携带了 19c 默认目录 `/opt/oracle/product/19c/dbhome_1`，应用无条件把它注入 21c 容器。`sqlplus` 命令虽然可以被 PATH 找到，但会使用错误的消息文件目录，因此原有 `command -v sqlplus` 检查出现假通过。

## 修复

- Oracle Home 必须同时包含 `bin/sqlplus`、`bin/impdp` 和 `sqlplus/mesg/sp1*.msb` 才视为有效。
- 配置目录无效时，检查容器现有环境、19c/21c 常见目录，并通过 `command -v sqlplus` 反推。
- 预检真实执行 `sqlplus -V`，并识别 SQL*Plus 初始化错误。
- 容器 Oracle Home 不再传给 Docker 宿主机上的 Python 自动导入进程。

## 验证结果

真实验证容器：`oracle-recovery-oracle21c-ee`

故意输入：

```text
/definitely/missing/oracle-home
```

解析与执行结果：

```text
RESOLVED_ORACLE_HOME=/opt/oracle/product/21c/dbhome_1
SQL*Plus: Release 21.0.0.0.0
Version 21.3.0.0.0
ORCLPDB1
ORACLE_HOME_AUTODETECT_REAL_INTEGRATION_OK
```

自动导入专项测试 `19/19` 通过。热更新后的 API 与 Worker 文件哈希一致，API 健康检查和 Worker ping 正常；验证过程未修改或重启 Oracle 21c 容器。

## 边界

本修复解决 19c/21c 容器之间 Oracle Home 路径不一致导致的 SQL*Plus 初始化失败，不改变 DMP 内容、Schema 映射、分区能力或 Data Pump 导入策略。
