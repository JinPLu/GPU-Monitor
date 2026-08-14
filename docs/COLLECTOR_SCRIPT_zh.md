# 服务器采集脚本协议（schema v2）

`server-script-v1` 是面向单台服务器的固定只读采集协议。ServerPilot 对这个 profile 只会通过 SSH
执行下面这一条只读入口：

```text
serverpilot-collect --schema-version 2
```

入口名、参数和 schema version 都由代码固定；Endpoint 配置、REST/MCP 输入或 Agent 不能传入
命令、路径、argv、Docker 参数、环境变量或 SSH option。采集不是远程执行授权。

## 在目标服务器安装

在每个被观测目标上安装包含相同版本 `serverpilot-collect` console entry point 的
ServerPilot 发布包（例如组织提供的 wheel）：

```text
python -m pip install /受控发布目录/serverpilot-<version>-py3-none-any.whl
serverpilot-collect --schema-version 2
```

第二条命令必须在 stdout 仅输出一行 JSON。因为 ServerPilot 使用非交互式 SSH，入口必须位于该
帐号的非交互 `PATH` 中；推荐安装到 `/usr/local/bin/serverpilot-collect`。不要把诊断信息、
banner 或日志写到 stdout。

有 Docker、厂商 runtime 或自定义工具前缀的服务器，可以在**该服务器本地**维护一个同名的
短脚本/包装器。它仍只能接受上面的固定参数并输出完全相同的 JSON。此类本地实现是服务器
管理员的部署责任；不要把 Docker 命令、路径或额外参数填入 ServerPilot Endpoint。

### 从源码构建受控 wheel

在受控构建机的同一版本源码根目录执行：

```bash
uv build
```

将 `dist/serverpilot-<version>-py3-none-any.whl` 交给目标服务器管理员，按其受控的软件分发方式安装，并确保 `serverpilot-collect` 位于该 SSH 用户的非交互 `PATH`。安装后先在目标服务器本地运行固定入口，再在 App 中登记 endpoint；不要把 wheel 路径、pip 命令或运行时环境填入 endpoint 配置。

## JSON 合同

版本 2 的顶层对象必须且只能包含：

```json
{
  "schema_version": 2,
  "identity": {"hostname": "node-a", "boot_id": "..."},
  "host": {
    "cpu_count": 64,
    "load_1m": 1.25,
    "cpu_total_ticks": 1000,
    "cpu_idle_ticks": 750,
    "memory_total_mib": 262144,
    "memory_available_mib": 196608
  },
  "gpu_probe_available": true,
  "gpus": [{"gpu_index": 7, "cuda_ordinal": 0, "gpu_uuid": "GPU-...", "name": "...", "total_vram_mib": 81920, "memory_used_mib": 0, "memory_free_mib": 81920, "gpu_utilization_pct": 0, "memory_utilization_pct": 0, "temperature_c": 35, "power_watts": 100.0, "pstate": "P0", "health": "OK"}],
  "processes": [{"gpu_uuid": "GPU-...", "pid": 123, "used_memory_mib": 1024, "executable": "python", "username": "gpu", "process_started_at": "2026-08-10T00:00:00+00:00"}]
}
```

`gpu_index` 保留 `nvidia-smi index` 供界面识别，`cuda_ordinal` 固定表示设置 `CUDA_DEVICE_ORDER=PCI_BUS_ID` 后的执行 selector，由采集脚本按 `pci.bus_id` 排序计算。两者不得混用。`identity`、`host`、每个 GPU 和每个 process 的字段也是精确集合，不能扩展。数值必须是 JSON
number（不能使用字符串、NaN 或 Infinity）；字符串不能含控制字符；`process_started_at` 必须带
时区。GPU UUID、index 与 CUDA ordinal 分别不得重复，process 的 `(gpu_uuid, pid)` 不得重复，并且 process 必须
指向本快照中的 GPU。

没有 NVIDIA runtime 或没有 GPU 的服务器应返回 `gpu_probe_available: false`，以及空的 `gpus`
和 `processes`。这会保留可用的 CPU/内存观测，但不会把既有 GPU 清单标记为已完成的缺失观测。

ServerPilot 将 stdout 限制为 1 MiB、stderr 限制为 16 KiB，拒绝截断、非 UTF-8、非单个 JSON 对象、
重复 JSON key、未知字段、超限集合和内部不一致的数据。任何拒绝都会使该 endpoint 的本轮观测
失败并保持 fail closed；不会降级为任意 SSH 命令。

中央 Collector 按持久化的 5 / 10 / 30 秒间隔采集。App 的“刷新”只重新读取控制面状态，不会绕过
该间隔另造一次观测。若到期仍没有新观测，ServerPilot 将其视为连接或采集问题，并停止分配对应
资源；不会用旧数据冒充当前状态。

## 迁移

远端只接受当前 schema v2，不读取或降级到 schema v1。安装当前脚本后，应先在目标上手工运行固定入口并验证 JSON，再启用 `server-script-v1` observation profile。
