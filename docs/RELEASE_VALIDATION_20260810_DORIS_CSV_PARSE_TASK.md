# Doris CSV 解析任务化发布验证报告

验证日期：2026-08-10  
验证环境：`192.168.150.128`  
发布模式：发布验证模式二  
范围：Doris CSV 导入模块的本地多文件任务化解析、进度展示和停止能力

## 1. 发布范围

本次只更新 API 服务，未重启数据同步、接口编排或其他 Worker。修改内容包括：

- `DorisCsvParseTask`、`DorisCsvParseFile` 模型和系统库迁移。
- `POST /api/v1/doris-csv/parse-tasks`。
- `GET /api/v1/doris-csv/parse-tasks/{task_id}`。
- `POST /api/v1/doris-csv/parse-tasks/{task_id}/stop`。
- 本地 CSV 多文件同表/多表解析逻辑。
- CSV 解析进度弹窗、文件节点状态、停止确认和操作禁用。
- CSV 任务专项测试。

历史 `preview/import`、FTP `preview/import` 接口保留，FTP 流程本轮未切换为任务化。

## 2. 本地验证

| 检查项 | 结果 |
|---|---|
| 全量 Python 测试 | `204 passed, 1 skipped` |
| Doris CSV 专项测试 | `2 passed` |
| 数据同步、微服务、资源开通相关测试 | `58 passed` |
| Python `compileall` | 通过 |
| 前端 JavaScript `node --check` | 通过 |

专项测试覆盖：

- 同表模式第二个文件复用第一个文件结构，并识别为无表头数据。
- 停止任务后未开始文件进入 `stopped`，已完成结果保留。

## 3. 128 服务核查

发布前已确认无在途任务，发布后仅更新并重启 `oracle-recovery-api`。

| 检查项 | 结果 |
|---|---|
| API 健康 | `{"status":"ok","mysql":{"ok":true}}` |
| `doris_csv_parse_tasks` | 已创建 |
| `doris_csv_parse_files` | 已创建 |
| `celery` 队列 | 0 |
| `data_sync` 队列 | 0 |
| `api_orchestration` 队列 | 0 |
| API 发布后近 30 分钟异常日志 | 未发现 `Traceback`、`500`、`Exception`、`panic` |

本次为热更新发布，容器仍显示既有 API 镜像标签；这不代表代码未更新，容器内应用文件已替换并重启。本轮没有重新构建或导出镜像。

## 4. 真实业务验证

使用 128 已存在 Doris 连接：

- 连接：`Doris CSV Test`
- 连接 ID：`87718e55-3d3a-409a-943e-e14cc76e00d4`

### 4.1 合并到同一张表

隔离库：`csv_task_e2e_20260810`  
文件：`orders_a.csv`、`orders_b.csv`

结果：

- 解析任务完成。
- 两个文件均成功。
- 第二个文件自动识别为无表头。
- 两个文件目标表均为 `orders_a`。
- 两次 Stream Load 均成功。
- Doris 查询目标表实际行数为 4。
- 隔离库和任务数据已清理。

### 4.2 停止任务

使用大文件/多文件任务验证协作式停止：

- 第一个文件完成。
- 后续未完成文件标记为 `stopped`。
- 任务最终状态为 `stopped`。
- 已完成文件结果保留。

## 5. Chrome 可见验收

Chrome：专用审计窗口，CDP `127.0.0.1:9222`，DPR 和真实窗口尺寸均现场读取。

### 5.1 页面动作

- 登录 `admin`。
- 打开 `Doris CSV 导入`。
- 核对 Doris 连接默认选中。
- 选择“多个文件合并导入同一张表”。
- 注入两个 CSV 文件并点击“解析文件”。
- 查看进度弹窗的阶段、百分比、文件数、字节数、有效行和问题行。
- 查看完成状态和文件节点。
- 点击“停止任务”，真实触发停止确认框。
- 关闭进度弹窗并查看表结构预览。

### 5.2 窗口和布局

| 状态 | 页面级横向溢出 | 截图 |
|---|---:|---|
| 最大化 | 无 | `artifacts/chrome-audit-doris-csv-20260810/maximized-csv-layout-no-modal.png` |
| 左半屏 | 无 | `artifacts/chrome-audit-doris-csv-20260810/left-half-csv-layout-no-modal.png` |
| 右半屏 | 无 | `artifacts/chrome-audit-doris-csv-20260810/right-half-csv-layout-no-modal.png` |
| 还原窗口 | 无 | `artifacts/chrome-audit-doris-csv-20260810/restored-csv-layout-no-modal.png` |

进度弹窗截图：

- `artifacts/chrome-audit-doris-csv-20260810/maximized-parse-modal.png`
- `artifacts/chrome-audit-doris-csv-20260810/maximized-parse-complete.png`
- `artifacts/chrome-audit-doris-csv-20260810/maximized-stop-final.png`

浏览器控制台错误：0。`pageerror`：0。

说明：Chrome 本轮停止点击后，120 个极小文件在停止请求生效前已经完成，因此该次页面任务最终显示 `completed`；停止按钮和二次确认交互已验证，停止语义以 128 独立真实停止任务和本地专项测试结果为准。

## 6. 清理结果

已清理：

- 128 隔离 Doris 库 `csv_task_e2e_20260810`。
- 128 本轮 CSV 解析任务和文件节点。
- `/tmp/csv_task_e2e_20260810`。
- `/tmp/csv_task_stop_20260810`。
- Chrome 验收产生的两个解析任务及其 staging 目录。

最终复核：

- `doris_csv_parse_tasks` 隔离记录：0。
- `doris_csv_parse_files` 隔离记录：0。
- Redis 相关队列：0。
- API 健康：通过。
- API 近期异常日志：未发现。

## 7. 未包含内容与后续风险

- 本轮未生成完整 Docker Run 包，用户未提出“打包”。
- 本轮未执行回滚演练。
- 解析任务第一阶段由 API 进程后台线程执行；API 重启会中断进行中的解析任务。后续若文件规模继续增大，应迁移到独立 Worker，但保持当前接口契约。
- 页面顶部模块导航在窄窗口下使用自身横向滚动，这是既有全局导航行为，不属于本次 CSV 内容区域溢出。
