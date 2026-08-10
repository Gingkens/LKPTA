# irq — 中断子系统（v7.2-rc7）

> 一句话摘要：per-irq 中断亲和性（绑核）、NUMA 局部性、spurious 伪中断检测/恢复、全局默认亲和性、IRQ handler 时长告警都在这。

## 我适合什么场景

- **中断集中某核**：某 CPU 中断处理率畸高、其他核闲置，软中断 (`/proc/softirqs`) 或网卡中断堆积在某核。
- **NUMA 局部性差**：网卡/设备中断跨 NUMA 节点处理，中断处理路径跨节点访问描述符/数据，`effective_affinity` 与 `node` 不一致。
- **中断隔离**：实时/业务核不想收中断，要把 IRQ 收束到 housekeeping 核（与 `isolcpus`/`nohz_full` 配套）。
- **改 smp_affinity 不生效**：写了 `/proc/irq/<irqN>/smp_affinity` 但中断仍落在原核——需查 `effective_affinity` 是否被芯片/managed 约束限制。
- **中断风暴/伪中断**：`/proc/irq/<irqN>/spurious` 显示 count/unhandled 畸高、中断被内核自动禁用、系统卡死或硬件坏中断无人响应。
- **长中断 handler 拖延迟**：某 IRQ handler 跑太久影响实时性，想抓出来定位（`irqhandler.duration_warn_us=`）。
- **新中断默认散落**：新中断默认 affinity 太散/太集中，想调全局默认（`default_smp_affinity` 或 boot `irqaffinity=`）。

不属于 irq 的情况（建议转模块）：CPU 任务调度/负载不均/抢占 → sched；内存回收/NUMA balancing 扫描 → mm；块 IO 队列/合并 → block；网络协议栈 softirq 计数本身（`/proc/softirqs` 的 NET_RX）属 net 的协议栈侧，但 **NIC 中断绑核属本模块**——`/proc/interrupts` 的设备中断行 + `/proc/irq/<irqN>/` 子树是本模块主战场。

## 我能观察什么（消费 readonly 做诊断）

- **per-irq 实际生效亲和性**：`/proc/irq/<irqN>/effective_affinity` / `effective_affinity_list` → 中断真正投递的 CPU。与 `smp_affinity` 对比能判断写入是否被采纳（effective ⊂ smp_affinity 正常，过窄说明受芯片/managed 限制）。
- **per-irq 驱动亲和性建议**：`/proc/irq/<irqN>/affinity_hint` → 驱动（如多队列网卡）建议掩码。全 0 说明驱动未给建议；非空时应优先在 hint 范围内选核。
- **per-irq NUMA 节点**：`/proc/irq/<irqN>/node` → 描述符所在 NUMA 节点。配 `smp_affinity` 判断中断处理是否跨节点。
- **per-irq spurious 统计**：`/proc/irq/<irqN>/spurious` → `count`/`unhandled`/`last_unhandled_ms`。判该中断是否大量无人处理、是否被 spurious 检测自动禁用。
- **全局中断计数**：`/proc/interrupts` → 每中断 per-CPU 计数，定位热点中断行与热点 CPU（虽不在本模块工单，但与本模块紧密配合——`/proc/interrupts` 行的 IRQ 号即 `/proc/irq/<irqN>/` 子树入口）。

先读 readonly 定位问题在哪一层（亲和性？局部性？伪中断？），再决定动哪个 tunable——盲调是反模式。

## 我的调优杠杆（消费 tunable 给建议）

- **per-irq 中断亲和性**：`smp_affinity`（位图格式）/`smp_affinity_list`（cpulist 格式），同物异格式任选其一。把中断绑到指定 CPU 集合。改完读 `effective_affinity` 验证。
- **全局默认亲和性**：`/proc/irq/default_smp_affinity`（运行时改默认，影响后续新中断）；boot 期可 `irqaffinity=<cpulist>` 设 boot 默认（boot-only）。
- **中断风暴/伪中断恢复（boot-only cmdline）**：`noirqdebug`（关 spurious 检测，防关键中断被误禁）、`irqfixup`（启用 misrouted 修复，IRQ_NONE 时扫共享 handler）、`irqpoll`（启用 misrouted 修复 + 每 HZ/10 定时轮询被禁中断）。
- **IRQ handler 时长告警（boot-only）**：`irqhandler.duration_warn_us=<us>`（设阈值，超限 pr_warn_ratelimited 抓长中断）。

