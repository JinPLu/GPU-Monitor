# Adapter 能力与边界

Adapter 只实现已注册的观测或调度协议；资源身份、准入、租约和审计始终属于
ServerPilot。它不是第二个控制面，也不是远程命令入口。

| Adapter | 能力 | 允许的工作 | 不允许 |
| --- | --- | --- | --- |
| `raw-ssh` | observation | 固定的主机/GPU telemetry 与已观测 PID 的进程详情 | 任意 shell、私钥读取、lease/claim 写入 |
| `server-script-v1` | endpoint_keepalive | 固定 `serverpilot-keepalive --schema-version 2` 的空闲 GPU 保活 | 项目任务启停、调用方指定 GPU、路径或环境 |
| `slurm-command` | scheduler | 固定的 Slurm 查询、提交、取消和上传 | 裸机分配、绕过 approval |

未知 adapter、未知 capability、陈旧/冲突观测或不确定远端结果一律 fail closed。配置只能选择
代码注册的 profile，不能携带 shell、argv、SSH 参数、秘密或 Agent 自定义 target。采集协议见
[COLLECTOR_SCRIPT_zh.md](COLLECTOR_SCRIPT_zh.md)。

## 空闲占卡

Endpoint 不要求用户手工填写 adapter。ServerPilot 在用户明确点击“开始占卡”时自动挂载
代码内置的 `server-script-v1` helper；公开接口仍只有 `enabled: true|false`。开启时只调和已验证的
空闲 GPU；忙碌、未托管、冲突或 stale GPU 保持不动。
ServerPilot 传入精确物理 UUID，helper 只能管理自己的 worker。

每张 GPU 独立判断：一张 GPU 冲突不会阻断同一服务器上其它空闲 GPU 的占卡启动。
若历史工作负载租约遗留为“归属待确认”，人类监控端可执行“清理遗留归属”；ServerPilot
会先重新采集，确认相关 GPU 都没有进程后才释放，单看 0% 利用率不会直接释放。

每张目标 GPU 有独立内部 lease、worker 和健康状态。worker 的目标是约 31% 显存、30% GPU duty
cycle、单 PyTorch CPU 线程和无稳态磁盘/网络 I/O；实际 CPU、RSS、GPU 干扰和停止响应仍须在获授权
的目标主机验证。

即时受管 claim 只有在普通分配失败且服务规划出完整、已验证的逐卡回收方案时，才停止这些 keeper、
取得新鲜空观测并重试原 claim。它不会影响同机其他卡、未托管进程或直接 SSH 任务；直接 SSH
工作前由管理员显式关闭该 endpoint 的策略。

## 外部调度器

外部集群是 `SchedulerTarget`，不是 Endpoint。transport / inspection profile 是封闭 ID；部署管理员
把 ID 映射到固定包装器，API 不保存路径或参数。Agent 用 scheduler 工具；VPN/access 或服务不可用
时报告并停止，不回退到 SSH。

## 不变边界

- adapter 不接触 `BrokerService`、数据库或租约/claim 写入；
- 不新增通用 `execute()`、第二套认证或非 loopback listener；
- GPU 身份始终是 `endpoint_id:gpu_uuid`，adapter ID 只作诊断来源。
