# Doris CSV 导入确认弹窗发布验证记录

验证日期：2026-08-10

## 1. 范围

- 模块：数据同步 / Doris CSV 文件导入。
- 修改范围：解析完成后点击“确认导入”的前端确认弹窗。
- 未修改内容：后端接口、解析策略、导入语义、数据库结构、Worker、镜像和部署脚本。

## 2. 变更内容

- 将导入前的浏览器原生 `window.confirm` 改为页面内弹窗。
- 弹窗复用解析进度弹窗的稳定尺寸：宽度 `720px` 上限，高度 `445px` 上限。
- 弹窗展示目标库、文件数量、导入模式、自动建表、覆盖策略和文件到目标表的映射摘要。
- 文件映射列表在弹窗内部滚动，避免 100+ 文件撑满页面。
- “取消”只关闭弹窗，不发起导入请求；“继续导入”才提交既有导入接口。

## 3. 本地验证

- `node --check ui-script-check.js`：通过。
- 从 `static/ui.html` 抽取内联脚本并以内存方式执行语法解析：`UI_INLINE_SCRIPT_SYNTAX_OK scripts=1`。
- 相关文件均为 UTF-8 无 BOM、LF 行尾。

## 4. 128 发布

- 发布方式：仅热更新 `oracle-recovery-api` 容器内 `/app/src/recovery_service/static/ui.html`。
- 未重启 Worker，未重建镜像，未执行数据库迁移。
- 更新前备份：`/root/oracle-recovery-backups/20260810-doris-csv-import-confirm-modal-ui/ui.html.before`。
- 更新后容器内页面 SHA256：`8ff11ebb18d900808d3fc3ad0bdd3172df94ca53a3a8f5cb3584db947f41cda9`。

## 5. Chrome 验证

- 验证地址：`http://192.168.150.128:8000/?dorisCsvImportConfirmFix=20260810real`。
- 测试连接：`Doris CSV Test（默认） - 192.168.150.128:9030`。
- 测试数据：120 个小 CSV 文件，前缀 `import_confirm_real_`。
- 真实执行：上传 120 个文件并点击“解析文件”，解析任务状态为 `completed`。
- 导入接口：点击“继续导入”时使用页面内 fetch mock 拦截，避免向 Doris 写测试数据。
- 窗口状态已覆盖：最大化、左半屏、右半屏、还原窗口。
- 弹窗尺寸：CSS 计算宽高为 `720 x 445`。
- 文件列表滚动：`scrollHeight 7119 > clientHeight 209`，内部滚动生效。
- 文件摘要：`120 个文件`。
- 操作按钮：四种窗口状态下均可见。
- 页面横向溢出：无。
- 错误：`page error` / `unhandledrejection` 采集为 0。
- 截图目录：`artifacts/chrome-audit-doris-csv-import-confirm-20260810`。

## 6. 按钮行为

- 取消：弹窗关闭，导入请求数为 0。
- 继续导入：弹窗关闭，导入请求数为 1，结果区显示成功，页面消息为 `Doris CSV 导入成功。`

## 7. 测试数据清理

- 已删除系统库中 `import_confirm_real_*.csv` 对应的 120 条文件节点记录。
- 已删除任务记录：`1a547acfca5b48c8809d572962c8e0c6`。
- 已删除临时目录：`/tmp/oracle-recovery-staging/doris-csv-parse/1a547acf-ca5b-48c8-809d-572962c8e0c6`。
- 清理后数据库中匹配记录数为 0。

## 8. 健康检查

- API `/api/v1/health`：正常。
- Redis 队列 `celery`、`data_sync`、`api_orchestration`：均为 0。
- API 近 10 分钟日志未发现 `error`、`exception`、`traceback`、`500` 关键字。
