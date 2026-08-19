# 全系统功能与实现一致性审计报告

审计日期：2026-08-01  
审计对象：`oracle-recovery-service-package-20260705` 当前源码、`192.168.150.128` 当前运行环境、最近完整 Docker Run 包  
审计性质：只读排查与安全验证，不修改业务源码，不执行恢复、授权、删除、加密、同步等生产写操作

## 1. 执行摘要

本次不是只检查“页面能打开”或“代码里有函数”，而是按以下五层交叉核对：

1. PRD 与项目规则是否明确要求该能力。
2. 前端入口、事件、请求参数和状态回填是否形成闭环。
3. 后端路由、服务、队列和持久化是否真正承接请求。
4. 128 当前运行版本是否与本地源码一致，接口和详情链路是否可用。
5. 最近完整包的脚本是否会按声明的镜像、并发和数据库约束启动。

结论：未发现 P0 级全站不可用问题；18 个功能入口均可在 128 打开，33 个只读列表/健康接口均返回 HTTP 200，历史详情和日志主链路可正常读取。本地自动化结果为 `183 passed, 1 skipped`。

但“页面存在、测试通过”仍掩盖了 5 项确认的 P1 缺陷或高风险偏差：

| 编号 | 等级 | 结论 |
|---|---|---|
| F-01 | P1 | 离线开发画布没有拖动阈值，鼠标移动 1px 就会误判为拖动并改写节点坐标 |
| F-02 | P1 | `data-platform` Worker 已启动但没有离线流程任务，离线运行实际在 API 进程 daemon thread 中执行 |
| F-03 | P1 | `data-sync`、`doris-sql` 独立 API 复用整个 `data_platform.router`，路由和调度职责超出服务边界 |
| F-04 | P1 | 最近完整包的 Worker 并发环境变量串用，包内声明的 API Orchestration 并发度 2 可能实际变成 1 |
| F-05 | P1 | 128 系统元数据库实际为 `mysql:latest` / MySQL `9.3.0`，违反固定 `mysql:8.4` 的强制规则 |

此外存在 1 项已写入 PRD 但尚未完成的高业务风险需求：批量授权缺少“授权前权限快照、批次引用计数、只回收本批次新增权限”的权限基线。它不是本轮新回归，但会带来误回收外部既有权限的风险。

## 2. 范围与判定口径

### 2.1 覆盖入口

业务模块：

1. 数据连接
2. 数据导入 / 恢复
3. Doris CSV 导入
4. Doris SQL 开发工作台
5. 数据同步
6. 数据变化触发器
7. 批量授权中心
8. 数据空间批量开通
9. 接口编排中心
10. Doris SM4 加密中心
11. SM4 调度中心
12. Doris SM3 映射脱敏
13. SM3 日志审计
14. 离线开发
15. 数据库清理

系统模块：当前用户、用户管理、API Key 管理。

### 2.2 验证级别

| 标记 | 含义 |
|---|---|
| 真实验证 | 在 128 可见 Chrome 或真实 HTTP/API/容器中执行并观察结果 |
| 历史样本验证 | 打开 128 已存在任务、运行记录和日志，不新建生产数据 |
| 自动化验证 | 由本地单元/集成测试覆盖，依赖使用 mock 或隔离数据库 |
| 静态验证 | 检查源码、路由、事件、参数、脚本和 PRD 契约 |
| 环境受限 | 当前 128 无样本，或操作会修改业务数据，因此未执行写入闭环 |

本报告不会把“静态存在”写成“真实成功”，也不会把空态页面写成完整详情验收通过。

## 3. 环境与版本证据

### 3.1 128 当前运行容器

