#!/usr/bin/env python3
"""把 tunable.json 里 consumes 的 `file:行号` 转成 `file:符号`。

根因:producer 抄源码行号当 consumes,但行号跨版本漂移,消费者不能当稳定引用。
规范(PRODUCER §5)要求 `file:符号(函数/数组名,非行号)`。

策略:对每个 `file:line`,用 ctags -x 抓该文件全部符号(每文件缓存一次),
取行号 ≤ target 的最大者作为包含符号。ctags 对带属性声明(`struct X name __attr = {`)
会把属性词当符号名 → 兜底用启发式从源码往上找 `^type name` 定义。

用法:
  python fix_consumes.py knowledge/v7.2-rc7/<mod>/tunable.json   # 改写
  python fix_consumes.py knowledge/v7.2-rc7/<mod>/tunable.json -n # dry-run
  python fix_consumes.py -a                                       # 全模块
"""
import json, os, re, subprocess, sys, bisect
from collections import defaultdict

KROOT = "/home/lgk/linux"

def _ctags_symbols(filepath):
    """返回 [(line, name)] 按行升序。ctags 解析带属性声明会出错名,留 name 备兜底判别。"""
    try:
        out = subprocess.run(
            ["ctags", "-x", "--language-force=c", filepath],
            capture_output=True, text=True, cwd=KROOT, timeout=30,
        ).stdout
    except Exception:
        return []
    syms = []
    for ln in out.splitlines():
        parts = ln.split(None, 3)
        if len(parts) < 3: continue
        name, kind, line_s = parts[0], parts[1], parts[2]
        if not line_s.isdigit(): continue
        syms.append((int(line_s), name, kind))
    syms.sort(key=lambda t: t[0])
    return syms

_ctags_cache = {}
def symbols_for(filepath):
    if filepath not in _ctags_cache:
        _ctags_cache[filepath] = _ctags_symbols(os.path.join(KROOT, filepath))
    return _ctags_cache[filepath]

_ATTR_NOISE = {"__cacheline_aligned","__read_mostly","__aligned","__init","__initdata",
               "__read_mostly_section","__rcu","__percpu","__packed","__cold",
               "__always_inline","__noinline","__read_mostly_aligned","__bpf"}
_TYPE_WORDS = {"static","extern","const","volatile","unsigned","signed","register",
               "inline","struct","enum","union","void","int","long","char","short",
               "bool","size_t","u8","u16","u32","u64","s8","s16","s32","s64","__u32",
               "__u64","atomic_t","atomic_long_t","DEFINE_RATELIMIT_STATE","DEFINE_*"}