详细字段（默认值/range/方向字段 `when_to_*`/`increasing`/`decreasing`）用 `python3 query.py --knowledge . --name <kernel_name>` 取，不要凭记忆。怎么用方向字段定调向见 [`CONSUMER.md`](../../../../CONSUMER.md) §3/§4。族引用（如 smp_affinity 与 smp_affinity_list 同物异格式）见 playbook 的 `interactions`。

## 边界与雷区（不该碰的）

- `smp_affinity`/`smp_affinity_list` 是同一掩码两种格式，改任一另一个同步反映——别当成两个独立参数。写 `smp_affinity_list` 用 `0-3,8` 这种 cpulist 比 bitmap 易写易读。
- 不允许把中断完全禁掉：`smp_affinity` 写全离线/空集 → `default_affinity_write`/`write_irq_affinity` 返回 `-EINVAL`，对 per-irq 路径空集会回退到 `irq_select_affinity_usr` 或 `-EINVAL`。别用空掩码当"禁用中断"手段。
- 对 **AFFINITY_MANAGED** 中断（多队列网卡 managed-IRQ，`irqd_affinity_is_managed` 真）和 **PER_CPU** 中断写 `smp_affinity` 返回 `-EPERM`（`irq_can_set_affinity_usr` 假）——这些中断亲和性由内核/driver 管理，用户态不可改。写之前先试 `cat effective_affinity` 看是否可改。
- `default_smp_affinity` 改了只影响后续新中断的默认继承，**已存在中断不回退**——已绑核的中断不会因为你改默认而重新均衡。要重设现有中断得逐个写其 `smp_affinity`。
- `affinity_hint` 是驱动给的建议非约束，内核不强制按 hint 投递；hint 全 0 说明驱动没给建议，别当掩码用。
- `node` 是描述符分配所在的 NUMA 节点，**不一定**等于中断硬件来源节点——判硬件局部性应结合 `affinity_hint` 与 `effective_affinity`。
- `effective_affinity` 可能比 `smp_affinity` 窄很多（某些 PIC 只能投递单核）——这是硬件限制不是 bug，写 `smp_affinity` 改不了的部分就是这部分。`CONFIG_GENERIC_IRQ_EFFECTIVE_AFF_MASK` 关则 `effective_affinity*` 文件不存在。
- 5 个 cmdline 参数（`noirqdebug`/`irqfixup`/`irqpoll`/`irqaffinity`/`irqhandler.duration_warn_us`）都是 **boot-only**：`__setup` 注册，运行时不可改，要改需 reboot 改 grub。`noirqdebug`/`irqfixup`/`irqpoll` 在 **PREEMPT_RT** 内核不生效（`irqfixup`/`irqpoll` pr_warn 后忽略，`noirqdebug` 仍可设但 RT 语义不同）。
- `irqpoll` 开了每 HZ/10 全量扫被禁中断，开销显著（源码 pr_warn 提示 "may significantly impact system performance"）——只在确认有坏中断需轮询恢复时用，正常负载别开。
- `irqfixup`/`irqpoll` 是递进关系：`irqfixup`=1（IRQ_NONE 时扫共享 handler），`irqpoll`=2（额外对 IRQF_IRQPOLL handler 也扫 + 启动定时轮询）。开了 `irqpoll` 等于包含 `irqfixup`。
- `irqhandler.duration_warn_us=0` 被源码拒绝（pr_err + return 0），必须 > 0。
- 改 `smp_affinity` 后立即生效但中断真正迁移可能在下次中断投递时（managed/在途中断有 pending mask 缓冲）——改完等几个中断周期再读 `effective_affinity` 验证。

## 配套

> 以下命令在模块目录内跑（`--knowledge .` 指当前目录）；在仓库根跑则用全路径如 `--knowledge knowledge/v7.2-rc7/irq`。
- 列模块所有参数摘要：`python3 query.py --knowledge . --list`
- 取某参数完整记录：`python3 query.py --knowledge . --name irq_affinity_proc_ops`
- 诊断流程与规则（智能/规则/混合三模式）：见同目录 `playbook.json`，用 `python3 query.py --knowledge . --playbook [--mode {intelligent|rule|hybrid}]` 取。
- 不懂的术语：`python3 query.py --vocab knowledge/vocab.json --name spurious`

> v7.2-rc7 增量说明：本模块为首次生产（无 v7.1 对照基线）。参数层已 verify（missing=0/extra=0/drifted=0），13 项全产（8 tunable + 5 readonly）。per-irq 项 path 含 `<irqN>` 占位符标 `per_irq: true`，消费者给建议时需代入实际中断号（从 `/proc/interrupts` 取 IRQ 行号）。所有 cmdline 参数标 `boot_only: true`。
