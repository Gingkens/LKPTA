#!/usr/bin/env python3
"""文本级原地给 path 含 <name>/<pid> 的 item 补 per_cgroup/per_pid 标志。

不重序列化 JSON。对每个目标 item,在其 "path": "..." 行后紧接着插一行
"per_cgroup": true, 或 "per_pid": true,(若该 item 还没此字段)。

从文件末尾往前处理,避免插入改变后续行号。

用法: python apply_per_flags.py [--apply]
"""
import json, sys, os, re

TARGETS = [
    ('sched', 'tunable.json'), ('sched', 'readonly.json'),
    ('mm',    'tunable.json'), ('mm',    'readonly.json'),
    ('net',   'tunable.json'), ('net',   'readonly.json'),
    ('io',    'tunable.json'), ('io',    'readonly.json'),
    ('irq',   'tunable.json'), ('irq',   'readonly.json'),
]

def process(path, do_write):
    """对单文件:找所有需补 flag 的 item,从后往前插。"""
    d = json.load(open(path, encoding='utf-8'))
    # 收集 (path_value, flag) 列表
    targets = []
    for it in d.get('items', []):
        pv = it.get('path', '')
        need_cgroup = '<name>' in pv and not it.get('per_cgroup')
        need_pid = '<pid>' in pv and not it.get('per_pid')
        if need_cgroup: targets.append((pv, 'per_cgroup'))
        if need_pid:   targets.append((pv, 'per_pid'))
    if not targets:
        return 0
    text = open(path, encoding='utf-8').read()
    lines = text.split('\n')
    # 找每个 path_value 在文件中的行号(精确匹配 "path": "..." 形式)
    # 从后往前插
    # 先建 path_value -> 需加 flags 的映射(一个 path 可能需加两个)
    from collections import defaultdict
    pv_flags = defaultdict(list)
    for pv, flag in targets:
        pv_flags[pv].append(flag)
    inserted = 0
    for pv, flags in pv_flags.items():
        needle = f'"path": "{pv}"'
        # 找所有出现位置(从后往前)
        positions = []
        for i, l in enumerate(lines):
            if needle in l:
                positions.append(i)
        if not positions:
            print(f"  WARN: path not found in text: {pv}")
            continue
        for pos in sorted(positions, reverse=True):
            # 在 pos 行后插 flag 行。缩进取该行前导空白
            indent = re.match(r'^(\s*)', lines[pos]).group(1)
            # 该 item 可能已有 per_cgroup/per_pid 在别处,二次检查
            # 检查紧邻若干行是否已含
            nearby = '\n'.join(lines[pos:pos+3])
            for flag in flags:
                if f'"{flag}"' in nearby:
                    continue
                # 插入位置:紧接 path 行之后。注意逗号:path 行末有逗号
                new_line = f'{indent}"{flag}": true,'
                lines.insert(pos+1, new_line)
                inserted += 1
    if do_write:
        open(path, 'w', encoding='utf-8').write('\n'.join(lines))
    return inserted

def main():
    do_write = '--apply' in sys.argv
    total = 0
    for mod, fn in TARGETS:
        path = f'knowledge/v7.2-rc7/{mod}/{fn}'
        if not os.path.exists(path): continue
        n = process(path, do_write)
        if n:
            print(f"[{'wrote' if do_write else 'dry'}] {path}: +{n} flags")
            total += n
    print(f"--- total: {total} flags {'applied' if do_write else 'previewed'}")

if __name__ == '__main__':
    main()