| 容器 | 当前镜像 | 状态 |
|---|---|---|
| `oracle-recovery-api` | `oracle-recovery-service-api:20260730-api-orchestration-dbeaver-sqlapi-canvas-r1` | Up |
| `oracle-recovery-worker-api-orchestration` | `20260730-api-orchestration-mapping-r1` | Up |
| `oracle-recovery-worker-resource-provisioning` | `20260729-resource-role-delete-r1` | Up |
| `oracle-recovery-worker-data-sync` | `20260728-data-sync-long-run-resilience-r1` | Up |
| `oracle-recovery-worker-data-platform` | `20260728-microservice-phase1-r1` | Up |
| Oracle/SM4/SM3/SQL Worker | `20260728-microservice-phase1-r1` | Up |
| `oracle-recovery-mysql` | `mysql:latest`，实际 `9.3.0` | Up |
| `oracle-recovery-redis` | `redis:latest` | Up |

128 API 运行模式为 `APP_SERVICE_MODE=monolith`。8 个业务 Worker 分别运行；队列检查时九条队列长度均为 0。

### 3.2 本地与 128 文件一致性

| 文件 | SHA256 |
|---|---|
| `static/ui.html` | `0e99531e8cb54a89832afe78e1ac5faa2a259ca00b1d7ce2ed48940f359f64d3` |
| `main.py` | `756d5786463f403b316e41c50eef23c1a40c77dd9b1686fc1c9b28e221234b60` |

本地文件与 128 API 容器内对应文件哈希一致。本次浏览器结果可以用于判断当前源码，而不是只验证了旧页面。

### 3.3 系统资源

| 项目 | 结果 | 判断 |
|---|---|---|
| 根分区 | 72GB，总用量 57GB，79% | 有发布空间压力 |
| 内存 | 14GB，available 约 3.3GB | 当前可用，但余量不高 |
| Swap | 0B | 长任务突发时缺少缓冲 |
| Docker 镜像 | 91 个，约 18.01GB | 历史镜像较多 |
| Docker 容器 | 57 个，仅 14 个活跃 | 退出容器约 794.7MB 可回收 |

本轮按只读边界没有清理任何容器、镜像或数据卷。

### 3.4 MySQL 日志资源参数

128 当前参数达到项目规则最低值：

```text
sort_buffer_size       16777216
join_buffer_size        4194304
read_buffer_size        1048576
read_rnd_buffer_size    4194304
tmp_table_size        268435456
max_heap_table_size   268435456
max_allowed_packet    268435456
innodb_buffer_pool_size 536870912
```

因此此前百表日志的 `Out of sort memory` 资源项目前已调大；当前主要问题是镜像和数据目录已经处于 MySQL 9.3，不符合固定 8.4 的交付契约。

## 4. 自动化与静态检查

### 4.1 本地测试

正确执行方式：

```powershell
$env:PYTHONPATH='src'
.\tmp\test-venv-microservices\Scripts\python.exe -m pytest -q
```

结果：

```text
183 passed, 1 skipped in 6.07s
```

跳过项为 Windows 不支持 POSIX `fcntl` 运行锁。`python -m compileall -q src` 通过，JavaScript 语法检查通过。当前测试虚拟环境未安装 Ruff，因此 Ruff 未执行。

直接在仓库执行 `pytest` 而不设置 `PYTHONPATH=src` 会产生 18 个收集错误。这不是业务缺陷，但说明测试入口尚未标准化。

### 4.2 测试覆盖分布

| 测试文件 | 用例数 | 主要范围 |
|---|---:|---|
| `test_api_orchestration.py` | 26 | 连接器、SQL API、Mapping、UI 契约 |
| `test_data_sync.py` | 22 | 同步方式、并行、日志、连接恢复 |
| `test_resource_provisioning.py` | 21 | Token、开通流程、敏感信息 |
| `test_oracle_auto_import_resilience.py` | 19 | Oracle 长任务恢复性 |
| `test_resource_permissions.py` | 17 | 资源授权、角色 ID、角色删除 |
| `test_standalone_change_trigger.py` | 15 | 独立触发器 |
| `test_data_platform_component_tasks.py` | 13 | 组件快照和执行 |
| `test_microservice_modes.py` | 9 | 路由存在性和队列映射 |
| 其余 11 个文件 | 42 | 目录、DDL、SM4、日志、恢复保护等 |

主要缺口：数据连接、CSV 导入、数据库清理、用户与 API Key 没有相应的模块级自动化闭环；当前主要依靠运行态只读/安全交互验证。

