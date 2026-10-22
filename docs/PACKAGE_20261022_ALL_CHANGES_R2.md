# 2026-10-22 当前项目全量 A 包（R2）

## 范围和兼容边界

- 按用户确认，将当前项目全部源码、部署脚本、测试和文档改动一起提交；取代此前限定增量的 `20261016-approval-update-password-r1`。
- 包版本：`20261022-all-changes-r2`。结构、基础镜像和配置模板仍从最近确认的 `20261012-sm4-like-dual-oracle-r1` 完整包派生，应用层使用本次 Git 提交的完整源码，不从 R1 挑选文件。
- 包含审批授权 apiAdd 后 apiUpdate、只读密码候选查询脚本、SM4 局部加解密和 LIKE、Oracle 连接参数统一、双实例启动诊断和独立 UTF8 创建脚本、知识库 MVP，以及全部现有回归用例。
- 包含 API 和所有 Worker/兼容标签、系统 MySQL 8.4、Redis 7-alpine；不包含业务 Oracle/Doris/SQL Server/MySQL 目标库镜像、OpenMetadata B 包、Qdrant 或模型服务。
- 知识库默认关闭，启用仍需单独配置 Vault、向量和模型服务；见 `deploy/knowledge-base/README.md`。
- `.env.example` 不重排、不改变基线默认值；版本由 `package-images.env` 管理。独立 UTF8 工具只读取 `oracle-utf8.env`，不会自动执行 `--reset`，见 `oracle21c-ee/README-standalone-utf8.md`。
- 不包含本机 `.env`、运行数据、私钥、缓存、虚拟环境、临时文件或历史二进制包；“全部改动”指版本管理范围内的项目改动。

## 已执行验证与交付门槛

- 2026-10-22 当前源码主测试：442 passed、4 skipped、45 subtests passed；按项目约定单独隔离 `test_microservice_modes.py`，不把它算作通过。
- 主工作台和独立助手 HTML 内联脚本语法检查通过；Git 空白错误检查通过（保留既有 Markdown 行尾双空格换行）。
- 候选源码测试发现 Git 在 Windows 导出 `package-images.env` 时转换为 CRLF；补充 `.gitattributes` 的 `*.env text eol=lf`，由已有包格式测试验证，不修改业务代码。
- 独立 UTF8 工具的 3 项 Linux 文件系统测试通过（模拟 Docker，真实临时目录备份和隔离检查）。首次普通 Linux 用户因测试根目录权限不足失败，使用本地 Linux root 运行隔离测试后通过；未操作真实容器。
- 修复后 Git 导出的候选源码主测试：442 passed、4 skipped、45 subtests passed。镜像层/config 摘要和旧 Docker 父链、11 个应用标签、Oracle initializer 一致性、原配置模板不变检查通过。
- Linux 候选目录 Shell 语法、UTF-8 无 BOM/LF、配置加载、历史版本参数覆盖、Docker 17 参数与 MySQL 调优检查通过。
- Linux 必须验证最终压缩包解压、SHA256、包内校验、Shell 语法、UTF-8 无 BOM/LF、配置加载和历史版本参数覆盖。实际机器结果保存在包内 `PACKAGE-CHECKS.json`，包内精确源码摘要见 `SOURCE-MANIFEST.json`。
- 本次只做本地回归及 Linux 包级验证，不替换/重启 128，不执行生产密码读取、Doris 改密或真实有数写接口。不把 mock、归档检查或历史验证记录当成本版真实部署验收。
- 本地 Docker daemon 和 JDK 不可用，复用确认包依赖层并更新应用层；没有重新执行容器启动、Java UDF 实机编译、真实 Oracle 建库、页面交互或知识库外部服务联调。

## 密码查询（可单独使用，无需升级）

将包根目录的脚本上传到应用服务器，运行：

```sh
sh ./find-approval-doris-password.sh 'Doris用户名'
```

默认 API 容器为 `oracle-recovery-api`，可用第二个参数指定容器名。脚本只读查询成功授权记录关联配置，输出该配置**当前保存的默认密码候选**，不是从 Doris 哈希反解密码。配置被覆盖、密码被外部修改或缺少记录时不能保证恢复历史密码。详见 `docs/APPROVAL_DORIS_PASSWORD_LOOKUP.md`。

## 使用步骤

1. 将完整 `.tar.gz` 及同名 `.sha256` 上传到 A 服务器。升级前确认无在途任务，备份系统库，保留旧包和原 `.env`。
2. 校验并解压：

```sh
sha256sum -c oracle-recovery-business-docker17-offline-20261022-all-changes-r2.tar.gz.sha256
tar -xzf oracle-recovery-business-docker17-offline-20261022-all-changes-r2.tar.gz
cd oracle-recovery-business-docker17-offline-20261022-all-changes-r2
```

3. 把原 `.env` 复制到新包目录；保留凭据加密密钥、连接、密码、路径等实际配置，不用模板覆盖。首次安装才复制 `.env.example` 并填写真实配置。无需填写本版镜像标签。
4. 启动和检查（将更新应用容器并执行既有系统库迁移）：

```sh
sh ./verify-package.sh
sh ./start-service.sh
sh ./status-service.sh
```

5. 打开 `http://服务器IP:8000/`，使用隔离审批申请检查创建连接、apiUpdate、权限导入和状态回写；返回 `defaultSchemaName` 缺失时必须阻断，不跳过更新。Oracle 仍按已有配置管理两个实例，不通过禁用 UTF8 绕过问题。
