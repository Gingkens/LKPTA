# 消费者指南：如何消费 knowledge

这份文档写给 **knowledge 消费者**（consumer agent）。读完你会知道怎么取用 knowledge 给出针对目标系统的调优建议。

消费者**不碰 recipe、不跑 scan.py、不读内核源码**——那些是生产者和维护者的事。你的输入是 `knowledge/` 下已经生产好的结构化 knowledge，用 `query.py` 按需查询。

## 1. 你的位置

```
knowledge (knowledge/<version>/<module>/{README, playbook, summary, tunable, readonly})
   │  你：先 --modules 选模块 → 读 README 确认 → --playbook 定诊断策略 → --name 取参数详情
   ▼
调优建议 (该调什么、怎么调、为什么)
```

## 2. 取用 knowledge

knowledge 按 **模块 × 版本** 存放：

```
knowledge/
└── <version>/            ← 先按 uname -r 定位版本
    └── <module>/         ← 再选子系统(如 sched)
        ├── README.md      ← 选型层(可选): 模块自我介绍, --modules 选模块时读
        ├── playbook.json  ← 流程层(可选): 诊断方法论+规则, 端到端分析时用
        ├── summary.json   ← 参数层: 模块级索引(每条 3 字段), 先扫这个决定看哪些参数
        ├── tunable.json   ← 参数层: 可调参数完整记录(调优主体)
        └── readonly.json  ← 参数层: 只读观测项(诊断依据)
```

消费者**不直接 cat 这些 JSON**，而是用 `query.py` 按需查询（见 §3.3）。版本在前、模块在后，符合"先 uname -r、再选模块"的顺序。

### 2.1 选版本

knowledge 带版本，必须按目标系统的内核版本取用，**不能跨版本套用**——同名参数在不同版本可能默认值、语义、甚至是否存在都不同。

取目标系统内核版本：
```bash
uname -r                       # 如 7.1.0-rc5
# 或从 /boot/ 目录、grub 配置看
```
然后取 `knowledge/<对应版本>/<module>/`。

**没有完全匹配的版本怎么办**：优先同 major.minor（`7.1.x` 之间最接近）；`-rc` 与正式版视为同基线（`7.1.0-rc5` 的 knowledge 可用于 `7.1.0`，但默认值需现场核对）；同 major.minor 都没有时**不要勉强套用跨 major 版本**，明确告知"无匹配 knowledge、需生产者补建"。任何不匹配都要在建议里标注"knowledge 版本与目标不一致，默认值/语义需现场核对"。

### 2.2 两类参数都要用

- `tunable`（`tunable.json`）：能调的参数，是建议的主体。
- `readonly`（`readonly.json`）：只读项，用来**诊断该不该调、调了有没有效**。例如 readonly 的 `domains/.../level`、`flags` 能告诉你拓扑结构，从而判断 `migration_cost_ns` 该往哪调。

只看 tunable 给建议是不够的——好的调优建议先用 readonly 项定位问题，再给 tunable 方案。

## 3. 字段含义

每条 knowledge 记录的字段（生产者按 [`PRODUCER.md`](./PRODUCER.md) 填写）：

