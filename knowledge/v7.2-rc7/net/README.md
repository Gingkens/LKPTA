# net — 网络协议栈（v7.2-rc7）

> 一句话摘要：网络协议栈的收发缓冲/队列深度、NAPI/busy poll、TCP/UDP 内存与拥塞、IPv6 选项与多路径、cgroup net_cls 分类、内核 cmdline 网络参数都在这。

## 我适合什么场景

- **收包丢包/softirq 拥塞**：`/proc/net/softnet_stat` 第二列 dropped > 0 或 time_squeeze 频繁，NAPI backlog 撑不住。
- **softirq CPU 占用过高**：NET_RX/NET_TX 软中断单次占用太久，想调 `netdev_budget`/`netdev_budget_usecs`/`dev_weight` 配比。
- **TX qdisc 队列堆积**：发方向 qdisc 出队批量小、`qdisc_max_burst` 太低。
- **TCP 内存配额瓶颈**：大量 TCP 连接、`tcp_mem` pressure 频繁触发，吞吐被内存上限压。
- **UDP 高 PPS 内存压力**：`udp_mem` pressure 频发。
- **BPF JIT 安全/性能权衡**：JIT 加固、kallsyms 暴露、可执行内存上限。
- **RPS/RFS/flow limit 调优**：跨 CPU 流分发、单流暴增拖慢 softirq。
- **busy poll 低延迟**：socket 阻塞读延迟高，想用 NAPI busy poll 避免软中断。
- **IPv6 选项/flow label/multipath 策略**：HBH/DST 选项上限、flow label 反射、ECMP hash 字段。
- **cgroup 流分类（net_cls）**：按 cgroup 把 socket 划到 tc classid 做分流。
- **boot 时网络哈希表大小**：tcp/udp/tcpmhash 槽数、fb_tunnels fallback 策略、carrier 等待。

不属于 net 的情况（建议转模块）：纯调度延迟/抢占 → sched；内存回收/大页/OOM → mm；块 IO 队列 → block；IRQ 亲和性硬中断路由 → irq（net 的 RPS/RFS 是软中断层流分发，与硬中断 IRQ 亲和性互补，可联合调）。

## 我能观察什么（消费 readonly 做诊断）

- **`/proc/net/softnet_stat`**（per-CPU，15 列 hex）：核心收包诊断。第 2 列 dropped=Per-CPU backlog 满；第 3 列 time_squeeze=NAPI 因预算退出；第 10 列 received_rps=RPS 工作；第 11 列 flow_limit_count=单流暴增被限；第 13 列 cpu_id 定位行；第 14/15 列 input_qlen/process_qlen=backlog 深度。详见 `query.py --name softnet_seq_ops`。
- **`/proc/sys/net/core/netdev_rss_key`**：RSS 密钥 hex 串。用于核对 RSS 是否随机化、配合 ethtool -x 计算 flow→queue 映射。
- **`/proc/sys/net/ipv4/tcp_available_ulp`**：当前注册的 TCP ULP 列表（如 mptcp）。setsockopt(TCP_ULP) 失败时核对。

先读这些 readonly 定位问题在哪一层（softirq？backlog？flow limit？），再决定动哪个 tunable——盲调 dev_weight/netdev_budget 是反模式。

## 我的调优杠杆（消费 tunable 给建议）

