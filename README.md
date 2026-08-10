# kt — 内核调优知识库

一个按 **内核版本 × 子系统** 组织的内核调优知识库系统。目标是把内核里散落在 debugfs / sysctl / cgroup / proc / cmdline 的可调参数，沉淀成结构化的 knowledge，供调优 agent 消费、给出性能优化建议。

## 整体架构

```
recipe (维护者按 模块+版本 写的规格)
   │  scan_recipes/scan.py  转码
   ▼
参数工单 (扁平表格, 一行一参数)
   │  生产者读源码理解参数
   ▼
knowledge (README+playbook+summary/tunable/readonly.json, 按版本+模块存储)
   │  消费者用 query.py 按需查询
   ▼
性能优化建议
```

三个角色：

| 角色 | 职责 | 读这份 |
|---|---|---|
| **维护者** | 为每个子系统、每个内核版本维护一份 recipe（`scan_recipes/<module>.json`），描述该模块暴露哪些可调参数、在源码哪里 | `scan_recipes/README.md` |
| **生产者** | 跑 scan.py 拿工单 → 去内核源码读懂每个参数 → 产出 knowledge → 跑 verify.py 自查完整性 | [`PRODUCER.md`](./PRODUCER.md) |
| **消费者** | 用 query.py 查询 knowledge → 给出针对目标系统的调优建议 | [`CONSUMER.md`](./CONSUMER.md) |

## 目录结构

```
kt/
├── README.md          ← 你在这
├── PRODUCER.md         ← 生产者指南：怎么生产 knowledge（含版本差异处理）
├── CONSUMER.md         ← 消费者指南：怎么用 query.py 查询并给建议
├── query.py            ← knowledge 查询工具（消费者用）
├── verify.py           ← 完整性校验/增量 diff 工具（生产者用）
├── knowledge/          ← 产物存放处, 按 <version>/<module>/{README,playbook,summary,tunable,readonly} + 全局 vocab.json
├── scan_recipes/       ← 工具 + recipe
│   ├── README.md       ← scan.py 工具手册（用法、工单列说明）
│   ├── scan.py         ← recipe → 参数工单的转码器
│   └── <module>.json   ← 各子系统 recipe（如 sched.json）
└── .semcode.db/        ← 内核源码语义索引（供高级查询）
```

knowledge 产物结构（版本在前、模块在后，符合"先按 uname -r 定位再选模块"的消费顺序）：

```
knowledge/
├── vocab.json        ← 跨模块共享领域词汇表(EEVDF/PELT/cache-cold...), 消费者遇到不懂的术语查这里
└── <version>/<module>/
    ├── README.md          ← 模块自我介绍(选型层): 适合什么场景/能观察什么/调优杠杆/边界, 消费者据此选模块
    ├── playbook.json      ← 诊断流程(流程层): capabilities(智能模式推理依据)+rules(规则模式if-then)+thresholds, 三模式
    ├── summary.json      ← 模块级索引: 每条 {kernel_name, category, summary}, 消费者先扫这个
    ├── tunable.json      ← 可调参数完整记录
    └── readonly.json     ← 只读参数完整记录
```

README.md 和 playbook.json 是**可选**产物（老模块或简单模块可不带），verify.py 不校验它们——参数完整性只对 summary/tunable/readonly 负责。新模块（如 sched）建议都带，让消费者能选型+端到端分析。

## 核心约定

- **按版本**：同一参数在不同内核版本可能改了名字、路径、语义或默认值。recipe 和 knowledge 都必须带 `version`（与内核 `Makefile` 的 VERSION/PATCHLEVEL/SUBLEVEL/EXTRAVERSION 对应，如 `v7.1.0-rc5`）。knowledge 按 `knowledge/<version>/<module>/` 存放，版本是第一隔离维度。
- **按模块**：每个子系统（sched / mm / block / net …）一份 recipe + 一份 knowledge，互不交叉。
- **recipe 给维护者、工单给生产者、query 给消费者**：recipe 是结构化/嵌套的维护规格，不适合直接消费；生产者的输入是 `scan.py` 产出的扁平工单；消费者用 `query.py` 按需查询 knowledge，**不直接 cat JSON**。
- **knowledge 三层结构**（模块目录内）：`README.md`=选型层（我适合什么场景，先读它选模块）、`playbook.json`=流程层（诊断方法论+规则，端到端分析时用）、`summary/tunable/readonly.json`=参数层（参数详情）。消费者先 `--modules` 选模块→读模块 README 确认→`--playbook` 拿诊断流程→`--name` 取参数详情。

## 快速开始

- 我是生产者（要建 knowledge / 查漏补缺）→ [`PRODUCER.md`](./PRODUCER.md)（用 `verify.py` 自查完整性、做跨版本增量）
- 我是消费者（要用 knowledge 给建议）→ [`CONSUMER.md`](./CONSUMER.md)
- 我要维护/新增 recipe → [`scan_recipes/README.md`](./scan_recipes/README.md)
