#!/usr/bin/env python3
"""文本级原地替换 sched tunable.json 的非规范 unit 值,不重序列化 JSON。

unit 归一到 canonical vocab(PRODUCER §5)。MB/s 非标保留并补 version_notes
标注"非标准单位"。

用法: python apply_unit_fix.py [--apply]
"""
import json, sys, os

PATH = 'knowledge/v7.2-rc7/sched/tunable.json'

# 旧 unit 值 -> 新 unit 值(纯值替换,保持其余字节)
RULES = {
    '个任务/次': 'count',
    '0..1024': 'count',
    '1..10000': 'count',
    'int（调度域层级，-1=不限制）': 'count',
    'flag': 'trigger',
    'flag (出现即生效，无参数)': 'trigger',
    'deprecated': 'cmdline',
    'nice (-20..19)': 'count',
    'bool/int': 'bool',
    'text': 'interface',
    'uclamp (0..1024)': 'count',
    'uclamp (0..SCHED_CAPACITY_SCALE=1024)': 'count',
    'uclamp percent (0..100, max=不钳位)': '%',
    'bool (0|1)': 'bool',
    'bool (boot-only, =0/=1)': 'bool',
    'bool（写 NAME 启用，写 NO_NAME 关闭）': 'bool',
    'enum: none voluntary full lazy': 'enum: none|voluntary|full|lazy',
    'enum: none / voluntary / full / lazy': 'enum: none|voluntary|full|lazy',
    'enum(0/1/2)': 'enum: 0|1|2',
    'enum: 0=disabled 1=normal 2|=memory_tiering 3=normal|tiering': 'enum: 0=disabled|1=normal|2=memory_tiering|3=normal|tiering',
    'enum: enable|disable': 'enum: enable|disable',
    'ms (>=0)': 'ms',
    '格式：<quota_us> <period_us>；quota 可写 max': 'cmdline',
    'cmdline：isolcpus=[子参数,]cpulist': 'cmdline',
    'cmdline：nohz_full=cpulist': 'cmdline',
}

# 非标保留需补 version_notes 的(unit 改值后,给对应 item 补 note)
# 这里 unit MB/s 保留不变,只补 vn。用 Edit 单独处理,本脚本只做 unit 值替换。

def main():
    do_write = '--apply' in sys.argv
    text = open(PATH, encoding='utf-8').read()
    applied = 0
    for old, new in RULES.items():
        if old == new: continue
        needle = f'"unit": "{old}"'
        replacement = f'"unit": "{new}"'
        count = text.count(needle)
        if count:
            text = text.replace(needle, replacement)
            applied += count
            print(f"  {old!r} -> {new!r}  ({count} 处)")
    if do_write:
        open(PATH, 'w', encoding='utf-8').write(text)
    print(f"--- {applied} replacements {'applied' if do_write else 'previewed'}")

if __name__ == '__main__':
    main()
