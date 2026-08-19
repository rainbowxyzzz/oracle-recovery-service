# 2026-07-29 数据连接授权角色删除发布验证

## 1. 发布范围

本次变更仅涉及数据空间开通微服务下的“数据连接授权”二级子应用：

- `importDataPermissions` 成功后读取并持久化顶层 `result` 角色 ID。
- 页面按授权行展示角色 ID、删除状态和手动“删除角色”操作。
- 新增角色删除接口：
  `POST /api/v1/resource-provisioning/permission-batches/{batch_id}/rows/{row_id}/delete-role`。
- 外部系统严格调用：

```text
POST /api/dash/role/ext/delete
{
  "token": "...",
  "roleId": 123
}
```

- 删除复用有数 Token 内存缓存；认证失效时只进行一次受控刷新。
- 删除状态和 `delete_role` 步骤日志独立持久化，删除成功后保留原始 `role_id` 用于审计。

本次未修改其他业务 Worker，也未重打完整 Docker Run 包；另行生成只包含两个受影响镜像和更新脚本的最小增量包。

## 2. 128 发布版本

```text
API_IMAGE=oracle-recovery-service-api:20260729-resource-role-delete-r1
RESOURCE_PROVISIONING_WORKER_IMAGE=oracle-recovery-service-worker-resource-provisioning:20260729-resource-role-delete-r1
```

镜像 ID：

```text
API=sha256:eeeece61b082ef1767d68f583bdfa846ab19c3662eb2889e2c57e440a68aa65a
RESOURCE_PROVISIONING_WORKER=sha256:dfffd36002ae2c2bccc1ea441f5730c3f1c9dca25131488bc40f3a898d825632
```

## 3. 自动化验证

候选镜像内执行完整测试：

```text
Ran 157 tests
OK (skipped=1)
```

跳过项是候选镜像没有 Node.js，非业务测试失败。Windows 本地直接运行微服务模式测试仍受既有 FastAPI `0.140` 与 `_IncludedRouter` 兼容问题影响；Docker 候选镜像测试已通过。

角色删除专项用例覆盖：

- 成功响应的 `result` 必须能转换为正整数角色 ID。
- 缺失、空值、非整数和非正数角色 ID 均按授权失败处理。
- 删除请求只包含 `token` 和 `roleId`。
- Token 失效后刷新一次并重试。
- 删除中的行禁止重复提交。
- 删除成功后保留 `role_id`，写入 `deleted` 状态和 `delete_role` 成功日志。

## 4. 128 真实闭环

128 使用隔离数据和测试 mock 完成以下验证：

- 开通批次成功，授权批次独立创建并成功执行。
- 授权接口返回的角色 ID 正确写入授权行。
- 授权 Token 401 后刷新成功，HTTP 500 不误触发刷新。
- 失败行重试成功，已成功步骤未被重复执行。
- 删除接口只收到 `token` 和 `roleId`。
- 删除第一次返回认证失效后刷新 Token，第二次调用成功。
- 删除结果返回 `deleted`，角色 ID 保留，`delete_role` 日志可回读。
- 重复删除被拦截，外部删除接口未重复调用。
- Token 和密码未进入持久化日志或验证结果。
- MySQL 新字段和索引存在，Redis 队列为空。
- 测试用户、测试库、`TESTS`、测试批次和 mock 容器已清理。

最终审计结果：

```text
RESOURCE_DATA_PERMISSIONS_FINAL_AUDIT_OK
```

机器可读证据：

```text
artifacts/128-releases/20260729-resource-role-delete-r1/resource-data-permissions-e2e-result.json
```

## 5. 打包边界

`oracle-recovery-service-docker-run-20260729-resource-data-permissions-r1-no-business-db.tar.gz`
生成于角色删除开发之前，不包含本次角色删除功能，也没有被本次增量交付覆盖或重组。

本次新增最小增量包：

```text
oracle-recovery-service-incremental-update-20260729-resource-role-delete-r1.tar.gz
```

最终交付校验：

```text
size=425582539 bytes
sha256=a8f4909e54016e8673e098a8d817c854f41d8a3aae0b4aec5c837c91d6d5b0a9
result=RESOURCE_ROLE_DELETE_INCREMENTAL_PACKAGE_OK
```

增量包只包含 API、Resource Provisioning Worker 两个镜像，以及镜像加载、更新、状态和回滚脚本。它复用现有完整包的 `.env`、`config`、Oracle Client 空目录和持久卷，不包含其他 Worker、MySQL、Redis 或业务数据库镜像。

增量更新脚本已在 128 的 Docker `1.13.1` 完成以下验证：

- UTF-8 无 BOM、LF 和 `sh -n` 检查。
- 镜像 SHA256、`gzip -t`、`docker load` 和精确镜像 ID 检查。
- 无在途任务只读预检。
- 系统元数据库备份和幂等迁移。
- 仅替换 API 与 Resource Provisioning Worker。
- API 健康、Worker ready、页面标记和删除路由检查。
- 手动回滚旧容器后健康恢复，再次更新成功。
- 迁移完整输出单独留档，成功输出不再混入 `aiomysql` 进程退出警告。
