# Doris CSV 导入进度弹窗发布验证记录

验证日期：2026-08-10

## 1. 范围

- 模块：数据同步 / Doris CSV 文件导入。
- 修改范围：仅调整解析进度弹窗的前端展示。
- 未修改内容：后端接口、解析策略、导入语义、数据库结构、Worker、镜像和部署脚本。

## 2. 变更内容

- 进度弹窗使用固定响应尺寸：宽度 `720px` 上限，高度 `445px` 上限。
- 高度按黄金比例约束：`720 * 0.618 = 445`。
- 文件节点列表改为弹窗内部滚动区域，避免 100+ 文件时撑满页面。
- 文件节点增加数量摘要，按钮区域保持可见。

## 3. 本地验证

- `node --check ui-script-check.js`：通过。
- 从 `static/ui.html` 抽取内联脚本后执行 `node --check`：通过。
- 直接执行 `node ui-script-check.js` 会报 `document is not defined`，这是该脚本依赖浏览器 DOM 的既有行为，不属于本次回归。

## 4. 128 发布

- 发布方式：仅热更新 `oracle-recovery-api` 容器内 `/app/src/recovery_service/static/ui.html`。
- 未重启 Worker，未重建镜像，未执行数据库迁移。
- 更新前备份：
  - `/root/oracle-recovery-backups/20260810-doris-csv-progress-modal-ui`
  - `/root/oracle-recovery-backups/20260810-doris-csv-progress-modal-ui-r2`
- 容器内标记已确认：
  - `.modal.doris-csv-progress-modal`
  - `.doris-csv-progress-file-list`

## 5. Chrome 验证

- 验证地址：`http://192.168.150.128:8000/?dorisCsvModalFix=20260810r2`
- 测试数据：120 个临时 CSV 文件，前缀 `modalfixr2_`。
- 弹窗尺寸：`720 x 445`。
- 文件节点摘要：`120 个节点`。
- 文件列表滚动：`scrollHeight 7119 > clientHeight 169`，内部滚动生效。
- 操作按钮：可见。
- 页面横向溢出：无。
- 已覆盖窗口状态：最大化、左半屏、右半屏、还原窗口。
- 截图目录：`artifacts/chrome-audit-doris-csv-modal-fix-20260810`。

## 6. 测试数据清理

- 已删除系统库中 `modalfixr2_*.csv` 对应的 120 条文件节点记录。
- 已删除任务记录：`8a568c7b18a9421eb973ebc21b20fa5b`。
- 已删除临时目录：`/tmp/oracle-recovery-staging/doris-csv-parse/8a568c7b-18a9-421e-b973-ebc21b20fa5b`。
- 清理后数据库中匹配记录数为 0。

## 7. 健康检查

- API `/api/v1/health`：正常。
- Redis 队列 `celery`、`data_sync`、`api_orchestration`：均为 0。
- API 近 10 分钟日志未发现 `error`、`exception`、`traceback`、`500` 关键字。
