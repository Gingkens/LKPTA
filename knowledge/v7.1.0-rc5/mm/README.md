# mm — 内存管理子系统（v7.1.0-rc5）

> 一句话摘要：内存分配/回收/碎片化、THP/HugeTLB 大页、脏页回写、OOM 策略、overcommit、cgroup 内存配额、NUMA 局部性等内存子层调优都在这。
> 给消费者选型用：如果你的性能问题落在"内存回收抖动/碎片化致高阶分配失败/大页用不上/脏页回写卡顿/OOM 杀错进程/cgroup 内存限额/NUMA 跨节点访问"，选 mm。

## 我适合什么场景

- **回收压力/抖动**：kswapd 频繁唤醒、swap in/out 高、anon 被 swap 出、回收效率低（pgsteal/pgscan 比例差）。
- **碎片化**：高阶分配（order-9+）失败、`/proc/buddyinfo` 高 order 全 0、紧缩 stall、外部碎片严重。
- **大页未生效**：THP 未能合并、hugepage 池不足、vmemmap 优化未开、大页应用 OOM。
- **脏页回写卡顿**：写者被 throttle、回写周期不合理、脏页堆积致 OOM 风险。
- **OOM 行为不当**：杀错进程、panic 还是 kill 的策略、reserve 不足致无法登录修复。
- **cgroup 内存配额**：组触限、组 OOM、组内 swap/zswap 限额、组间公平。
- **NUMA 局部性差**：内存跨 node 分布、node_reclaim 未生效、远端访问多。

不属于 mm 的情况（建议转模块）：CPU 调度/负载不均/抢占 → sched；块 IO 调度/队列/IO 合并 → block；网络协议栈收发/NIC → net；cgroup CPU 份额 → sched（cgroup memory/swap 在这，cgroup cpu 在 sched）。

## 我能观察什么（消费 readonly 做诊断）

- **zone 水位与碎片**：`/proc/zoneinfo`（zone 的 min/low/high 实际值，判断 min_free_kbytes/watermark_scale_factor/boost_factor 调对了没）、`/proc/buddyinfo`（各 order 空闲块数，高 order 全 0 = 外部碎片严重）、`/proc/pagetypeinfo`（movable 类型是否被 unmovable 污染，判断紧缩能否凑出高阶页）。
- **回收成效**：`/proc/vmstat`（pgsteal_kswapd/pgscan_kswapd 比例看回收效率、compact_stall 看紧缩 stall、pgmajfault 看 IO 压力、nr_* 看各类页存量）。
- **进程内存画像**：`/proc/<pid>/smaps`（每 VMA 的 RSS/PSS/Anonymous/Swap/Referenced）、`/proc/<pid>/smaps_rollup`（进程总量汇总）、`/proc/<pid>/numa_maps`（每 VMA 跨 node 程度，判 NUMA 局部性）、`/proc/<pid>/maps`（VMA 数量，配 max_map_count）、`/proc/<pid>/oom_score`（OOM 时被杀候选）。
- **cgroup 内存账本**：`memory.current`（当前用量）、`memory.stat`（anon/file/slab 构成 + pgfault/pgmajfault）、`memory.events`/`events.local`（high/max/oom 触发计数，local 定位触限来源在后代还是自身）、`memory.numa_stat`（组内内存跨 node 比例）、`swap.current`/`swap.events`/`zswap.current`（swap 与 zswap 用量/触发）。

先读 readonly 定位问题在哪个子层（回收？碎片？大页？回写？配额？），再决定动哪个 tunable——盲调是反模式。

## 我的调优杠杆（消费 tunable 给建议）

