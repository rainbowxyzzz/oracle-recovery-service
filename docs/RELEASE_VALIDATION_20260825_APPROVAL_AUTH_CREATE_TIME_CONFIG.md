# 审批流自动授权 createTime 与配置项发布验证

日期：2026-08-25

## 发布范围

- 模式：模式二，发布到 `192.168.150.128` 并验证。
- 更新审批流自动授权服务和页面，不改数据库结构或 Worker 容器。
- Doris 用户名与有数 `apiAdd.name` 的日期后缀改为同一待办记录 `getMyTodoList.createTime` 的 `MMDD`。
- 新建配置默认映射库、授权信息库均为 `ai_recovery`，`apiAdd.paths` 默认目录为 `API自动授权`；页面提供三个独立配置项。

## 发布前检查

- 128 API 和 8 个独立 Worker 运行正常。
- `approval_authorization_runs` 中 `created/running` 在途任务数为 0。
- 本地 Python 编译、内联 JavaScript 语法和 `git diff --check` 通过。
- 本机缺少 `pytest`、`sqlalchemy` 测试依赖，新增单元测试未执行。

## 发布与回退点

- 发布前备份目录：`/opt/oracle-recovery-hotfix-backups/approval-auth-create-time-and-config-20260825-102903`。
- 仅替换 API 容器的 `/app/src/recovery_service/services/approval_authorization.py` 与 `/app/src/recovery_service/static/ui.html`，随后重启 `oracle-recovery-api`。
- 未重启任何 Worker；若需回退，可将备份目录中的两份文件复制回 API 容器相同路径并重启 API。

## 发布后验证

- API 容器启动完成，`GET http://192.168.150.128:8000/api/v1/health` 返回 `status=ok` 且 MySQL 连接成功。
- API 与 8 个独立 Worker 均为运行状态。
- 发布后 API 日志未发现 `Traceback`、`ERROR` 或 `Exception`。
- 审批授权仍无在途任务。
- 受本机浏览器连接信任路径故障影响，未能完成可编辑配置页面的真实保存/回读浏览器闭环；该项未标记为通过，需在 128 页面补充验证。