- **收发缓冲/队列深度**：`dev_weight`（NAPI 配额基准）、`dev_weight_rx_bias`/`dev_weight_tx_bias`（rx/tx 倍率）、`netdev_max_backlog`（per-CPU backlog 上限）、`qdisc_max_burst`（TX 出队批量）、`netdev_budget`/`netdev_budget_usecs`（NAPI 预算与时间预算）、`max_skb_frags`（单 skb frag 上限）、`gro_normal_batch`（GRO 合并批）、`skb_defer_max`（per-CPU defer 队列）、`mem_pcpu_rsv`（per-cpu socket 内存预留）。
- **NAPI busy poll**：`busy_poll`（全局默认）、`busy_read`（per-socket 默认 sk_ll_usec）。需 `CONFIG_NET_RX_BUSY_POLL`。
- **RPS/RFS/flow limit**：`rps_sock_flow_entries`（RFS 全局表，需 `CONFIG_RPS`）、`flow_limit_cpu_bitmap`/`flow_limit_table_len`（单流限速，需 `CONFIG_NET_FLOW_LIMIT`）。
- **TCP 内存/孤儿**：`tcp_mem`（全局三值配额）、`tcp_max_orphans`（orphan 上限）。`tcp_low_latency` 已废弃（deprecated）。
- **UDP/FIB/peer**：`udp_mem`、`fib_sync_mem`（FIB trie 同步释放阈值）、`inet_peer_threshold`/`inet_peer_minttl`/`inet_peer_maxttl`（peer 缓存）。
- **BPF JIT**：`bpf_jit_enable`/`bpf_jit_harden`/`bpf_jit_kallsyms`/`bpf_jit_limit`（需 `CONFIG_BPF_JIT`）。
- **CIPSO（CONFIG_NETLABEL）**：`cipso_cache_enable`/`cipso_cache_bucket_size`/`cipso_rbm_optfmt`/`cipso_rbm_strictvalid`。
- **IPv6 选项/flow label/multipath**：`bindv6only`、`auto_flowlabels`、`flowlabel_reflect`、`flowlabel_consistency`、`flowlabel_state_ranges`、`max_dst_opts_number`/`max_hbh_opts_number`/`max_dst_opts_length`/`max_hbh_length`、`fib_multipath_hash_policy`/`fib_multipath_hash_fields`（需 `CONFIG_IP_ROUTE_MULTIPATH`）、`seg6_flowlabel`、`ioam6_id`/`ioam6_id_wide`、`anycast_src_echo_reply`、`fwmark_reflect`、`ip_nonlocal_bind`、`fib_notify_on_flag_change`、`idgen_retries`/`idgen_delay`。
- **netns 行为**：`fb_tunnels_only_for_init_net`、`devconf_inherit_init_net`。
- **杂项**：`netdev_tstamp_prequeue`（入队时间戳）、`message_cost`/`message_burst`（告警限速）、`warnings`（已废弃）、`high_order_alloc_disable`（大页 static_key）、`netdev_unregister_timeout_secs`、`default_qdisc`（需 `CONFIG_NET_SCHED`）。
- **cgroup net_cls**：`classid`（需 `CONFIG_CGROUP_NET_CLASSID`）。
- **boot-only cmdline**（运行时不可改，需 reboot）：`fb_tunnels`、`tcpmhash_entries`、`uhash_entries`、`thash_entries`、`carrier_timeout`。

详细字段（默认值/range/方向字段 when_to_*/increasing/decreasing）用 `query.py --knowledge . --name <kernel_name>` 取，不要凭记忆。怎么用方向字段定调向见 [`CONSUMER.md`](../../../../CONSUMER.md) §3/§4。

## 边界与雷区（不该碰的）

- `netdev_max_backlog` 调大只是掩盖拥塞信号；要根因解决应先看 NAPI 配额（`dev_weight`/`netdev_budget`）和网卡通道数。
- `skb_defer_max=0` 会触发 `static_branch_enable(skb_defer_disable_key)` 关闭整套 defer 优化，不是简单数值调小——生产慎用。
- `high_order_alloc_disable=1` 禁用 compound page（大页）优化，会显著增加 SKB truesize，仅调试用。
- `bpf_jit_enable=2` 会 `pr_warn` 警告勿生产用（导出 JIT 调试符号，地址泄漏风险）；`CONFIG_BPF_JIT_ALWAYS_ON` 时 min=max=1 永久锁定，写无效。
- `bpf_jit_harden`/`bpf_jit_kallsyms`/`bpf_jit_limit` 写入需 `CAP_SYS_ADMIN`。
- `tcp_low_latency` 和 `warnings` 是 **deprecated**：源码注释 obsolete/Unused，改了无效。消费者应过滤掉，别建议调。
- `fb_tunnels`/`tcpmhash_entries`/`uhash_entries`/`thash_entries`/`carrier_timeout` 是 **boot-only** cmdline：运行时不可改，需 reboot（改 grub）。`fb_tunnels` 只识别 `initns`/`none`，其他值忽略。
- `flow_limit_table_len` 写非 2 的幂会回滚 old 并返回 -EINVAL；改后只对新分配的 `sd_flow_limit` 生效，已存在的桶不变。
- `rps_sock_flow_entries` >`1<<29` 返回 -EINVAL；写 0 关闭 RFS；写时 vmalloc 重排并 static_branch 切换 rps_needed/rfs_needed。
- `dev_weight`/`dev_weight_rx_bias`/`dev_weight_tx_bias` 三者通过 `proc_do_dev_weight` 互斥锁联动：改任一个都会重算 `net_hotdata.dev_rx_weight`/`dev_tx_weight`。
- `default_qdisc` 改后只影响之后创建的 netdev，已存在网卡不改。
- `devconf_inherit_init_net` 是 4 值枚举（0~3），IPv4/IPv6 行为不同，别当布尔开关。
- `classid`（net_cls cgroup）写后会 `iterate_fd` 批量更新已建 socket 的 classid（每 1000 fd 释放一次 task_lock），大 fd 表写会有可见开销。
- `tcp_mem`/`udp_mem` 默认值运行时按 `nr_free_buffer_pages/16` 自动初始化（min 128 页），不同内存机器默认不同——别照抄示例值。

