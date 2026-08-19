# 功能优化闭环落地方案

日期：2026-07-12

本文档承接 `PROJECT_PRD_SUMMARY.md` 的当前版本结论，细化下一阶段需要落地的关键优化项。

## 1. 优化目标

本轮优化不以新增大量入口为目标，而是补齐现有高风险能力的业务闭环：

1. 批量授权中心：避免到期回收时误撤用户原本已有权限。
2. SM4 加密中心：自动生成密钥的任务，后续解密不再要求用户手动输入密钥。
3. Oracle 自动导入：任务执行前给出清晰环境检查，减少运行中失败。
4. 数据连接测试：拆清数据库连接与服务器文件访问能力。
5. 文档与测试：形成每版可验收、可追溯的交付说明。

## 2. 批量授权权限基线

### 2.1 当前问题

批量授权执行的是 Doris 源表查询权限授权，例如：

```sql
GRANT SELECT_PRIV ON `MAP`.`sm3_map_id` TO 'fgc'@'%';
```

但用户可能在系统外已经拥有该表权限，或同一权限被多个有效批次重复授权。到期回收时如果直接执行：

```sql
REVOKE SELECT_PRIV ON `MAP`.`sm3_map_id` FROM 'fgc'@'%';
```

可能误撤用户原有权限或其他批次仍需要的权限。

### 2.2 设计原则

- 系统只回收自己新增的权限。
- 系统不回收授权前已经存在的权限。
- 系统不回收仍被其他有效批次引用的权限。
- 所有跳过、回收、失败都要有可读原因。

### 2.3 数据模型建议

在授权用户明细表中补充字段：

| 字段 | 说明 |
|---|---|
| privilege_existed_before | 授权前该用户是否已拥有该权限 |
| granted_by_this_batch | 本批次是否实际新增了该权限 |
| active_reference_count | 当前系统内有效批次引用数 |
| revoke_decision | 回收决策：revoke / skip_existing / skip_referenced / failed |
| revoke_decision_reason | 回收决策说明 |
| checked_before_grant_at | 授权前检查时间 |
| checked_before_revoke_at | 回收前检查时间 |

如不希望改动现有表过多，也可以新增独立表：

```text
batch_auth_privilege_baselines
```

建议字段：

| 字段 | 说明 |
|---|---|
| id | 主键 |
| batch_id | 授权批次 |
| table_id | 授权表明细 |
| user_id | 授权用户明细 |
| connection_id | Doris 连接 |
| db_user_identity | Doris 用户身份 |
| source_database | 源库 |
| source_table | 源表 |
| privilege_type | 权限类型 |
| existed_before | 授权前是否已有 |
| granted_by_batch | 是否本批次新增 |
| grant_sql | 授权 SQL |
| revoke_sql | 回收 SQL |
| revoke_state | 回收状态 |
| revoke_reason | 回收原因 |
| created_at | 创建时间 |
| updated_at | 更新时间 |

### 2.4 授权流程

1. 对每个用户、源库、源表、权限执行授权前检查。
2. 如果已存在权限：
   - 记录 `existed_before=true`。
   - 可以跳过 GRANT 或仍执行幂等 GRANT。
   - 标记 `granted_by_batch=false`。
3. 如果不存在权限：
   - 执行 GRANT。
   - 成功后标记 `granted_by_batch=true`。
4. 写入授权明细、基线记录和审计日志。

### 2.5 回收流程

1. 只处理 `granted_by_batch=true` 的记录。
2. 查询系统内是否还有其他有效批次引用同一用户、同一源表、同一权限。
3. 如果存在其他有效批次：
   - 跳过 REVOKE。
   - 记录 `skip_referenced`。
4. 如果不存在其他有效批次：
   - 执行 REVOKE。
   - 成功记录 `revoke`。
5. 如果 `granted_by_batch=false`：
   - 不执行 REVOKE。
   - 记录 `skip_existing`。

### 2.6 验收标准

- 用户授权前已有权限，到期下线后权限仍保留。
- 两个批次授权同一用户同一源表，先下线一个批次不回收权限。
- 最后一个有效批次下线后，权限被回收。
- 回收页面展示每个用户的回收决策和原因。

## 3. SM4 密钥版本治理

### 3.1 当前问题

如果用户选择自动生成随机密钥种子，加密任务可以正常执行，但后续解密时不能再要求用户手动输入密钥。否则业务流程不闭合。

### 3.2 设计原则

- 每次密钥生成或手动输入都形成密钥版本。
- 加密任务必须绑定密钥版本。
- 解密默认从任务或文件元数据中选择密钥版本。
- 密钥明文不在前端展示。
- 密钥使用必须可审计。

### 3.3 数据模型建议

新增密钥版本表：

```text
doris_sm4_key_versions
```

建议字段：

