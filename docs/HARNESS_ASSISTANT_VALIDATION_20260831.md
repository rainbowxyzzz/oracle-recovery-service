# Harness 智能助手本地开发验证记录 — 2026-08-31

## 结论与边界

本地实现已完成，尚未发布，不等同于真实模型或 128 业务验收通过。本轮未生成完整包、未修改远端容器/数据库、未提交 GitHub。使用当前本地源码为开发基线，保留进入本轮前已有的 CSV、Oracle TEMP 修复和对应文档。

需求依据：`HARNESS_ASSISTANT_PRD.md`。用户确认加密范围为已保存全库加密任务的表；助手不使用旧流水线的血缘反推逻辑裁剪该任务。自然语言只负责规划，经管理员核对确认后自动推进四阶段。

## 实现清单

| 路径 | 本轮修改目的 |
| --- | --- |
| `extracted-app/src/recovery_service/services/harness_assistant.py` | 受控候选元数据、Harness 桥接、文件限定、计划快照、确认哈希/幂等、查询 |
| `extracted-app/src/recovery_service/services/assistant_execution.py` | 助手专属四阶段状态机、执行快照、失败重试与未知派发阻断、准确全库加密血缘 |
| `extracted-app/src/recovery_service/api/v1/harness_assistant.py` | 管理员接口、中文 OpenAPI 摘要、计划/确认/继续 |
| `extracted-app/src/recovery_service/core/models/task.py` | 新增 `assistant_plans` 系统元数据表，不删改旧表字段 |
| `extracted-app/src/recovery_service/services/data_automation.py` | 只对助手批次切换推进路径、读取同步快照、阻断旧入口绕过确认重试 |
| `extracted-app/src/recovery_service/services/data_platform.py` | 助手同步在提交和 Worker 中读取快照；助手工作流运行冻结生产版本 |
| `extracted-app/src/recovery_service/settings.py`、`api/v1/router.py` | 默认关闭的桥接 URL/Token，注册助手路由 |
| `extracted-app/src/recovery_service/static/assistant.html`、`assistant.js` | 独立页面、计划范围二级窗口、冻结 SQL 折叠、历史状态与运行 ID、失败确认 |
| `extracted-app/src/recovery_service/static/ui.html` | 仅新增一个助手入口链接，不重排原表单 |
| `deploy/harness-assistant/` | 隔离桥接、固定 SDK、显式无工具配置、Dockerfile、环境示例、使用说明和真实运行时检查 |
| `extracted-app/tests/test_harness_assistant.py` | 23 项新增自动化用例（含参数化案例） |
| `extracted-app/tests/assistant_ui_server.py`、`assistant_ui_check.cjs` | 隔离内存数据的页面闭环检查，不访问真实业务数据库 |
| `docs/HARNESS_ASSISTANT_PRD.md`、项目 PRD 汇总及本文 | 需求、兼容边界与验证记录 |

未改变原 SM4 任务保存/生成密钥/调度接口；最后一步调用原 `run_sm4_task_snapshot`。任务范围在确认时冻结，密钥仍在 SM4 批次创建时由原模块绑定当前有效部署版本。不是跨数据库事务，也不是跨所有路径与手工任务的全局写锁。

## 已验证

### 应用回归

在 Windows 本地 `extracted-app` 目录，以项目源码与测试依赖运行：

```text
python -m pytest tests -q
315 passed, 1 skipped, 32 subtests passed in 14.42s
```

新增测试覆盖：规划不派发、管理员限制、目录越界与通配符、文件不存在、变更草案拒绝、确认幂等与重复文件阻断、同步/生产 SQL/SM4 范围快照、模拟四阶段顺序、真实恢复 Schema 传递、失败停止下游、未知提交不自动重试、显式失败继续、确认后文件变化、旧路径配置保持、有效同步范围显示、跨库/Catalog 同名表不伪造加密血缘、未知/缺失模型候选阻断、合法模型响应只建草案、模型请求不含业务 SQL 或凭据。

这组测试的 Oracle、Doris、Celery 与模型业务结果采用隔离模拟，不能宣称实际导入、SQL 或 SM4 数据结果通过。

### 页面

本机 `127.0.0.1:18098` 内存数据服务 + 独立 headless Chrome，通过 Playwright 验证：路径选择、草案、同步写入策略、冻结 SQL 展开、完整两表 SM4 范围、确认勾选、提交、刷新回读、历史状态、重复确认按钮关闭、未配置模型提示。

`1440×900`、`960×900`、`720×900`、`390×844` 无页面横向溢出，`pageerror=0`。JavaScript 语法检查通过。截图位于本地 `tmp/assistant-ui-review.png`，不纳入部署。

内置浏览器控制工具因可信 Node 进程启动故障无法使用，因此采用隔离 headless Chrome；没有接管用户浏览器。以上不是项目要求的可见 Chrome 最大化/左右半屏/还原窗口验收，原模块真实编辑保存回读的可见窗口回归仍待发布验证补齐。

UI 测试每次需要新启 `assistant_ui_server.py`，获得空的内存数据库。重复使用同一服务会触发正确的文件去重，不能再次确认相同测试文件。

### 真实 Harness 运行时

在本机 WSL Ubuntu 24.04/Python 3.12 中，以独立目录解压安装的官方 `deepseek-harness-sdk==0.1.1rc1`、Linux 运行时及依赖，执行 `deploy/harness-assistant/verify_runtime.py`。未安装进应用 Python 环境，未改动系统 Python 包。

```text
PASS: real Harness 0.1.1rc1 boot, local synthetic model, JSON result, no model-facing tools
```

确认真实 SDK/原生运行时启动，使用本地合成 SSE 模型返回 JSON；检查请求没有可供模型调用的 Shell/文件等工具。不是 mock SDK，也不是 DeepSeek 真实模型连通性或自然语言语义准确率测试。

