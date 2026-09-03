# sched — 调度子系统（v7.2-rc7）

> 一句话摘要：CPU 任务的调度、抢占、负载均衡、cache-aware LLC 均衡、NUMA 均衡、RT 带宽、cgroup CPU 份额、uclamp/idle/psi 等调度类调优都在这。
> 给消费者选型用：如果你的性能问题落在"任务跑得慢/不均/延迟高/cache 局部性差/NUMA 跨节点/RT 饥饿/cgroup 抢占"，选 sched。

## 我适合什么场景

- **负载不均**：某些 CPU 打满而其他空闲，任务排队不均。
- **调度延迟**：延迟敏感任务响应慢、被抢占多、`wait` 时间长。
- **LLC 局部性差**：同进程多线程跨 LLC 域分散运行、cache 命中率低、跨 LLC 迁移频繁（v7.2 新增 cache-aware 调度可调）。
- **NUMA 跨节点**：多节点系统跨节点访问/迁移频繁、局部性差。
- **RT 饥饿/限流**：RT 任务占满配额导致 throttle，或非 RT 任务被 RT 饿死。
- **cgroup 份额不公**：多 cgroup 抢 CPU，某组响应差或抢占过多。
- **idle 选择不当**：polling idle 耗电、或深睡导致唤醒延迟高。

不属于 sched 的情况（建议转模块）：内存分配/换页压力 → mm；块 IO 调度/队列 → block；网络协议栈收发 → net；纯 cgroup memory/IO → 对应子系统。

## 我能观察什么（消费 readonly 做诊断）

- **调度域拓扑**：`/sys/kernel/debug/sched/domains/cpuN/domainN/{level,flags,name,groups_flags}` → 看 NUMA/SMT 层级、可用 CPU 范围、域类型（`flags` 含 `SD_NUMA` 即跨节点域）。需先开 `verbose` 才创建该子树。
- **cgroup CPU 计费**：`/sys/fs/cgroup/cpuacct.<name>/{usage,stat,usage_percpu,usage_all}` → 看各 cgroup 占 CPU 比例、user/sys 构成、热点 CPU。
- **调度统计（核心）**：`/proc/schedstat` → per-cpu + per-domain 的 `lb_count`/`lb_failed`/`lb_gained`/`alb_*`/`ttwu_*`/`rq_cpu_time`/`run_delay`，判断负载均衡成效、迁移热度、唤醒模式；`/proc/<pid>/schedstat` → 单任务的 `wait_sum`/`wait_max`/`run_sum`/`pcount`，定位"这个任务被亏待"。
- **运行队列全景**：`/sys/kernel/debug/sched/debug` → per-cpu rq 的 `nr_running`、per-cgroup cfs_rq 的队列深度与 load、PELT 平均值。诊断"某 CPU 是否拥塞"的依据。

注意：v7.2 的 cache-aware 均衡统计目前未在 debugfs 暴露专门只读文件，靠 `llc_balancing/` 下的可调值（`aggr_tolerance`/`epoch_period`/`imb_pct`/`overaggr_pct`）间接观察配置态；运行态判断需结合 `/proc/schedstat` 的 lb 统计与 `cpuacct/usage_percpu` 的热点分布。

先读 readonly 定位问题在哪一层，再决定动哪个 tunable——盲调是反模式。

## 我的调优杠杆（消费 tunable 给建议）

- **调度粒度/延迟权衡**：`base_slice_ns`（EEVDF 基础时间片）、sched_features 里的 `WAKEUP_PREEMPTION`/`RUN_TO_PARITY`/`PREEMPT_SHORT` 等 feature bit。
- **负载均衡/迁移**：`migration_cost_ns`（cache-hot 门槛，`-1`/`0` 是特殊语义不是数值）、`relax_domain_level`、域字段 `imbalance_pct`/`cache_nice_tries`/`busy_factor`、sched_features 的 `LB_MIN`/`CACHE_HOT_BUDDY` 等。
- **cache-aware LLC 均衡（v7.2 新增）**：`llc_balancing/enabled`（总开关）、`aggr_tolerance`（聚合激进度）、`epoch_period`/`epoch_affinity_timeout`（cache 亲和时效）、`overaggr_pct`/`imb_pct`（过载/不均衡阈值）。CONFIG_SCHED_CACHE 编译态存在（默认 y）。
- **NUMA 均衡**：`numa_balancing` 总开关 + `scan_delay_ms`/`scan_period_min_ms`/`scan_period_max_ms`/`scan_size_mb`/`hot_threshold_ms`。
- **RT 带宽**：`sched_rt_runtime_us` / `sched_rt_period_us`（配对用，v7.2 默认 runtime=period 不节流）、`rt_group_sched`（boot-only）、`RT_PUSH_IPI`（v7.2 非 PREEMPT_RT 默认关）。
- **cgroup CPU 限额/份额**：`cpu.max`（硬限）、`cpu.weight`（份额）、`cpu.uclamp.{min,max}`（util 钳位）。
- **uclamp / idle / psi / schedstats 等专项**：`uclamp.min/max`、`nohlt`/`hlt`、`psi=`、`sched_schedstats`。