| 字段 | 你怎么用 |
|---|---|
| `name` | 用户态名字，你给用户的命令里写这个路径。 |
| `kernel_name` | 稳定标识，跨版本追踪用；query.py 按它查。 |
| `path` | 完整路径（含 `cpuN`/`<pid>` 等占位符）。实际给建议时按目标系统的 CPU/进程代入。 |
| `category` | `tunable` → 给调参建议；`readonly` → 给诊断步骤。 |
| `gate` | **先看这个**。字符串（空串=无条件存在）或空格分隔的多个 `CONFIG_*`（全部启用该参数才编译进来，AND 关系）。如果目标系统没开这些 config（查 `/boot/config-*` 或 `zcat /proc/config.gz`），该参数不存在，别建议用户去调它。readonly 项的 gate 同样要先过，否则诊断无据。 |
| `unit` | 单位（ns/us/ms/MBps 等），默认值和给建议时要用。 |
| `default` | 默认值（数字）。比对当前值与默认值的差距是建议的核心。 |
| `range` | 合法范围，对象格式 `{"min":0,"max":N}`；有特殊语义值时 `{"min":0,"max":N,"special":{"0":"禁用判断","-1":"强制"}}`。给建议时不得超出 range。 |
| `summary` | 一句话理解参数是什么。summary.json 里也带这条，可先扫摘要。 |
| `when_to_increase`/`when_to_decrease` | **你的行动入口**（tunable 项）：什么**症状/目标**时该往这调（如"cache 命中率低"）。遇到症状直接在这里对号入座定方向，不用从效果反推。 |
| `increasing` | **你的核心依据之一**（tunable 项）：调大时的效果，`good_for`/`bad_for` 都是动词+方向（如"提升 cache 局部性""加剧负载不均"）。`good_for`=调大得到的好处，`bad_for`=调大**引发的真实代价**（非 good_for 反义词）。据此判断"该不该调大、代价能否接受"。 |
| `decreasing` | **你的核心依据之一**（tunable 项）：调小时的效果，同 increasing 的结构。据此判断"该不该调小"。 |
| `use` | readonly 项怎么用来诊断——读哪个值、怎么判读。 |
| `deprecated` | tunable 项若为 `true`，该参数在当前内核已废弃（改无效），**别建议用户调它**——`--list` 时可据此过滤。 |
| `boot_only` | tunable 项若为 `true`，该参数只能 boot cmdline 设置（运行时不可改）。给建议时提示"改 grub/reboot"而非 `echo`。 |
| `consumes` | 该变量在内核被哪里消费（可选，数组），帮你理解调参的影响面。 |
| `version_notes` | 该参数在本版本的特殊说明，留意。 |
| `per_cpu`/`per_domain` 等标注 | path 含占位符时出现，提醒你该参数是 per-cpu/per-domain，需遍历或抽样。 |

（生产者**不放 `file_symbol`/`source` 字段**——那是定位源码用的，消费者不需要；行号更是会跨版本漂移，故整个系统用符号名。）

### 3.1 记录样例

你需要先知道一条 knowledge 记录长什么样。以下是生产者产出的真实形态（字段定义见上表，完整 schema 见 [`PRODUCER.md`](./PRODUCER.md) §5）：

**tunable.json 的一条**（`migration_cost_ns`）：
```jsonc
{
  "name": "migration_cost_ns",
  "kernel_name": "sysctl_sched_migration_cost",
  "path": "/sys/kernel/debug/sched/migration_cost_ns",
  "category": "tunable",
  "gate": "",
  "unit": "ns",
  "default": 500000,
  "range": {"min": 0, "special": {"0": "全部视为 cache-cold,允许迁移", "-1": "全部视为 cache-hot,禁用基于热度的迁移"}},
  "summary": "cache-cold 迁移成本阈值。低于该值的跨CPU迁移被认为亏cache，调度器倾向不迁。",
  "when_to_increase": ["cache 命中率低", "热点任务被频繁跨 CPU 迁走"],
  "when_to_decrease": ["负载不均某 CPU 闲置", "跨 CPU 利用率差需更激进迁移"],
  "increasing": { "good_for": ["提升 cache 局部性", "减少跨 CPU 迁移"], "bad_for": ["加剧负载不均", "降低跨 CPU 利用率均衡"] },
  "decreasing": { "good_for": ["促进负载均衡", "提升跨 CPU 利用率均衡"], "bad_for": ["增加 cache miss", "增加迁移开销"] },
  "consumes": ["kernel/sched/core.c:8996"]
}
```

**readonly.json 的一条**（`level`）：
```jsonc
{
  "name": "level",
  "kernel_name": "sd->level",
  "path": "/sys/kernel/debug/sched/domains/cpuN/domainN/level",
  "category": "readonly",
  "gate": "",
  "summary": "该调度域在拓扑层级中的深度。level 从 CPU 向上递增，0=最底层最小域，越往上越接近 NUMA 根域。",
  "use": "读值=域深度；配合同目录 flags（含 SD_NUMA 则是 NUMA 域）判读跨域迁移是否频繁，作为 migration_cost_ns 调参依据。",
  "per_cpu": true,
  "per_domain": true
}
```

### 3.2 采集手段

