# 瀚海22：GPU Broker 外部 Slurm（CPU / GPU）调度使用指南

更新时间：2026-07-31（Asia/Shanghai）

## Agent 警告

本文中的 `hh22`、`ssh`、`sbatch`、`srun`、`scancel` 示例仅供人类诊断、核查或 Broker 尚未覆盖操作时参考。Agent 处理瀚海22或其他外部 Slurm 集群必须通过 `SchedulerTarget` 和 `gpu-broker` MCP 的 scheduler 工具；不得把这些手工命令当成 MCP 不可用时的 fallback，也不得把登录节点按项目本地 SSH endpoint 规则登记或认领 GPU。若 MCP 或 scheduler 工具不可用，Agent 应报告不可用并停止到需要人工处理的位置。

## 定位

瀚海22不是一台可以长期独占、由 GPU Broker 直接分配单卡的 SSH 裸机，
而是一个由 Slurm 统一分配计算节点和 GPU 的外部集群：

```text
用户 / Agent
    -> GPU Broker：记录任务意图、资源合同和协调边界
    -> USTC SCC VPN：提供校外网络入口
    -> SSH 登录节点：编辑、编译、传输、提交和管理作业
    -> Slurm：排队并授予 CPU / GPU 资源
    -> 临时计算节点：在作业时限内运行 workload
```

因此，瀚海22的实际 GPU 分配权属于 Slurm。登录节点不是计算节点，
`inventory`、登录节点连通性或登录节点上的静态信息都不能证明某张 A100
当前可用。

## 当前集成状态

当前采用独立的 `external-slurm` adapter；人工 `hh22` 只作诊断入口：

| 项目 | 当前行为 |
| --- | --- |
| 网络接入 | OpenVPN Connect 中的 `USTC SCC VPN (账号认证)` |
| 登录 | 本机 `hh22` 命令，自动处理 SSH 密码和 TOTP |
| 目标发现 | `gpu_scheduler_targets`，目标 ID 为 `hanhai22` |
| 接入检测 | `gpu_scheduler_access_status("hanhai22")`，只检测、不操作 VPN |
| 资源申请 | 项目 Profile 用 `gpu_scheduler_submit_profile`；带明确资源合同的一次性脚本用 `gpu_scheduler_submit_once` |
| 状态与取消 | `gpu_scheduler_job_status` / `gpu_scheduler_cancel` |
| 数据上传 | 当前不公开；由项目自己既有的数据路径处理 |
| GPU Broker | 保存授权、请求摘要、Job ID、状态事件和审计；不替代 Slurm 分配 |
| 作业归属 | 以 Slurm Job ID、account、partition、QoS 和 AllocTRES 为准 |

持续边界：

- 不要把两个瀚海22登录节点粘贴到 Dashboard 的“添加服务器”，也不要让
  Collector 把登录节点当成 GPU endpoint。
- 不要用 `gpu_claim` 的裸机租约表示一个瀚海22作业已经取得 GPU。
- 不要对瀚海22作业调用 `gpu_bind_observed_workload`；当前 Collector 无法从
  登录节点观测 Slurm 分配到的动态计算节点和 GPU。
- 不要通过 Broker 的静态 inventory 推断瀚海22空闲卡。用
  adapter 的固定 `sinfo`、`squeue`、`sacct` 查询结果判断 Slurm 事实。
- Profile 与带完整资源合同的一次性脚本可由项目 Agent 日常提交；脚本默认只长期保存
  SHA-256 摘要，只有显式设置时保留全文。
- `gpu_scheduler_cancel` 只作用于调用项目拥有的作业。

adapter 只开放批处理提交、状态刷新和 owner cancel；不公开抢占、自动 VPN、任意远端
shell、上传/下载或交互式终端。

## 本机一次性配置

本机已经配置：

- OpenVPN Connect 配置：`USTC SCC VPN (账号认证)`。
- 登录命令：`~/.local/bin/hh22`。
- SSH 密码和 TOTP 安全密钥：保存在 macOS 钥匙串。
- TOTP 防复用状态：
  `~/Library/Caches/USTC Hanhai22/last_totp_counter`。
- 登录节点：
  - `211.86.151.113`（`hanhai22-01`）
  - `211.86.151.115`（`hanhai22-02`）

脚本文件不包含 SSH 密码、安全密钥或应急码。安全密钥是长期 TOTP 种子；
每 30 秒变化的是由它计算出的验证码，不需要重复申请安全密钥。

## 每次使用的最短流程