def _extract_name(line):
    """从一行定义里提取被定义的符号名。
    处理:int name __attr = val;  / struct X name __attr = {;  / type arr[] = {; /
          DEFINE_MACRO(name, ...);  / type func(...) {  / type name(...) ;"""
    l = line.rstrip()
    # 1) 宏调用定义: DEFINE_XXX(name, ...)  → name 是第一个参数
    m = re.match(r'^\s*(?:#\s*define\s+)?DEFINE_\w+\s*\(\s*(\w+)', l)
    if m: return m.group(1)
    # 2) 函数定义: ... name(args) {   (非声明:不以 ; 结尾,或以 { 结尾)
    m = re.match(r'^[\w\s\*]*?\b(\w+)\s*\([^;]*\)\s*\{?\s*$', l)
    if m and ('(' in l) and not l.rstrip().endswith(';'):
        cand = m.group(1)
        if cand not in _TYPE_WORDS and cand not in _ATTR_NOISE:
            return cand
    # 3) 变量/数组/结构体定义: 取所有标识符,丢掉类型词和属性词,第一个剩下的
    #    形如: int bpf_jit_harden __read_mostly;  → bpf_jit_harden
    #          struct net_hotdata net_hotdata __cacheline_aligned = {  → net_hotdata
    #          static struct ctl_table foo[] = {  → foo
    #          long bpf_jit_limit __read_mostly = X;  → bpf_jit_limit
    # 去掉数组下标和初始化值,只留声明部分
    decl = l.split('=')[0]  # 等号左边
    decl = decl.split('{')[0]
    # 找出所有标识符
    toks = re.findall(r'[A-Za-z_]\w*', decl)
    # 去掉类型词、属性词、指针星号词;第一个剩下的就是变量名
    # 但要跳过开头的 storage/类型词
    cands = [t for t in toks if t not in _TYPE_WORDS and t not in _ATTR_NOISE]
    # struct/enum/union 后第一个标识符是 tag,第二个才是 name
    # 简单处理:如果有 struct/enum/union,取 tag 之后第一个
    if re.search(r'\b(struct|enum|union)\b', decl):
        # toks: [static, struct, net_hotdata, net_hotdata, __attr] → 去类型属性后 [net_hotdata, net_hotdata]
        # 第一个是 tag 第二个是 name;若 tag==name 取第二个
        # 但若只有 struct X { ... } X; 这种 typedef 风格也 OK
        # 取 cands 里 tag 之后第一个不等于 tag 的
        for t in cands:
            # 跳过 tag 本身(第一个出现的)
            pass
        # 实操:取最后一个 cand(变量名通常在属性词之前,tag 之后,即中间位置)
        # 但 cands 已过滤属性词,对 'struct X X __attr' → [X, X],取 [1] 即 name
        if len(cands) >= 2:
            return cands[1] if cands[0] == cands[1] else cands[0]
        return cands[0] if cands else None
    if cands:
        # 普通变量 'int name __attr' → cands=[name]; 'int name' → [name]
        return cands[0]
    return None

def _strip_comments(line):
    """去掉行内注释 /* ... */ 和 // ...(跨行注释按单行近似:剥 /* 到行尾)。"""
    l = re.sub(r'/\*.*?\*/', '', line)   # 闭合的 /* ... */
    l = re.sub(r'/\*.*$', '', l)         # 未闭合的跨行注释开头 /* ...
    l = re.sub(r'//.*$', '', l)          # // 行尾注释
    return l.rstrip()

def _is_init_item(line):
    """初始化项行:以 . 开头(字段赋值)、或纯缩进表达式(数组元素),
    不是定义行。"""
    s = line.lstrip()
    if s.startswith('.'): return True              # .field = val,
    if re.match(r'^\[\d+\]\s*=', s): return True    # [n] = ...,
    return False

def _is_var_decl(line):
    """变量声明行:行首是 storage/类型词 或 struct/enum/union。
    用于区分 'int x = (1<<12);'(声明) vs 'foo(x);'(调用语句)。"""
    return re.match(r'^\s*(?:static\s+|extern\s+|const\s+|volatile\s+|unsigned\s+|signed\s+|'
                    r'register\s+|inline\s+|__\w+\s+)*'
                    r'(?:struct\s+|enum\s+|union\s+|'
                    r'(?:void|int|long|short|char|bool|size_t|u8|u16|u32|u64|s8|s16|s32|s64|__u32|__u64|atomic_t|atomic_long_t)\b)',
                    line) is not None

def _is_def_line(line):
    """定义行:含未配平 { (块起始)、或以 ; 结尾的声明、或函数定义头。"""
    l = line.rstrip()
    if '{' in l and '}' not in l: return True           # 块起始 = { 或 ) {
    if re.search(r'\)\s*\{?\s*$', l) and not l.endswith(';'): return True  # 函数头
    if l.endswith(';') and _is_var_decl(l): return True  # 变量声明
    if re.match(r'^\s*DEFINE_\w+\s*\(', l) and l.endswith(';'): return True  # 宏定义
    if re.match(r'^\s*#\s*define\s+', l): return True   # #define
    return False