### 4.3 前后端契约扫描

- 静态 HTML 共 791 个唯一 ID，无重复 ID。
- 所有 `<label for>` 均存在对应控件。
- 未发现真实“按钮存在但完全无事件”的入口；7 个没有直接 ID 查找的按钮中，5 个属于表单 submit，2 个是已隐藏的 Deprecated 控件。
- 前端提取到 179 个 API 路径模式；4 个初始未匹配项均为模板字符串解析误差，实际后端路由存在。
- 后端 API 文件共有约 202 个路由装饰器。
- JavaScript 中发现 6 个只定义一次的遗留函数，属于死代码或旧交互残留，不代表对应功能被调用。

## 5. 可见 Chrome 验证

### 5.1 真实窗口尺寸

| 窗口状态 | outer | inner viewport | DPR |
|---|---|---|---:|
| 最大化 | 1920 x 1032 | 1920 x 945 | 1 |
| 左半屏 | 960 x 1032 | 944 x 937 | 1 |
| 右半屏 | 960 x 1032 | 944 x 937 | 1 |
| 还原窗口 | 1440 x 850 | 1424 x 755 | 1 |

屏幕为 1920 x 1080。18 个入口在四种窗口状态均可切换，无页面级横向溢出、无 `pageerror`、无 console error。权限矩阵、对象树等宽内容在半屏使用组件内部滚动，不属于页面整体遮挡。

### 5.2 主操作与详情链路

已安全点击并观察反馈：用户/API Key/连接/恢复/同步/触发器/授权/资源开通/接口编排/SM4/SM3/离线开发刷新，CSV 空文件校验，SQL 对象树刷新，SM3 空任务 ID 校验，数据库清理连接测试。

详情链路：

| 链路 | 结果 |
|---|---|
| 恢复任务详情 | HTTP 200，可见任务、事件和 Oracle 日志元数据 |
| 数据同步运行日志 | HTTP 200，可见表级节点、同步方式、阶段和 SQL |
| SM4 调度运行日志 | 可见离线节点、表级结果和 SQL |
| 批量授权详情 | HTTP 200，可回读表和用户明细 |
| 接口编排四个页签 | 连接器、流程设计、SQL API、运行中心均可切换 |
| 接口编排运行详情 | HTTP 200，可见 4 个节点及运行状态 |
| 资源开通二级应用 | 批量开通、数据连接授权可双向切换 |
| 离线开发运行日志 | 页面和列表接口 HTTP 200；当前选中任务无历史运行样本，未验证节点详情 |

本轮详情交互没有新增 console error、`pageerror` 或失败请求。

### 5.3 截图证据

- 四种窗口、18 个入口，共 72 张截图：`artifacts/128-releases/20260801-full-system-audit/chrome/`
- 详情补充截图：
  - `detail-data-sync-run.png`
  - `detail-sm4-run.png`
  - `detail-api-orchestration-run.png`
  - `detail-resource-permissions.png`
  - `detail-data-platform-run.png`

## 6. API 与运行态验证

33 个只读列表/健康接口均为 HTTP 200，覆盖：健康、当前用户、用户、API Key、数据连接、恢复任务、SQL ETL、离线开发 dashboard/nodes/folders/workflows/versions/schedules/runs/triggers、批量授权、资源开通、接口编排、SM4 和 SM3。

历史详情实测 HTTP 200：恢复任务详情、Oracle 日志、数据同步 `component-runs`、离线运行节点接口、SM4 日志、SM3 日志、接口编排运行详情、批量授权详情。

审计脚本曾错误使用不存在的 `batch_id` 字段，请求了：

```text
/api/v1/batch-authorization/grant-batches/undefined
```

该请求产生一次 422。改用真实字段 `id` 后详情返回 200，因此这一次 422 是测试工具误差，不是系统缺陷。

## 7. 逐模块结果矩阵

