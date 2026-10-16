# 2026-10-16 审批授权更新与密码查询完整 A 包

## 版本和范围

- 版本：`20261016-approval-update-password-r1`，Docker 17.03 完整离线 Docker Run A 包；不发布 128。
- 功能提交：`fdbdb9b`。仅提交本次审批授权服务、单行 UI 说明、查询脚本、测试和文档，其他既有未提交改动保持原状。
- 结构和镜像基线：最近完整包 `20261012-sm4-like-dual-oracle-r1`；保留该包已有 SM4、知识库等能力。
- 源码增量：已提交的 Oracle 连接修复 `5a78bd9`、审批授权 apiUpdate 和密码查询 `fdbdb9b`；按项目打包硬规则同步当前 Oracle initializer 与对应测试，避免历史启动日志导致误判失败。精确文件摘要见包内 `SOURCE-MANIFEST.json`。
- 包含 API 和全部兼容/独立 Worker 共 11 个应用镜像标签、系统 MySQL 8.4、Redis 7-alpine、源码与回归测试。
- 不包含业务 Oracle、Doris、SQL Server、MySQL 恢复目标库镜像，也不包含 OpenMetadata B 包；未混入独立 Oracle UTF8 重建脚本。
- `.env.example` 与基线逐字节一致。应用版本由 `package-images.env` 管理，保留生产 `.env` 即可，无需修改版本相关参数。

## 测试证据与边界

- 当前工作区回归：442 passed、4 skipped、45 subtests。
- 实际交付候选源码回归：435 passed、1 skipped、45 subtests。数量差异来自未纳入本包的独立 Oracle UTF8 脚本测试，并非删除既有交付测试。
- 审批授权/查询脚本/UI/Oracle initializer 专项：49 passed、2 subtests。
- 128 MySQL SELECT-only 查询验证通过，没有写测试数据、读取真实密码或尝试 Doris 登录。
- 所有镜像层/config 摘要、11 个标签、旧 Docker 层级父链、交付源码字节与归档一致性检查通过。
- Linux 候选目录 Shell 语法、UTF-8 无 BOM、LF、环境加载、旧版本变量覆盖、Docker 17 参数与 MySQL 调优契约通过。
- 最终压缩包通过同目录 `.sha256` 和解压后 `verify-package.sh` 校验；完整压缩包交付路径见本次交付说明。
- 未执行新镜像容器启动、真实有数 apiUpdate 联调、生产密码读取或 Doris 登录；未替换/重启 128 服务。包级校验不能替代生产业务验收。

## 密码查询（无需升级）

单独上传 `find-approval-doris-password.sh` 到应用宿主机：

```sh
sh ./find-approval-doris-password.sh '张三_1234_1016'
```

默认 API 容器 `oracle-recovery-api`，自定义名称作为第二个参数。输出的是关联任务当前保存的默认密码候选；默认密码被覆盖或用户在外部改密时，不能恢复历史值。详见 `APPROVAL_DORIS_PASSWORD_LOOKUP.md`。

## 完整包升级步骤

1. 将 `.tar.gz` 与同名 `.sha256` 上传到应用宿主机；确认无在途任务，保留旧包、旧 `.env` 和系统库备份。
2. 校验和解压：

```sh
sha256sum -c oracle-recovery-business-docker17-offline-20261016-approval-update-password-r1.tar.gz.sha256
tar -xzf oracle-recovery-business-docker17-offline-20261016-approval-update-password-r1.tar.gz
cd oracle-recovery-business-docker17-offline-20261016-approval-update-password-r1
```

3. 将旧包的 `.env` 复制到新包目录，保持真实账号、凭据密钥、路径、连接配置不变；不要用模板覆盖生产配置。首次部署才由 `.env.example` 创建 `.env` 并配置实际参数。
4. 在新包目录依次执行：

```sh
sh ./verify-package.sh
sh ./start-service.sh
sh ./status-service.sh
```

5. 访问原应用地址 `http://服务器IP:8000/`，使用隔离申请验证创建连接、apiUpdate、权限导入和状态回写。apiAdd 必须实际返回非空 `defaultSchemaName`；缺失会明确阻断，不能绕过继续授权。