def _heuristic_symbol(filepath, target_line):
    """ctags 出错时的兜底:从 target 行往上找最近的顶层定义行,提取被定义符号名。
    也能处理"target 行落在文件级结构体/数组初始化块内"——往上扫到块的
    起始定义行(含未配平 { 的行),从那里提取变量/结构体名。"""
    full = os.path.join(KROOT, filepath)
    if not os.path.exists(full): return None
    try:
        lines = open(full, errors="replace").read().splitlines()
    except Exception:
        return None
    depth = 0
    start = min(target_line-1, len(lines)-1)
    for i in range(start, -1, -1):
        raw = lines[i]
        if not raw.strip(): continue
        # 跳过纯注释行(整行注释)
        if re.match(r'^\s*(\*|/\*|\*/|//|#)', raw): continue
        l = _strip_comments(raw).rstrip()
        if not l: continue
        opens = l.count('{') - l.count('}')
        # 若该行是定义行(块起始/声明),提取名
        if _is_def_line(l):
            name = _extract_name(l)
            if name and re.match(r'^[A-Za-z_]\w*$', name) and name not in _ATTR_NOISE:
                return name
        depth += opens
        if depth < 0:  # 跳出外层块了还没找到 → 放弃
            break
    return None

def resolve(filepath, line):
    syms = symbols_for(filepath)
    if syms:
        # 二分找 ≤ line 的最大符号
        idx = bisect.bisect_right([s[0] for s in syms], line) - 1
        if idx >= 0:
            ln, name, kind = syms[idx]
            if name not in _ATTR_NOISE and re.match(r'^[A-Za-z_]\w*$', name):
                return name
    # ctags 失败或名字是属性噪声 → 启发式
    h = _heuristic_symbol(filepath, line)
    if h and h not in _ATTR_NOISE and re.match(r'^[A-Za-z_]\w*$', h):
        return h
    return None  # 实在解不出 → 保留原值(避免改成错名)

def fix_file(path, dry_run=False):
    d = json.load(open(path, encoding="utf-8"))
    items = d.get("items", [])
    stats = {"total":0, "fixed":0, "unchanged":0, "failed":0}
    changes = []
    for it in items:
        c = it.get("consumes")
        if not isinstance(c, list): continue
        new_c = []
        for x in c:
            if not (isinstance(x,str) and ":" in x): new_c.append(x); continue
            stats["total"] += 1
            fp, _, tail = x.rpartition(":")
            if not tail.isdigit(): new_c.append(x); stats["unchanged"]+=1; continue
            sym = resolve(fp, int(tail))
            if sym:
                new_x = f"{fp}:{sym}"
                if new_x != x:
                    stats["fixed"]+=1
                    changes.append((it.get("kernel_name",""), x, new_x))
                else:
                    stats["unchanged"]+=1
                new_c.append(new_x)
            else:
                stats["failed"]+=1
                new_c.append(x)
        it["consumes"] = new_c
    if not dry_run:
        json.dump(d, open(path,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    return stats, changes

def main():
    args = sys.argv[1:]
    dry = "-n" in args or "--dry" in args
    args = [a for a in args if a not in ("-n","--dry")]
    if args and args[0] in ("-a","--all"):
        targets = []
        for mod in ["sched","mm","net","io","irq"]:
            for v in os.listdir("knowledge"):
                p = f"knowledge/{v}/{mod}/tunable.json"
                if os.path.exists(p): targets.append((p,mod,v))
    else:
        targets = [(p,"","") for p in args]
    for p,mod,ver in targets:
        if not os.path.exists(p): print(f"skip(missing): {p}"); continue
        s, ch = fix_file(p, dry_run=dry)
        tag = "[dry]" if dry else "[fix]"
        print(f"{tag} {p}: total={s['total']} fixed={s['fixed']} unchanged={s['unchanged']} failed={s['failed']}")
        for kn,old,new in ch[:8]:
            print(f"    {kn}: {old} -> {new}")
        if len(ch)>8: print(f"    ... +{len(ch)-8} more")

if __name__ == "__main__":
    main()
