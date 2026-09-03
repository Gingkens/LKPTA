# 生产者指南：如何生产 knowledge

这份文档写给 **knowledge 生产者**（producer agent）。读完你会知道怎么把一份 recipe 最终变成结构化的 `summary.json` / `tunable.json` / `readonly.json`，以及怎么处理内核版本差异。

## 1. 你的工作流

```
recipe (scan_recipes/<version>/<module>.json)
   │  scan_recipes/scan.py --recipe ... --ksrc ...
   ▼
参数工单 (扁平表格, 一行一参数, 7 列)
   │  按工单的 file_symbol 去内核源码读懂每个参数
   ▼
参数层 knowledge (knowledge/<version>/<module>/{summary,tunable,readonly}.json)   ← 必产, verify 通过
   │  参数层扎实后, 补选型层 + 流程层 (见 §7)
   ▼
README.md (选型层) + playbook.json (流程层)                                          ← 必产,见 §7.3
```

scan.py 的具体用法、工单每一列的含义，见 [`scan_recipes/README.md`](./scan_recipes/README.md)。本文档专注于**如何把工单加工成 knowledge**。

### 1.1 端到端示范

一条命令，产出长这样（sched 模块，前 5 行）：

```bash
$ python3 scan_recipes/scan.py --recipe scan_recipes/v7.2-rc7/sched.json --ksrc /home/lgk/linux
id	kernel_name	userspace_name	control_entry	file_symbol	type	gate
1	sched_feat_fops	features	/sys/kernel/debug/sched/features	kernel/sched/debug.c:sched_init_debug	tunable	(空)
2	sched_debug_verbose	verbose	/sys/kernel/debug/sched/verbose	kernel/sched/debug.c:sched_init_debug	tunable	(空)
3	sched_dynamic_fops	preempt	/sys/kernel/debug/sched/preempt	kernel/sched/debug.c:sched_init_debug	tunable	(空)
4	sysctl_sched_base_slice	base_slice_ns	/sys/kernel/debug/sched/base_slice_ns	kernel/sched/debug.c:sched_init_debug	tunable	(空)
5	sysctl_resched_latency_warn_ms	latency_warn_ms	/sys/kernel/debug/sched/latency_warn_ms	kernel/sched/debug.c:sched_init_debug	tunable	(空)
...（共 100 行）
[sched] 100 rows, by_type={'tunable': 86, 'readonly': 14}
```

（实际输出里 `gate` 为空的列是末尾的空 tab，这里用 `(空)` 标注便于阅读。）

其中第 4 行 `base_slice_ns`，加工成 knowledge 的一条：

```jsonc
{
  "name": "base_slice_ns",
  "kernel_name": "sysctl_sched_base_slice",
  "path": "/sys/kernel/debug/sched/base_slice_ns",
  "category": "tunable",
  "gate": "",
  "unit": "ns",
  "default": 700000,
  "range": {"min": 0},
  "summary": "EEVDF 调度的基础时间片，任务每次被授予的运行时长基准。",
  "when_to_increase": ["高负载吞吐瓶颈", "上下文切换开销占比高"],
  "when_to_decrease": ["调度延迟敏感任务响应慢", "交互响应差"],
  "increasing": { "good_for": ["延长单次运行", "减少抢占和上下文切换", "提升吞吐"], "bad_for": ["加剧调度延迟", "恶化交互/低延迟响应"] },
  "decreasing": { "good_for": ["缩短调度延迟", "提升交互响应"], "bad_for": ["增加上下文切换开销", "降低吞吐"] }
}
```

## 2. 从工单到 knowledge：逐行加工

**覆盖要求（重要）**：工单的**每一行都要产**，不能"挑代表性参数抽样"。同一类下的多个参数不是"同类样本"——每个 sched_feat、每个 cgroup cftype、每个 sysctl 条目都是**独立可调参数**，消费者要的是全集，不是示例。例如 sched 有 27 个 SCHED_FEAT、cpu_files 有 7 项、cpuacct 有 8 项，缺一个消费者就漏一个可调点。无法读出语义的行也要标注"未确认"收录，不要跳过。完整性别靠 `verify.py` 对照工单检查（见 §6.1）。

工单是 TSV（制表符分隔），第一行表头、其后每行一个参数。完整字段定义见 scan_recipes/README 的"输出列"。一行就是 7 个值：

```
id	kernel_name	userspace_name	control_entry	file_symbol	type	gate
8	sysctl_sched_migration_cost	migration_cost_ns	/sys/kernel/debug/sched/migration_cost_ns	kernel/sched/debug.c:sched_init_debug	tunable	(空)
```

