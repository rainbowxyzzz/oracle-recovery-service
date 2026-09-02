# 20260831 完整包验证结果

## 交付版本

- 包名：`oracle-recovery-service-docker-run-20260831-full-source-r1-no-business-db.tar.gz`。
- 大小：485162702 字节（约 463 MiB）。
- SHA256：`14193d19128e440f3e7c2d931b8df802d0750bad9181fd84c4317ae6e0fc32fe`。
- 构建目录：128 `/root/codex-packaging/20260831-full-source-r1`。
- 基线：上一完整包 20260826，源码 `5330cc0` 加 20260831 未提交的 CSV 修复；用户明确要求按最新源码打包。
- 包含审批流最新修改及全部当前源码模块；详见 `PACKAGE_20260831_FULL_SOURCE.md`。

## 已执行检查

1. 本地完整测试：277 passed、1 skipped、32 subtests passed。
2. API、通用 Worker 镜像内 162 个运行文件 SHA256 与最新源码一致；9 个专用 Worker 标签指向该最新 Worker 镜像。
3. 使用断网的临时容器检查模块导入、审批申请日期规则、动态 Bearer、Worker 并发入口及 Java8 编译分支；API 不执行文件导出，其镜像无 openpyxl/pyarrow，实际导出 Worker 的依赖为 openpyxl 3.1.5、pyarrow 19.0.1，检查通过。
4. `.env.example` 与上版逐字比较，只有版本标签与镜像包名变化。
5. 启动脚本以模拟 Docker 分别覆盖外部/本地系统库配置：11/13 次模拟 run，均保留旧 Docker 兼容参数，本地 MySQL 固定 8.4 且包含规定资源参数。
6. 应用镜像仅导出一次，共 11 个标签；没有业务数据库或基础设施镜像。
7. 外层完整包在 Linux 解压，逐文件 SHA256、UTF-8 无 BOM、LF、全部 Shell 语法及 `.env.example` 加载通过。历史 CSV 验证报告 BOM 已在交付副本规范化，不改变仓库测试数据或业务代码；复用了已导出的镜像。
8. 本项目既有容器 ID/启动时间保持不变，API health 返回 ok、MySQL 连接成功。

## 镜像

- API：`oracle-recovery-service-api:20260831-full-source-r1`，ID `sha256:4b95e2a341a8b205c30ff9dc57b521911de7f798423d599b6721b1dfb4e0b3bb`。
- Worker：`oracle-recovery-service-worker:20260831-full-source-r1`，ID `sha256:dc358b366203b18797ddcd58663f7584ef16e393017af87c604ffad43e315343`。

## 边界与现场并行变化

本次仅打包，不执行服务替换、数据库迁移、真实调度/授权任务或浏览器验收。不能据此声称 128 所有在运行 Worker 已升级，或完成新镜像的全系统部署验收。

打包期间另两个无关容器 `scip-secure-app`、`scip-secure-doris` 被其他操作更新，最终环境检查已将其与本项目区分；本次没有操作这两个容器，也没有重启宿主机 Doris。

包内含 `PACKAGE-CHECKS.json`、`SOURCE-MANIFEST.json`、`IMAGE-MANIFEST.txt`、`SHA256SUMS.txt`、源码快照及测试。128 额外保留 `FINAL-VERIFICATION.json` 和 `package-hash-check.log`，可追溯最终包校验。GitHub 未提交。
