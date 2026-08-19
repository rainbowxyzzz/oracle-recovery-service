# 测试报告问题优化闭环说明

更新时间：2026-07-11

## 0. 2026-07-13 本轮新增闭环

### SM4 自动密钥解密闭环

- SM4 密钥版本增加 Doris 连接归属信息。
- SM4 加密批次增加 `sm4_key_version_id` 和 `sm4_key_fingerprint`，提交批次时必须绑定密钥版本。
- 页面“文件脱敏/解密”新增“历史加密批次”密钥来源，用户可填写历史 SM4 批次 ID 自动找回当时绑定的密钥版本。
- SM4 密钥创建与任务提交已解耦；任务只绑定目标数据库当前已成功部署并验证的密钥版本，不再在提交前隐式刷新密钥。
- 已发布到 128，并验证：
  - `/ui` 返回 200。
  - `doris_sm4_key_versions.connection_id/connection_name` 已补列。
  - `doris_sm4_batch_jobs.sm4_key_version_id/sm4_key_fingerprint` 已补列。

### Oracle 自动导入 preflight 闭环

- `OracleAutoImportRunner` 新增任务执行前结构化检查项：
  - SSH 连通。
  - 宿主机 Python 3.7+。
  - 自动导入脚本上传和 `py_compile`。
  - Oracle Docker 容器运行/健康状态。
  - 容器内 `sqlplus` 和 `impdp`。
  - 宿主机 DMP 文件或 `DUMPFILE` 模式匹配。
  - 容器内 DMP 目录可访问。
  - PDB 可连接，且不落在 `CDB$ROOT`。
  - DIRECTORY 使用受控路径策略。
- preflight 失败时不再继续正式导入，任务结果中返回 `preflight_checks`，每项包含 `code/name/state/message/detail/suggestion`。
- 已发布到 128，并验证：
  - 使用不存在的 `codex_probe_missing.dmp` 时，只有 `host_dmp_files` 结构化失败。
  - 使用真实分卷模式 `codex_directdmp_%U.dmp` 时，9 项检查全部通过。

### Oracle 数据连接测试明细闭环

- 数据连接测试页面新增明细展示区。
- Oracle 连接测试结果明确展示：
  - 数据库连接是否成功。
  - 服务器 SSH 是否成功或是否被跳过。
  - Docker 容器是否成功、是否 running、健康状态。
- 容器检查从“能 inspect 到容器名”增强为“必须正在运行”。
- 已发布到 128，并用默认 Oracle 连接验证返回：
  - 数据库成功。
  - SSH 成功。
  - `oracle-recovery-oracle19c` running=true，health=healthy。

## 1. Oracle 自动导入脚本兼容问题

### 问题

测试报告发现历史恢复任务失败：

```text
/opt/oracle-recovery-service-package/tools/oracle_dmp_auto_import.py, line 74
source_dir: str
SyntaxError: invalid syntax
```

根因方向：远端执行环境只校验了“Python3”，但自动导入脚本依赖变量注解、dataclass 等 Python 3.7+ 能力；如果远端 `python3` 是过低版本，仍会出现语法错误。

### 已落地优化

- `OracleAutoImportRunner` 远端 Python 版本要求从 Python 3 提升为 Python 3.7+。
- 上传 `oracle_dmp_auto_import.py` 后，正式执行前先运行：

```bash
python3 -m py_compile /opt/oracle-recovery-service-package/tools/oracle_dmp_auto_import.py
```

- 如果远端脚本不能被当前解释器编译，任务会在预检阶段失败，并返回明确错误：

```text
Oracle auto import script is not compatible with remote Python: ...
```

### 验收闭环

1. 在 Oracle 宿主机确认 `python3 --version` 不低于 3.8。
2. 重新提交一次直连目录 DMP 导入任务。
3. 预期不再出现 `source_dir: str SyntaxError`。
4. 若失败，错误应指向 DMP、impdp、目录权限、表空间或 Oracle 原生日志，而不是 Python 语法。

## 2. 任务时间 8 小时错位问题

### 问题

测试报告发现历史任务中：

```text
created_at = 2026-07-10T21:33:08
updated_at = 2026-07-10T21:33:12
finished_at = 2026-07-10T13:33:12
```

同一任务完成时间比创建时间早 8 小时，说明部分代码使用 UTC 写入业务 DATETIME，而创建/更新时间使用数据库会话时区。

### 已落地优化

- 恢复任务 `finished_at` 改用 `app_now()`。
- 恢复任务步骤 `finished_at` 改用 `app_now()`。
- Worker 正常结束和异常结束都写入 `finished_at=app_now()`。
- SM3 任务、SM3 任务模板、脱敏资产完成时间改用 `app_now()`。
- 用户最后登录时间、API Key 最后使用时间改用 `app_now()`。
- JWT token 的 `exp` 仍保留 UTC 时间戳逻辑，不参与页面业务时间展示。

### 验收闭环

1. 新建一个恢复任务或使用测试任务触发失败。
2. 查看 `/api/v1/tasks` 和 `/api/v1/tasks/{id}/detail`。
3. 预期 `finished_at >= created_at`，并且与当前 Asia/Shanghai 时间一致。
4. 页面中“提交时间/结束时间/耗时”不再出现负数或 8 小时错位。

## 3. 批量授权真实授权/下线专项

### 问题

全面测试为避免误撤真实部门用户权限，没有直接执行真实 `GRANT/REVOKE`。

### 落地验收方式

使用专用测试对象，不使用现有业务部门用户：

| 对象 | 建议值 |
|---|---|
| 测试部门 | Codex授权测试处 |
| 测试用户 | codex_auth_test |
| 部门库 | DWH_Codex授权测试处 |
| 源库表 | MAP.sm3_map_id |

验收步骤：

1. 初始化导入测试部门、测试用户、测试部门库。
2. 授权清单导入：

```text
序号,库名,表名,类型
1,MAP,sm3_map_id,二类
```

3. 执行授权后，用 `codex_auth_test@%` 验证可查询 `MAP.sm3_map_id`。
4. 手动下线该授权批次。
5. 再次用 `codex_auth_test@%` 验证源表查询权限已回收。
6. 验证 `DWH_Codex授权测试处` 基础权限仍保留。

### 后续建议

下线逻辑应长期保留“本批次权限基线”，即只回收本批次新增权限，避免误撤用户原本已有的 Doris 权限。

## 4. SM3 反查数据前置问题

### 问题

SM3 反查接口可响应，但当前 `证件号` 字段类别没有匹配到可用映射表。

### 闭合方式

1. 准备一张确定存在的映射表，例如：

```text
MAP.sm3_map_id
```

2. 页面或接口显式传入：

```json
{
  "mapping_database": "MAP",
  "mapping_table": "sm3_map_id"
}
```

3. 用映射表中真实 `sm3_value` 验证能反查出 `original_value`。

### 后续建议

前端增加“指定映射表反查”入口，避免完全依赖字段关系表自动匹配。

## 5. MySQL 版本确认

### 问题

数据连接测试返回 MySQL `version=9.3.0`。由于部署规则要求系统库镜像使用 MySQL 8.4，需要确认该连接指向的是系统库、目标库还是外部测试库。

### 闭合方式

1. 在 128 服务器执行：

```bash
docker exec oracle-recovery-mysql mysql -uroot -proot -e "SELECT VERSION();"
```

2. 如果系统库不是 8.4，需要按打包规则重建系统库容器。
3. 如果 9.3.0 是外部目标库，在连接名称和部署说明中标注，避免误判为系统库。