- **回收力度/倾向**：`swappiness`（0-200，anon vs file 回收倾向）、`zone_reclaim_mode`（NUMA 本地回收位图）、`overcommit_memory`/`overcommit_ratio`/`overcommit_kbytes`（CommitLimit 策略）。
- **水位线**：`min_free_kbytes`（zone 最小空闲基准）、`watermark_scale_factor`/`watermark_boost_factor`（low/boost 间距）、`lowmem_reserve_ratio`（低 zone 保留）。
- **碎片/紧缩**：`defrag_mode`（分配路径反碎片）、`compact_memory`（触发全紧缩）、`compaction_proactiveness`（kcompactd 激进度）、`extfrag_threshold`（costly 分配判紧缩门槛）、`compact_unevictable_allowed`（紧缩是否动 unevictable）。
- **大页**：`transparent_hugepage`/`thp_anon`/`thp_shmem`（THP cmdline 策略）、`nr_hugepages`/`nr_overcommit_hugepages`（HugeTLB 池）、`hugetlb_optimize_vmemmap`（vmemmap 优化）、`hugepage_alloc_threads`（boot 分配并行度）。
- **脏页回写**：`dirty_ratio`/`dirty_bytes`（写者 throttle 门槛）、`dirty_background_ratio`/`dirty_background_bytes`（后台回写门槛）、`dirty_writeback_centisecs`/`dirty_expire_centisecs`（回写周期/过期）。
- **OOM 策略**：`panic_on_oom`/`oom_kill_allocating_task`/`oom_dump_tasks`、`user_reserve_kbytes`/`admin_reserve_kbytes`（OOM 预留）。
- **cgroup 内存配额**：`memory.min`/`memory.low`（软下限，回收保护）、`memory.high`（软上限，超则同步回收）、`memory.max`（硬上限，超则 OOM）、`memory.oom.group`（整组杀）、`memory.reclaim`（触发主动回收）；swap 限额 `swap.high`/`swap.max`；zswap 限额 `zswap.max`/`zswap.writeback`。
- **NUMA**：`numa_zonelist_order`/`min_unmapped_ratio`/`min_slab_ratio`、`numa_balancing`（cmdline，与 sched 共享）、`numa_stat`（开关统计）。

详细字段（默认值/range/方向字段 increasing/decreasing）用 `query.py --knowledge . --name <kernel_name>` 取，不要凭记忆。怎么用方向字段定调向见 [`CONSUMER.md`](../../../../CONSUMER.md) §3/§4。族引用（如 dirty_ratio 与 dirty_bytes 互斥、overcommit_ratio 与 overcommit_kbytes 互斥）见 playbook 的 `interactions`。

## 边界与雷区（不该碰的）

- `dirty_ratio` 与 `dirty_bytes` 互斥：写一个清零另一个；同理 `dirty_background_ratio`/`dirty_background_bytes`、`overcommit_ratio`/`overcommit_kbytes`。别同时设。
- `swappiness=0` 不代表完全不 swap（仍有 anon/file 之外的路径），且 0 在某些场景反而劣化；>100 在 v7.1 允许（倾向 swap anon 保 file cache），但 swap IO 代价大。
- `min_free_kbytes` 设太大会浪费内存、太小则突发分配易 OOM；它驱动 min/low/high 水位线，改它会重算所有 zone 水位。
- `compact_memory`（写 1 触发全紧缩）是同步阻塞操作，节点多时耗时长，别在承载业务的瞬间触发。
- `transparent_hugepage`/`thp_anon`/`thp_shmem`/`numa_balancing`/`memhp_default_state`/`reserve_mem`/`slab_nomerge`/`norandmaps` 是 **boot-only** cmdline，运行时不可改（THP 运行时有 sysctl/debugfs 路径可改，但 cmdline 是 boot 默认）。
- `nr_overcommit_hugepages` 超额池有 OOM 风险，运行时会收缩，别当持久池用。
- `memory.high` 超限是同步回收（卡写者），`memory.max` 超限是 OOM——别把 high 当 max 用。
- cgroup 配额的 `min`/`low` 是软保护（回收时尽量不碰，但父组紧张时仍会被回收），不是硬保留。
- `laptop_mode`、`swapaccount` 在 v7.1.0-rc5 已废弃（handler 仅 pr_warn），改无效——收录只为让消费者知道别再调。
- `randomize_va_space` 在 `/proc/sys/kernel/`（不在 `/proc/sys/vm/`），属 mm 但注册在 kernel 命名空间。

## 配套

> 以下命令在模块目录内跑（`--knowledge .` 指当前目录）；在仓库根跑则用全路径如 `--knowledge knowledge/v7.2-rc7/sched`。
- 列模块所有参数摘要：`python3 query.py --knowledge . --list`
- 取某参数完整记录：`python3 query.py --knowledge . --name vm_swappiness`
- 诊断流程与规则（智能/规则/混合三模式）：见同目录 `playbook.json`，用 `python3 query.py --knowledge . --playbook [--mode {intelligent|rule|hybrid}]` 取。
- 不懂的术语：`python3 query.py --vocab knowledge/vocab.json --name THP`
