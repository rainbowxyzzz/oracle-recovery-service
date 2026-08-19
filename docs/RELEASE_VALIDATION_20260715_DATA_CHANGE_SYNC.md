# 数据变化触发器与数据同步组件发布验证报告

日期：2026-07-15  
验证环境：`192.168.150.128`  
访问入口：`http://192.168.150.128:8000/ui`

## 1. 发布范围

本次以文件级热更新方式发布到 `oracle-recovery-api` 和 `oracle-recovery-worker`，未重新打包镜像，未修改业务恢复目标库。

主要发布内容：

- 离线开发新增数据变化触发器和数据同步组件。
- 工作流发布快照与 `execution_content_hash`。
- SM3任务修订冻结，SM4/SM3显式修订绑定。
- 变化触发三态水位、探测审计、暂停恢复、在途策略和失败重试。
- Doris同连接跨库及跨连接同步；全量、追加、增量追加和主键更新。
- 原SM4自动快照停止自动巡检，历史任务和批次保留。
- SM4密钥与任务解耦，任务提交和运行不再隐式刷新密钥。

正式需求见 `docs/DATA_CHANGE_TRIGGER_AND_SYNC_PRD.md`。

## 2. 发布前保护

- 发布前确认工作流、SM4和SM3在途任务均为0。
- API和Worker原始源码分别备份。
- 系统元数据库完成全量SQL备份，共37张表。
- 备份目录：`/root/oracle-recovery-backups/data-change-20260715-164453`。
- 备份文件：`oracle_recovery.sql`，9.2MB，末尾存在 `Dump completed`，SHA256校验通过。

## 3. 自动化测试

- 本地 `compileall` 通过。
- 线上候选源码在API容器隔离目录中完成完整测试：32/32通过。
- UI内联JavaScript语法检查通过。
- API和Worker发布后关键文件SHA256一致。

发布过程中根据测试和真实运行补充修复：

- 历史任务引用缺失时，发布元数据回填不再阻止应用启动。
- 工作流复制允许保留已失效任务引用供用户修复，提交上线仍严格校验。
- 快速工作流结束时，不再把已完成触发器状态倒退为running。
- 探测历史增加稳定的 `created_at + id` 倒序。
- 启动时中断的变化触发运行立即完成失败收尾并释放 `pending_run_id`。
- 手动运行增量同步时保留用户配置的初始水位。
- 单个探测失败记录审计和日志，不阻断同轮其他触发器。
- 暂停状态跨服务重启保持，运行中禁止重建基线。

## 4. 数据库与API迁移

启动后确认新增表：

- `data_platform_change_trigger_states`
- `data_platform_change_probes`
- `doris_sm3_task_definition_revisions`

新增列：

- `data_platform_workflow_versions.release_snapshot`
- `data_platform_workflow_versions.execution_content_hash`
- `data_platform_workflow_runs.trigger_context`

历史27个工作流版本全部成功回填发布快照和内容哈希。新变化触发器API均已进入OpenAPI。

## 5. 真实Doris端到端验证

验证连接：`Doris CSV Test`  
隔离验证库：`CODEX_CHANGE_20260715`

### 5.1 变化触发与全量原子同步

- 工作流按“开发版创建、提交生产、上线、首次基线、数据变化、自动运行”完整执行。
- 首次探测只建立 `MAX(update_seq)` 基线。
- 第一次变化：生成 `data_change` 运行，触发节点和同步节点均成功，目标2行。
- 第二次变化：再次成功，目标3行，证明目标已存在时 `REPLACE WITH TABLE ... swap=false` 在当前Doris可用。
- 两次成功均将 `applied_value`推进到对应最大值。
- 同内容重复提交复用了同一个生产版本。

### 5.2 失败不推进水位

- 故意配置源表与目标表相同，数据同步节点失败。
- 工作流状态为partial，触发节点成功、同步节点失败。
- `applied_value`保持原基线，最新变化保留在 `pending_value`，状态为 `pending_retry`。

### 5.3 同步模式矩阵

| 模式 | 结果 | 读取/写入 | 水位结果 |
|---|---|---:|---|
| `full_replace` | 成功 | 3/3 | 完整替换并校验 |
| `append` | 成功 | 3/3 | 不使用水位 |
| `incremental_append` | 成功 | 2/2 | 只读取初始水位之后数据，返回下一水位 |
| `primary_key_merge` | 成功 | 2/2 | 写入Doris UNIQUE KEY表并返回下一水位 |

