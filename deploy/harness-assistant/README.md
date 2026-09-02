# Harness 数据智能助手

代码已于 2026-09-01 以热更新方式发布到 128，并完成隔离 Oracle→Doris 四阶段真实链路验证；本次没有生成完整部署包。真实模型地址、模型名与 API Key 尚未提供，因此自然语言模型入口保持失败关闭，不能将合成模型运行时验证视为真实模型验收。

## 使用方式

1. 在原有模块准备一条已经验证的处理路径：成功的 Oracle DMP 恢复模板、数据同步组件、已上线的 Doris SQL 生产工作流、标准目标库表及已保存的全库 SM4 加密任务。
2. 在“数据自动化流水线”绑定上述对象。恢复模板必须能生成并保留真实 Oracle 用户、目标连接引用与加密口令，供同步 Worker 直连；同步映射的源表必须存在于该 DMP。SQL 生产版本只允许 Doris SQL 和 manual 节点，不能夹带另一次同步或加密。标准目标必须与 SQL 实际写入目标一致。
3. 若只希望通过助手手动触发，不要打开这条路径的目录自动监听；确认助手计划不依赖监听开关。已开启的旧监听仍按原规则扫描，并可能先登记同一个文件，助手会阻断重复批次。
4. 管理员登录主工作台，点击侧栏“Harness 智能助手”。例如输入：“把 customer_202608.dmp 自动还原，同步到 ODS，更新自然资源 DWD，然后执行全库加密任务。”可提前选择处理路径、全库任务，消除歧义。
5. 点击“让 Harness 规划”，只生成草案。展开确认窗口，核对分卷文件、实际同步表/写入策略、冻结的生产 SQL、DWD 目标、全部加密表与字段。勾选确认后点击“确认并启动”。
6. 后台通常每 30 秒推进一次，页面每 10 秒回读。关闭页面不停止已确认任务；从“最近计划”恢复查看，按四阶段运行 ID 到原模块查看详细日志。

未配置模型时，页面明确提示未启用。可以使用“显式指定文件生成计划（不调用模型）”验证业务计划，但这不等于自然语言能力已启用。

## 全库加密规则

- 执行所选全库任务保存的**全部表、全部选定字段、目标后缀和输出策略**。范围可能大于本次 DMP 或 DWD，不按字段血缘缩减或推断另一组表。
- 确认计划后冻结本次范围；随后修改原任务只影响新计划，不能悄悄改变当前批次。
- 不生成密钥、不轮换函数。加密批次创建时，由原 SM4 模块绑定当时有效的已部署密钥版本；不是规划时间的版本。需要全链路固定某历史密钥版本不在本版范围内。
- 只对能按连接、Catalog、数据库、表名确证的源资产登记加密血缘；不会因不同库里有同名表而伪造关系。

## 模型和隔离配置

使用独立规划桥接进程，不把 Harness 装入应用 Python 环境。桥接只接收用户指令、候选任务名称/ID/目标库表等元数据，不接收数据库密码、密钥种子、业务行数据或生产 SQL。用户指令仍会发送给所配置的模型，因此不要在聊天中输入密码或业务敏感记录。

固定 `deepseek-harness-sdk==0.1.1rc1`，该发布版使用 `cordis/session_root`；不能直接套用 GitHub master 的 `dsh_home/profile/patches` 示例。运行时是 Linux/macOS 原生程序，本版本无 Windows wheel；Linux wheel 要求 glibc 2.28 或更新。Dockerfile 使用 Debian bookworm，避免直接依赖旧宿主机的 glibc。官方将其标记为开发预览，隔离不可省略。

配置文件：

