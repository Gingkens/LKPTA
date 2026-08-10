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
    scan.py --recipe scan_recipes/sched.json --ksrc /home/lgk/linux
    scan.py --recipe scan_recipes/sched.json --ksrc /home/lgk/linux --out brief.tsv

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


def _rows_debugfs(src, recipe_source, ksrc):
    """debugfs_create_dir -> one row per child node actually attached to it."""
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    locator = recipe_source.get('locator', '')
    srcobj, _ = _resolve(ksrc, locator)
    text = srcobj.text

    dir_name = filt.get('dir')
    child_loc = filt.get('child_locator')

    nodes = []

    def harvest_direct(body_text, base_src, parent_var):
        """Scan debugfs_create_* calls; if parent_var given, keep only those
        whose parent argument == parent_var (precise dir scoping).

        The call may span several physical lines and embed cast parens like
        (u32 *), so we balance parens from the opening '(' to the matching ')'
        rather than stopping at the first ')'.
        """
        for m in re.finditer(
            r'debugfs_create_(\w+)\s*\(\s*"([^"]+)"\s*,\s*(\d+)\s*,',
            body_text):
            api, name, mode = m.group(1), m.group(2), m.group(3)
            if api == 'dir':
                continue
            # walk to the matching close paren of this call
            i = m.end()
            depth = 1
            n = len(body_text)
            while i < n and depth > 0:
                c = body_text[i]
                if c == '"':
                    # skip a string literal
                    i += 1
                    while i < n and body_text[i] != '"':
                        if body_text[i] == '\\':
                            i += 2
                        else:
                            i += 1
                    i += 1
                    continue
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                if depth == 0:
                    break
                i += 1
            rest = body_text[m.end():i]
            # split top-level args (commas not nested inside parens)
            args = _split_args(rest)
            parent_arg = args[0].strip() if args else ''
            if parent_var and parent_arg != parent_var:
                continue
            var = args[1].strip() if len(args) > 1 else ''
            fops = args[2].strip() if len(args) > 2 else ''
            kn = _clean_kernel_name(var if var and var != 'NULL' else fops)
            nodes.append({
                'name': name,
                'kind': api,
                'kernel_name': kn,
                'mode': mode,
                'file': base_src.relpath,
                'symbol': getattr(base_src, 'symbol', '') or '',
            })

    def harvest_sdm(body_text, base_src):
        """Local SDM(type, mode, member) macro calls -> field name + mode."""
        # mask preprocessor lines so the #define SDM(...) line is not matched
        # but offsets stay aligned with body_text (mask, don't delete).
        masked = re.sub(r'(?m)^[ \t]*#[^\n]*',
                        lambda mm: ' ' * len(mm.group(0)), body_text)
        for m in re.finditer(
            r'\bSDM\s*\(\s*(\w+)\s*,\s*(\d+)\s*,\s*(\w+)\s*\)', masked):
            typ, mode, member = m.group(1), m.group(2), m.group(3)
            nodes.append({
                'name': member,
                'kind': typ,
                'kernel_name': 'sd->' + member,
                'mode': mode,
                'file': base_src.relpath,
                'symbol': getattr(base_src, 'symbol', '') or '',
            })

    # Scope: if the dir has a local handle (var = debugfs_create_dir(...)),
    # only collect nodes attached to that handle. If the recipe names a dir
    # but the source has no `VAR = debugfs_create_dir("<dir>", ...)` (the dir
    # doesn't exist in THIS kernel version — e.g. a v7.2-only dir scanned
    # against v7.1 source), parent_var is None and we must NOT fall through
    # to unfiltered harvesting (that would mis-attribute every create_* call
    # in the function to this dir). Return zero rows for a dir that isn't
    # present in the source.
    parent_var = _dir_parent_var(text, dir_name) if dir_name else None
    if dir_name and not parent_var:
        return []
    harvest_direct(text, srcobj, parent_var)

    # SDM nodes live in the child_locator body (register_sd), not in every
    # dir's own function. Only harvest SDM from the child_locator body so a
    # dir source that merely references register_sd does not duplicate them.
    if child_loc:
        cobj, _ = _resolve(ksrc, child_loc)
        harvest_direct(cobj.text, cobj, None)
        harvest_sdm(cobj.text, cobj)

    # de-dup by name (keep first), skip the dir itself
    seen = set()
    uniq = []
    for n in nodes:
        if dir_name and n['name'] == dir_name:
            continue
        if n['name'] in seen:
            continue
        seen.add(n['name'])
        uniq.append(n)

    rows = []
    # Prefer child_dir_template (relative, e.g. cpuN/domainN) over the full
    # layout string (which may repeat the dir name already present in prefix).
    child_dir = filt.get('child_dir_template') or filt.get('layout')
    for n in uniq:
        if child_dir:
            ce = prefix.rstrip('/') + '/' + child_dir + '/' + n['name']
        else:
            ce = prefix.rstrip('/') + '/' + n['name']
        rows.append({
            'kernel_name': n['kernel_name'],
            'userspace_name': n['name'],
            'control_entry': ce,
            'file_symbol': f"{n['file']}:{n.get('symbol','') or n.get('line','')}",
            'type': _mode_to_type(n['mode']),
            'gate': '',
        })
    return rows


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

    table = filt.get('table')
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
    cft_loc = filt.get('cftype_locator') or recipe_source.get('locator', '')
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