详细字段（默认值/range/方向字段 when_to_*/increasing/decreasing）用 `query.py --knowledge . --name <kernel_name>` 取，不要凭记忆。怎么用方向字段定调向见 [`CONSUMER.md`](../../../../CONSUMER.md) §3/§4。

## 边界与雷区（不该碰的）

- `base_slice_ns` 有缩放（`tunable_scaling`，默认 LOG=1+ilog2(在线CPU,封顶8)），debugfs 看到的是缩放后值；改极大/极小都劣化，只在吞吐-延迟权衡里微调。
- `migration_cost_ns` 的 `-1`=全 cache-hot 禁迁移、`0`=全 cold 允许迁移，**不是数值大小**，别当数调。
- `rt_group_sched`、`sched_proxy_exec`、`noautogroup`、`psi`、`nohz_full`、`isolcpus`、`resched_latency_warn_ms` 是 **boot-only** cmdline，运行时不可改（要改需 reboot）。
- 开了 `sched_ext`（BPF 调度器）后，传统 sched 调参大多失效或语义改变——先确认没开 sched_ext。
- `isolcpus` 隔离的 CPU 与 cgroup `cpuset` 互斥：已被 isolcpus 排除的 CPU 不应再被 cpuset 分配给 cgroup。
- **RT 带宽 v7.2 变化**：`sched_rt_runtime_us` 默认已从 950000 改为 1000000（=period，不节流）。FIFO/RR 节流改由 fair_server/ext_server 承担，本参数现主要约束 DEADLINE 实体总带宽。若系统需给非 RT 留底，需手动调回 950000 或更低。
- `RT_PUSH_IPI` v7.2 默认仅 PREEMPT_RT 开；非 RT 内核默认关，多 CPU 抢锁风暴场景可手动开。
- cache-aware 调度（`llc_balancing/enabled`）默认开，与 `migration_cost_ns`/传统 LB 有交互：两者都影响迁移决策，调 cache-aware 时留意别和 migration_cost 同向过度调（可能互相抵消或叠加迁移量）。

## 用户空间工具（消费 playbook.userspace_tools 落地）

> 内核参数改的是"调度策略/配额/门槛"，但**任务最终落在哪个 CPU/NUMA 节点、用哪个调度类**还要用户空间工具配合。本节列sched 域常用工具，**详细字段（key_options/when_to_use/kernel_alternative/caveats）见 `playbook.json` 的 `userspace_tools` 节**——这里只给消费者选型速查。

- **绑核/拓扑观察**：`taskset`（单进程绑核）、`lscpu`/`lstopo`/`hwloc-ls`（看 NUMA→LLC→Core→PU 拓扑，决定 pin 策略）、`nproc`（可用核数，验证亲和生效）。
- **NUMA 内存策略**：`numactl`（`--cpunodebind`/`--physcpubind` 绑核、`--membind`/`--preferred`/`--interleave` 内存策略、`--balancing` 配合内核 NUMA balancing）。
- **调度类/优先级**：`chrt`（SCHED FIFO/RR/DEADLINE/BATCH/IDLE，6.12+ `-T` 给 OTHER/BATCH 自定义 slice）、`nice`/`renice`（nice 权重，renice 改运行中进程）。
- **cgroup 落地**：`systemd-run --property=`（现代推荐，CPUQuota=/CPUWeight=/AllowedCPUs=）、`cgcreate`/`cgset`/`cgexec`（libcgroup，通过 `-c scope` 部分适配 v2）、`lscgroup`（**v1 only，v2 不可用，改 `systemd-cgls` 或 `ls /sys/fs/cgroup/`**）。
- **cpuset v1 接口**：`cset`（shield/proc/set 三子命令，自动建 root/system/user 三 cpuset，v7.2 cgroup v2 unified 上需手动挂 legacy 或优先用 `systemd-run --property=AllowedCPUs=`）。

工具不替代 tunable：`kernel_alternative` 字段标明每个工具替代/互补哪个内核参数（kernel_name 引用），改 tunable 后用工具把决策落到任务。boot-only 参数（`isolcpus`/`nohz_full`/`rt_group_sched`）运行时改不了，靠 `taskset`/`cset`/`systemd-run AllowedCPUs=` 在运行时实现等价隔离。

## 配套

> 以下命令在模块目录内跑（`--knowledge .` 指当前目录）；在仓库根跑则用全路径如 `--knowledge knowledge/v7.2-rc7/sched`。
- 列模块所有参数摘要：`python3 query.py --knowledge . --list`
- 取某参数完整记录：`python3 query.py --knowledge . --name sysctl_sched_base_slice`
- 诊断流程与规则（智能/规则/混合三模式）：见同目录 `playbook.json`，用 `python3 query.py --knowledge . --playbook [--mode {intelligent|rule|hybrid}]` 取。
- 用户空间工具清单：同 `playbook.json` 的 `userspace_tools` 节，用 `python3 query.py --knowledge . --playbook` 取（含完整 key_options/when_to_use/kernel_alternative/caveats）。
- 不懂的术语：`python3 query.py --vocab knowledge/vocab.json --name EEVDF`
