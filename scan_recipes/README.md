# scan.py 工具手册

这份文档只讲 `scan.py` 这个工具：怎么跑、产出什么、每一列什么含义。

如果你是**生产者**，想知道拿到工单后怎么加工成 knowledge（工作流、版本差异、knowledge schema），请读 [`../PRODUCER.md`](../PRODUCER.md)。本文档是工具说明书，不重复方法论。

## scan.py 是什么

`scan.py` 是 **recipe → 参数工单** 的转码器。它读一份 recipe（维护者按模块+版本写的规格），结合内核源码，输出一张扁平表格，一行一个参数，给生产者作为去源码读懂参数的工作清单。

它读源码是为了填上 recipe 里没有的关键信息（`file_symbol`、`type`、`kernel_name`、逐参数的 `gate`），并把 recipe 嵌套的维护者结构坍缩成扁平行动表。

## 用法

```bash
# 默认输出 TSV 到 stdout（一行一参数）
python3 scan.py --recipe scan_recipes/<version>/<module>.json --ksrc <内核源码根>

# 写到文件
python3 scan.py --recipe scan_recipes/v7.2-rc7/sched.json --ksrc /home/lgk/linux --out brief.tsv

# 输出 JSON（便于程序化注入）
python3 scan.py --recipe scan_recipes/v7.2-rc7/sched.json --ksrc /home/lgk/linux --out brief.json --format json
```

| 参数 | 必需 | 含义 |
|---|---|---|
| `--recipe` | 是 | recipe JSON 路径 |
| `--ksrc` | 是 | 内核源码根目录 |
| `--out` | 否 | 输出文件路径；不写则打到 stdout |
| `--format` | 否 | `tsv`（默认）或 `json`；不指定时若 `--out` 以 `.json` 结尾自动选 json |

换模块（如以后有 `mm.json`）只换 `--recipe`，脚本本身不变。

## 输出列

表头：

```
id  kernel_name  userspace_name  control_entry  file_symbol  type  gate
```

| 列 | 来源 | 含义 |
|---|---|---|
| `id` | 脚本生成 | 顺序号，无业务含义。 |
| `kernel_name` | 源码挖出 | 该参数绑定的内核符号，**生产者用它在源码里 grep 定位语义**。取值随 mechanism 不同：debugfs → 底层变量或 fops（如 `sysctl_sched_base_slice`、`sd->level`）；sysctl → `.data` 变量（如 `sysctl_sched_uclamp_util_min`）；cftype → 第一个 read/write 回调（如 `cpu_weight_write_u64`）；proc → recipe `filter.show` 指向的 `.show` 回调（如 `proc_sched_show_task`），回退到 ops 结构名；cmdline → `__setup` handler（如 `setup_preempt_mode`）。极少为空（cgroup 用 `.seq_show` 时），空则以 `file_symbol` 为准。 |
| `userspace_name` | recipe/源码 | 用户态名字（debugfs 节点名、sysctl procname、cmdline 参数名）。 |
| `control_entry` | recipe 解析 | 用户态完整路径，由 `path_prefix` + 目录布局拼出。含占位符（`cpuN`/`domainN`/`<pid>`/`<name>`）表示 per-cpu/per-domain/per-pid/per-cgroup，运行时展开。 |
| `file_symbol` | 源码挖出 | 格式 `相对路径:符号名`（函数/数组/结构体名，**非行号**）。如 `kernel/sched/core.c:sched_core_sysctls`、`kernel/sched/debug.c:register_sd`。**生产者打开源码的位置**——用符号名跨版本稳定，行号每版漂移故不用。 |
| `type` | 源码挖出 | `tunable`（可写，权限位含写）或 `readonly`（只读）。决定生产者把该参数写到 `tunable.json` 还是 `readonly.json`。少数情况 `unknown`，需读源码权限位判定。 |
| `gate` | 源码解析 #ifdef | 该参数依赖的 `CONFIG_*`。两级解析：先按该参数在源码处的 `#ifdef` 嵌套逐参数解析（精确）；若该参数无 `#ifdef` 包裹且其 recipe source 段声明了 `filter.config`，回退到该模块级清单；均为空则输出空串。空串=无条件存在。多值时空格分隔，全部满足该参数才编译进来（AND）。 |