- 复制此目录 `.env.example` 为本地保密的 `planner.env`，填写模型地址、模型名称和 API Key；示例地址/模型仅为填写示例，需以自己的实际服务为准。
- `HARNESS_BRIDGE_TOKEN` 填入足够长的随机秘密，并与应用环境保持一致；不要提交 Git、写进镜像或发送到聊天。
- 参考 `app.env.example`，只向应用追加桥接 URL 和 Token，不重写已有应用配置。生产桥接限私网访问；跨主机应使用 TLS 和访问控制。
- 默认未配置时功能关闭。桥接只允许单个并发规划请求，Harness 请求超时 60 秒，应用等待上限 90 秒。模型输出仅是建议，执行授权始终来自管理员确认和服务端校验。
- `cordis.yml` 不注册 Shell、文件读写、子进程、Web 或数据库工具，不加载工作区上下文或 skills。每次规划使用临时会话目录，结束后清理。

## 部署要点

先授权发布并完成无在途任务检查及备份，再部署。新增系统表 `assistant_plans` 由现有 `init_db.py`/应用初始化创建，不删除历史表；系统 MySQL 仍固定 `mysql:8.4`。

应用需要同步更新 API、Data Sync Worker 和 Data Platform Worker 所使用的源码。它们依赖新模型及快照函数，不能仅替换一个页面。Oracle/SM4 Worker 复用原执行入口。新增源码和已有修改项见验证记录。API 需运行 `monolith/all` 或存在 `data-platform` 服务来推进后台批次；单独 gateway 不启动此调度器。

以下为在本目录构建隔离桥接的操作示例，需在授权部署时将网络名替换为项目已存在的私有 Docker 网络；不发布桥接宿主机端口：

```sh
docker build -t oracle-recovery-harness:0.1.1rc1 .
docker run -d --name oracle-recovery-harness \
  --network YOUR_EXISTING_APP_NETWORK \
  --env-file ./planner.env \
  --security-opt seccomp=unconfined --pids-limit -1 \
  --ulimit nproc=65535:65535 \
  --read-only --tmpfs /tmp:rw,nosuid,size=256m \
  --memory 1g --cpu-period 100000 --cpu-quota 100000 \
  oracle-recovery-harness:0.1.1rc1
```

保留旧 Docker 线程兼容参数；不使用 compose、`--pull`、`--mount` 或 host-gateway。未挂载 Docker socket、应用源码、数据库目录，也不向规划器提供应用登录令牌。上述 Dockerfile/资源限额尚未在 docker-ce 17.03 实机验收，应在后续发布验证中检查，不能把 Linux 原生运行时测试等同于 Docker 验收。

## 失败与执行边界

这不是跨 Oracle/Doris 的分布式事务。源文件按路径/大小/mtime 做至少两次稳定性观测，不是 DMP 内容哈希；仍应使用完整上传后原子改名等规范，防止同大小同时间戳替换。

只在上游成功后进入下一步。明确失败时先核对部分写入，再点“核对后继续失败阶段”；只重试该阶段，保留前面成功阶段。不建议在未检查追加数据、SQL 副作用、加密输出的情况下重试。

如果显示“提交中 / 结果待核对”，可能已有外部任务提交成功但回执丢失，系统不会自动重复派发。本版未提供一键关联不确定运行的管理工具，也不提供总流程暂停/取消或跨所有路径的数据库写锁；应按已有运行 ID/时间/任务名人工核对，不直接修改元数据状态来强行重试。

路径内阻断并发确认不代表与所有手工任务或其他路径互斥。确认前仍需检查同一库表是否有冲突任务。规划器不会自动编写业务 SQL、创建新路径、上传 DMP，或验证模型推荐路径的业务语义完全正确。

## 本地验证

- 应用测试：在 `extracted-app` 中设置包含当前目录和 `src` 的 `PYTHONPATH`，运行 `python -m pytest tests -q`。
- UI：运行 `extracted-app/tests/assistant_ui_server.py` 后，执行 `extracted-app/tests/assistant_ui_check.cjs`（需 Playwright 与 Chrome）。仅访问本机 18098 端口的内存数据库，无真实业务写入。
- 真实 Harness：在独立 Linux 环境安装 `requirements.txt`，运行 `python deploy/harness-assistant/verify_runtime.py`。使用本地合成模型，测试真实 SDK/运行时启动、JSON 返回和没有模型可调用工具；不是付费模型测试。

详细结果与待验项见 `docs/HARNESS_ASSISTANT_VALIDATION_20260831.md`。