`gate` 列为空表示该参数无条件存在；有值（如 `CONFIG_NUMA_BALANCING`）表示依赖对应 config。`kernel_name` 是你去源码 grep 的锚点，`file_symbol` 是该参数所在函数/数组名（打开源码用），两者都跨版本稳定，含义随 mechanism 不同（详见 scan_recipes/README 的列说明）。

对每一行，按 `file_symbol` 打开源码（定位到该函数/数组，用 grep 找 `kernel_name`），读懂以下信息（工单只给位置和元数据，语义要你从源码读出来）：

| 要读出来什么 | 怎么读 | 用到的工单列 |
|---|---|---|
| **含义** | 打开 `file_symbol` 指向的函数/数组，读参数创建语句 + 周围注释 + 绑定变量/回调的实现。`kernel_name` 是 grep 锚点。 | `file_symbol` + `kernel_name` |
| **单位** | 从变量名后缀（`_ns`/`_us`/`_ms`/`_MBps`）和初始化值推断，必要时看读写回调。 | `kernel_name` |
| **默认值** | grep `kernel_name` 的初始化赋值（如 `= 500000UL`）。注意默认值可能依赖 `CONFIG_*` 或定义在别的文件（如 sysctl 变量常定义在对应 class 的 .c 里，不在 debug.c）。 | `kernel_name` |
| **合法范围** | sysctl：到 `file_symbol` 指向的 ctl_table 数组里读该条目的 `.extra1`(min)/`.extra2`(max)（工单不含这两个，需去源码读）；debugfs/sysfs：看写回调里的校验逻辑；没有显式约束就读代码语义推断。 | `file_symbol` |
| **调参效果与代价** | 读该变量被消费的地方（grep 全仓），看调大/调小分别影响哪段逻辑。 | `kernel_name` |
| **是否版本敏感** | 见下文第 4 节。 | — |

**示例**——拿上面那行工单去源码读：
- `kernel/sched/debug.c:612` 看到它挂到 `&sysctl_sched_migration_cost`。
- `grep sysctl_sched_migration_cost kernel/sched/fair.c` → `fair.c:82: __read_mostly unsigned int sysctl_sched_migration_cost = 500000UL;`（默认 500000ns = 0.5ms）。
- `fair.c:9634/9644` 看到特殊值 `-1`/`0` 的分支语义。
- `core.c:8996` 看到它被用来设 `rq->avg_idle`。

产出一条 knowledge（见第 5 节 schema）。

## 3. 分流：tunable vs readonly

工单的 `type` 列决定每行写到哪份文件：

- `tunable` → `tunable.json`（用户可写、能调的参数，是调优的主体）
- `readonly` → `readonly.json`（只读观测项，不能调但可用于诊断/决策依据）
- `unknown` → 读 `file_symbol` 处的权限位自行判定后归入 tunable 或 readonly（少见）

**两条都重要**：调优不只靠可写参数，readonly 项（如 `domains/.../level`、`cpuacct.stat`）往往是判断"该不该调、调了有没有效"的依据，消费者需要它们做诊断。

## 4. 内核版本差异（重点）

这是生产者最需要警惕的事。**同一参数在不同内核版本可能完全不同**，所以 knowledge 必须按版本隔离，recipe 也按版本维护。

### 4.1 什么会跨版本变

| 会变的东西 | 例子 |
|---|---|
| **参数名 / 路径** | EEVDF 合入后（6.6+），CFS 的 `sched_latency_ns` 等被 `base_slice_ns` 取代；debugfs 路径可能调整。 |
| **默认值** | `sysctl_sched_migration_cost` 默认 500000ns，不同版本可能改。 |
| **语义** | 同名参数行为可能变（如某 feature flag 在新版本被默认开启/移除）。 |
| **是否存在** | 新增参数（如 `sched_ext` 相关）只在较新版本出现；旧参数被删除。 |
| **条件编译 gate** | 同名参数的 `CONFIG_*` 依赖可能变。 |
| **所在函数** | 函数可能被重命名/拆分——scan.py 工单和 recipe 都用**符号名**（函数/数组名）定位，符号重命名才是 breaking change，普通行号漂移不影响。若某版本的 `file_symbol` 指向的符号在新版找不到了（重命名了），需重新核对 recipe 的 locator。 |

### 4.2 生产者怎么应对