## 数据驱动

scan.py 按 recipe 的 `mechanism` 字段分派到提取器，不硬编码任何模块细节。当前支持的 mechanism：

| mechanism | 对应接口 | scan 自动读的字符串 | recipe 提供的清单(校验源) | 变量(scan 遵循 recipe) |
|---|---|---|---|---|
| `debugfs_create_dir` | debugfs | dir 名、file 名、mode | `dirs[].children` | parent 赋值链(自动推导)、`child_dir_template`(per-cpu) |
| `sched_feat` | debugfs | NAME、default | `features` | (无) |
| `register_sysctl_init` | sysctl | path("kernel")、procname、mode | `procnames` | table 名(定位用) |
| `cftype` | cgroup | name | `names` | cftype_locator(定位用) |
| `proc_pid_entry` | proc | name、FLAGS | `names` | (无) |
| `proc_create_seq` | proc | name、mode | `names` | (无) |
| `proc_create_data` | proc | name | `names` | `child_dir_template`(per-instance) |
| `__setup` | cmdline | param | `param` | (无) |

**设计原则**(字符串 vs 变量):
- **字符串字面量**(如 `debugfs_create_dir("sched", ...)` 的 "sched"、`__setup("nohz_full=",...)` 的 "nohz_full"、`.procname = "foo"` 的 "foo")——scan 从源码自动读,recipe 的清单用来**校验**(scan 读到的 vs recipe 列的,报 `claim_gap`/`claim_extra`)。
- **变量**(如 `debugfs_sched` 句柄、`buf`(per-cpu dir 名)、`desc->dir`)——scan 读不出运行时值,遵循 recipe:`debugfs` 的 parent 关系由源码"变量赋值链"自动推导(`numa = debugfs_create_dir("numa_balancing", debugfs_sched)` 这行表达 numa 的 parent 是 debugfs_sched 句柄);per-cpu/per-instance 的 dir 名变量用 recipe 的 `child_dir_template`(如 `cpuN`/`irq/<irqN>`)。

新增 mechanism(如 tracepoint、module_param)时,在 `scan.py` 的 `EXTRACTORS` 注册表里加一个提取函数即可。

## scan 自检(claim_diff)

scan 不只扫,还自检:对每个 source,把 recipe 清单 vs scan 实际扫出的字符串参数名对照,报两类差异:

- `claim_gap` — scan 扫到但 recipe 清单没列(清单漏写,提醒补)
- `claim_extra` — recipe 清单列了但 scan 没扫到(清单过时或 scan 漏扫)

scan 的 JSON 输出带 `claim_diff` 字段,verify.py 直接读展示(不再自己跑对照)。这把"recipe 是源,源不可靠下游全错"的上游校验内化进 scan。

## recipe 格式(给维护者)

recipe 是按模块+版本维护的 JSON,结构:

```jsonc
{
  "module": "sched",
  "version": "v7.2-rc7",        // 与内核 Makefile 一致；换版本时改此字段
  "sources": [
    {
      "mechanism": "debugfs_create_dir",
      "locator": "kernel/sched/debug.c:sched_init_debug",   // 文件:函数名(跨版本稳定)
      "interface": "debugfs",
      "path_prefix": "/sys/kernel/debug/sched",             // 函数基址(顶层 dir 的路径)
      "dirs": [                                              // 同 locator 多 dir 合一(减重复)
        {"name": "sched", "children": ["features","verbose","preempt","base_slice_ns",...]},
        {"name": "numa_balancing", "parent": "sched",
         "children": ["scan_delay_ms","scan_period_min_ms",...]},
        {"name": "llc_balancing", "parent": "sched", "config": "CONFIG_SCHED_CACHE",
         "children": ["enabled","aggr_tolerance",...]}
      ]
    }
  ]
}
```

各 mechanism 的 recipe 字段约定:

### `debugfs_create_dir`(同 locator 多 dir 合一,用 `dirs` 列表)
- `locator`:指向创建这些 dir 的 init 函数(如 `sched_init_debug`)
- `path_prefix`:函数基址(顶层 dir 的完整路径,如 `/sys/kernel/debug/sched`)
- `dirs`:dir 描述列表,每个含 `name`(dir 名,scan 自动读+校验)、可选 `parent`(父 dir 名字符串,如 "sched";scan 同时从源码赋值链推导,双重定位)、可选 `children`(预期子节点名清单,scan 自动读源码的 `debugfs_create_file/u32` 调用名校验)、可选 `config`(该 dir 的模块级 CONFIG)
- `child_dir_template`(可选):per-cpu 子目录模板(如 `cpuN`),当源码用 `buf` 变量创建 per-cpu 子目录时,scan 用此模板拼路径

### `sched_feat`
- `locator`:`kernel/sched/features.h`(无符号,纯文件)
- `path_prefix`:`/sys/kernel/debug/sched/features`
- `features`:预期 SCHED_FEAT 名清单(scan 读 `SCHED_FEAT(NAME,...)` 的 NAME 校验)

### `register_sysctl_init`
- `locator`:init 函数(如 `sched_core_sysctl_init`)
- `path_prefix`:`/proc/sys/<path>`(如 `/proc/sys/kernel`)
- `table`:ctl_table 数组名(scan 定位数组用,从 `register_sysctl_init("path",TABLE)` 调用也能自动读)
- `procnames`:预期 procname 清单(scan 读数组每条 `.procname` 校验)

### `cftype`
- `locator`:cgroup_subsys 结构体(如 `cpu_cgrp_subsys`)
- `cftype_locator`:cftype 数组(如 `kernel/sched/core.c:cpu_files`)
- `path_prefix`:`/sys/fs/cgroup/<subsys>.<name>`
- `names`:预期 cftype name 清单(scan 读数组每条 `.name` 校验)

### `proc_pid_entry`(REG/ONE 表)
- `locator`:`fs/proc/base.c:tgid_base_stuff`
- `path_prefix`:`/proc/<pid>` 或 `/proc/<pid>/<file>`
- `names`:预期 name 清单(scan 读 `REG/ONE("name",...)` 校验)

### `proc_create_seq`
- `locator`:init 函数
- `path_prefix`:`/proc`
- `names`:预期 name 清单(scan 读 `proc_create_seq("name",...)` 校验)

### `proc_create_data`(含 `proc_create`/`proc_create_single` 变体)
- `locator`:init 函数
- `path_prefix`:`/proc` 或 `/proc/irq`
- `child_dir_template`(可选):per-instance 模板(如 `irq/<irqN>`)
- `names`:预期 name 清单(scan 读 `proc_create_*("name",...)` 校验)

### `__setup`
- `locator`:handler 函数(如 `housekeeping_nohz_full_setup`)
- `path_prefix`:`kernel cmdline`
- `param`:预期 param(scan 读 `__setup("param=",handler)` 或 `__setup("param",handler)` 校验)

### 共通字段
- `locator` 用 `文件:函数名`(不用行号,跨版本稳定)
- `path_prefix` 是用户态路径前缀(含占位符如 `<name>`/`<pid>`/`<irqN>` 表示 per-cgroup/per-pid/per-irq)
- `config`(可选):模块级 CONFIG 清单,scan 会进一步按 #ifdef 解析每个参数的精确 gate
- **存放路径**:recipe 存 `scan_recipes/<version>/<module>.json`,`<version>` = `recipe.version` 字段值(与 `knowledge/<version>/` 目录名对齐)。每个支持的内核版本一份 recipe,放该版本子目录下;换版本时新建 `scan_recipes/<新版本>/` 子目录、复制上一版 recipe、按新版源码核对调整(`version`/`locator`/`sources`,新版符号可能重命名/增删)。维护者**不跨版本复用同一份 recipe**——即使 source 大量重复,也按版本各存一份,保证 scan 不静默漏参数。
- 新增子系统的 recipe,在该版本目录下照此结构写一份 `<module>.json` 即可,scan.py 自动支持。