| 模块 | 页面/接口结果 | 自动化/源码结果 | 最终判定与未验证边界 |
|---|---|---|---|
| 当前用户 | 页面可见，当前用户接口 200 | RBAC 公共链路存在 | 读取通过；未修改当前账号 |
| 用户管理 | 列表和刷新正常，四窗口无溢出 | 无独立模块用例 | 部分通过；新增、编辑、停用未执行 |
| API Key | 列表和刷新正常 | 无独立模块用例 | 部分通过；创建、撤销未执行 |
| 数据连接 | 列表和刷新正常 | 前后端路由匹配 | 部分通过；未保存或删除真实连接 |
| 数据导入/恢复 | 列表、详情、日志元数据 200 | Oracle 恢复相关 32 项用例覆盖 | 历史链路通过；未提交真实 DMP 恢复 |
| Doris CSV 导入 | 空文件校验反馈正确 | 路由与页面事件存在 | 部分通过；未上传和导入真实文件 |
| Doris SQL 工作台 | 对象树刷新正常 | SQL 策略与 DDL 6 项用例 | 部分通过；未执行生产 SQL |
| 数据同步 | 历史运行可见表级节点、阶段和 SQL | 35 项同步/组件用例覆盖 | 主链路通过；未新建百表任务，本次未重跑写入 |
| 数据变化触发器 | 列表刷新、画布 1px/真实拖动交互正常 | 22 项触发器与架构用例 | 基础通过；未启动真实探测和下游写任务 |
| 批量授权中心 | 初始化/授权页签和历史详情可读 | 后端流程存在 | 部分通过；未执行授权、回收、延期；权限基线尚未实现 |
| 数据空间批量开通 | 两个二级应用可切换 | 38 项开通/权限用例覆盖 | 契约通过；128 当前无可用授权批次样本，未调用外部系统 |
| 接口编排中心 | 四页签、历史运行和四节点详情通过 | 26 项自动化；真实 DEMO 运行存在 | 当前 MVP 主链路通过；PRD 明示高级能力未完成 |
| Doris SM4 加密中心 | 页面、列表、历史任务可读 | 9 项 SM4 用例 | 批次主链路有历史证据；本轮未新增加密任务 |
| SM4 调度中心 | 历史运行、节点和 SQL 可读 | 调度源码与历史发布记录可追溯 | 读取通过；未立即运行或修改计划 |
| Doris SM3 映射脱敏 | 任务列表、队列状态可读 | SM3 与组件快照有间接覆盖 | 部分通过；未执行真实脱敏任务 |
| SM3 日志审计 | 历史日志接口 200，空 ID 校验正确 | 日志路由存在 | 读取通过 |
| 离线开发 | 目录、画布、日志页可打开 | 20 项目录/组件/排布用例 | 存在 F-01、F-02；当前 128 无节点运行详情样本 |
| 数据库清理 | 连接测试返回 `Doris connection succeeded.` | 无独立模块用例 | 连接验证通过；未执行任何清理 |

## 8. 确认缺陷与风险

### F-01 P1：离线画布 1px 移动即误判拖动

证据：`extracted-app/src/recovery_service/static/ui.html:18665-18686`。

`pointermove` 一发生就更新坐标并把 `dataPlatformDidDrag` 设为 `true`，没有起点距离或 4/5px 阈值。真实 Chrome 验证中，鼠标只移动 1px，节点就从 `left:0/top:0` 被改为 `left:16/top:16`，版本 JSON 同步变化。

影响：

- 普通单击可能被识别为拖动，节点轻微跳动。
- 用户未主动调整布局也会产生“未保存修改”。
- 这与同项目接口编排画布的 5px 阈值、触发器画布的 4px 阈值不一致。

建议：复用接口编排的起点距离判断，超过阈值后才首次改写坐标和 `didDrag`；补 1px 单击、阈值边界、真实拖动和双击回归。

### F-02 P1：`data-platform` Worker 没有承接离线流程

PRD 在 `docs/PROJECT_PRD_SUMMARY.md:122` 声明 `data-platform` 负责离线开发、版本、调度和变化触发器，默认队列为 `data_platform`。

实际：

