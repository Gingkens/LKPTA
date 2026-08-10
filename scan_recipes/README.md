# scan.py 工具手册

这份文档只讲 `scan.py` 这个工具：怎么跑、产出什么、每一列什么含义。

如果你是**生产者**，想知道拿到工单后怎么加工成 knowledge（工作流、版本差异、knowledge schema），请读 [`../PRODUCER.md`](../PRODUCER.md)。本文档是工具说明书，不重复方法论。

## scan.py 是什么

`scan.py` 是 **recipe → 参数工单** 的转码器。它读一份 recipe（维护者按模块+版本写的规格），结合内核源码，输出一张扁平表格，一行一个参数，给生产者作为去源码读懂参数的工作清单。

它读源码是为了填上 recipe 里没有的关键信息（`file_symbol`、`type`、`kernel_name`、逐参数的 `gate`），并把 recipe 嵌套的维护者结构坍缩成扁平行动表。

## 用法

```bash
# 默认输出 TSV 到 stdout（一行一参数）
python3 scan.py --recipe <module>.json --ksrc <内核源码根>

# 写到文件
python3 scan.py --recipe scan_recipes/sched.json --ksrc /home/lgk/linux --out brief.tsv

# 输出 JSON（便于程序化注入）
python3 scan.py --recipe scan_recipes/sched.json --ksrc /home/lgk/linux --out brief.json --format json
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

| mechanism | 对应接口 | 扫什么 |
|---|---|---|
| `debugfs_create_dir` | debugfs | dir + 其下挂的 create_file/create_u32 等子节点（按父变量精确归属） |
| `sched_feat` | debugfs | `SCHED_FEAT(NAME, default)` 宏 |
| `register_sysctl_init` | sysctl | ctl_table 数组的 `.procname`/`.data`/`.mode`/`.proc_handler` |
| `cftype` | cgroup | cftype 数组的 `.name`/`.write_*`/`.read_*` |
| `proc_pid_entry` | proc | pid entry 表的 `REG/ONE` |
| `__setup` | cmdline | `__setup("param", handler)` |

新增 mechanism（如 tracepoint、module_param）时，在 `scan.py` 的 `EXTRACTORS` 注册表里加一个提取函数即可。

## recipe 格式简述（给维护者）

recipe 是按模块+版本维护的 JSON，结构：

```jsonc
{
  "module": "sched",
  "version": "v7.1.0-rc5",      // 与内核 Makefile 一致
  "sources": [
    {
      "mechanism": "debugfs_create_dir",
      "locator": "kernel/sched/debug.c:sched_init_debug",  // 文件:函数名(跨版本稳定)
      "filter": { "dir": "numa_balancing", "children": {...}, "child_locator": "...", "config": [...] },
      "interface": "debugfs",
      "path_prefix": "/sys/kernel/debug/sched/numa_balancing"
    }
  ]
}
```

- `locator` 用 `文件:函数名`（不用行号，跨版本稳定）。
- `filter.config` 是模块级 CONFIG 清单；scan.py 会进一步按 #ifdef 解析出每个参数的精确 gate。
- 新增一个子系统的 recipe，照此结构写一份 `<module>.json` 即可，scan.py 自动支持。