1. **只认当前版本的工单**：scan.py 是针对 `--ksrc` 指向的那棵源码树跑的，工单反映的就是那个版本的真实情况。不要拿旧版本的工单去套新版本的源码。
2. **带版本存储**：knowledge 存到 `knowledge/<version>/<module>/`（版本在前），`version` 取自 recipe 的 `version` 字段（与内核 `Makefile` 一致）。换版本就重跑 scan.py、重建一份，**不覆盖旧版本**。
3. **默认值/gate 要核对源码，别照抄旧 knowledge**：即使参数名没变，默认值和 gate 可能变了。每条 knowledge 的 `default`/`gate` 都以当前版本源码为准。
4. **留意参数消失/新增**：对比相邻版本的工单，新增的参数要补 knowledge，消失的参数在旧版本 knowledge 里保留但新版本不收录（消费者按版本取用）。
5. **符号定位，不用行号**：工单的 `file_symbol` 是符号名（函数/数组），跨版本稳定；行号每版漂移故不用。knowledge 里用 `kernel_name` + `path` 作稳定标识（注意：knowledge **不含** `file_symbol`，那是生产者定位源码用的，不进产物）。

### 4.3 版本号的取法

取内核 `Makefile` 头四行拼成：
```
VERSION = 7
PATCHLEVEL = 1
SUBLEVEL = 0
EXTRAVERSION = -rc5
→ "v7.1.0-rc5"
```
recipe 的 `version` 字段已按此填好，生产者直接用，不要自创格式。

## 5. knowledge 产出格式

> **字段权威定义源：[`knowledge/schema.json`](./knowledge/schema.json)**。该文件由**系统维护者**维护，机器可读，列出 tunable/readonly/summary 每个字段的 `required`/`type`/`desc`，以及 `consumes` 的格式规范（`consumes_format`）。本节 §5 的字段表是给人读的导引，与 schema.json 冲突时**以 schema.json 为准**。生产者填写前应读 schema.json 确认字段必填性、类型与填法（`unit` 填什么词见 `fields.tunable.unit.desc`）。

每个模块、每个版本，目录布局如下（五份都是**必产**，README+playbook 见 §7，生产完成度要求见 §7.3）：

```
knowledge/<version>/<module>/
├── summary.json      ← 参数层: 模块级索引(每条 3 字段), 消费者先扫
├── tunable.json      ← 参数层: 可调参数完整记录
├── readonly.json     ← 参数层: 只读参数完整记录
├── README.md         ← 选型层(可选): 模块自我介绍, 消费者 --modules 选模块用
└── playbook.json     ← 流程层(可选): 诊断流程 + 专家能力 + 规则, 端到端分析用
```

版本在前、模块在后：消费者先按 `uname -r` 定位版本，再选模块。三层结构（选型层→流程层→参数层）的总览见根 [`README.md`](./README.md)，消费者视角的使用见 [`CONSUMER.md`](./CONSUMER.md)。

### summary.json（模块级索引，消费者先读这个）

每条只 3 字段，用于消费者快速扫一遍决定要看哪些参数的完整记录：

```jsonc
{
  "module": "sched",
  "version": "v7.1.0-rc5",
  "items": [
    { "kernel_name": "sysctl_sched_base_slice", "category": "tunable", "summary": "EEVDF 基础时间片,任务每次被授予的运行时长基准。" },
    { "kernel_name": "sd->level", "category": "readonly", "summary": "调度域拓扑层级深度,level 从 CPU 向上递增,0=最底层最小域。" }
  ]
}
```

### tunable.json

```jsonc
{
  "module": "sched",
  "version": "v7.1.0-rc5",
  "items": [
    {
      "name": "migration_cost_ns",                       // userspace_name
      "kernel_name": "sysctl_sched_migration_cost",      // 稳定标识, 跨版本追踪用
      "path": "/sys/kernel/debug/sched/migration_cost_ns",
      "category": "tunable",                             // tunable/readonly
      "gate": "",                                        // CONFIG 依赖, 无则空串
      "unit": "ns",
      "default": 500000,
      "range": {"min": 0, "special": {"0": "全部视为 cache-cold,允许迁移", "-1": "全部视为 cache-hot,禁用基于热度的迁移"}},
      "summary": "cache-cold 迁移成本阈值。低于此值的跨CPU迁移被认为亏cache，调度器倾向不迁。",
      "when_to_increase": ["cache 命中率低", "热点任务被频繁跨 CPU 迁走"],   // 什么症状/目标时该调大(actionable)
      "when_to_decrease": ["负载不均某 CPU 闲置", "跨 CPU 利用率差需更激进迁移"],
      "increasing": {                                    // 调大时的效果
        "good_for": ["提升 cache 局部性", "减少跨 CPU 迁移"],   // 动词+方向,禁裸名词
        "bad_for":  ["加剧负载不均", "降低跨 CPU 利用率均衡"]   // 调大的真实代价,非 good_for 反义词
      },
      "decreasing": {                                    // 调小时的效果
        "good_for": ["促进负载均衡", "提升跨 CPU 利用率均衡"],
        "bad_for":  ["增加 cache miss", "增加迁移开销"]
      },
      "consumes": ["kernel/sched/core.c:8996"],          // 该变量被消费的位置(可选, ≤5条)
      "version_notes": ""                                // 本版本特殊说明(可选)
    }
  ]
}
```