- `workers/celery_app.py:26-32` 没有任何任务路由到 `data_platform`。
- `data_platform.component_task_run` 明确路由到 `data_sync`。
- `services/data_platform.py:1634` 用 API 进程内 daemon thread 执行整个离线运行。
- `services/data_platform.py:1735-1762` 的时间调度和变化触发器轮询也运行在 API 进程。

影响：API 重启会中断离线运行并标记失败；`data-platform` Worker 虽显示 Up，却基本没有业务可消费，服务拆分的故障隔离和扩缩容目标没有实现。

建议：新增明确的 `data_platform.workflow_run` Celery 任务并路由到 `data_platform`；调度器只负责可靠派发，运行状态必须可由 Worker 恢复。迁移前需处理在途运行、幂等和触发器水位。

### F-03 P1：独立 API 路由与调度职责过宽

证据：`api/v1/router.py:46-50`、`main.py:36-43`。

- `data-sync` 模式挂载整个 `data_platform.router`。
- `doris-sql` 模式同时挂载 `doris_sql_etl.router` 和整个 `data_platform.router`。
- 两种模式都会启动完整 data platform 调度线程。

因此独立 data-sync/doris-sql API 会暴露 folders、workflows、versions、schedules、change-triggers 等不属于自身的高风险接口；若以后同时启动多个独立 API，还可能重复运行调度循环。

当前 128 仅运行 monolith API，所以这是已确认的架构能力缺陷，不是当前页面故障。

建议：按资源域拆分 router，或在单 router 内对 route group 建立显式白名单；微服务测试必须增加负向断言，确认不属于该服务的写接口不存在。

### F-04 P1：完整包 Worker 并发参数串用

证据：

- `extracted-app/scripts/worker-entrypoint.sh:21`
- `artifacts/oracle-recovery-service-docker-run-20260731-api-orchestration-complete-r1-no-business-db/start-service.sh:204-214`

entrypoint 使用：

```sh
WORKER_CONCURRENCY="${SM3_WORKER_CONCURRENCY:-${WORKER_CONCURRENCY:-2}}"
```

完整包又给所有 Worker 注入 `SM3_WORKER_CONCURRENCY=$MICROSERVICE_WORKER_CONCURRENCY`。因此即使 API Orchestration 传入 `WORKER_CONCURRENCY=2`，仍可能被全局 SM3 值 1 覆盖。

128 当前日志显示 API Orchestration 并发 2、其余为 1，但 128 尚未由该最近完整包替换，不能用当前容器证明完整包脚本正确。

建议：entrypoint 只读取通用 `WORKER_CONCURRENCY`；各服务的专用变量在启动脚本调用 `start_worker` 前解析一次，不要把 SM3 变量注入所有 Worker。增加“容器实际 `celery concurrency` 等于包声明值”的包级测试。

### F-05 P1：128 系统 MySQL 已被 `latest` 初始化为 9.3

只读实测：

```text
image: mysql:latest
version: 9.3.0
volume: oracle_recovery_mysql_data:/var/lib/mysql
```

这违反项目强制 `mysql:8.4` 的规则。因为数据目录已经由 9.3 初始化，不能直接把镜像标签改为 8.4 后复用同一卷，否则存在数据字典不兼容和无法启动风险。

建议：先备份系统元数据库，在隔离环境验证 9.3 到 8.4 的逻辑迁移方案；通过导出/导入迁回 8.4，新卷验证后再切换。不得对当前卷直接降版本启动。

### F-06 P1 业务风险：批量授权权限基线未实现

`docs/PROJECT_PRD_SUMMARY.md` 已明确列为 P1：授权前记录既有权限、本批次新增标记、多批次引用判断和回收前二次校验。当前模型和服务中未找到对应权限快照或引用计数字段。

影响：用户原本已有权限，或同一权限被多个有效批次使用时，到期/手动回收可能误撤不属于本批次的权限。

该项属于已知未完成需求，不是本轮新增回归，但在完成前不应把批量授权回收描述为生产安全闭环。

## 9. P2/P3 风险候选

