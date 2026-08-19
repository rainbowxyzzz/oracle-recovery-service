# Oracle 21c 容器生命周期与快速补丁验证

## 快速补丁问题复盘

旧快速补丁没有在目标 Linux/老 Docker 环境按最终 ZIP 原样执行。打包过程中还错误地把 Linux `sed` 命令内联到 PowerShell，导致 `$` 被提前处理，文件中的小写字母 `r` 被删除，出现 `worker` 变成 `worke` 等破坏。

R3 补丁已改为：

- 通过 `apply_patch` 重建 shell 文件；
- 从原始源码重新复制 Python 文件；
- 通过独立 Linux 脚本打包，不再内联混用 PowerShell 与 shell；
- 对最终 ZIP 做无 BOM、`sh -n`、`bash -n`、原样解压执行验证。

最终执行结果：

```text
HOTFIX_OK
RUNNER_SHA256=8ca8fbb599c312e5cc7dffbdde1f6585cb8637f1a6cdaa52494a4db191e637ff
TOOL_SHA256=9e3d9f9f33e1a02d15f618bf51f021a81404881164a708cf17ec5fcc1c63cc4e
HOTFIX_R3_EXACT_ZIP_TEST_OK
```

## Oracle 21c 生命周期验证

真实镜像：`softwareplant/oracle:clean-21.3.0-ee`  
真实容器：`oracle-recovery-oracle21c-ee`  
数据库：Oracle 21.3 / `ORCLPDB1`

已验证：

1. 容器已运行：保留容器，检查挂载并重复执行幂等初始化。
2. 容器已停止：自动启动，等待 PDB 后初始化。
3. 容器不存在、镜像存在：原容器停止并改名保留，脚本从镜像创建同名新容器，挂载持久数据并完成初始化；测试后删除新容器、恢复原容器。
4. 强制容器模式缺少镜像：明确报错并停止。
5. 自动模式没有容器、镜像或镜像包：明确记录并跳过，不阻断应用启动。

创建后的验证结果：

```text
SQL*Plus: Release 21.0.0.0.0 - Production
Version 21.3.0.0.0
ORCLPDB1
RECOVERY_DMP_DIR=/opt/oracle/recovery_dmp
RECOVERY_TABLESPACE_DIR=/opt/oracle/recovery_tablespaces
ORACLE21C_LIFECYCLE_REAL_TEST_OK
```

测试结束后，原 `oracle-recovery-oracle21c-ee` 容器已恢复并确认 `healthy`。测试过程没有删除或重建 Oracle 持久数据目录。