### 1. 连接 VPN

1. 连接前关闭 Clash Verge 的 TUN 模式，避免 OpenVPN UDP 流量被代理接管。
2. 在 OpenVPN Connect 选择 `USTC SCC VPN (账号认证)`。
3. 点击 **Connect**；如客户端询问密码，输入单独的 SCC VPN 密码。
4. 确认客户端显示 `Securely Connected!`。

VPN 密码与瀚海22 SSH 密码是两套独立凭据。

### 2. 登录

```bash
hh22       # 登录 hanhai22-01
hh22 2     # 登录 hanhai22-02
```

也可以直接运行只读或管理命令：

```bash
hh22 1 hostname
hh22 1 squeue -u '$USER'
```

`hh22` 会在认证提示出现时生成当前验证码，并通过本地状态锁避免在不同连接中
重复使用同一个 30 秒验证码。短时间内的 SSH 会复用同一个 master connection。

### 3. 只在登录节点做轻量工作

允许的典型工作：

- 编辑代码和作业脚本。
- 编译、准备环境和少量预处理。
- 上传、下载和整理数据。
- 执行 `sinfo`、`squeue`、`scontrol`、`sacct`、`sbatch`、`scancel`。
- 用 `tmux` 保持轻量管理会话。

不要在登录节点直接运行训练、推理、大规模数据处理或长期服务。瀚海22官方说明
要求正式计算通过 Slurm 调度。

## Broker 任务输入如何映射到 Slurm

每个瀚海22任务至少明确以下合同：

| Broker / 任务语义 | Slurm 参数 |
| --- | --- |
| `project_id` | 本地任务归属标签；当前不改变 Slurm account |
| `task_ref` | `--job-name`，并记录返回的 Job ID |
| `gpu_count > 0` | `--gres=gpu:<type>:<N>`（未指定 type 时为 `gpu:<N>`） |
| `gpu_count = 0` | 纯 CPU / 内存作业：不传 `--gres`，也不得填写 `gpu_type` |
| CPU 核数 | `--cpus-per-task=<N>` 或 MPI 的 `--ntasks=<N>` |
| 系统内存 | `--mem=<SIZE>` |
| 最长运行时间 | `--time=<D-HH:MM:SS>` |
| 分区 | `--partition`（CPU-only 合同选择实际可用的 CPU 分区） |
| QoS | 可选 `--qos`；提供时必须是该分区允许的值 |
| 节点拓扑 | `--nodes`、`--ntasks-per-node`；GPU 作业另有 GRES |

这里的合同用于明确用户意图和生成 Slurm 请求，不能在 Slurm 返回资源前被解释为
“已经获得计算资源”。真正的授权证据是作业进入 `RUNNING` 且
`scontrol show job <JOB_ID>` / `sacct` 报告了已分配的 TRES。

CPU-only 合同适合没有 GPU、但可提供 CPU 和内存的分区或节点。它仍必须指定
真实的分区、CPU 核数、内存、节点数和运行时限；在分区要求或合同明确时指定
QoS。Broker 不会因省略 GPU 而绕过 Slurm 或把登录节点视作计算资源。

## 提交一个正式 GPU 作业

示例 `gpu_job.slurm`：

```bash
#!/bin/bash
#SBATCH --job-name=my-train
#SBATCH --partition=GPU-8A100
#SBATCH --qos=gpu_8a100
#SBATCH --gres=gpu:a100:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=1-00:00:00
#SBATCH --output=my-train-%j.out
#SBATCH --error=my-train-%j.err

set -euo pipefail

# 按项目实际情况加载环境。
# source ~/miniconda3/bin/activate
# conda activate myenv

srun python -u train.py
```

提交：

```bash
sbatch gpu_job.slurm
```

保存输出的 Job ID，例如：

```text
Submitted batch job 123456
```

`sbatch` 作业提交成功后不依赖当前 SSH 终端。退出 SSH 或 VPN 后，作业仍由
Slurm 管理并继续运行，直到完成、失败、被取消或达到时限。

## 交互式调试

短时间 GPU 调试：

```bash
srun \
  --partition=GPU-8A100 \
  --qos=gpu_8a100 \
  --gres=gpu:a100:1 \
  --nodes=1 \
  --ntasks=1 \
  --cpus-per-task=8 \
  --mem=64G \
  --time=02:00:00 \
  --pty bash
```

短时间 CPU 测试：