桥接目录中的文件已检查 UTF-8 无 BOM、LF，无 CRLF。没有执行 Docker build/run，不能把此检查称为部署包或旧 Docker 验证。

### 测试过程说明

一次全量测试误从仓库根目录启动，旧测试按 `src/...` 相对路径读文件，出现 18 项 FileNotFoundError；未修改原测试，改在约定 `extracted-app` 目录运行后全量通过。一次重复使用 UI 内存服务触发文件去重，重新启动隔离 fixture 后页面测试通过；未放松业务幂等校验。

## 待完成的外部验收

1. 提供实际模型 API 地址、模型名，并以保密环境变量配置 API Key，验证中文意图、歧义追问和真实服务错误。
2. 授权后核对 128 当前 API/Worker 版本差异，完成备份及无在途任务检查；更新 API、同步 Worker 和生产工作流 Worker，创建新元数据表。
3. 在隔离 Oracle/Doris 数据上执行真实 DMP 还原→ODS→DWD→全库任务 SM4，核对表级数据、密钥指纹、日志及血缘；不测试生产表覆盖。
4. 验证 docker-ce 17.03 桥接启动、私网连接和资源限制，以及可见 Chrome 原功能兼容回归。

提交结果不确定时采取阻断而非自动重复写入；本版没有自动对账/一键关联未知运行、总流程暂停取消、自动生成新 SQL/路径或多个路径的全局数据锁。需在后续明确需求后扩展，不能按已实现能力交付。

部署配置与日常操作详见 `deploy/harness-assistant/README.md`。

## 128 发布验证补充 — 2026-09-01

本次按“模式二：发布验证”执行，未生成完整部署包、未执行回滚演练、未提交 GitHub。发布前确认 9 个 Worker 的 active/reserved/scheduled 队列为空；系统 MySQL 及三个受影响应用容器的发布前备份位于服务器 `/root/codex-release/harness-20260831-r1`。仅热更新 API、Data Sync Worker、Data Platform Worker 所需源码，创建 `assistant_plans` 新表，并各重启一次；随后为私网桥接配置仅重启 API 一次。Oracle、Doris、MySQL、Redis 及其他 Worker 未因发布重启。

隔离桥接使用官方 Python 3.12 slim bookworm 基础镜像及固定的 `deepseek-harness-sdk==0.1.1rc1`，镜像为 `oracle-recovery-harness:20260831-r1`。真实 SDK/原生运行时以本地合成 SSE 模型通过启动、JSON 返回和无模型工具验证。服务以 `10001:10001`、只读根文件系统、私网无宿主端口、1 GB 内存和 1 CPU 运行；API→桥接 DNS/TCP/HTTP、随机令牌认证通过。因未提供真实模型地址、模型名与 API Key，认证请求按设计返回 503 且不派发任务；真实模型中文语义仍未验收。

真实页面使用 128 登录与可见 Chrome 完成助手入口、路径选择、计划生成、冻结 SQL 展开、两表完整 SM4 范围、确认勾选、提交和刷新回读；覆盖最大化、左右半屏及还原窗口，无横向溢出，`pageerror=0`。接口验证覆盖目录越界 422、未配置模型阻断、确认哈希错误 422、确认幂等和未授权 401。没有启用监听或生产版本调度进行测试，原开关保持不变。

隔离批次 `02c97ede-c5bd-4a49-8993-d266875a44e0` 真实完成：Oracle 恢复为 `succeeded_with_warnings`（中间一个自动导入命令失败后被既有恢复策略纠正，最终无业务错误）；ODS 同步成功 1 表/3 行/过滤 0；冻结生产 SQL 成功更新 DWD 3 行；所选全库 SM4 快照成功执行 2 表。字段级断言确认 DWD `NAME` 加前缀，其他业务字段保持一致（时间按目标列秒级精度）；两张加密输出表均为 3 行，只有 `PHONE`、`ID_CARD` 改变。第二张表不在本次 DWD 血缘中，证明执行范围来自所选全库任务而非血缘裁剪。SM4 批次绑定当前有效密钥版本 `06f3f13c-7683-4d99-8616-9f856255971c` 和指纹 `3f06b2c48a818d90`，没有生成或轮换密钥。发布前既有恢复任务、流水线、节点、工作流版本、SM4 任务/调度/密钥版本逐行哈希保持不变。

本地最终回归：`315 passed, 1 skipped, 32 subtests passed in 16.14s`，`assistant.js` 语法检查通过。发布后 API 状态正常、MySQL 已连接，9 个 Worker 队列为空；API、Data Sync Worker、Data Platform Worker、Harness 近 10 分钟未发现 Traceback/Critical/Unhandled/Fatal。

三容器中的 11 个热更新文件均与热更新包 `manifest.json` 的 SHA-256 一致；本地 `data_platform.py` 因 Windows CRLF 与包内 LF 的字节哈希不同，包内及三个远端容器哈希一致，源码语义差异检查无额外内容。本次服务器实际报告 Docker `1.13.1`，比项目文档目标 `17.03` 更旧；部署过程未使用 compose、`--pull`、`--mount` 或 host-gateway 等新参数，真实容器构建、私网、只读根文件系统和资源限额已在该环境通过。

清理隔离 Schema、独占表空间、5 张 Doris 测试表、测试元数据和 DMP 副本的精确脚本已准备，但不可逆删除被安全审核拦截且未执行。因此上述隔离对象目前仍保留在 128，不能写成“已清理”。热更新代码位于现有容器可写层，容器普通重启会保留，但若按旧镜像重新创建容器会丢失；后续完整打包必须把本版源码、`assistant_plans` 初始化及桥接启动/配置纳入包内。