### 5.4 跨连接路径

- 通过系统数据库连接API创建第二个临时Doris连接配置并完成连接测试。
- 工作流强制走跨连接批量读取/参数化写入路径。
- 读取3行、写入3行，字段、目标行数和最大水位校验通过。
- 验证后归档流程并删除临时连接。

该场景验证了不同连接配置的代码路径；当前环境只有一个物理Doris实例，未覆盖两个物理集群之间的网络、版本和字符集差异。

## 6. 触发器控制验证

- `merge`：在途时将 `pending_value`更新为最新值，队列为空，run_id不变。
- `queue`：保留当前 `pending_value`，新值进入 `pending_queue`，run_id不变。
- `skip`：保留当前 `pending_value`，新值不入队，run_id不变。
- 探测失败隔离：不存在的表记为 `probe_failed`并生成failed探测记录，同轮正常触发器仍建立基线。
- 暂停持久化：API重启前后均保持 `enabled=false`、`state=paused`、`next_probe_at=null`。
- 中断恢复：在途运行被API重启中断后，运行变failed，水位不推进，pending保留，`pending_run_id`释放，暂停状态保持。

## 7. 旧自动快照验证

现有历史自动快照任务跨35秒调度周期：

- `last_scan_at`不变。
- `next_scan_at`不变。
- `updated_at`不变。
- 服务日志无自动快照扫描记录。

说明旧能力的数据和手动兼容接口仍保留，但后台不再自动巡检。

## 8. 页面与服务验证

- 页面存在且仅存在一个“数据同步”和一个“数据变化触发器”系统能力入口。
- 旧自动快照配置块保持hidden。
- 980px以下响应式布局规则存在。
- 静态ID检查未发现新增组件ID冲突；`dataPlatformArrow`来自初始SVG与运行时重绘模板，不会同时存在于运行后DOM。
- API健康检查正常，MySQL连接成功。
- API、Worker、Redis、系统MySQL容器均运行。
- 收尾时工作流、SM4、SM3在途任务均为0；临时连接和未归档Codex测试流程均为0。
- 最终API和Worker日志无新Traceback或ERROR。

本机没有可用浏览器二进制，且考虑磁盘空间未额外下载Playwright浏览器，因此未执行像素级截图检查；本次完成HTML、DOM结构、响应式CSS和JavaScript语法验证。

## 9. 保留验证数据与已知环境风险

- 隔离Doris验证库 `CODEX_CHANGE_20260715`保留，便于用户复核测试表和结果；确认不再需要后可单独清理。
- 现网系统MySQL容器镜像显示为 `mysql:latest`，与项目固定 `mysql:8.4`规则不一致。本次未重建系统库容器，避免扩大变更范围；后续应先确认数据目录实际版本并制定迁移方案，禁止直接降级复用数据卷。
- 当前环境未覆盖两个独立物理Doris集群的跨集群验证。
- 未执行浏览器像素级截图验证。

## 10. 完整部署包验证

交付包：`oracle-recovery-service-docker-run-20260715-data-change-sync-no-business-db.zip`

- 从上一版唯一基线包增量派生，目录、脚本组织和README章节顺序保持一致。
- `.env.example`逐行对比仅修改API镜像、Worker镜像和镜像归档3个版本字段；没有新增、删除、重排或修改其他默认值。
- 包内文本文件统一为UTF-8无BOM和LF换行。
- `sh -n start-service.sh load-images.sh status-service.sh stop-service.sh`通过。
- `. ./.env.example`可正常加载，并验证系统MySQL固定为`mysql:8.4`。
- 镜像归档只包含API和Worker，不包含Oracle、SQL Server、Doris或MySQL业务恢复目标镜像。
- API和Worker镜像均为Linux/amd64，Worker默认entrypoint启动并进入ready。
- 在目标Docker 1.13.1上使用包内`load-images.sh`和`start-service.sh`完成真实加载、迁移和容器重建。
- 包启动后API健康检查、Worker ping、新API路由、新元数据表和SM4共享JAR卷均通过。
- 包内镜像启动后再次执行32项测试，全部通过。
- 测试进程退出时出现一次`aiomysql`对象析构阶段的`Event loop is closed`警告；测试退出码为0，迁移、健康检查和服务运行均正常，不影响交付结论。
- 最终ZIP通过`unzip -t`和独立SHA256校验，校验值由同名`.sha256`文件提供。