```bash
srun \
  --partition=test \
  --qos=qos_test \
  --nodes=1 \
  --ntasks=1 \
  --cpus-per-task=4 \
  --mem=8G \
  --time=00:20:00 \
  --pty bash
```

`srun` / `salloc` 交互任务依赖提交终端。官方说明提交终端断开时交互任务会终止，
因此正式训练和长任务应使用 `sbatch`。虽然登录节点安装了 `tmux`，也不要把
它当作正式作业持久性的替代保证。

## 查看、审计和取消

```bash
# 分区实时状态
sinfo -l
scontrol show partition GPU-8A100

# 当前任务
squeue -u "$USER"
scontrol show job <JOB_ID>

# 已完成任务和实际资源
sacct -j <JOB_ID> \
  --format=JobID,JobName,State,Elapsed,AllocTRES,ExitCode

# 日志
tail -f my-train-<JOB_ID>.out

# 取消
scancel <JOB_ID>
```

建议为每个 Job ID 记录：

- `project_id` / `task_ref`
- 提交参数和脚本路径
- Slurm Job ID
- partition / QoS
- 请求与实际 `AllocTRES`
- 最终状态、退出码和日志路径

adapter 会把这组记录写入 `SchedulerJob` 及其追加式状态事件；它们不是裸机
GPU Broker lease。Slurm `PENDING` 时绝不会生成“已获配 GPU”的假 lease。

## “常驻服务器”的边界

可以常驻：

- home 目录中的代码、环境、数据和结果。
- 登录节点上的轻量 `tmux` 管理会话。
- 已提交的 `sbatch` 作业；断开 SSH / VPN 后仍继续运行。
- 在 Slurm 分配时限内运行的 Jupyter、推理服务等 workload。

不能常驻：

- 不能把登录节点当计算服务器。
- 不能永久占有一台 A100 计算节点。
- 不能让交互式 allocation 无限期保留。
- 不能绕过 Slurm 直接选择计算节点或 GPU。

Jupyter 或推理服务也应作为 Slurm 作业启动，并通过登录节点建立 SSH tunnel；
到达 `--time`、作业失败或被取消后，计算资源会被 Slurm 回收。需要永久服务、
固定节点或超出队列时限时，应向超算中心申请专门方案，而不是绕过调度器。

## 当前分区快照

官方硬件类型与分区的对应关系：

| 分区 | 计算节点硬件 |
| --- | --- |
| `GPU-8A100` | 2 × Intel Xeon 8358（64 核）、1 TB 内存、8 × NVIDIA A100 SXM4 80 GB |
| `CPU-64C256GB` | 2 × Intel Xeon 8358（64 核）、256 GB 内存 |
| `CPU-96C3TB` | 4 × Intel Xeon 6348H（96 核）、3 TB 内存 |
| `CPU-192C768GB` | 2 × AMD EPYC 9654（192 核）、768 GB 内存 |

以下是 2026-07-31 14:14（Asia/Shanghai）通过当前账户读取的实时快照：

| 分区 | QoS | 节点数 | CPU（已分配/空闲/其他/总数） | GRES | 当前最长时间 |
| --- | --- | ---: | --- | --- | ---: |
| `GPU-8A100` | `gpu_8a100` | 18 | `912/240/0/1152` | `gpu:a100:8` | 10 天 |
| `CPU-64C256GB` | `qos_cpu_64c256gb` | 69 | `3828/588/0/4416` | 无 | 15 天 |
| `CPU-96C3TB` | `qos_cpu_96c3tb` | 10 | `547/413/0/960` | 无 | 15 天 |
| `CPU-192C768GB` | `qos_cpu_192c768gb` | 66 | `12242/430/0/12672` | 无 | 15 天 |
| `test` | `qos_test` | 2 | `0/128/0/128` | 无 | 20 分钟 |

`GPU-8A100` 当前报告 144 张 A100。队列规模、时限和策略可能变化，提交前以
以下实时结果为准：

```bash
sinfo -l -p GPU-8A100
scontrol show partition GPU-8A100
```

公开旧培训材料中的节点数和 5 天时限已经与当前集群配置不同，不应硬编码到
Broker 或 workload profile。

## 当前账户的实机路径

2026-07-31 14:13（Asia/Shanghai）通过 Broker 固定只读检查确认：