注意：**不放 `file_symbol` / `source` 字段**——那是生产者定位源码用的（符号名），消费者不需要。`kernel_name` + `path` 是稳定标识。

### readonly.json

```jsonc
{
  "module": "sched",
  "version": "v7.1.0-rc5",
  "items": [
    {
      "name": "level",
      "kernel_name": "sd->level",
      "path": "/sys/kernel/debug/sched/domains/cpuN/domainN/level",
      "category": "readonly",
      "gate": "",
      "summary": "该调度域在拓扑层级中的深度。level 从 CPU 向上递增，0=最底层最小域（SMT/CLS/MC），越往上越接近 NUMA 根域。",
      "use": "读值=域深度；配合同目录 flags 判断是否 NUMA 域，作为 migration_cost 调参依据。",
      "per_cpu": true                                     // per-cpu/per-domain 项标注(可选)
    }
  ]
}
```

readonly 项用 `use`（怎么用来诊断）替代 tunable 的 `increasing`/`decreasing`。

### 字段约定

| 字段 | 必需 | 说明 |
|---|---|---|
| `name` | 是 | userspace_name |
| `kernel_name` | 是 | 稳定标识，跨版本追踪用 |
| `path` | 是 | control_entry，占位符保留模板形式 |
| `category` | 是 | tunable / readonly |
| `gate` | 是 | CONFIG 依赖；无则空串。重点标注模块特性级开关 |
| `unit`/`default`/`range` | tunable 必需 | 从源码读出；readonly 可省。`range` 用 `{"min":..,"max":..}`，特殊值用 `"special":{"0":"禁用"}` |
| `summary` | 是 | 一句话说清参数是什么。summary.json 里也带这条 |
| `increasing`/`decreasing` | tunable 必需 | 各含 `good_for`/`bad_for` 数组。`good_for`=该方向的调后效果(动词+方向,如"提升 cache 局部性");`bad_for`=该方向**真实代价/风险**(非 good_for 反义词)。消费者据此判断方向 |
| `when_to_increase`/`when_to_decrease` | tunable 必需 | 数组,什么**症状/目标**时该往这调(如"cache 命中率低")。这是消费者的行动入口——看到症状直接对号入座,不用反推 |
| `use` | readonly 必需 | 怎么用来诊断——读哪个值、怎么判读 |
| `deprecated` | tunable 否 | `true` 表示该参数在当前内核已废弃（handler 仅 pr_warn/改无效）。消费者据此过滤——别建议用户调它。从 `unit: "deprecated"` 或源码确认 |
| `boot_only` | tunable 否 | `true` 表示该参数只能在 boot cmdline 设置（运行时不可改，需 reboot）。消费者给建议时改提示"改 grub/重启"而非 `echo`。从 mechanism=`__setup` 且非运行时可改判定 |
| `consumes` | 否 | grep `kernel_name` 全仓命中的主要消费点，≤5 条，格式 `文件:符号`（函数/数组名，非行号） |
| 其余 | 否 | `per_cpu`/`per_domain`/`version_notes` 等按需 |

**不放的字段**：`file_symbol` / `source`——生产者定位源码用，消费者不需要。`kernel_name`+`path` 已是稳定标识。

**`good_for`/`bad_for`/`when_to_*` 填写约定（重要，消费者可操作性的关键）**：

这三组字段是消费者决定"往哪调、该不该调"的入口，填写必须 actionable，禁止结果状态名词堆砌。三条硬规则：

1. **`good_for`/`bad_for` 必须动词+方向，禁裸名词**。消费者要能直接读出"调大→提升X、加剧Y"。
   - ✓ `提升 cache 局部性`、`减少跨 CPU 迁移`、`加剧负载不均`、`增加迁移开销`
   - ✗ `cache 局部性`、`负载均衡`、`吞吐`、`延迟`（裸名词，方向含混，消费者要二次推理）