def _rows_proc(src, recipe_source, ksrc):
    """proc_pid_entry -> one row per REG/ONE in the pid entry table.

    kernel_name prefers the per-file ops symbol captured from the REG/ONE call
    (e.g. proc_pid_maps_operations, proc_mem_operations) — it is the stable,
    per-file identifier the producer greps to reach that file's implementation.
    filter.show (a recipe-supplied .show callback) is only used as a fallback
    when the ops symbol is absent (rare; e.g. ONE entries using a bare func).
    """
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    locator = recipe_source.get('locator', '')
    srcobj, _ = _resolve(ksrc, locator)
    relpath = locator.split(':')[0]
    base = Source(ksrc, relpath)
    expected = filt.get('name')
    expected_set = set([expected]) if isinstance(expected, str) else set(expected or [])
    # recipe may name a .show callback (file:func) as fallback when ops absent
    show_anchor = filt.get('show')
    show_symbol = ''
    if show_anchor and ':' in show_anchor:
        show_symbol = show_anchor.split(':')[1].split()[0]

    rows = []
    for m in re.finditer(
        r'\b(REG|ONE)\s*\(\s*"([^"]+)"\s*,\s*([^,]+)\s*,\s*([A-Za-z_]\w*)\s*\)',
        srcobj.text):
        kind, name, mode_flags, ops = m.groups()
        if expected_set and name not in expected_set:
            continue
        abs_pos = srcobj.body_start + m.start()
        gate = _ifdef_stack(base.text, abs_pos)
        # Build control_entry: append the per-file name to the prefix unless the
        # prefix already ends with that name (recipe may write a single-file
        # prefix like /proc/<pid>/sched). For multi-file sources the prefix is
        # the directory (/proc/<pid>) and each file gets /proc/<pid>/<name> —
        # without this, all rows collapse to the same path (aggregated path).
        import os as _os
        base_seg = _os.path.basename(prefix.rstrip('/'))
        ce = prefix if base_seg == name else prefix.rstrip('/') + '/' + name
        rows.append({
            'kernel_name': ops or show_symbol,
            'userspace_name': name,
            'control_entry': ce,
            'file_symbol': f"{relpath}:{srcobj.symbol or ''}",
            'type': _mode_flags_to_type(mode_flags),
            'gate': ' '.join(sorted(gate)) if gate else '',
        })
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
    param = filt.get('param')
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

    locator points at the *_init function that calls proc_create_seq. Each
    call yields a global /proc/<name> entry backed by a seq_operations.
    """
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    locator = recipe_source.get('locator', '')
    srcobj, _ = _resolve(ksrc, locator)
    expected = filt.get('name')
    expected_set = set([expected]) if isinstance(expected, str) else set(expected or [])
    relpath = locator.split(':')[0] if ':' in locator else locator
    base = Source(ksrc, relpath)
    symbol = srcobj.symbol if getattr(srcobj, 'symbol', None) else (
        locator.split(':')[1].split()[0] if ':' in locator else '')

    rows = []
    # match proc_create_seq("name", MODE, parent, &sops)  — call may span lines
    for m in re.finditer(
        r'proc_create_seq\s*\(\s*"([^"]+)"\s*,\s*([^,)]+)', srcobj.text):
        name, mode = m.group(1), m.group(2).strip()
        if expected_set and name not in expected_set:
            continue
        # sops is the 4th arg; walk to the matching close paren to get it
        i = m.end()
        depth = 1
        n = len(srcobj.text)
        sops = ''
        while i < n and depth > 0:
            c = srcobj.text[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and c == ',':
                # reached the 4th arg boundary commas; collect up to next ','
                pass
            i += 1
        # simpler: grab the tail and split args top-level
        tail = srcobj.text[m.end():i] if i > m.end() else ''
        args = _split_args(tail) if tail else []
        # args[0]=mode(已取), args[1]=parent, args[2]=sops
        sops_raw = args[2] if len(args) > 2 else ''
        sops = _clean_kernel_name(sops_raw)
        abs_pos = (srcobj.body_start if hasattr(srcobj, 'body_start') else 0) + m.start()
        gate = _ifdef_stack(base.text, abs_pos)
        # path_prefix may already include the name (e.g. /proc/schedstat) —
        # don't append it twice; only append when prefix's last segment != name.
        import os as _os
        base_seg = _os.path.basename(prefix.rstrip('/'))
        ce = prefix if base_seg == name else prefix.rstrip('/') + '/' + name
        rows.append({
            'kernel_name': sops or filt.get('ops', ''),
            'userspace_name': name,
            'control_entry': ce,
            'file_symbol': f"{relpath}:{symbol}",
            'type': _mode_to_type(mode),
            'gate': ' '.join(sorted(gate)) if gate else '',
        })
    return rows


def _rows_proc_create_data(src, recipe_source, ksrc):
    """proc_create[_data]/proc_create_single[_data] -> one row per /proc file.

    Covers the proc_create family not handled by _rows_proc_create_seq:
      proc_create("name", mode, parent, &fops)
      proc_create_data("name", mode, parent, &ops, data)
      proc_create_single("name", mode, parent, show_func)
      proc_create_single_data("name", mode, parent, show_func, data)
    Used by irq (per-irq /proc/irq/<N>/ files) and any global proc entry
    created via these calls. kernel_name prefers the ops/fops struct (4th arg,
    the &X form) then the show func (proc_create_single). For per-instance
    sources (irq), recipe supplies child_dir_template (e.g. 'irq/<irqN>') so
    each file path = prefix/child_dir_template/name.
    """
    filt = recipe_source.get('filter', {}) or {}
    prefix = recipe_source.get('path_prefix', '')
    locator = recipe_source.get('locator', '')
    srcobj, _ = _resolve(ksrc, locator)
    expected = filt.get('name')
    expected_set = set([expected]) if isinstance(expected, str) else set(expected or [])
    relpath = locator.split(':')[0] if ':' in locator else locator
    base = Source(ksrc, relpath)
    symbol = (srcobj.symbol if getattr(srcobj, 'symbol', None) else
              (locator.split(':')[1].split()[0] if ':' in locator else ''))
    child_dir = filt.get('child_dir_template') or filt.get('layout')

    rows = []
    # match each call shape separately — ops/show arg position differs per shape:
    #   proc_create_data("name", mode, parent, &ops, data)   -> ops is 3rd arg after name
    #   proc_create("name", mode, parent, &fops)            -> fops is 3rd arg
    #   proc_create_single_data("name", mode, parent, show, data) -> show is 3rd arg
    #   proc_create_single("name", mode, parent, show)     -> show is 3rd arg
    # In all cases the 3rd arg (index 2 counting from name=0) is the ops/show.
    # We capture name+mode, then read the 3rd arg via a dedicated regex on rest.
    call_re = re.compile(
        r'\b(proc_create(?:_single)?(?:_data)?)\s*\(\s*"([^"]+)"\s*,\s*([^,)]+)')
    for m in call_re.finditer(srcobj.text):
        call, name, mode = m.group(1), m.group(2), m.group(3).strip()
        if expected_set and name not in expected_set:
            continue
        # walk to matching close paren to get the rest (parent, ops/show, [data])
        i = m.end()
        depth = 1
        n = len(srcobj.text)
        while i < n and depth > 0:
            c = srcobj.text[i]
            if c == '"':
                i += 1
                while i < n and srcobj.text[i] != '"':
                    i += 2 if srcobj.text[i] == '\\' else 1
                i += 1; continue
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        rest = srcobj.text[m.end():i] if i > m.end() else ''
        # rest starts with ', parent, &ops/show, [data]' — split and take index 2
        args = _split_args(rest) if rest else []
        # args[0]='' (leading comma), args[1]=parent, args[2]=ops/show, [3]=data
        kn = _clean_kernel_name(args[2]) if len(args) > 2 else ''
        abs_pos = (srcobj.body_start if hasattr(srcobj, 'body_start') else 0) + m.start()
        gate = _ifdef_stack(base.text, abs_pos)
        import os as _os
        base_seg = _os.path.basename(prefix.rstrip('/'))
        ce = prefix if base_seg == name else (
            prefix.rstrip('/') + '/' + child_dir + '/' + name if child_dir
            else prefix.rstrip('/') + '/' + name)
        rows.append({
            'kernel_name': kn,
            'userspace_name': name,
            'control_entry': ce,
            'file_symbol': f"{relpath}:{symbol}",
            'type': _mode_to_type(mode),
            'gate': ' '.join(sorted(gate)) if gate else '',
        })
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


def transcode(recipe_path, ksrc):
    with open(recipe_path) as fh:
        recipe = json.load(fh)

    rows = []
    seq = 0
    errors = []
    for src in recipe.get('sources', []):
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

    return {
        'module': recipe.get('module'),
        'version': recipe.get('version'),
        'rows': rows,
        'summary': {
            'total': len(rows),
            'by_type': _count_by(rows, 'type'),
            'errors': errors,
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