字段表告诉你"该读什么"，这一节告诉你"怎么读"。消费者不读源码，但要读目标系统的运行时值。最小命令集：

```bash
# 1) 目标系统 config（查 gate）
zcat /proc/config.gz | grep CONFIG_NUMA_BALANCING
# 或: grep CONFIG_NUMA_BALANCING /boot/config-$(uname -r)

# 2) debugfs 参数当前值（debugfs 须已挂载在 /sys/kernel/debug）
cat /sys/kernel/debug/sched/migration_cost_ns

# 3) sysctl 参数当前值
sysctl kernel.sched_rt_runtime_us
# 或 cat /proc/sys/kernel/sched_rt_runtime_us

# 4) per-cpu / per-domain 的占位符遍历
for cpu in $(seq 0 $(nproc)); do
  for d in /sys/kernel/debug/sched/domains/cpu$cpu/domain*; do
    [ -d "$d" ] || continue
    echo "== $d =="
    cat "$d/level" "$d/flags" 2>/dev/null
  done
done
```

**占位符处理**：`path` 里的 `cpuN`/`domainN`/`<pid>`/`<name>` 要代入实际值。per-cpu 项遍历所有 CPU；per-domain 项遍历该 CPU 下所有 domainN；per-pid 项选代表进程（如目标负载的 PID）；per-cgroup 项取目标 cgroup 路径。不必全量遍历时，抽代表性 CPU（CPU0 + 一个非 0 CPU）即可。

**读不到值时**：若 debugfs 未挂载，提示用户 `mount -t debugfs none /sys/kernel/debug`（需 root）；若路径不存在，多半是 `gate` 的 CONFIG 未开，回到 §3 gate 核对。

### 3.3 用 query.py 查询（不要直接 cat JSON）

消费者用 `query.py` 按需提取参数，**不要直接 cat tunable.json/readonly.json 全量读**（效率低）。query.py 默认只读 summary.json（快），指定 `--name` 或加 `--detail` 时才展开读完整记录。

```bash
# 列出模块所有参数的摘要（只读 summary.json，快）
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --list

# 按名字取某个参数的完整记录（自动展开 detail）
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --name sysctl_sched_base_slice

# 按 category 过滤（tunable/readonly）
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --category readonly

# 按 path 部分匹配
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --path migration_cost

# 按 keyword 搜 summary（快路径）
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --keyword numa --list

# 列某类全部并取完整记录
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --category tunable --detail
```

`--knowledge` 指向 `knowledge/<version>/<module>/` 目录。先 `--list` 扫摘要，决定要看哪些参数，再 `--name` 取完整记录——这是高效消费的节奏。

### 3.4 选模块与诊断流程（README + playbook）

参数查询（§3.3）是"已知查哪个参数"时用的。还有两类查询面向"分析"而非"取参数"：

**选模块（不知道该看哪个子系统）**：
```bash
# 列出该版本所有模块 + 各自的一句话自我介绍（从模块 README 提取）
python3 query.py --knowledge knowledge/v7.1.0-rc5 --modules
```
输出每个模块的 `intro`（一句话摘要）、`scenarios`（适合的场景关键词）、`excludes`（不属于本模块→该转哪个模块）、`has_playbook`、`item_count`。**凭 profiling 症状在 `scenarios` 里对号入座**——命中某模块的 scenario 就选它;若症状更像别的，看 `excludes` 指引转模块。边界场景(如 NUMA/cgroup 既属 sched 又属 mm)按 `excludes` 的指向区分。选定时再读该模块 `README.md` 的完整自我介绍确认。README 是**选型层**。

**诊断流程（选定模块后，端到端分析）**：
```bash
# 取该模块的整个 playbook（流程层：诊断方法论+规则+判据）
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --playbook

# 只取某模式需要的原料：
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --playbook --mode rule         # 规则模式：rules+thresholds
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --playbook --mode intelligent # 智能模式：capabilities+maps
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --playbook --mode hybrid       # 混合：全部

# 只取单个能力（如负载不均诊断）
python3 query.py --knowledge knowledge/v7.1.0-rc5/sched --playbook --capability load-imbalance
```