2. **`bad_for` 写该方向的真实代价，禁止镜像 `good_for` 取反**。这是镜像取反错的根因——生产者偷懒把 good_for 反着塞进 bad_for，反错方向。
   - 镜像取反 ✗：good_for=`cache 局部性` → bad_for=`cache 局部性差`（反名词，易错且不提供新信息）
   - 真实代价 ✓：调大 migration_cost 的 bad_for=`加剧负载不均`（调大**额外引发**的坏处，不是"cache 局部性"的反）
   - 自检问法：填 bad_for 时问"调这个方向，除了得到 good_for，还会**引发什么实际问题**"，而不是"good_for 的反义词是什么"。

3. **`when_to_increase`/`when_to_decrease` 写症状/目标，是消费者的行动入口**。
   - ✓ `cache 命中率低`、`热点任务被频繁迁走`、`某 CPU 闲置负载不均`
   - ✗ `cache 局部性`（结果状态，不是症状）、`高负载`（太泛）
   - 消费者遇到症状直接对号入座：看到"cache 命中率低"→ 查 when_to_increase 命中 → 调大。不用从 good_for 反推。

**非单调参数（trigger/interface/deprecated）的 `when_to_*`**：这些没有调大调小语义，`increasing`/`decreasing` 填 `(无单调性,触发动作)`，但 `when_to_*` 仍填该触发的使用场景（如 `memory.reclaim` 的 when_to_increase=`组内存紧张想主动回收`），让消费者知道什么时候去触发它。布尔开关(sched_feat)的 increasing=启用/decreasing=禁用，when_to 写何时该启用/禁用。

**迁移策略**：`when_to_*` 是新增字段，旧 knowledge 缺它不报错（query.py 不依赖），不强制回填；但 `good_for`/`bad_for` 的动词化约定应用后，旧 knowledge 的是"合规但不够 actionable"，在后续增量/返工时顺手改。**新产的 tunable 必须按此 schema**（when_to_* 必有、good_for/bad_for 动词化）。

**补充约定**：

- `category` = `tunable`/`readonly`，和文件分流一致；summary.json 里也带这个字段。
- `gate` 格式：空串=无条件存在；有值=空格分隔的 `CONFIG_*`，全部满足才编译（AND）。这是消费者判断"该参数在目标系统存不存在"的判据，必须准确。
- **非数值参数**：`unit`/`default`/`range`/`increasing`/`decreasing` 是给数值型 tunable 设计的；对非数值参数（布尔开关、cmdline 开关、枚举字符串、格式字符串、触发式写入、接口、已废弃），按下列约定填，不要硬套数字。**`unit` 字段填什么词、各类参数怎么填，见 [`knowledge/schema.json`](./knowledge/schema.json) 的 `fields.tunable.unit.desc`（权威源，生产者照此填）**；其余字段（`default`/`range`/`increasing`/`decreasing`）按该类参数的语义如下配合：
  - **布尔开关**（如 sched_feat、`sched_energy_aware`）：`default` 写 true/false 或 "未指定"，`range` 省略或 `{}`；`increasing`=启用（true），`decreasing`=禁用（false），`good_for`/`bad_for` 按启用/禁用效果写。
  - **cmdline 开关**（如 `isolcpus`、`preempt`）：`default` 写 "未指定"（默认在源码/boot config 决定），`range` 省略；`increasing`/`decreasing` 按"开启该隔离/该模式" vs "不开启"的效果写。
  - **枚举字符串**（如 `preempt` 的 none/voluntary/full/lazy）：`default` 写当前版本默认值或 "未指定"，`range` 省略。
  - **格式字符串**（如 cgroup `max "100000"` 双 token）：`default` 如实填字符串，`range` 省略。
  - **触发式写入**（写一个值触发动作，非单调数值调参，如 cgroup `memory.peak` 写任意值重置峰值、`memory.reclaim` 写字节数触发回收）：`default` 写 "无"（无稳态值），`range` 省略；`increasing`/`decreasing` 填 `{"good_for": ["(无单调性,触发)"], "bad_for": []}`，并在 `summary`/`version_notes` 说清"写什么触发什么"。
  - **接口类**（路径像文件但本质是注入/调试接口，非调参旋钮，如 `/proc/<pid>/mem` 是 ptrace 读写接口）：`default` 写 "无"，`range` 省略；`increasing`/`decreasing` 同 trigger 填无单调性说明，`version_notes` 注明"非调参,是接口"。
  - **不接受参数或已废弃**（如 `sched_thermal_decay_shift` 只打警告、`laptop_mode` 已 deprecation-only）：`summary` 注明"已废弃/不生效"，`increasing`/`decreasing` 填 `{"good_for": ["(已废弃,改无效)"], "bad_for": []}`，`version_notes` 说明废弃后行为靠哪个子系统，仍收录以便消费者知道它没用。
  - `unit` 填不下的信息放 `summary` 或 `version_notes`，别硬塞进数字字段。

  **`range` 省略约定**：源码里读不到边界约束（sysctl 无 `.extra1`/`.extra2` 且 write 回调无显式校验）时，`range` 写 `{}`（空对象）而非省略字段，让消费者明确"源码未约束"而非"生产者漏填"。`increasing`/`decreasing` 仍要填——它们看的是调参效果不是边界。