| 字段 | 说明 |
|---|---|
| id | 主键 |
| connection_id | Doris 连接 |
| key_fingerprint | 密钥指纹 |
| key_source | auto / manual |
| encrypted_seed | 加密保存的密钥种子 |
| function_name | 对外固定函数名 |
| internal_function_name | 内部版本函数名 |
| jar_name | jar 文件名 |
| state | active / inactive / failed |
| created_by_user_id | 创建人 |
| created_by_username | 创建人 |
| created_at | 创建时间 |
| verified_at | 验证时间 |
| message | 摘要 |

加密任务表补充：

| 字段 | 说明 |
|---|---|
| sm4_key_version_id | 本任务使用的密钥版本 |
| sm4_key_fingerprint | 密钥指纹快照 |

文件解密接口补充：

| 参数 | 说明 |
|---|---|
| key_version_id | 指定密钥版本，可选 |
| task_id | 根据历史加密任务自动选择密钥版本，可选 |
| sm4_key | 手动密钥，兜底可选 |

### 3.4 加密流程

1. 用户选择自动密钥或手动密钥。
2. 后端生成密钥版本。
3. 创建或刷新 Doris UDF。
4. 固定函数名验证通过。
5. 加密任务绑定 `sm4_key_version_id`。
6. 执行任务。

### 3.5 解密流程

密钥选择优先级：

1. 用户显式选择 `key_version_id`。
2. 用户选择历史任务 `task_id`，系统读取任务绑定的密钥版本。
3. 文件中携带密钥指纹时，系统按指纹匹配密钥版本。
4. 用户手动输入 `sm4_key`。

如果无法确定密钥版本：

- 不直接失败为技术错误。
- 页面提示用户选择密钥版本或历史任务。

### 3.6 验收标准

- 自动密钥加密任务完成后，可不输入密钥完成解密。
- 手动密钥任务仍支持手动输入密钥解密。
- 页面可看到密钥版本指纹，但看不到明文密钥。
- 错误密钥版本解密失败时，返回清晰失败明细。
- 密钥版本删除或禁用后，不影响历史审计记录。

## 4. Oracle 自动导入 preflight

### 4.1 检查项

任务执行前应检查：

- SSH 是否可连接。
- Docker 是否可执行。
- Oracle 容器是否存在并健康。
- Oracle 容器内 `sqlplus`、`impdp` 是否可用。
- 宿主机 `python3` 是否存在。
- 宿主机 Python 版本是否 `>= 3.7`。
- 自动导入脚本能否 `py_compile`。
- DMP 目录是否存在。
- DMP 文件或 `DUMPFILE` 模式是否能匹配到文件。
- PDB 是否存在且 `READ WRITE`。
- DIRECTORY 是否指向受控目录。

### 4.2 输出格式

每个检查项返回：

| 字段 | 说明 |
|---|---|
| code | 检查项编码 |
| name | 检查项名称 |
| state | passed / warning / failed |
| message | 人类可读说明 |
| detail | 原始详情，敏感信息脱敏 |
| suggestion | 修复建议 |

### 4.3 页面体验

Oracle 导入页面增加“一键环境检查”。

检查失败时：

- 不允许直接 execute。
- 可以允许 dry-run 中只做不依赖失败项的部分检查，但必须明确风险。

## 5. 数据连接测试拆分

### 5.1 当前问题

Oracle 数据库连接配置和服务器文件访问配置容易混淆。数据库连接成功不代表 DMP 文件所在服务器可访问。

### 5.2 改造方向

连接测试拆分为：

1. 数据库连接测试。
2. SSH 服务器连接测试。
3. 文件目录权限测试。
4. 容器环境测试。

页面文案应明确：

- Oracle 用户密码用于连接数据库。
- 服务器账号密码用于访问备份文件和 Docker 宿主机。
- 备份文件所在服务器可以和 Oracle 数据库服务器不同。

## 6. 前端向导化

### 6.1 批量授权向导

步骤：

1. 维护或导入部门关系。
2. 校验部门用户和部门库。
3. 上传源表清单。
4. 校验源表和重复授权。
5. 执行授权。
6. 查看批次和下线状态。

### 6.2 Oracle 导入向导

步骤：

1. 选择 DMP 来源。
2. 环境 preflight。
3. dry-run 生成计划。
4. 人工确认计划。
5. execute 正式导入。
6. 校验目标 schema 和表。

### 6.3 SM4 加密向导

步骤：

1. 选择连接、库、表和字段。
2. 选择密钥版本或刷新密钥。
3. 提交任务。
4. 任务调度监控。
5. 解密或抽样验证。

## 7. 测试报告常态化

每个重要版本应生成测试报告，包含：

- 版本号。
- 包名。
- 环境信息。
- 已验证功能。
- 未验证功能。
- 已知问题。
- 风险和建议。
- 回滚方式。

报告建议保存到：

```text
docs/test-reports/
```

文件名示例：

```text
TEST_REPORT_20260712.md
```