**三模式怎么选**（playbook 的 `modes` 字段有各模式的 description）：
- **intelligent（智能）**：你（消费者 LLM）把 sched 当专家，靠 `capabilities` 的 `reasoning` 推理下一步读什么/判什么/动哪个参数。profiling 是任意格式（perf top/sched、火焰图、pidstat…），你自己描述好，playbook 提供专家方法论让你自由推理。适合复杂/非典型问题。
- **rule（规则）**：按 `rules` 逐条匹配 `trigger`（symptom+keywords），命中就执行 `steps`(read→judge→recommend)。可复现可审计，适合典型已知问题。无命中则报告无规则匹配。
- **hybrid（混合）**：规则先试，命中用规则结论但用 capabilities 补充"规则可能漏的角度"；无命中转 intelligent。

**关键认知**：query.py 的 `--mode` 只决定**展示哪部分 playbook 原料**，真正"用智能推理还是规则匹配"是**你自己（消费者 agent）的行为**——playbook 供弹药不强制走哪条路。三种模式你按 profiling 数据的复杂度自选。

`playbook.json` 是**可选**产物（老模块可能没有），无则 `--playbook` 报"no playbook.json"，退化为只用 §3.3 的参数查询 + CONSUMER.md 的通用工作流。

**遇到不懂的术语**：knowledge 的 summary/tuning/use 字段里可能出现内核行话（EEVDF、cache-cold、PELT、SD_NUMA、TTWU…）。用共享词汇表查：

```bash
# 查某个术语的确切含义 + 消费者需要知道什么
python3 query.py --vocab knowledge/vocab.json --name EEVDF

# 按关键字搜词表
python3 query.py --vocab knowledge/vocab.json --keyword cache
```

`knowledge/vocab.json` 是跨模块共享的领域词汇表（schema 字段名如 category/path 不在此，见 §3 字段表）。消费者读到不懂的术语就查它，不用猜，也别要求用户解释。

## 4. 给建议的工作流

1. **定版本**：取目标系统内核版本，定位 `knowledge/<对应版本>/`。版本不匹配要标注风险（见 §2.1）。读各参数的 `version_notes` 留意本版本特殊说明。

2. **选模块**（新增）：用 `query.py --knowledge knowledge/<版本> --modules` 列出该版本所有模块的 `intro`+`scenarios`+`excludes`。**凭 profiling 症状在 `scenarios` 里对号入座**选模块——命中某模块的场景关键词就选它;边界场景(如 NUMA 既属 sched 又属 mm)看 `excludes` 的"不属于本模块→转 X"区分。选定后读该模块 `README.md` 的完整自我介绍确认选对了。模块路径 = `knowledge/<对应版本>/<module>/`。

3. **定诊断策略**（新增）：若模块带 `playbook.json`（`has_playbook=true`），用 `query.py --playbook` 取诊断方法论。按 profiling 数据复杂度选三模式之一（intelligent/rule/hybrid，见 §3.4）：典型已知问题走 rule；复杂非典型走 intelligent 让你自由推理；不确定走 hybrid。无 playbook 则跳到第 4 步用通用流程。这一步你要决定"该读哪些 readonly、用什么判据、考虑哪些 tunable"——playbook 的 capabilities/observation_map/thresholds/leverage_map 是你的弹药。

4. **查 gate**：读目标系统 config（`zcat /proc/config.gz` 或 `/boot/config-$(uname -r)`），过滤掉 gate 不满足的参数——这些参数在目标系统根本不存在，建议了也是误导。tunable 和 readonly 都要过 gate。

5. **读现状**：按第 3 步定的策略（playbook 的 `observation_map`/rules 的 `read` 步骤）读出相关 readonly 项的观测值 + 相关参数当前值。用 §3.2 的命令集。比如调 `migration_cost_ns` 前，先看 `domains/.../level` 和 `flags` 了解拓扑，看 `/proc/schedstat` 的 `lb_*` 了解均衡成效，看 `cpuacct.stat` 了解各 CPU 负载。**用 thresholds 判异常**：lb_failed/lb_count > 0.25 → 迁移阻力大，等。