| 编号 | 等级 | 风险 | 证据与边界 |
|---|---|---|---|
| R-01 | P2 | 遗留 `POST /doris-encryption/execute` 使用 API 内 daemon thread，任务状态只在进程内存 | `services/doris_encryption.py:228-255`；当前页面不调用该入口，批次接口走 Worker |
| R-02 | P2 | SM4 调度刷新最多对 220 个离线运行逐个请求节点详情，可能形成 N+1 请求峰值 | `ui.html:10009-10024`；当前样本量小，未压测确认故障 |
| R-03 | P2 | SM3 任务完成时自动重新加载 Catalog 并重建选择集合，可能覆盖用户此时正在编辑的未保存字段选择 | `ui.html:11757-11771`；本轮无运行中样本，仅静态确认风险路径 |
| R-04 | P2 | `/health` 只检查 MySQL，不检查 Redis、业务队列和 Worker | `api/v1/health.py:8-14`；容器也没有应用级 Docker HEALTHCHECK |
| R-05 | P2 | 批量授权到期 scheduler 没有分布式抢占锁 | `services/batch_authorization.py:679-733`；当前只有一个 monolith API，不会立即重复执行 |
| R-06 | P2 | 128 无 Swap、根分区 79%、历史容器和镜像较多 | 当前未造成故障，但会提高构建、长任务和发布失败概率 |
| R-07 | P2 | Redis 使用 `redis:latest` | 当前可用，但版本不可复现，建议固定已验证版本 |
| R-08 | P2 | 关键模块测试覆盖不均 | 连接、CSV、清理、用户/API Key 缺少模块级自动化写入闭环 |
| R-09 | P3 | 测试必须手工设置 `PYTHONPATH=src`，Ruff 不在测试环境 | 降低新会话和 CI 的可复现性 |
| R-10 | P3 | 项目根目录不是 Git 仓库 | 无法可靠追踪基线、修改归属和回滚点 |
| R-11 | P3 | `ui.html` 约 1MB，跨 18 个入口共享全局状态和事件 | 不是功能缺陷，但显著增加“改一处影响另一处”的回归概率 |

## 10. PRD 与实现差异

| 需求域 | PRD 目标 | 当前实现 | 判断 |
|---|---|---|---|
| 数据同步多表执行 | 一条总运行记录、每表节点、实时日志、可配置并行度 | 模型、API、历史页面和自动化均存在；历史样本可见逐表阶段和 SQL | 一致；本轮未重跑百表压力 |
| 数据同步方式 | `auto/insert_select/stream_load`，日志记录真实方式 | 自动化覆盖 internal/catalog，历史日志显示 `INSERT SELECT` | 一致 |
| 数据同步结构基准 | `source/target`，源基准补字段，类型映射 | 自动化和服务代码存在 | 自动化一致；未对生产表执行 DDL |
| 百表连接恢复 | 写前受控重试，写后结果未知不盲重试，共享恢复门控 | 22 个同步测试及 128 历史发布记录覆盖 | 一致；未在本轮注入网络故障 |
| 离线开发 Worker | 独立 `data_platform` 队列承接离线开发 | Worker 无对应任务，API thread 执行 | 明确偏差，见 F-02 |
| 微服务 API 隔离 | 独立 API 只加载所属业务路由 | data-sync/doris-sql 暴露整个 data-platform 路由 | 明确偏差，见 F-03 |
| 资源开通 Token | 用户名密码自动换 Token、内存缓存、失效刷新一次 | 单元/集成测试覆盖，页面不再手工输入 Token | 一致 |
| 数据连接授权 | 手工二级应用、只读查询资源 ID、独立批次与日志 | 页面可切换，自动化覆盖唯一/冲突/重试 | 一致；128 当前无样本 |
| 角色删除 | 保存 `result` 角色 ID，逐行人工删除，Token 脱敏 | 自动化覆盖，相关 API/模型存在 | 一致；本轮未调用真实外部接口 |
| 接口编排 Token 传递 | 前节点结果映射到后节点请求 | 128 DEMO 四节点运行成功，动态 bearer 自动化覆盖 | 当前 MVP 一致 |
| 接口编排高级认证 | 连接器级用户名密码登录、Token 缓存和受控刷新 | PRD 明确尚未实现 | 已知缺口，不应宣称完成 |
| 接口编排失败续跑 | 从失败节点原位继续 | 当前会从发布快照新建运行并从开始节点执行 | 已知缺口 |
| 接口编排高级节点 | 并行/汇聚、数组、人工确认、补偿、定时、Webhook、业务适配器 | 尚未实现 | 已知分期范围 |
| 批量授权权限基线 | 保护外部既有权限和多批次共享权限 | 尚未实现 | 高风险缺口，见 F-06 |
| MySQL 交付版本 | 固定 `mysql:8.4` | 包规则正确，但 128 当前为 `latest` / 9.3.0 | 环境偏差，见 F-05 |

