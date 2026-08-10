# io — 块 IO 子系统（v7.2-rc7）

> 一句话摘要：块 IO cgroup 限额（iocost 权重/QoS/成本模型、ioprio 优先级类）、IO 统计观测（/proc/diskstats、io cgroup stat）、块设备调度（已废弃 elevator）、bdev 写已挂载设备开关都在这。

## 我适合什么场景

- **cgroup IO 份额/限额**：多 cgroup 抢同一块设备，关键组延迟差或被挤压，想按权重/QoS 分配 IO 时间。
- **IO 延迟 SLA**：业务对设备读写延迟有硬指标（rlat/wlat），想让 iocost 按 SLA 调度 vrate。
- **IO 优先级分级**：某些 cgroup 的 IO 要提为 RT 抢调度，或次要组降到 BE/IDLE 让路。
- **设备 IO 性能建模**：iocost auto 模型不贴合设备实际，想手动设成本模型系数。
- **块设备 IO 观测**：不知道 IO 在哪盘、什么类型、设备忙不忙，先看 /proc/diskstats 与 io.<name>/stat。
- **保护文件系统防误写**：担心失控进程裸写已挂载的块设备损坏 fs，想关 bdev_allow_write_mounted。

不属于 io 模块的情况（建议转模块）：CPU 调度/负载均衡 → sched；内存/换页压力 → mm；网络协议栈 → net；纯文件系统调优（mount 选项、ext4 features、xfs knobs） → fs（不在本仓库范围）；CPU/内存 cgroup 限额 → cpu/memory 子系统；IO 调度器 per-device 选择（`/sys/block/<dev>/queue/scheduler`，非 boot cmdline elevator）走 sysfs 不在本模块工单覆盖范围。

## 我能观察什么（消费 readonly 做诊断）

- **cgroup 级 IO 统计**：`/sys/fs/cgroup/io.<name>/stat` → 每设备 `rbytes/wbytes/dbytes/rios/wios/dios`（+ iocost policy 行尾追加 vtime/busy 等延迟信息）。判断某 cgroup 在哪些盘上 IO 占多少、是否被 iocost 加 delay（`use_delay`/`delay_nsec`，需 `blkcg_debug_stats`）。
- **系统级块设备/分区统计**：`/proc/diskstats` → 每设备/分区的 `rio/wio/dio/flush`、`rmerge/wmerge`、`rsect/wsect`、`ruse/wuse`、`io_ticks`、`aveq`、`inflight`。看磁盘忙不忙、什么 IO 类型为主、有没有合并机会。
- **块设备/分区拓扑**：`/proc/partitions` → `major minor #blocks name` 列表。和 diskstats 的 major:minor 关联，先确认有哪些盘再读 diskstats。

注意：blkcg 还有一个 `reset_stats` 触发式 cftype（legacy_cftypes，dfl 不暴露），写它清零统计，但 scan 没扫到（cftype 提取器只取 dfl_cftypes）。诊断场景下读 stat 即可，一般不需重置。/proc/diskstats 与 io.<name>/stat 各是系统级/cgroup 级视图，组合看才能定位 IO 是哪个 cgroup 在哪盘上贡献的。

先读 readonly 定位问题在哪盘、哪个 cgroup、什么 IO 类型，再决定动哪个 tunable——盲调是反模式。

## 我的调优杠杆（消费 tunable 给建议）