- **以源码为准**：本文档和任何示例仅供参考，**与内核源码冲突时以源码为准**。读到示例与源码不一致，按源码语义填写，并在该参数的 `version_notes` 里标注"文档示例有误，实际语义见源码"。示例是为说明 schema 形态，不保证参数语义永远正确——具体参数含义必须亲自读源码核实，不要照抄示例或旧 knowledge。

**同名不同路径的参数**：同一 `userspace_name` 可能在不同路径下各一份（如 sched 的 `runtime` 在 `fair_server/cpuN/` 和 `ext_server/cpuN/` 各一条），是不同参数。items 里各占一条，`name` 可相同、靠 `path` 全路径唯一区分，不要合并：

```jsonc
// tunable.json items 里两条并列
{ "name": "runtime", "path": "/sys/kernel/debug/sched/fair_server/cpuN/runtime", "kernel_name": "fair_server_runtime_fops", "category": "tunable", ... },
{ "name": "runtime", "path": "/sys/kernel/debug/sched/ext_server/cpuN/runtime", "kernel_name": "ext_server_runtime_fops", "category": "tunable", ... }
```

**维护 vocab.json**：生产时在 knowledge 的 `summary`/`tuning`/`use` 里用到消费者可能不懂的内核领域术语（EEVDF、PELT、cache-cold、SD_NUMA、TTWU…），把该术语追加到 `knowledge/vocab.json` 的 `terms` 数组（term + definition + consumer_need_to_know）。已存在的不重复加。消费者遇到不懂的词会用 query.py 查这份词表——生产者填的词若没进 vocab，消费者就得猜。

## 6. 自检清单

产出 knowledge 前逐条核对：

**参数层（必产，verify.py 会校验）**：

- [ ] 每行的 `default`/`gate` 是从**当前版本源码**核对的，不是照抄旧 knowledge。
- [ ] `category` 为 `tunable` 的都进了 `tunable.json`，`readonly` 的进了 `readonly.json`。
- [ ] 每条 tunable 带了 `increasing`/`decreasing`（各含 good_for/bad_for）+ `when_to_increase`/`when_to_decrease`，每条 readonly 带了 `use`。
- [ ] `good_for`/`bad_for` 是**动词+方向**（提升X/减少Y/加剧Z），无裸名词（"吞吐"/"延迟"/"cache 局部性"算不合格）。
- [ ] `bad_for` 是该方向**真实代价**，不是 `good_for` 的反义词镜像（自查：调这方向除 good_for 外还引发什么实际问题，而非 good_for 反着填）。
- [ ] `when_to_increase`/`when_to_decrease` 写**症状/目标**（消费者看到症状能直接对号入座调方向），不是结果状态。
- [ ] **没有** `source` 字段（消费者不需要）。
- [ ] `path` 含 `cpuN`/`domainN`/``<pid>`/``<name>` 占位符的，标了 `per_cpu`/`per_domain` 等。
- [ ] 三份文件都存到了 `knowledge/<version>/<module>/`（版本在前），`version` 与 recipe 一致。
- [ ] summary.json 每条只有 3 字段（`kernel_name`/`category`/`summary`），和详细文件一一对应。
- [ ] gate 不为空的参数，消费者能据此判断目标系统是否编译了该参数。
- [ ] **arch-guard gate**：scan.py 的 gate 解析只认 `CONFIG_*`，对 `HAVE_ARCH_*`/`__ARCH_*` 这类 arch-internal 宏和 OR 条件会跳过（设计如此，因为它们不进 `.config`、消费者查不到）。遇到参数真实存在性还依赖 arch 宏的（如 `legacy_va_layout` 依赖 `HAVE_ARCH_PICK_MMAP_LAYOUT`），在 `version_notes` 注明"存在性另依赖 arch 宏 X，见源码"，不强行塞进 gate。
- [ ] 同名不同路径的参数按全路径区分，没合并。
- [ ] 已废弃参数（`unit: "deprecated"` 或源码确认 handler 仅 pr_warn）标了 `deprecated: true`，消费者可过滤。
- [ ] boot-only cmdline 参数（运行时不可改）标了 `boot_only: true`，消费者给建议时改提示重启而非 `echo`。
- [ ] **字段对照 schema.json**：每条 tunable/readonly/summary 的字段符合 `knowledge/schema.json` 的 `required`/`type` 约束——必填字段无缺失，`category` 取值在 `tunable|readonly`，`unit` 按 `fields.tunable.unit.desc` 的填法说明填、表外自定义单位已在 `version_notes` 标注非标准单位，`consumes` 每条符合 `consumes_format`（`文件:符号`，禁行号/裸文件/`struct->field`/带注后缀）。verify.py 已据此自动校验，但生产者自检时先对照一遍。

