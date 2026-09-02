# 128 运行版本核对（打包前）

日期：2026-08-31。

## 范围与结果

用户要求确认 128 是最新版本后再完整打包。本次仅执行只读运行检查与临时校验文件传输，未更新、重启服务，未执行迁移或业务任务，也未生成新的交付包。

以本地 `5330cc0defcba9564e4b9ad33f54f480ba92244f` 加 2026-08-31 未提交的 CSV 修复为源码基线，对 API 和 9 个 Worker 中 162 个受 Git 跟踪的运行文件逐项比较 SHA256（比较前统一 CRLF/LF）。

- API：160 个文件一致。`db/session.py` 仅多两个空行；`scripts/worker-entrypoint.sh` 仍为旧版本，但 API 不通过 Worker 入口执行。
- 审批流服务、schema、API 路由、主页面及调度启动代码与本地一致。已包含申请 `createTime` 的 MMDD 日期、apiAdd 目录、映射表/授权信息表所属库、importDataPermissions 单目录配置、监听状态及分层日志。
- 最新 CSV 多行单元格与引号保真修复已在 API 中。
- Worker 未全量对齐：导出 Worker 158/162 一致，其余 8 个 Worker 分别存在 22–31 个不一致或缺失文件。部分是该 Worker 不使用的其他模块，但不能全部视为无影响。
- 已确认接口编排 Worker 的实际业务文件 `services/api_orchestration.py` 缺少本地 `dynamic_bearer` 分支；其任务入口直接导入该文件的 `execute_run`。这不是只存在于无关页面或镜像标签的差异。
- 多个 Worker 的入口仍采用 `SM3_WORKER_CONCURRENCY` 优先于通用 `WORKER_CONCURRENCY` 的历史逻辑，本地已修正。
- 运行版本与 2026-08-26 完整源码包不完全一致。直接从当前全部运行容器打包，不能声明为统一最新版本。

## 已核实的 API 原始文件 SHA256

| 文件 | SHA256 |
|---|---|
| services/approval_authorization.py | f0d86ff293bbb3f439f6d02e3024dc1b50b2d12e9cb5a643d534ed3bcdbfa5b0 |
| static/ui.html | 2d7dcacb29a602e917b9b771fb96a253a30b41f7e4ac01d4f99a24bc58ea8b77 |
| services/doris_csv_import.py | e31ea34c01e3c1bfa347a7163e424de16b740074c5fcc92f0cab08b68b81c8d0 |

API `/api/v1/health` 返回 `status=ok`、MySQL 连接成功。API 和 9 个 Worker 均运行中。以上为源码与健康核对，不代表重新完成全部模块业务或浏览器验收。

## 下一步边界

按项目“源码、运行版本与确认包不一致时先确认基线”规则，暂停打包。建议用户授权先将受影响 Worker 与最新源码对齐，完成相应回归后再打包；不得把用户本次有条件的打包要求自动扩大为更新生产服务的授权。

机器可读明细保存在本地 `tmp/package-20260831/runtime-source-verification.json`；服务器原始结果为 `/tmp/runtime-source-verification-20260831.json`。均只含文件名与校验值，不含凭据。