## 用户空间工具（消费 playbook.userspace_tools 落地）

> 内核参数改的是"缓冲深度/TCP mem 上限/NAPI busy poll/RPS 掩码"，但**改网卡队列数/Coalesce/Ring、看 socket 状况、设 qdisc、按 cgroup 分类流量**还要用户空间工具配合。详见 `playbook.json` 的 `userspace_tools` 节。

- **网卡硬件配置**：`ethtool -L`（channels combined/tx/rx 数）、`-C`（coalesce rx-usecs/adaptive-rx/tx-aggr-*）、`-G`（ring rx/tx 大小）、`-K`（offload tso/gso/gro）、`-x/-X`（rxfh indirection/重映射，对 RPS/RFS 流量分布核心）、`-S`（per-queue statistics，含 eth-mac/rmon groups）、`-k`（feature 状态）。
- **socket 观察**：`ss -t/-u/-x -a -m -i -p`（替代 netstat，skmem r/w/f/bl/d + cwnd/rtt/rto + 进程）、`netstat -r/-i/-s`（旧，man 自标 obsolete，但仍常用）、`ss --cgroup`（cgroup 归属）、`ss -N <netns>`（netns 切换）。
- **统计监控**：`nstat -z/-r/-a/-j`（内核 SNMP 计数 Tcp/IpExt/UDP 等，per-pattern wildcard）、`lnstat -k softnet_stat:cpus/-k nf_conntrack`（统一 /proc/net/stat/ 周期采样）、`sar -n SOFT`（softnet_stat per-cpu）、`sar -n DEV/EDEV`（接口 rxkB/s/txkB/s/rxerr/txdrop）、`sar -n TCP/ETCP/UDP/SOCK`（协议层统计）。
- **流量控制（tc）**：`tc qdisc add root <qdisc>`（mq/fq/codel/fq_codel/htb/tbf 等）、`tc class add`（classid 分层）、`tc filter add cgroup/bpf/flower/u32/fw`（按 cgroup/BPF/flow 分类）、`tc -s qdisc show`（统计）。
- **netns**：`ip netns add/exec/list/identify/pids`（容器/子 netns 操作，含 monitor）、`ip -n <netns> link set`（在 netns 内执行 ip 命令）。
- **网络配置**：`ip link set txqueuelen/mtu/xdp`、`ip route via+weight+congctl`（路由选路 + 拥塞控制）、`ip neigh`（邻居表）、`ip rule`（策略路由）、`ip -j -br link`（JSON 简表，脚本友好）。
- **内核参数读写**：`sysctl net.core.somaxconn=X`、`sysctl -w net.ipv4.tcp_rmem='4096 87380 6291456'`、`sysctl --system`、`sysctl -a | grep net.ipv4.tcp`（探查）。
- **cgroup v2 网络限额**：`systemd-run --property=IPAccounting=`（统计）、`--property=IPAddressAllow=/IPAddressDeny=`（IP 白/黑名单）、`--property=IPIngressFilterPath=/IPEgressFilterPath=`（BPF filter）、`--property=NetworkNamespacePath=`（指定 netns）、`--property=PrivateNetwork=`（私有 netns）。注意 `net_cls` cgroup v1 已 deprecated，v2 用 IPAccounting/IPAddress* 替代。

工具不替代 tunable：`kernel_alternative` 字段标明每个工具替代/互补哪个内核参数（kernel_name 引用，如 `net_hotdata.max_backlog`/`sysctl_tcp_mem`/`bpf_jit_enable`/`init_net.ipv6.sysctl.multipath_hash_policy`/`cipso_v4_cache_enabled` 等）。boot-only 的哈希表大小（`hashsize=`/`tcp_max_.*_buckets`/`fib_buckets`）运行时改不了，靠 `ip route show cache`/`ss -m` 观察。conntrack/ifstat 本机未装未读，需要时自行装（conntrack 属 conntrack-tools 包）。

## 配套

> 以下命令在模块目录内跑（`--knowledge .` 指当前目录）；在仓库根跑则用全路径如 `--knowledge knowledge/v7.2-rc7/net`。
- 列模块所有参数摘要：`python3 query.py --knowledge . --list`
- 取某参数完整记录：`python3 query.py --knowledge . --name net_hotdata.max_backlog`
- 诊断流程与规则（智能/规则/混合三模式）：见同目录 `playbook.json`，用 `python3 query.py --knowledge . --playbook [--mode {intelligent|rule|hybrid}]` 取。
- 用户空间工具清单：同 `playbook.json` 的 `userspace_tools` 节，用 `python3 query.py --knowledge . --playbook` 取（含完整 key_options/when_to_use/kernel_alternative/caveats）。
- 不懂的术语：`python3 query.py --vocab knowledge/vocab.json --name NAPI`
