# 2026-10-12 SM4 LIKE 与双 Oracle 完整离线包

## 包信息

- 包版本：`20261012-sm4-like-dual-oracle-r1`
- 包类型：A 服务器 Docker 17.03 完整离线 Docker Run 包
- 应用镜像：`oracle-recovery-service-*:20261012-sm4-like-dual-oracle-r1`
- 系统依赖镜像：`mysql:8.4`、`redis:7-alpine`
- 不包含：业务 Oracle 镜像、Doris、SQL Server、MySQL 恢复目标库和 OpenMetadata B 包

## 本版内容

- 包含当前 SM4 整字段、四类局部加解密和 `CQ_SM4_LIKE` 能力。
- 应用镜像版本继续由 `package-images.env` 管理，升级时不需要在既有 `.env` 中填写新版本镜像标签。
- 同一 Oracle 21c EE 镜像按顺序管理两个独立实例：主实例 `ZHS16GBK`，备用实例 `AL32UTF8`；两者仅共享 DMP 输入目录。
- 保留 Oracle 镜像架构、Oracle Home、DBCA 失败诊断、字符集校验、TEMP Smallfile 上限和既有 `oradata` 保护逻辑。

## 验证结果

- 主测试：`390 passed, 1 skipped, 43 subtests passed`。
- Oracle、打包和 SM4 专项：`42 passed, 8 subtests passed`。
- 双实例入口执行测试验证主库先于 UTF8 库，且 SID、PDB、字符集和数据目录互相隔离。
- Linux 解压、包内 SHA256、Shell 语法、`.env.example` 加载、UTF-8 无 BOM 和 LF 换行检查通过。
- 离线应用镜像归档共 11 个标签，配置摘要、旧版 layer 父链、源码层摘要及当前 `CQ_SM4_LIKE` 源码一致性检查通过。

本次只生成交付包，没有替换或重启 128 上的运行容器。Oracle 生命周期脚本与 2026-10-09 已在 128 完成双实例新建验证的 R3 版本一致；当前任务未重复清理业务环境或重建数据库。
