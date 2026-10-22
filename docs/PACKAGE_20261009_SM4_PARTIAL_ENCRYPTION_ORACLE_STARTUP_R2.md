# 20261009 A 服务器完整包 R2

## 范围与边界

- 包版本：`20261009-sm4-partial-encryption-oracle-startup-r2`。
- 只生成 A 服务器 Docker 17 完整离线包；B 服务器包没有变化。
- 复用 R1 已构建的 API、Worker、MySQL 8.4 和 Redis 镜像，不重复构建业务镜像。
- 本轮只在本地开发和执行包级验证，不替换 128 容器、不执行数据库迁移、不重启服务。

## 本版修复

1. 新增包内 `package-images.env`，统一固定包版本、应用镜像标签和应用镜像归档文件名。
2. `start-service.sh` 与 `load-images.sh` 在读取用户 `.env` 后强制读取包清单；历史 `.env` 中遗留的旧镜像标签不再阻止升级。
3. `.env.example` 不再提供活动的应用版本字段，用户以后升级无需逐项修改 `API_IMAGE`、Worker 标签或 `APP_IMAGE_TAR`。
4. Oracle 21c 创建前核验宿主机/镜像 CPU 架构和镜像内 SQLPlus；容器退出或 DBCA 明确建库失败时立即输出原始诊断，不再等待 1800 秒。
5. 保留默认 `ZHS16GBK` 与 `AL32UTF8` 两个 Oracle 目标，诊断逻辑不删除、移动或重建已有 `oradata`，也不以 `UTF_ORACLE21C_MODE=external` 作为修复方案。

## 兼容性

- 原 `.env` 可直接复用；业务、密码、网络和 Oracle 目标参数仍由 `.env` 管理。
- R1 应用镜像内容没有变化，R2 只更新部署脚本、文档、测试和包清单。
- 回滚时运行 R1 旧包目录，不修改 R2 的 `.env` 指向旧镜像。

## 验证

- 主测试：`387 passed, 1 skipped`。
- Oracle 生命周期及包清单专项：`23 passed`。
- 已知 `test_microservice_modes.py`：`6 failed, 4 passed`，仍为 FastAPI `_IncludedRouter` 兼容性基线，与本次修改无关。
- 前端内联脚本语法检查通过。
- 最终压缩包继续执行 SHA256、UTF-8 无 BOM、LF、Shell 语法、`.env.example`/`package-images.env` 可加载及 Linux 包级校验。

## 未执行

- 未发布到 128，未替换任何运行容器。
- 当前 Windows Docker daemon 不可用，因此没有在本机真实创建 Oracle；真实双 Oracle 启动仍需在授权的 x86_64 Linux 验证环境完成。
