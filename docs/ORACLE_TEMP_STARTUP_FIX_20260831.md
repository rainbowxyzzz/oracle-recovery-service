# Oracle TEMP 启动修复（2026-08-31）

## 背景与规则

现场完整包 `20260831-full-source-r1` 在 Oracle 初始化阶段因固定 `MAXSIZE 100G` 超过 smallfile TEMP 单文件块数限制而报 ORA-03206，中断后续应用启动。只修复 `deploy/oracle21c-ee/initialize.sh`，交付包内同路径为 `oracle21c-ee/initialize.sh`。不修改应用镜像、任务、密钥、审批授权、系统库迁移或容器生命周期。

- 默认仍启用恢复 tempfile 创建，保留 20G/2G/100G 配置及文件名；100G 是请求上限而非保证分配的容量。
- 创建前读取 TEMP 的 BIGFILE 和 BLOCK_SIZE。smallfile 采用 `(2^22-1) * block_size` 向下取整 MiB 的单文件上限；配置上限更小时遵从配置，初始大小与增长步长不能超过有效上限，并按块对齐。输出请求值和实际值。
- 已有同名 tempfile 不调整、不删除、不收缩。BIGFILE TEMP 只保留现有文件并明确提示，不尝试添加第二个文件，不擅自改变已有容量策略。
- 不通过禁用 TEMP 初始化或吞掉 SQL 错误绕过故障；元数据读取失败、创建失败继续阻断启动。关闭 TEMP 自动扩展的旧配置仍不执行 TEMP SQL。
- 保留 PDB 保存、目录创建/授权、SYSTEM 连接验证与 SQLPlus 21c 检查。根 start-service.sh 不变，重跑仍具有原有替换应用容器及迁移的影响，须在维护窗口执行。

## 验收与边界

本地通过 POSIX shell 语法、UTF-8 无 BOM/LF、模拟 Docker 的完整初始化流程与生成 SQL 验证：不同块大小、用户自定义大小、已有文件幂等 SQL、BIGFILE、禁用开关、元数据/SQL 失败传播。模拟不等同于真实 Oracle 执行。

本轮仅交付本地脚本，不连接或修改现场/128，不重打完整镜像包。真实 Oracle 数据文件创建及系统启动仍需现场执行确认；磁盘空间不足等其他错误不属于本次修复。

参考：https://docs.oracle.com/en/database/oracle/oracle-database/21/refrn/physical-database-limits.html