| 项目 | 实机结果 | 用法 |
| --- | --- | --- |
| 登录节点 | `hanhai22-01` | 仅编辑、编译、传输和提交作业 |
| 用户名 | `jinplu` | SSH / Slurm 用户身份 |
| 个人主目录 | `/home/zwxionggroup/jinplu` | 已确认存在且当前账户可写 |
| `/home` 根目录 | 存在、不可直接写 | 不要把文件直接放在 `/home` 根目录 |
| `/opt` | 存在、不可写 | 只读取中心安装的公共软件 |
| `/data` 根目录 | 存在、不可直接写 | 不能假设拥有 `/data` 根目录写权限 |
| `/scratch`、`/public` | 固定检查未发现 | 不要在作业脚本中硬编码 |

个人主目录位于共享的 GPFS `/home` 文件系统，登录节点和计算节点均可见。检查时
文件系统总量约 8.0 PiB、全局可用量约 1.77 PiB、整体使用率 78%；这些是整个
文件系统数据，不是个人剩余额度。`quota -s` 本次未返回个人配额摘要，个人额度
仍以中心政策和后续 `quota`/管理员查询为准。

项目默认工作目录应放在：

```text
/home/zwxionggroup/jinplu/<project-name>
```

目录不存在时，必须在具体项目获得写入授权后再创建；Broker 不会在只读接入检查
中自动建目录。

## 故障处理

### OpenVPN 一直超时

确认 Clash Verge TUN 已关闭，再重新连接。到 VPN 服务端的 UDP 流量被 TUN
全局代理接管时，OpenVPN 可能只有发送、没有服务器响应。

如果 OpenVPN 日志持续显示 `Can't assign requested address`，检查 VPN 服务端
`114.214.240.89` 是否残留指向旧 Wi-Fi 网关的 `/32` 路由。完整退出并重新启动
OpenVPN Connect 可让其 privileged helper 清理该路由；不要在未知目标上批量
删除系统路由。

### SSH 端口不可达

```bash
nc -vz -w 5 211.86.151.113 22
nc -vz -w 5 211.86.151.115 22
```

先确认 OpenVPN 显示已连接，再检查路由是否经 VPN。

### 再次索要密码或验证码

立即停止该次连接，不要连续重试。系统每 30 秒最多允许有限次数，并禁止验证码
复用。检查系统时间、钥匙串条目和 `hh22` 状态文件后，再进入下一个验证码周期。

### 作业排队

`PENDING` 不表示已经获得 GPU。查看原因：

```bash
squeue -j <JOB_ID> -o "%.18i %.9P %.16j %.2t %.10M %.6D %R"
scontrol show job <JOB_ID>
```

常见原因包括资源不足、优先级、QoS、时限或参数不满足。不要改用登录节点直接跑。

## 已实现的 Slurm adapter 契约

GPU Broker 使用独立 Slurm adapter，不复用裸机 endpoint / Collector 语义：

1. 以 `cluster_id + partition + qos` 标识调度域，不能以登录节点 IP 代表 GPU。
2. 请求由 Broker 生成或校验 Slurm 资源合同，再提交到 Slurm。
3. Broker 请求排队与 Slurm `PENDING` 状态需要显式映射，不能出现双重“已分配”。
4. 当前不建立裸机镜像 lease；`RUNNING` 和 AllocTRES 只进入外部 allocation
   事实记录，避免伪造 GPU UUID。
5. 作业归属以 `cluster_id + job_id + allocated_node + GPU TRES` 为身份边界。
6. 状态采集使用固定只读 Slurm 查询，不接受 MCP 提供任意 SSH 选项或查询命令。
7. `COMPLETED`、`FAILED`、`CANCELLED`、`TIMEOUT` 等终态驱动镜像 lease 收尾。
8. 提交和取消是独立权限，不能由普通 Broker lease 自动获得；续期、抢占和
   服务暴露尚未开放。
9. VPN、TOTP 和登录连接属于 access adapter；Slurm 分配属于 scheduler adapter，
   两者必须分层，不能把“能 SSH”解释为“能占 GPU”。

人工 `hh22`/`sbatch` 仅用于诊断或 Broker 尚未覆盖的操作，不能让 Agent
以项目本地 SSH endpoint 规则替代 `SchedulerTarget`。

## 官方资料

- [瀚海22系统介绍](https://scc.ustc.edu.cn/zlsc/user_doc/html/introduction/hanhai22-introduction.html)
- [Slurm作业调度系统](https://scc.ustc.edu.cn/zlsc/user_doc/html/slurm/slurm.html)
- [GPU计算说明](https://scc.ustc.edu.cn/zlsc/user_doc/html/gpu-computing/gpu-computing.html)
- [超算中心VPN说明](https://scc.ustc.edu.cn/vpn/readme.html)