- **cgroup IO 权重份额**：`weight`（iocost，1..10000，默认 100）。仅按权重比例分配，不是绝对配额。根 cgroup 不可写（CFTYPE_NOT_ON_ROOT）。
- **cgroup IO QoS（延迟 SLA）**：`cost.qos`（per-device QoS：`rlat/wlat` 延迟目标 us、`rpct/wpct` 比例、`min/max` vrate clamp、`enable`/`ctrl`）。只在 root io cgroup 可写（CFTYPE_ONLY_ON_ROOT），per-device 写法 `MAJ:MIN ...`。
- **cgroup IO 成本模型**：`cost.model`（per-device 线性模型系数：`rbps/wbps/rseqiops/rrandiops/...`，`ctrl=auto|user`、`model=linear`）。只在 root io cgroup 可写。
- **cgroup IO 优先级类**：`prio.class`（枚举：`no-change|promote-to-rt|restrict-to-be|idle|none-to-rt`，blk-ioprio 策略）。
- **块设备写已挂载开关**：`bdev_allow_write_mounted`（boot-only，默认 true=允许；改需 reboot）。
- **已废弃的 IO 调度器 cmdline**：`elevator`（boot-only，v7.2 已完全废弃，仅 pr_warn 不生效；改 IO 调度器用 `/sys/block/<dev>/queue/scheduler`）。

详细字段（默认值/range/方向字段 when_to_*/increasing/decreasing、boot_only/deprecated 标志）用 `python3 query.py --knowledge . --name <kernel_name>` 取，不要凭记忆。怎么用方向字段定调向见 [`CONSUMER.md`](../../../../CONSUMER.md) §3/§4。

## 边界与雷区（不该碰的）

- **iocost weight 是份额不是配额**：1..10000 是相对比例，不直接对应绝对带宽；同一组在不同设备上分到多少还看设备 vrate 与权重→hweight→vtime 的层级分配+donation 机制。
- **cost.qos/cost.model 是接口式不是单调数值**：写一串 token=value 而非一个数；`increasing`/`decreasing` 用"接口式配置"语义。两者只在 root io cgroup 可见（CFTYPE_ONLY_ON_ROOT），per-device 写法必带 `MAJ:MIN` 前缀。
- **prio.class 是枚举切换不是单调调参**：不是数值大小，5 个策略是离散选择；`idle` 会让该组 IO 仅在设备空闲时才处理，可能长期饥饿，慎用。
- **iocost VRATE 极限**：`min/max` clamp 的合法范围是 VRATE_MIN_PPM=10000(1%) 到 VRATE_MAX_PPM=100000000(10000%)，写超出会被 clamp；过紧的 `rlat/wlat` 可能顶到 max 反而抖动。
- **`elevator` 完全废弃**：v7.2 handler 只 pr_warn 不生效。要改 IO 调度器写 `/sys/block/<dev>/queue/scheduler`（不在本工单覆盖范围）。工单保留它仅作"消费者知道它没用"的标记。
- **`bdev_allow_write_mounted` 是 boot-only**：`__setup` 只在 boot 解析一次，改需 reboot（改 grub）。默认取 `CONFIG_BLK_DEV_WRITE_MOUNTED`（默认 y）。关闭后部分运维工具（fsck rw、ext4 在线特性修改）无法工作，且仅保护 block device 写打开、不挡 SCSI 直连命令或下层栈裸改。
- **iocost 与 ioprio 是两个独立控制器**：同一 cgroup 可同时受两者影响，调 weight 不影响 prio.class 的优先级调度。blk-ioprio 改 `bio->bi_ioprio` 在 bio 提交路径生效，需要 IO scheduler/设备支持 IOPRIO 才有效（mq-deadline、multiqueue 等）。
- **blkcg 的 `reset_stats` 不在 dfl_cftypes**：是 legacy_cftypes 的 `.write_u64`，且 handler 还会 `pr_info_once("blkio.%s is deprecated")`——本身已废弃。诊断场景读 stat 即可，不要依赖 reset_stats。

## 配套

> 以下命令在模块目录内跑（`--knowledge .` 指当前目录）；在仓库根跑则用全路径如 `--knowledge knowledge/v7.2-rc7/io`。
- 列模块所有参数摘要：`python3 query.py --knowledge . --list`
- 取某参数完整记录：`python3 query.py --knowledge . --name ioc_weight_write`
- 诊断流程与规则（智能/规则/混合三模式）：见同目录 `playbook.json`，用 `python3 query.py --knowledge . --playbook [--mode {intelligent|rule|hybrid}]` 取。
- 不懂的术语：`python3 query.py --vocab knowledge/vocab.json --name iocost`
