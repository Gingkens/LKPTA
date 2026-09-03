#!/usr/bin/env python3
"""文本级原地替换 consumes 的 file:行号 -> file:符号,不重序列化 JSON。

和 fix_consumes.py 的区别:fix_consumes 用 json.load/dump(会重排版);
本脚本用 fix_consumes.resolve() 算映射,然后对原始文本做精确字符串替换,
保持生产者原本的紧凑数组格式,git diff 只显示真实改值。

用法: python apply_consumes_fix.py [--apply]
"""
import json, sys, os
import fix_consumes as fc

TARGETS = [
    ('sched', 'tunable.json'), ('sched', 'readonly.json'),
    ('net',   'tunable.json'), ('net',   'readonly.json'),
]

def build_mapping():
    """返回 {filepath: {old_ref: new_ref}}"""
    out = {}
    for mod, fn in TARGETS:
        path = f'knowledge/v7.2-rc7/{mod}/{fn}'
        if not os.path.exists(path): continue
        d = json.load(open(path, encoding='utf-8'))
        m = {}
        for it in d.get('items', []):
            for x in it.get('consumes', []) or []:
                if not (isinstance(x, str) and ':' in x): continue
                fp, _, tail = x.rpartition(':')
                if not tail.isdigit(): continue
                sym = fc.resolve(fp, int(tail))
                if sym:
                    new = f"{fp}:{sym}"
                    if new != x:
                        m[x] = new
        if m: out[path] = m
    return out

def apply_text_replace(path, mapping, do_write=False):
    """对 path 文件做文本级精确替换。带引号匹配避免子串误伤。"""
    text = open(path, encoding='utf-8').read()
    applied = 0
    skipped_collision = []
    for old, new in mapping.items():
        # 精确匹配 "old"(带 JSON 字符串引号),避免子串误匹配
        needle = f'"{old}"'
        replacement = f'"{new}"'
        count = text.count(needle)
        if count == 0:
            skipped_collision.append((old, 'not found in text'))
            continue
        text = text.replace(needle, replacement)
        applied += count
    if do_write:
        open(path, 'w', encoding='utf-8').write(text)
    return applied, skipped_collision

def main():
    do_write = '--apply' in sys.argv
    mapping = build_mapping()
    total = 0
    for path, m in mapping.items():
        applied, skipped = apply_text_replace(path, m, do_write=do_write)
        total += applied
        tag = 'wrote' if do_write else 'dry'
        print(f"[{tag}] {path}: replaced {applied} refs")
        for old, why in skipped:
            print(f"    SKIP {old}: {why}")
    print(f"--- total: {total} replacements {'applied' if do_write else 'previewed'}")

if __name__ == '__main__':
    main()