6. **给建议**：先按 profiling 症状在候选参数的 `when_to_increase`/`when_to_decrease` 里对号入座定方向（症状命中 `when_to_increase`→建议调大），再用 `increasing`/`decreasing` 的 `good_for`/`bad_for` + 现状 + playbook 的 `leverage_map`/`interactions`，给出"调哪个、改成多少、为什么、预期效果、代价"。每条建议至少包含：
   - 参数路径（占位符代入实际值）
   - 当前值 → 建议值
   - 方向依据（症状命中 `when_to_*`；效果用 `good_for`；代价用 `bad_for`——注意 bad_for 是该方向真实代价不是反义词）
   - 现状依据（readonly 诊断 + thresholds 判据）
   - 风险/代价（对应 bad_for；涉及 interactions 时注明联动/互斥）
   - 回滚方式（通常记下原值即可，或写默认值）

7. **标注版本**：建议里注明所用 knowledge 版本，若与目标系统不一致要提示用户现场核对默认值/语义。

> 工作流的第 2、3 步是"分析"环节（选模块 + 定诊断策略），第 4~6 步是"取用"环节（§3.2/§3.3 的参数查询）。前两步靠 README+playbook，后几步靠 tunable/readonly+命令集。profiling 数据是任意格式（perf top/sched、火焰图、pidstat…），你在第 3 步自己描述好它的含义，playbook 据此驱动分析。

## 5. 建议输出示例

目标系统：7.1.0-rc5，NUMA 多节点，负载不均。

先用 query.py 取 `migration_cost_ns` 的完整记录，并用 readonly 诊断拓扑：

```
诊断：
  /sys/kernel/debug/sched/domains/cpu0/domain0/level = 0 (NUMA 顶层域)
  /sys/kernel/debug/sched/domains/cpu0/domain0/flags 含 SD_NUMA
  → 跨 NUMA 节点迁移频繁，但 cache miss 高。

建议：
  参数: /sys/kernel/debug/sched/migration_cost_ns
  当前: 500000 (默认, 0.5ms)
  建议调小到: 100000 (0.1ms)
  方向依据: 症状"负载不均某 CPU 闲置"命中 when_to_decrease → 调小;
            效果 decreasing.good_for=促进负载均衡/提升跨 CPU 利用率均衡, 正对症;
            代价 decreasing.bad_for=增加 cache miss/增加迁移开销, 短暂可接受。
  现状依据: /proc/schedstat lb_failed/lb_count 偏高 + 拓扑跨 NUMA 域, 确认迁移阻力大、负载不均。
  回滚: echo 500000 > .../migration_cost_ns

注意: 所用 knowledge 版本 v7.1.0-rc5 与目标一致, 默认值已核对。
```

### 5.1 写入权限与持久化

给建议涉及让用户写参数时：

- **权限**：debugfs/sysfs/proc 的写入通常需 root，建议命令前提示 `sudo` 或"需 root"。
- **持久化**：debugfs/sysfs 的 `echo > ...` 改动**重启失效**。若用户要持久化，sysctl 类参数走 `/etc/sysctl.conf` 或 `/etc/sysctl.d/*.conf`（`kernel.sched_xxx = value`，`sysctl -p` 生效）；debugfs 类参数目前无标准持久化机制，建议用 systemd unit 或 rc.local 在启动时写回。建议里要告诉用户"这是运行时改动，重启失效，需 X 方式持久化"。

## 6. 自检清单

给建议前核对：

- [ ] knowledge 版本与目标系统内核版本匹配，或已标注不一致风险。
- [ ] gate 列出的 CONFIG 在目标系统已启用（否则参数不存在，删掉该建议）；readonly 项也过了 gate。
- [ ] readonly 项先读了，建议有诊断依据，不是盲调。
- [ ] 每条建议含：路径、当前值→建议值、依据、代价、回滚。
- [ ] path 占位符已按目标系统代入实际 CPU/进程。
- [ ] 涉及写入的建议注明了 root 权限需求与持久化方式。

## 7. 其他

- **有哪些 module 可消费**：`ls knowledge/`。每个 module 名下一个或多个版本目录。
- **当前值非默认值时**：用户可能已调过该参数。读到的当前值若与 `default` 不同，建议里要据此调整（比如用户已调过且更优，应提示"无需改"）。
