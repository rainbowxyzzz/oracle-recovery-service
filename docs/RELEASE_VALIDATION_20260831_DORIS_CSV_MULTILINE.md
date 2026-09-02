# 2026-08-31 Doris CSV 多行单元格与引号保真验证

## 范围与基线

- 用户授权启动 128 本项目服务，验证截图同类 Excel/WPS CSV，失败时最小修改并复测。
- 使用模式二的发布验证边界，仅热更新一个 API 服务文件；不打整包、不执行迁移或回滚演练、不提交 GitHub。
- 128 原有 API、9 个 Worker、系统 MySQL、Redis、Oracle 21c 共 13 个容器已启动并保持运行。无关容器未操作。
- Doris 未重启：验证前后 FE PID `41384`、BE PID `42520` 未变，8030/8040/9030/9050 持续监听；实际版本为 `doris-4.1.1-rc01-b10073ad9ca`。
- 变更前本地服务文件与 128 文件 SHA256 一致；与 20260826 完整包的源码快照比较，仅存在 CRLF/LF 差异，无逻辑差异。
- 用户提供的是截图而非原始 CSV。本轮以标准 CSV writer 按 Excel/WPS 常见双引号转义和 CRLF 记录终止方式构造等价场景，不宣称验证过用户原文件或实际点击过 Excel/WPS 另存为。

## 已复现问题

修复前，中/英文字段与 UTF-8 BOM/GB18030 四个组合均解析为 8 条有效记录、0 条问题记录，预览和重写内容能按标准 CSV 还原；真实 Stream Load 全部返回成功且过滤 0 行，但单元格正文中的双引号出现重复，例如 `a"b` 入库变成 `a""b`。

结论：本次样例的内嵌换行没有被错误拆成记录；缺陷位于 Python 标准 CSV 双引号重复转义与 Doris Stream Load 转义配置不匹配。不能只用“导入成功”和行数作为保真验收。

## 最小修复

运行文件：`extracted-app/src/recovery_service/services/doris_csv_import.py`。

1. 输入仍使用原标准 CSV reader，不改变源文件格式、编码识别及问题行处理。
2. 中间 CSV writer 增加 `escapechar="\\"`、`doublequote=False`，Stream Load Header 对应增加 `escape`。
3. 行数回退改为解析得到的逻辑记录数，不计表头或单元格内物理换行。

中文 `csv_with_names`、英文 `columns/skip_lines`、字段类型、字段映射、建表和过滤策略、任务和 FTP 接口保持不变。没有修改共享 UI、Worker、历史任务或既有业务表。

转义参数参考：[Apache Doris CSV 文档](https://doris.apache.org/docs/4.x/data-operate/import/file-format/csv/)。当前仍按文件读取和发送，未新增流式分块。

## 本地回归

- 新用例先在旧代码失败，再验证修复后通过。
- 新增 38 项覆盖：中英文字段、UTF-8 BOM/UTF-8/GB18030、逗号/分号/Tab、带表头/无表头、字段子集映射及任务行数回退。
- 样例覆盖 LF、CRLF、单 CR、多行中文、连续引号、首尾引号、只有一个引号、逗号、Tab、Unicode 分隔字符、空值及反斜杠/引号组合。
- 最终完整回归：`277 passed, 1 skipped, 32 subtests passed`。
- Python 编译与 `git diff --check` 通过。

## 128 候选及发布后真实验证

### 候选（不替换运行代码）

4 个组合，每组 10 行：源值、预览值、中间重写值与真实 Doris 查询值逐单元格完全一致；每组导入 10 行、过滤 0 行。对应隔离库已清理。

### 发布前安全检查

- CSV 在途任务：0。
- 9 个 Celery Worker 的 active/reserved 均为 0。
- 备份：`/root/codex-backups/20260831-csv-multiline-r1/doris_csv_import.py`。
- 原文件 SHA256：`4b56cfdad1a1083fe045a6a8f8d8e45a037efc940eb3fba882b4e3bbee1c734b`。
- 只替换该文件并重启一次 API；未重启 Worker 或 Doris。
- 新文件本地/128 SHA256：`e31ea34c01e3c1bfa347a7163e424de16b740074c5fcc92f0cab08b68b81c8d0`。

### 发布后 API 闭环

使用已存在的 `Doris CSV Test` 连接，在随机后缀隔离库中测试。

| 路径 | 场景 | 实际写入 | 过滤 | 逐单元格一致 |
|---|---|---:|---:|---|
| 任务化 | 中文、同表、逗号、两文件混合编码 | 10 | 0 | 是 |
| 任务化 | 英文、同表、逗号、两文件混合编码 | 10 | 0 | 是 |
| 任务化 | 中文、多表、分号、两文件混合编码 | 10 | 0 | 是 |
| 任务化 | 英文、多表、Tab、两文件混合编码 | 10 | 0 | 是 |
| 历史同步 API | 中文、新建及已有表追加 | 20 | 0 | 是 |
| 历史同步 API | 英文、新建及已有表追加 | 20 | 0 | 是 |

任务化覆盖上传解析、预览、逐文件保存 VARCHAR 类型、重新 GET 回读、启动导入、状态与逻辑行数、日志和真实 SELECT；四个任务日志均无 ERROR。合并同表的逐文件保存沿用当前前端契约，未修改服务端保存范围。

初次 E2E 测试脚本误假定单次 PATCH 会自动同步全部文件，已按现有前端逐文件保存方式校正；其只读 MySQL 事务快照也已结束后再清理，不把测试脚本假设当成产品缺陷。初次隔离任务和库均已精确清理。

## 清理与运行状态

- 删除本轮精确创建的测试任务、文件节点、任务日志和对应 staging 目录，以及随机隔离库；未清理系统审计或既有业务数据。
- 复核本轮隔离任务数 `0`，两种隔离数据库前缀查询结果为空。
- API `/api/v1/health` HTTP 200，系统 MySQL 连接成功。
- 13 个项目容器保持运行，Oracle 21c healthy；API 近期日志未发现 Traceback、ERROR、CRITICAL 或 HTTP 500。

本地证据保存在 `artifacts/csv-multiline-20260831/`：修复前、候选、发布后 API 三份验证日志。测试脚本位于 `tmp/csv_multiline_probe_20260831.py` 和 `tmp/csv_multiline_api_e2e_20260831.py`。

## 未完成与限制

- 可见浏览器控制程序在启动时被本机 Windows sandbox `helper_unknown_error: setup refresh had errors` 阻断。未把 API/静态验证冒充真实页面点击回归。
- 未进行真实 FTP 服务器下载测试；FTP 复用的 CSV 解析和导入服务已由本轮相关自动化覆盖。
- 不推测修复损坏 CSV（例如未闭合引号），不修改历史已入库的错误内容；原始文件如仍有问题，需要用实际文件单独复现。
- 20260826 完整部署包未重新生成，不包含本次补丁；GitHub 未提交。