**选型层 + 流程层（必产，生产完成度的要求，见 §7.3；verify.py 不校验但 §6 自检保证）**：

- [ ] README.md 的 5 章节齐全（适合场景/能观察什么/调优杠杆/边界雷区/配套）。
- [ ] README.md 开头 `> 一句话摘要` 行紧跟 H1 标题后、且是第一个非空块——`query.py --modules` 靠它提取。
- [ ] playbook.json 是合法 JSON，顶层至少含 `modes`（其余字段按 §7.2 schema 选配）。
- [ ] playbook 的所有 lever 引用都用 `kernel_name` 锚定现有 tunable/readonly，没复制详情字段（避免版本漂移）。
- [ ] playbook 的 `capabilities.reasoning`/`observation_map` 引用的源码字段（如 `/proc/schedstat` 的 `lb_count`）是源码真实暴露的，没编造。
- [ ] playbook 的 `rules` 每条带 `fallback_to_capability`，衔接智能模式。
- [ ] playbook 的 `version` 字段与 recipe/参数层一致。

## 6.1 用 verify.py 查漏

产出后**必须**跑 verify 自查完整性——它会拿 scan 工单（ground truth）和你产出的 knowledge 对照，diff 出 missing/extra：

```bash
# 同版本完整性校验（你产完后跑）
python3 verify.py --recipe scan_recipes/<version>/<module>.json --ksrc <内核源码> \
    --against knowledge/<version>/<module>/

# missing=0 才算完整。missing 列出的就是你漏掉的，逐个补上。
# extra=0 说明没乱造（产出的都在工单里）。
```

跨版本增量（如内核从 7.1 升到 7.2，用 7.2 的 ksrc 跑，against 7.1 的 knowledge）：missing 就是 7.2 新增需补的，extra 就是 7.2 已删可弃的——据此增量更新，不用从头重产。

## 7. README.md 与 playbook.json（选型层 + 流程层）

参数层（summary/tunable/readonly）是地基。本节两个文件是**选型层 + 流程层**，与参数层构成完整的三层 knowledge。**生产者一次生产的完整产物是五份**（参数层三份 + README + playbook），缺一不算生产完成——见 §7.3。它们让消费者能"选模块"和"端到端分析"，否则消费者只拿到一堆参数无法定位从何入手。

> verify.py 只校验参数层（missing/extra/drifted），不校验 README/playbook 的存在——这是"机械完整性"和"生产完成度"的区别。生产者不能因为 verify 不查就不产，README/playbook 的完整性靠 §6 自检清单和本节规范保证。

### 7.1 README.md（选型层）

放 `knowledge/<version>/<module>/README.md`。固定 5 章节，让跨模块对齐（消费者读它做选型）：

```markdown
# <module> — <中文名>（<version>）

> 一句话摘要（给消费者 --modules 索引提取用）：本模块管什么，一句话。

## 我适合什么场景
- 列 5~8 个 profiling 症状，对应本模块能解决/诊断的。
- 末尾加一段"不属于本模块的情况 → 建议转哪个模块"，帮消费者排除。

## 我能观察什么（消费 readonly 做诊断）
- 列本模块 readonly 项能观测到的信号 + 它揭示什么。消费者据此知道"该读什么"。

## 我的调优杠杆（消费 tunable 给建议）
- 按调优维度分组列 tunable 族，引用 userspace_name 或 kernel_name。
- 强调"详细字段用 query.py --name 取，不要凭记忆"。

## 边界与雷区（不该碰的）
- 特殊语义值、boot-only 参数、互斥语义、常见误调。

## 配套
- query.py 用法（--list / --name / --playbook）、vocab 入口。
- **`knowledge/schema.json`**（字段权威定义）：tunable/readonly/summary 每个字段的必填性/类型/填法说明、consumes 格式规范。填写 knowledge 前先读它确认字段约定（`unit` 怎么填见 `fields.tunable.unit.desc`），与源码冲突时以源码为准并在 `version_notes` 标注。
- `knowledge/vocab.json`（领域术语）：summary/use 里用到消费者可能不懂的内核术语（EEVDF/PELT/cache-cold…）在此追加，见 §5 末尾。
```