## 11. 定时刷新与编辑状态审计

当前前端共有 5 处 `setInterval`，另有离线日志的递归 `setTimeout`。

| 刷新器 | 刷新内容 | 编辑覆盖判断 |
|---|---|---|
| 恢复详情 2.5 秒 | 任务摘要和事件 | 不覆盖提交表单，安全 |
| 资源开通 2 秒 | 批次列表与已选批次详情 | 不改新建表单，安全 |
| SM4 批次/调度 3 秒 | 运行列表、上下文和日志 | 不改任务表单；存在 R-02 请求放大 |
| SM3 任务 3 秒 | 任务/队列，任务结束时刷新 Catalog | 可能重建未保存选择，见 R-03 |
| 离线运行日志 3.5 秒 | 当前运行和节点详情 | 仅日志页启用，不覆盖画布 |

未发现一个全局定时器会无条件重载所有模块表单。确认的问题主要集中在 SM3 完成时的 Catalog 重载风险，而不是普遍性的“所有编辑页面定时被清空”。

## 12. 建议修复顺序

1. 修复 F-01 离线画布拖动阈值，并补真实指针回归。
2. 修复 F-04 完整包并发参数串用，先保证下一次交付包不会带错启动语义。
3. 设计 F-02/F-03 的 data-platform 真正队列化和路由拆分；这是架构改造，需单独版本和迁移验收。
4. 制定 F-05 MySQL 9.3 到 8.4 的逻辑迁移方案，禁止原卷直接降级。
5. 完成 F-06 批量授权权限基线后，再做授权和回收生产闭环。
6. 处理 R-01/R-02/R-03，随后补健康检查、固定 Redis 版本和测试入口。

## 13. 本轮未执行事项

- 未新建、修改或删除用户、API Key、数据连接。
- 未运行 Oracle/MySQL/SQL Server 恢复。
- 未执行 CSV 导入、Doris SQL 写入、数据同步、SM4/SM3、批量授权、角色删除或数据库清理。
- 未触发真实外部系统接口。
- 未重启或替换 128 容器。
- 未清理 Docker 历史资源。
- 未修改任何业务源码。

因此本报告是“全模块只读审计 + 安全交互 + 自动化 + 历史运行证据”，不是对所有高风险写操作的生产验收。后续修复后仍需按受影响模块创建隔离数据，执行发布验证模式的真实保存、运行、日志和结果闭环。

## 14. 2026-08-04 整改复核附录

本报告中的 F-01 至 F-05 已完成整改，并于 2026-08-04 在 `192.168.150.128` 复核通过；F-06 经源码和数据库核验确认原结论已过时，权限基线字段已存在并有专项测试覆盖。复核包含 MySQL 9.3 到独立 MySQL 8.4 数据卷的逻辑迁移、`data_platform` Celery 队列真实闭环、API/Worker/Redis/在途任务检查和可见 Chrome 四种窗口状态验收。

具体证据见 [`RELEASE_VALIDATION_20260804_AUDIT_REMEDIATION.md`](RELEASE_VALIDATION_20260804_AUDIT_REMEDIATION.md)。历史章节保留原审计时的缺陷结论，避免把审计时点与整改后状态混在一起。
