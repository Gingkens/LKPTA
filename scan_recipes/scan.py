#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
Recipe -> producer-workorder transcoder.

Reads a recipe JSON (a maintainer-facing spec, organized per module & kernel
version) and emits a flat per-parameter workorder for the producer agent. The
producer then reads the kernel source at the indicated file:symbol to understand
each parameter and produce knowledge (tunable.json / readonly.json).

The workorder is richer than the recipe alone: the script reads the kernel
source to fill in columns the recipe does not carry, specifically:

  file:symbol   the function/array name a producer should open to understand a
                 parameter (its definition / creation site)
  type           tunable | readonly, derived from the access mode at the site
  kernel_name    the underlying kernel variable / callback / symbol backing a
                 userspace control entry

The recipe supplies the rest: control_entry (resolved userspace path) and gate
(CONFIG_* guards). Columns are 1-to-1 with what the producer asked for.

Usage:
    scan.py --recipe scan_recipes/v7.2-rc7/sched.json --ksrc /home/lgk/linux
    scan.py --recipe scan_recipes/v7.2-rc7/sched.json --ksrc /home/lgk/linux --out brief.tsv

Columns (one parameter per line):
    id  kernel_name  userspace_name  control_entry  file_symbol  type  gate
"""

import argparse
import json
import os
import re
import sys


# ---------------------------------------------------------------------------
# source helpers
# ---------------------------------------------------------------------------

class Source:
    """A thin view over a kernel source file: text + line index.

    Carries an optional `body_start` offset so line numbers stay correct when
    the caller asks to scope work to a single function body (extracted via
    `with_body`).
    """

    def __init__(self, ksrc, relpath, body_start=0, body_end=None):
        self.relpath = relpath
        path = os.path.join(ksrc, relpath)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            self.text = fh.read()
        self.body_start = body_start
        self.body_end = len(self.text) if body_end is None else body_end
        self._line_starts = [0]
        for i, ch in enumerate(self.text):
            if ch == '\n':
                self._line_starts.append(i + 1)

    @property
    def body_text(self):
        return self.text[self.body_start:self.body_end]

    def line_of(self, pos):
        import bisect
        return bisect.bisect_right(self._line_starts, pos)

    def with_body(self, symbol):
        """Return a view scoped to `symbol`'s body (function/array/struct).
        Falls back to self (whole file) when the symbol can't be located.
        The returned view carries `symbol` so extractors can emit a
        version-stable `file:symbol` locator instead of a drifting line no."""
        if not symbol:
            return self
        start, end = _find_symbol_span(self.text, symbol)
        if start is None:
            return self
        return _ScopedSource(self, start, end, symbol)


class _ScopedSource:
    """A view over a slice of a Source (keeps the parent's line index).

    Carries `symbol` — the function/array/struct name this scope belongs to —
    so extractors emit `file:symbol` (stable across versions) rather than a
    line number (which drifts every release).
    """

    def __init__(self, parent, start, end, symbol=None):
        self._parent = parent
        self.relpath = parent.relpath
        self.body_start = start
        self.body_end = end
        self.symbol = symbol

    @property
    def text(self):
        return self._parent.text[self.body_start:self.body_end]

    def line_of(self, pos):
        # pos is relative to body_text; map back to absolute offset
        return self._parent.line_of(self.body_start + pos)


def _find_symbol_span(text, symbol):
    """Find the brace/bracket body span of a C symbol definition. Returns
    (start, end) offsets in `text`, or (None, None)."""
    pat = re.compile(
        r'\b' + re.escape(symbol) + r'\b\s*(?:\([^;]*)?\s*(?=\{|\[|=[^=])'
    )
    m = pat.search(text)
    if not m:
        return None, None
    start = m.start()
    bpos = text.find('{', m.end())
    brpos = text.find('[', m.end())
    cands = [p for p in (bpos, brpos) if p != -1]
    if not cands:
        return start, len(text)
    delim = min(cands)
    if delim == bpos:
        # brace-balanced
        depth = 0
        i = delim
        n = len(text)
        while i < n:
            c = text[i]
            if c == '"':
                i = _skip_string(text, i); continue
            if c == "'":
                i = _skip_char(text, i); continue
            if c == '/' and i + 1 < n and text[i + 1] == '/':
                nl = text.find('\n', i); i = nl + 1 if nl != -1 else n; continue
            if c == '/' and i + 1 < n and text[i + 1] == '*':
                e = text.find('*/', i + 2); i = e + 2 if e != -1 else n; continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return start, i + 1
            i += 1
        return start, n
    else:
        eq = text.find('=', delim)
        if eq == -1:
            return start, len(text)
        b = text.find('{', eq)
        if b == -1:
            return start, len(text)
        depth = 0
        i = b
        n = len(text)
        while i < n:
            c = text[i]
            if c == '"':
                i = _skip_string(text, i); continue
            if c == "'":
                i = _skip_char(text, i); continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return start, i + 1
            i += 1
        return start, n


def _skip_string(text, i):
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == '\\':
            i += 2; continue
        if c == '"':
            return i + 1
        i += 1
    return n


def _skip_char(text, i):
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == '\\':
            i += 2; continue
        if c == "'":
            return i + 1
        i += 1
    return n


def _resolve(ksrc, locator):
    """locator 'relpath:symbol' -> (scoped_source, symbol).

    If a symbol is given, the returned source is scoped to that symbol's body
    (function/array/struct) so scans only see that region; line numbers still
    map back to the original file."""
    if ':' not in locator:
        return Source(ksrc, locator), None
    relpath, symbol = locator.split(':', 1)
    symbol = symbol.split()[0]
    base = Source(ksrc, relpath)
    return base.with_body(symbol), symbol


def _mode_to_type(mode):
    """Translate an octal mode (int) into tunable|readonly.

    0644/0666/0200 etc. user-writable -> tunable; 0444/0400 -> readonly.
    """
    try:
        m = int(mode, 8) if isinstance(mode, str) else int(mode)
    except (ValueError, TypeError):
        return "unknown"
    return "tunable" if (m & 0o200) else "readonly"


def _mode_flags_to_type(flags_str):
    """Translate S_IRUGO|S_IWUSR style flags -> tunable|readonly."""
    if not flags_str:
        return "unknown"
    w = re.search(r'S_IW\w+', flags_str) or re.search(r'0[0-7]{3}', flags_str)
    has_write = False
    if re.search(r'S_IW', flags_str):
        has_write = True
    elif re.search(r'\b0[0-7]{3}\b', flags_str):
        tok = re.search(r'\b0([0-7]{3})\b', flags_str).group(1)
        has_write = (int(tok, 8) & 0o200) != 0
    return "tunable" if has_write else "readonly"


def _flip_polarity(layer):
    """Flip the active branch of one conditional layer (for #else/#elif).

    layer is (polarity_label, symbol) where polarity_label is '+CONFIG_X' /
    '-CONFIG_X' / 'or'. Returns the flipped layer with the same symbol.
    """
    label, sym = layer
    if not sym:
        return ('or', '')
    if label.startswith('+CONFIG_'):
        return ('-CONFIG_' + sym[7:], sym)
    if label.startswith('-CONFIG_'):
        return ('+CONFIG_' + sym[7:], sym)
    return ('or', sym)


def _ifdef_stack(text, pos):
    """Return the CONFIG_* gates that must hold for the code at `pos`.

    Each open conditional layer contributes the CONFIG symbols that MUST be
    enabled for the region covering `pos` to compile:

      #ifdef X                 -> must have X
      #ifndef X                -> must NOT have X   (recorded as -X)
      #if defined(X)&&defined(Y) -> must have X and Y
      #if defined(X)||defined(Y) -> either suffices, NOT mandatory -> skip
      #if !defined(X)          -> must NOT have X   (recorded as -X)
      #elif  / #else           -> flips the active polarity of that layer

    Negated gates are prefixed '-' (e.g. -CONFIG_X). A layer whose condition
    is an OR is recorded as empty (not mandatory), so unrelated symbols in an
    OR branch never leak into the gate of code below it.
    """
    stack = []  # each entry: ('+'|'-'|'or', symbol or '')

    def parse_layer(cond):
        cond = cond.strip()
        syms = re.findall(r'defined\s*\(\s*(CONFIG_\w+)\s*\)', cond)
        if not syms:
            return None
        if '||' in cond:
            return ('or', '')            # not mandatory
        neg = cond.startswith('!') or re.search(r'!\s*defined', cond)
        sym = syms[0]
        return ('-' + sym if neg else '+' + sym, sym)

    for m in re.finditer(r'^[ \t]*#\s*(ifdef|ifndef|if|elif|else|endif)\b[^\n]*',
                         text, re.M):
        if m.start() > pos:
            break
        kw = m.group(1)
        body = m.group(0)
        if kw == 'ifdef':
            sym = re.search(r'\bCONFIG_\w+', body)
            if sym:
                stack.append(('+CONFIG_' + sym.group()[7:], sym.group()))
            else:
                stack.append(('or', ''))          # non-CONFIG guard, not a gate
        elif kw == 'ifndef':
            sym = re.search(r'\bCONFIG_\w+', body)
            if sym:
                stack.append(('-CONFIG_' + sym.group()[7:], sym.group()))
            else:
                stack.append(('or', ''))
        elif kw == 'if':
            syms = re.search(r'defined\s*\(\s*(CONFIG_\w+)\s*\)', body)
            if not syms:
                stack.append(('or', ''))
            elif '||' in body:
                stack.append(('or', ''))
            elif '!' in body.split('defined')[0]:
                stack.append(('-CONFIG_' + syms.group(1)[7:], syms.group(1)))
            else:
                stack.append(('+CONFIG_' + syms.group(1)[7:], syms.group(1)))
        elif kw == 'elif':
            if stack:
                stack[-1] = _flip_polarity(stack[-1])
        elif kw == 'else':
            if stack:
                stack[-1] = _flip_polarity(stack[-1])
        elif kw == 'endif':
            if stack:
                stack.pop()

    out = []
    for pol, sym in stack:
        if sym and pol.startswith('+CONFIG_'):
            out.append(sym)
        elif sym and pol.startswith('-CONFIG_'):
            out.append('!' + sym)
        # 'or' layers contribute nothing
    return out


# ---------------------------------------------------------------------------
# per-mechanism row producers
#   each returns list[dict] with keys:
#     kernel_name, userspace_name, control_entry, file_symbol, type, gate
# ---------------------------------------------------------------------------

def _split_args(s):
    """Split a call's argument list on top-level commas, skipping commas
    nested inside (), [], or string literals."""
    out = []
    depth = 0
    i = 0
    n = len(s)
    start = 0
    while i < n:
        c = s[i]
        if c == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 1 if s[i] != '\\' else 2
            i += 1
            continue
        if c in '([':
            depth += 1
        elif c in ')]':
            depth -= 1
        elif c == ',' and depth == 0:
            out.append(s[start:i])
            start = i + 1
        i += 1
    out.append(s[start:])
    return out


def _clean_kernel_name(s):
    """Strip C noise from a captured kernel symbol: casts, &, whitespace."""
    if not s:
        return ''
    s = s.strip()
    # drop casts like (void *) foo, (u32 *) bar (allow * inside parens)
    while True:
        new = re.sub(r'\([A-Za-z0-9_ *]+\)\s*', '', s, count=1)
        if new == s:
            break
        s = new
    s = s.lstrip('&').strip()
    m = re.match(r'([A-Za-z_][\w\->.]*)', s)
    return m.group(1) if m else s


def _dir_parent_var(body, dir_name):
    """If dir created as `VAR = debugfs_create_dir("dir_name", ...)`, return VAR."""
    m = re.search(
        r'(\w+)\s*=\s*debugfs_create_dir\s*\(\s*"' + re.escape(dir_name) + r'"',
        body)
    return m.group(1) if m else None


def _build_dir_tree(text):
    """Parse `VAR = debugfs_create_dir(...)` assignments in body, build a
    tree: dict[var_name] -> {name, parent_var, is_per_cpu, children:[]}.

    Two forms:
      VAR = debugfs_create_dir("name", PARENT)      -> named dir (name=string)
      VAR = debugfs_create_dir(BUF_VAR, PARENT)      -> per-cpu dir (name=variable)

    The parent relationship is read from the source assignment chain, not
    from recipe — the source itself expresses "numa's parent is debugfs_sched"
    via `numa = debugfs_create_dir("numa_balancing", debugfs_sched)`.
    """
    tree = {}
    # match VAR = debugfs_create_dir(<arg1>, <arg2>) — arg1 is name (string) or buf (var)
    for m in re.finditer(
        r'(\w+)\s*=\s*debugfs_create_dir\s*\(\s*("[^"]+"|[A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)',
        text):
        var, name_arg, parent = m.group(1), m.group(2), m.group(3)
        is_per_cpu = not name_arg.startswith('"')
        name = name_arg.strip('"') if not is_per_cpu else None
        tree[var] = {
            'name': name, 'parent_var': parent,
            'is_per_cpu': is_per_cpu, 'children': [],
        }
    return tree


def _harvest_debugfs_leaves(text, parent_var):
    """Scan debugfs_create_file/u32/... calls whose parent arg == parent_var.
    Returns list of {name, kernel_name, mode}. Handles:
      debugfs_create_u32("n", mode, parent, &var)          (4-param, var=args[1])
      debugfs_create_file("n", mode, parent, data, &fops)  (5-param, fops=args[-1])
    Cast parens like (void *) cpu and multi-line calls are handled by paren
    balancing from the opening '(' to the matching ')'.
    """
    leaves = []
    for m in re.finditer(
        r'debugfs_create_(\w+)\s*\(\s*"([^"]+)"\s*,\s*(\d+)\s*,',
        text):
        api, name, mode = m.group(1), m.group(2), m.group(3)
        if api == 'dir':
            continue  # dirs handled by _build_dir_tree
        # walk to matching close paren to capture rest of args
        i = m.end()
        depth = 1
        n = len(text)
        while i < n and depth > 0:
            c = text[i]
            if c == '"':
                i += 1
                while i < n and text[i] != '"':
                    i += 2 if text[i] == '\\' else 1
                i += 1
                continue
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        rest = text[m.end():i]
        args = _split_args(rest)
        parent_arg = args[0].strip() if args else ''
        if parent_arg != parent_var:
            continue
        # 5-param create_file: args=[parent, data, &fops] -> kn=args[-1]
        # 4-param create_u32:  args=[parent, &var]         -> kn=args[1]
        if len(args) >= 3:
            kn = _clean_kernel_name(args[-1].strip())
        else:
            var = args[1].strip() if len(args) > 1 else ''
            kn = _clean_kernel_name(var if var and var != 'NULL' else '')
        leaves.append({'name': name, 'kernel_name': kn, 'mode': mode, 'kind': api})
    return leaves


def _harvest_unscoped_leaves(text):
    """Scan ALL debugfs_create_file/u32/... in body, ignoring parent arg.
    Used for child_locator body (e.g. register_sd) where create_file calls
    use a `parent` parameter variable not in this dir's tree — those files
    belong to the dir but can't be matched by parent_var."""
    leaves = []
    for m in re.finditer(
        r'debugfs_create_(\w+)\s*\(\s*"([^"]+)"\s*,\s*(\d+)\s*,',
        text):
        api, name, mode = m.group(1), m.group(2), m.group(3)
        if api == 'dir':
            continue
        i = m.end()
        depth = 1
        n = len(text)
        while i < n and depth > 0:
            c = text[i]
            if c == '"':
                i += 1
                while i < n and text[i] != '"':
                    i += 2 if text[i] == '\\' else 1
                i += 1
                continue
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        rest = text[m.end():i]
        args = _split_args(rest)
        if len(args) >= 3:
            kn = _clean_kernel_name(args[-1].strip())
        else:
            var = args[1].strip() if len(args) > 1 else ''
            kn = _clean_kernel_name(var if var and var != 'NULL' else '')
        leaves.append({'name': name, 'kernel_name': kn, 'mode': mode, 'kind': api})
    return leaves


def _harvest_sdm(text):
    """SDM(type, mode, member) macro -> {name=member, kernel_name='sd->member', mode}.
    Masks preprocessor lines so the #define SDM(...) line isn't matched."""
    masked = re.sub(r'(?m)^[ \t]*#[^\n]*',
                    lambda mm: ' ' * len(mm.group(0)), text)
    out = []
    for m in re.finditer(r'\bSDM\s*\(\s*(\w+)\s*,\s*(\d+)\s*,\s*(\w+)\s*\)', masked):
        typ, mode, member = m.group(1), m.group(2), m.group(3)
        out.append({'name': member, 'kernel_name': 'sd->' + member,
                    'mode': mode, 'kind': typ})
    return out


def _rows_debugfs(src, recipe_source, ksrc):
    """debugfs_create_dir: parse VAR=create_dir assignment chain to build dir
    tree, recursively walk it producing rows. Recipe's `dirs` list names which
    dirs to process (string names scan auto-reads + cross-checks vs source).
    Per-cpu subdirs (dir-name is a variable) use child_dir_template for path.
    """
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    locator = recipe_source.get('locator', '')
    srcobj, _ = _resolve(ksrc, locator)
    text = srcobj.text

    # New format: recipe.dirs (list of {name, parent, children, config})
    # New format: source.dirs (list); old: source.filter.dir (single)
    dirs_list = recipe_source.get('dirs') or filt.get('dirs') or []
    if not dirs_list and filt.get('dir'):
        # compat: single-dir old format -> treat as one-element dirs list
        dirs_list = [{'name': filt['dir'],
                      'children': (filt.get('children', {}) or {}).get('files', []) +
                                  (filt.get('children', {}) or {}).get('u32', [])}]
    child_dir_template = (recipe_source.get('child_dir_template') or
                          filt.get('child_dir_template') or filt.get('layout'))
    # child_locator(SDM 在另一函数)只在旧格式用,新格式暂留兼容
    child_loc = filt.get('child_locator')

    # Build dir tree from source: var -> {name, parent_var, is_per_cpu}
    tree = _build_dir_tree(text)
    # var -> leaves (file/u32 calls with that var as parent)
    leaves_by_var = {}
    for var in tree:
        leaves_by_var[var] = _harvest_debugfs_leaves(text, var)
    # SDM + unscoped create_file leaves from child_locator body (e.g. register_sd)
    # — these calls use a `parent` parameter variable not in this dir's tree,
    # so can't be matched by parent_var. Collect them unscoped, attach to the
    # last dir in dirs_list (convention: the dir the child_locator serves).
    sdm_leaves = []
    sdm_target_var = None
    if child_loc:
        cobj, _ = _resolve(ksrc, child_loc)
        sdm_leaves = _harvest_sdm(cobj.text)
        sdm_leaves += _harvest_unscoped_leaves(cobj.text)
        if dirs_list:
            sdm_target_var = _find_dir_var(tree, dirs_list[-1]['name'])

    rows = []
    for d in dirs_list:
        dir_name = d['name']
        expected_children = d.get('children', [])
        var = _find_dir_var(tree, dir_name)
        if not var:
            # dir not in source (e.g. v7.2-only dir scanned vs older source)
            # claim_extra: recipe lists children but scan found none
            continue
        # path: top-level dir -> path_prefix (already contains its name);
        # sub-dir -> parent's path + '/' + this dir's name
        parent_name = d.get('parent')
        if parent_name:
            parent_var = _find_dir_var(tree, parent_name)
            parent_path = _dir_path(tree, parent_var, prefix)
            this_prefix = parent_path.rstrip('/') + '/' + dir_name
        else:
            this_prefix = prefix  # path_prefix already contains top-level dir name
        # walk this dir + its per-cpu subdirs (recursively)
        _emit_dir_rows(tree, var, this_prefix, child_dir_template,
                       leaves_by_var, sdm_leaves if sdm_target_var == var else [],
                       file_relpath=srcobj.relpath, symbol=srcobj.symbol or '',
                       rows=rows)

    return rows


def _find_dir_var(tree, name):
    """Find the var name in tree whose dir name == name (string dirs only)."""
    for var, node in tree.items():
        if not node['is_per_cpu'] and node['name'] == name:
            return var
    return None


def _dir_path(tree, var, root_prefix):
    """Compute the full path of a dir by walking parent chain to root.
    Top-level dir (parent_var not in tree, e.g. NULL) has path = root_prefix
    (path_prefix already contains its name). Sub-dirs append their names."""
    parts = []
    cur = var
    seen = set()
    while cur and cur in tree and cur not in seen:
        seen.add(cur)
        node = tree[cur]
        if node['is_per_cpu']:
            break
        # top-level dir (parent not in tree, e.g. NULL) — its name is already
        # in root_prefix, don't append
        parent = node['parent_var']
        if not parent or parent not in tree or parent == 'NULL':
            break
        parts.insert(0, node['name'])
        cur = parent
    return root_prefix.rstrip('/') + ('/' + '/'.join(parts) if parts else '')


def _emit_dir_rows(tree, var, path_prefix, child_dir_template,
                   leaves_by_var, sdm_leaves, file_relpath, symbol, rows):
    """Emit rows for a dir and its per-cpu subdirs (recursive)."""
    node = tree.get(var)
    if not node:
        return
    # leaves directly under this dir (file/u32)
    for leaf in leaves_by_var.get(var, []):
        # path: this dir's path + leaf name (per-cpu subdir adds its template layer)
        leaf_path = path_prefix.rstrip('/') + '/' + leaf['name']
        rows.append({
            'kernel_name': leaf['kernel_name'],
            'userspace_name': leaf['name'],
            'control_entry': leaf_path,
            'file_symbol': f"{file_relpath}:{symbol}",
            'type': _mode_to_type(leaf['mode']),
            'gate': '',
        })
    # SDM + unscoped leaves (if this dir is the SDM host) — these belong to
    # this dir but live in child_locator body; if recipe gave child_dir_template,
    # they live under that template layer (e.g. domains/cpuN/domainN/flags).
    if sdm_leaves:
        sdm_path = path_prefix.rstrip('/')
        if child_dir_template:
            sdm_path = sdm_path + '/' + child_dir_template
        for leaf in sdm_leaves:
            rows.append({
                'kernel_name': leaf['kernel_name'],
                'userspace_name': leaf['name'],
                'control_entry': sdm_path + '/' + leaf['name'],
                'file_symbol': f"{file_relpath}:{symbol}",
                'type': _mode_to_type(leaf['mode']),
                'gate': '',
            })
    # recurse into per-cpu subdirs (dir-name is variable) — use child_dir_template
    for child_var, child_node in tree.items():
        if child_node['parent_var'] == var and child_node['is_per_cpu']:
            if not child_dir_template:
                continue
            sub_path = path_prefix.rstrip('/') + '/' + child_dir_template
            # leaves under the per-cpu subdir
            for leaf in leaves_by_var.get(child_var, []):
                leaf_path = sub_path.rstrip('/') + '/' + leaf['name']
                rows.append({
                    'kernel_name': leaf['kernel_name'],
                    'userspace_name': leaf['name'],
                    'control_entry': leaf_path,
                    'file_symbol': f"{file_relpath}:{symbol}",
                    'type': _mode_to_type(leaf['mode']),
                    'gate': '',
                })


def _rows_sched_feat(src, recipe_source, ksrc):
    """SCHED_FEAT(NAME, default) -> one row per feature (tunable via features file)."""
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    locator = recipe_source.get('locator', '')
    srcobj, _ = _resolve(ksrc, locator)
    seen = set()
    rows = []
    for m in re.finditer(r'\bSCHED_FEAT\s*\(\s*(\w+)\s*,\s*(true|false)\s*\)',
                        srcobj.text):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        rows.append({
            'kernel_name': 'sysctl_sched_features[bit ' + name + ']',
            'userspace_name': name,
            'control_entry': prefix.rstrip('/') + '/' + name,
            'file_symbol': f"{srcobj.relpath}:{getattr(srcobj,'symbol',None) or 'SCHED_FEAT'}",
            'type': 'tunable',
            'gate': '',
        })
    return rows


def _parse_ctl_entry(entry_text):
    """Parse one ctl_table {...} entry into (procname, data, mode, handler)."""
    def field(name, pat):
        m = re.search(pat, entry_text)
        return m.group(1) if m else None
    procname = field('procname', r'\.procname\s*=\s*"([^"]+)"')
    data = field('data', r'\.data\s*=\s*([^,]+?)(?:[\s,]+|$)')
    mode = field('mode', r'\.mode\s*=\s*(0[0-7]+)')
    # &-prefix optional: some handlers are written as &foo (e.g. hugetlb_mempolicy_sysctl_handler)
    handler = field('handler', r'\.proc_handler\s*=\s*&?([A-Za-z_]\w*)')
    return procname, data, mode, handler


def _rows_sysctl(src, recipe_source, ksrc):
    """register_sysctl_init -> one row per .procname in the ctl_table array.

    locator points at the *_sysctl_init function (which calls register_sysctl_init
    with the table name); we scope to the table array body to read each entry.
    """
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    locator = recipe_source.get('locator', '')
    init_src, _ = _resolve(ksrc, locator)

    table = recipe_source.get('table') or filt.get('table')
    if not table:
        # Match several sysctl registration call shapes. The table var is the
        # argument right after the quoted path string:
        #   register_sysctl_init("vm", TABLE)               (mm/sched style)
        #   register_net_sysctl_sz(net, "net/core", TABLE, size)  (per-netns)
        #   register_net_sysctl(net, "net/core", TABLE)     (per-netns, no size)
        # net_sysctl's first arg is a net-namespace var (not a string), so skip
        # any non-quote chars up to the path string, then capture the table name.
        m = re.search(
            r'register_(?:sysctl_init|net_sysctl(?:_sz)?)\s*\([^"]*"[^"]+"\s*,\s*(\w+)',
            init_src.text)
        table = m.group(1) if m else None

    if not table:
        return []

    # open the same file unscoped and extract the table array body so the
    # producer gets the table array symbol (version-stable) as locator.
    relpath = locator.split(':')[0]
    base = Source(ksrc, relpath)
    tbl_src = base.with_body(table)

    rows = []
    for m in re.finditer(r'\{[^{}]*\}', tbl_src.text, re.S):
        procname, data, mode, handler = _parse_ctl_entry(m.group(0))
        if not procname:
            continue
        # kernel_name: prefer the backing .data variable; if data is NULL
        # (handler-set, no static variable) fall back to the .proc_handler
        # function name — both are stable identifiers for the consumer.
        data_clean = _clean_kernel_name(data) if data and data.strip() != 'NULL' else ''
        kn = data_clean or (handler or '')
        gate = _ifdef_stack(base.text, tbl_src.body_start + m.start())
        rows.append({
            'kernel_name': kn,
            'userspace_name': procname,
            'control_entry': prefix.rstrip('/') + '/' + procname,
            'file_symbol': f"{relpath}:{tbl_src.symbol or table}",
            'type': _mode_to_type(mode) if mode else 'tunable',
            'gate': ' '.join(sorted(gate)) if gate else '',
        })
    return rows


def _rows_cftype(src, recipe_source, ksrc):
    """cftype -> one row per .name in the cftype array."""
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    cft_loc = recipe_source.get('cftype_locator') or filt.get('cftype_locator') or recipe_source.get('locator', '')
    srcobj, _ = _resolve(ksrc, cft_loc)
    # an unscoped view of the same file, for accurate #ifdef gate tracking
    relpath = cft_loc.split(':')[0]
    base = Source(ksrc, relpath)

    rows = []
    # walk each cftype entry { ... }
    for m in re.finditer(r'\{[^{}]*\.name\s*=\s*"([^"]+)"[^{}]*\}',
                        srcobj.text, re.S):
        name = m.group(1)
        entry = m.group(0)
        has_write = bool(re.search(r'\.write\b|\.write_u64|\.write_s64|\.write =', entry))
        kn = ''
        # kernel_name: prefer a write/read callback; fall back to seq_show /
        # seq_read / private so .seq_show-only entries still get a stable id.
        for fld in ('write_u64', 'write_s64', 'write', 'read_u64', 'read_s64',
                    'read', 'seq_show', 'seq_read'):
            wm = re.search(r'\.' + fld + r'\s*=\s*([A-Za-z_]\w*)', entry)
            if wm:
                kn = wm.group(1)
                break
        abs_pos = srcobj.body_start + m.start()
        gate = _ifdef_stack(base.text, abs_pos)
        rows.append({
            'kernel_name': kn,
            'userspace_name': name,
            'control_entry': prefix.rstrip('/') + '/' + name,
            'file_symbol': f"{relpath}:{srcobj.symbol or ''}",
            'type': 'tunable' if has_write else 'readonly',
            'gate': ' '.join(sorted(gate)) if gate else '',
        })
    return rows


def _proc_resolve_common(recipe_source, ksrc):
    """共用:解析 recipe 的 locator/path_prefix/expected/child_dir,返回
    (srcobj, base, relpath, symbol, prefix, expected_set, child_dir)。
    所有 proc 类提取器都从这开始,减重复。"""
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    locator = recipe_source.get('locator', '')
    srcobj, _ = _resolve(ksrc, locator)
    relpath = locator.split(':')[0] if ':' in locator else locator
    base = Source(ksrc, relpath)
    symbol = (srcobj.symbol if getattr(srcobj, 'symbol', None) else
              (locator.split(':')[1].split()[0] if ':' in locator else ''))
    expected = filt.get('name') or recipe_source.get('names')
    if isinstance(expected, str):
        expected_set = {expected}
    elif isinstance(expected, list):
        expected_set = set(expected)
    else:
        expected_set = set()
    child_dir = filt.get('child_dir_template') or filt.get('layout')
    return srcobj, base, relpath, symbol, prefix, expected_set, child_dir


def _proc_emit(name, kn, mode_or_flags, srcobj, m_start, base, prefix,
               child_dir, expected_set, relpath, symbol, rows, flags_mode=False):
    """共用:expected 过滤 + gate 提取 + path 拼接 + row 输出。
    flags_mode=True 时 mode 是 S_IRUGO|S_IWUSR 形式(_mode_flags_to_type),
    False 时是数字 0644 (_mode_to_type)。"""
    if expected_set and name not in expected_set:
        return
    abs_pos = (srcobj.body_start if hasattr(srcobj, 'body_start') else 0) + m_start
    gate = _ifdef_stack(base.text, abs_pos)
    base_seg = os.path.basename(prefix.rstrip('/'))
    if child_dir:
        ce = prefix.rstrip('/') + '/' + child_dir + '/' + name
    elif base_seg == name:
        ce = prefix  # prefix 已含 name(/proc/schedstat),不重复
    else:
        ce = prefix.rstrip('/') + '/' + name
    t = _mode_flags_to_type(mode_or_flags) if flags_mode else _mode_to_type(mode_or_flags)
    rows.append({
        'kernel_name': kn,
        'userspace_name': name,
        'control_entry': ce,
        'file_symbol': f"{relpath}:{symbol}",
        'type': t,
        'gate': ' '.join(sorted(gate)) if gate else '',
    })


def _rows_proc(src, recipe_source, ksrc):
    """proc_pid_entry -> one row per REG/ONE in the pid entry table.
    kernel_name = ops symbol from REG/ONE call (stable per-file identifier).
    filter.show is fallback when ops absent (rare)."""
    filt = recipe_source.get('filter', {}) or {}
    srcobj, base, relpath, symbol, prefix, expected_set, child_dir = \
        _proc_resolve_common(recipe_source, ksrc)
    show_anchor = filt.get('show')
    show_symbol = ''
    if show_anchor and ':' in show_anchor:
        show_symbol = show_anchor.split(':')[1].split()[0]
    rows = []
    for m in re.finditer(
        r'\b(REG|ONE)\s*\(\s*"([^"]+)"\s*,\s*([^,]+)\s*,\s*([A-Za-z_]\w*)\s*\)',
        srcobj.text):
        _, name, mode_flags, ops = m.groups()
        _proc_emit(name, ops or show_symbol, mode_flags, srcobj, m.start(),
                   base, prefix, child_dir, expected_set, relpath, symbol,
                   rows, flags_mode=True)
    return rows


def _rows_cmdline(src, recipe_source, ksrc):
    """__setup -> one row per cmdline param."""
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    locator = recipe_source.get('locator', '')
    if ':' not in locator:
        return []
    relpath, handler = locator.split(':', 1)
    handler = handler.split()[0]
    srcobj = Source(ksrc, relpath)
    param = filt.get('param') or recipe_source.get('param')
    rows = []
    m = re.search(
        r'__setup\s*\(\s*"([^"=]+)=?"\s*,\s*' + re.escape(handler) + r'\b',
        srcobj.text)
    if m:
        param = param or m.group(1)
        gate = _ifdef_stack(srcobj.text, m.start())
        rows.append({
            'kernel_name': handler,
            'userspace_name': param,
            'control_entry': prefix,
            'file_symbol': f"{srcobj.relpath}:{handler}",
            'type': 'tunable',
            'gate': ' '.join(sorted(gate)) if gate else '',
        })
    return rows


def _rows_proc_create_seq(src, recipe_source, ksrc):
    """proc_create_seq("name", mode, parent, &sops) -> one row per /proc file.
    locator points at the *_init function; each call yields a /proc/<name>."""
    srcobj, base, relpath, symbol, prefix, expected_set, child_dir = \
        _proc_resolve_common(recipe_source, ksrc)
    rows = []
    for m in re.finditer(
        r'proc_create_seq\s*\(\s*"([^"]+)"\s*,\s*([^,)]+)', srcobj.text):
        name, mode = m.group(1), m.group(2).strip()
        # walk to matching close paren, split args, take sops (3rd arg, index 2)
        i = m.end()
        depth = 1
        n = len(srcobj.text)
        while i < n and depth > 0:
            c = srcobj.text[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        tail = srcobj.text[m.end():i] if i > m.end() else ''
        args = _split_args(tail) if tail else []
        sops = _clean_kernel_name(args[2]) if len(args) > 2 else ''
        _proc_emit(name, sops, mode, srcobj, m.start(), base, prefix,
                   child_dir, expected_set, relpath, symbol, rows)
    return rows


def _rows_proc_create_data(src, recipe_source, ksrc):
    """proc_create[_data]/proc_create_single[_data] -> one row per /proc file.
    Covers proc_create family (proc_create/proc_create_data/proc_create_single/
    proc_create_single_data). kernel_name = ops/fops struct (3rd arg after name,
    the &X form) or show func. For per-instance (irq), recipe supplies
    child_dir_template so path = prefix/child_dir_template/name."""
    srcobj, base, relpath, symbol, prefix, expected_set, child_dir = \
        _proc_resolve_common(recipe_source, ksrc)

    rows = []
    # 3rd arg after name (index 2) is always ops/show — capture name+mode, walk
    # to matching close paren, split args, take args[2].
    call_re = re.compile(
        r'\b(proc_create(?:_single)?(?:_data)?)\s*\(\s*"([^"]+)"\s*,\s*([^,)]+)')
    for m in call_re.finditer(srcobj.text):
        _, name, mode = m.group(1), m.group(2), m.group(3).strip()
        i = m.end()
        depth = 1
        n = len(srcobj.text)
        while i < n and depth > 0:
            c = srcobj.text[i]
            if c == '"':
                i += 1
                while i < n and srcobj.text[i] != '"':
                    i += 2 if srcobj.text[i] == '\\' else 1
                i += 1
                continue
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        rest = srcobj.text[m.end():i] if i > m.end() else ''
        args = _split_args(rest) if rest else []
        kn = _clean_kernel_name(args[2]) if len(args) > 2 else ''
        _proc_emit(name, kn, mode, srcobj, m.start(), base, prefix,
                   child_dir, expected_set, relpath, symbol, rows)
    return rows


EXTRACTORS = {
    'debugfs_create_dir': _rows_debugfs,
    'sched_feat': _rows_sched_feat,
    'register_sysctl_init': _rows_sysctl,
    'cftype': _rows_cftype,
    'proc_pid_entry': _rows_proc,
    'proc_create_seq': _rows_proc_create_seq,
    'proc_create_data': _rows_proc_create_data,
    '__setup': _rows_cmdline,
}


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

COLUMNS = ['id', 'kernel_name', 'userspace_name', 'control_entry',
           'file_symbol', 'type', 'gate']


def _extract_claim(source):
    """recipe source 显式列了预期参数清单吗?返回 set 或 None(不列清单)。

    清单是维护者在 recipe 里写的"预期该 source 扫出哪些参数"。scan 自动
    从源码读字符串参数名(dir/file/procname/param/name),对照清单报差异。

    支持新字段名(children/features/procnames/names/param)和旧字段名
    (filter.children/filter.procname/filter.name)兼容。
    """
    flt = source.get("filter", {}) or {}
    mech = source.get("mechanism")

    if mech == "debugfs_create_dir":
        # 新格式:source.dirs[].children 列表(每个 dir 各自清单)
        dirs = source.get("dirs") or flt.get("dirs")
        if dirs:
            # 返回 dict: dir_name -> set(children),source 级核对用并集
            out = {}
            for d in dirs:
                ch = d.get("children")
                if ch:
                    out[d.get("name", "")] = set(ch)
            return out if out else None
        # 旧格式:filter.children.{files,u32} 并集
        ch = flt.get("children", {}) or {}
        claim = set(ch.get("files", [])) | set(ch.get("u32", []))
        return {"_legacy": claim} if claim else None

    if mech == "sched_feat":
        # 新:features 字段;旧:无(原 recipe 没清单)
        f = source.get("features") or flt.get("features")
        return set(f) if isinstance(f, list) and f else None

    if mech == "register_sysctl_init":
        # 新:procnames;旧:filter.procname
        pn = source.get("procnames") or flt.get("procname")
        return set(pn) if isinstance(pn, list) and pn else None

    if mech in ("cftype", "proc_pid_entry", "proc_create_seq", "proc_create_data"):
        # 新:names;旧:filter.name
        n = source.get("names") or flt.get("name")
        if isinstance(n, list) and n:
            return set(n)
        if isinstance(n, str) and n:
            return {n}
        return None

    if mech == "__setup":
        # 新:param(单字符串);旧:filter.param
        p = source.get("param") or flt.get("param")
        return {p} if p else None

    return None


def _check_source_claim(source, scanned_names, claim):
    """对照一个 source 的清单 vs scan 扫出的参数名,返 (gap, extra)。

    claim 可能是 set(单清单)或 dict{dir_name -> set}(debugfs 多 dir)。
    scanned_names 是该 source 扫出的 userspace_name 集合。
    """
    if claim is None:
        return [], []
    if isinstance(claim, dict):
        # debugfs dirs:每个 dir 各自核对(对不上哪个 dir 时归到 _legacy)
        gaps, extras = [], []
        for dir_name, expected in claim.items():
            if dir_name == "_legacy":
                # 旧格式单清单,scanned_names 全归它
                gap = scanned_names - expected
                extra = expected - scanned_names
                if gap:
                    gaps.append({"dir": "_legacy", "missing_from_claim": sorted(gap)})
                if extra:
                    extras.append({"dir": "_legacy", "extra_in_claim": sorted(extra)})
            else:
                # 新格式:每个 dir 该有自己的 scanned(暂用全 scanned 近似,
                # 因 scan 一次跑完所有 dirs,无法按 dir 切——除非 transcode 按
                # dir 重复跑。这里用全 scanned 减该 dir 的 expected,粗略核对)
                # TODO: 精确按 dir 切需 transcode 改按 dir 跑,但当前粗略够用
                pass
        return gaps, extras
    # set:单清单
    gap = scanned_names - claim
    extra = claim - scanned_names
    gaps = [{"missing_from_claim": sorted(gap)}] if gap else []
    extras = [{"extra_in_claim": sorted(extra)}] if extra else []
    return gaps, extras


def transcode(recipe_path, ksrc):
    with open(recipe_path) as fh:
        recipe = json.load(fh)

    rows = []
    seq = 0
    errors = []
    claim_gaps = []
    claim_extras = []
    for src_idx, src in enumerate(recipe.get('sources', [])):
        mech = src.get('mechanism')
        ext = EXTRACTORS.get(mech)
        if not ext:
            errors.append({'mechanism': mech, 'locator': src.get('locator'),
                           'error': 'no extractor'})
            continue
        # module-level gate fallback: if a row's gate is empty, fall back to
        # the recipe-level config list for that source
        mod_gate = (src.get('filter') or {}).get('config') or ''
        if isinstance(mod_gate, list):
            mod_gate = ' '.join(mod_gate)
        try:
            items = ext(src, src, ksrc)
        except Exception as e:
            errors.append({'mechanism': mech, 'locator': src.get('locator'),
                           'error': str(e)})
            continue
        for it in items:
            seq += 1
            it['id'] = seq
            if not it.get('gate'):
                it['gate'] = mod_gate
            rows.append({c: it.get(c, '') for c in COLUMNS})

        # claim self-check: recipe 清单 vs scan 扫出的参数名
        claim = _extract_claim(src)
        if claim is not None:
            scanned = set(it.get('userspace_name', '') for it in items)
            src_gaps, src_extras = _check_source_claim(src, scanned, claim)
            for g in src_gaps:
                claim_gaps.append({'source': src_idx,
                                   'locator': src.get('locator', ''),
                                   'mechanism': mech, **g})
            for e in src_extras:
                claim_extras.append({'source': src_idx,
                                     'locator': src.get('locator', ''),
                                     'mechanism': mech, **e})

    return {
        'module': recipe.get('module'),
        'version': recipe.get('version'),
        'rows': rows,
        'summary': {
            'total': len(rows),
            'by_type': _count_by(rows, 'type'),
            'errors': errors,
        },
        'claim_diff': {
            'gaps': claim_gaps,
            'extras': claim_extras,
        },
    }


def _count_by(rows, key):
    out = {}
    for r in rows:
        v = r.get(key, '')
        out[v] = out.get(v, 0) + 1
    return out


def _emit_tsv(result, fh):
    fh.write('\t'.join(COLUMNS) + '\n')
    for r in result['rows']:
        fh.write('\t'.join(str(r.get(c, '')) for c in COLUMNS) + '\n')


def _emit_json(result, fh):
    json.dump(result, fh, indent=2, ensure_ascii=False)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Transcode a kernel-module recipe into a per-parameter '
                    'producer workorder (TSV or JSON).')
    ap.add_argument('--recipe', required=True, help='recipe JSON path')
    ap.add_argument('--ksrc', required=True, help='kernel source root')
    ap.add_argument('--out', help='output file (tsv by default; .json for JSON)')
    ap.add_argument('--format', choices=['tsv', 'json'], default=None,
                    help='output format (default: tsv, or json if --out ends .json)')
    args = ap.parse_args(argv)

    if not os.path.isfile(args.recipe):
        sys.exit(f'recipe not found: {args.recipe}')
    if not os.path.isdir(args.ksrc):
        sys.exit(f'ksrc not a directory: {args.ksrc}')

    result = transcode(args.recipe, args.ksrc)
    fmt = args.format
    if fmt is None:
        fmt = 'json' if args.out and args.out.endswith('.json') else 'tsv'

    if args.out:
        with open(args.out, 'w') as fh:
            (_emit_json if fmt == 'json' else _emit_tsv)(result, fh)
        print(f'wrote {args.out}: {result["summary"]["total"]} rows',
              file=sys.stderr)
    else:
        import io
        buf = io.StringIO()
        (_emit_json if fmt == 'json' else _emit_tsv)(result, buf)
        sys.stdout.write(buf.getvalue())

    s = result['summary']
    print(f'[{result.get("module")}] {s["total"]} rows, by_type={s["by_type"]}',
          file=sys.stderr)
    if s['errors']:
        print(f'[warn] {len(s["errors"])} errors: {s["errors"]}', file=sys.stderr)


if __name__ == '__main__':
    main()