**关键约束**：开头 `> 一句话摘要` 行是 `query.py --modules` 提取的，必须第一行 `>` 引用且紧跟在 H1 标题后（空行后第一个非空块）。简介要够"一句话"——消费者 `--modules` 横向比较多个模块时靠它，太长就没法比。

### 7.2 playbook.json（流程层）

放 `knowledge/<version>/<module>/playbook.json`。把本模块当**专家**：既给智能模式的能力描述（让消费者 LLM 推理下一步读什么/判什么/动哪个参数），又给规则模式的固定 if-then（fallback）。三种模式（intelligent/rule/hybrid）由消费者选择如何用，query.py 的 `--mode` 只决定**展示哪部分原料**，真正用哪种推理是消费者自己的行为——**playbook 供弹药不强制走哪条路**。

schema（以 sched 为参照，见 `knowledge/v7.1.0-rc5/sched/playbook.json`）：

| 顶层字段 | 模式 | 用途 |
|---|---|---|
| `modes` | 全 | 三模式的 description + how_to_use，消费者读它知道每种模式怎么用本 playbook |
| `capabilities` | intelligent | 专家能力：每个含 `id`/`name`/`when_applies`/`reasoning`(专家推理步骤,自然语言给LLM当思路)/`observe`/`candidate_levers` |
| `observation_map` | intelligent+rule | 症状信号 → 该读的 readonly（path/kernel_name/字段），反向索引 |
| `thresholds` | 全 | 判据规则：readonly 值什么范围算异常，含 `warn_if_gt`/`meaning`。**阈值是经验值不是源码硬约束**，须标明 |
| `leverage_map` | intelligent+hybrid | 症状类别 → 候选 tunable 族（kernel_name 引用，不复制详情） |
| `interactions` | intelligent+hybrid | 参数依赖/互斥/boot_only/mutex——消费者调 A 联动 B |
| `rules` | rule+hybrid | 固定 if-then：每个含 `trigger`(symptom+keywords)/`steps`(read→judge→recommend)/`fallback_to_capability` |

**生产要求**：
- **引用不复制**：`candidate_levers`/`leverage_map`/`interactions` 用 `kernel_name` 锚定现有 tunable/readonly，**不复制** summary/range/默认值——避免冗余和版本漂移。消费者拿 kernel_name 去 `query.py --name` 取详情。
- **capabilities 要 from 源码真实**：`reasoning` 引用的字段（如 `/proc/schedstat` 的 `lb_count`/`lb_failed`）必须是源码真实暴露的，不能编造。
- **thresholds 判据要可执行**：`warn_if_gt` 是能真判的比值/阈值，`desc` 说清怎么算。不是泛泛"看下统计"。
- **rules 要带 fallback_to_capability**：每条 rule 失配或需要补充时指回某个 capability id，这是混合模式的衔接点。
- **族引用粒度**：参数族（如 `sysctl_sched_features` 含多个 `[bit]`）在 playbook 用族名引用即可，消费者用 keyword/path 定位具体 bit——README 的"配套"段要说明这点。
- **版本差异**：capabilities/rules 跨版本可能变（如 sched_ext 合入后加 mutex 项），按版本隔离，`version` 字段与 recipe 一致。

### 7.3 与参数层的优先级（生产者必产全部五份）

参数层（summary/tunable/readonly）是地基，**先产全参数、verify 通过**。但**生产者一次生产的完整产物是五份**——参数层 verify 通过后，README.md 和 playbook.json 也由**同一生产者**接着产，不是"可选"也不是"留给别人补"。

**为什么必产**：没有 README，消费者 `--modules` 无法选模块（intro 为空）；没有 playbook，消费者无法端到端分析（只拿到一堆参数不知道从何入手）。参数层扎实了不产 README+playbook，等于造了零件没给说明书，消费者用不起来。

**时序**：先参数层（verify missing=0/extra=0/drifted=0），再 README+playbook。playbook 的 lever 引用 kernel_name，必须等参数层稳定才不会引用错——所以参数层先行，但**都属同一次生产任务，收尾时五份齐全才算完成**。

**增量场景同样适用**：跨版本增量（如 v7.1→v7.2）时，即使参数层 0 变化只 carry forward，也要核对 README+playbook 是否需要随语义变化更新（如 v7.2 改了某参数默认值或新增能力，README 的"调优杠杆"段和 playbook 的 capabilities/thresholds 要同步）。
