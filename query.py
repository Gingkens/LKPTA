#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
Knowledge query tool for consumers.

Consumers do NOT cat the JSON files directly. Instead they run this tool to
extract the parameters they care about: by name, by category, by path, or by
keyword. By default it only reads summary.json (fast); when a specific
--name is given or --detail is set, it loads tunable.json/readonly.json and
returns the full record(s).

Knowledge layout:
    knowledge/<version>/<module>/
        README.md         module self-intro (selection layer: scenarios/signals/boundaries)
        playbook.json     flow layer: capabilities + rules + thresholds (3 modes)
        summary.json      { module, version, items: [{kernel_name, category, summary}] }
        tunable.json      full records of tunable parameters
        readonly.json     full records of readonly parameters
    README.md and playbook.json are OPTIONAL (older modules may lack them).

Module selection (consumer picks a module before drilling in):
    query.py --knowledge knowledge/v7.1.0-rc5 --modules            # list modules + one-line intros
Diagnostic flow (after selecting a module):
    query.py --knowledge knowledge/v7.1.0-rc5/sched --playbook                # whole playbook
    query.py --knowledge knowledge/v7.1.0-rc5/sched --playbook --mode rule    # rule-mode slices
    query.py --knowledge knowledge/v7.1.0-rc5/sched --playbook --capability load-imbalance

Usage:
    query.py --knowledge knowledge/v7.1.0-rc5/sched --name base_slice_ns
    query.py --knowledge knowledge/v7.1.0-rc5/sched --category tunable
    query.py --knowledge knowledge/v7.1.0-rc5/sched --path migration_cost
    query.py --knowledge knowledge/v7.1.0-rc5/sched --keyword numa
    query.py --knowledge knowledge/v7.1.0-rc5/sched --category tunable --detail
    query.py --vocab knowledge/vocab.json --name EEVDF        # vocabulary lookup
    query.py --vocab knowledge/vocab.json --keyword uclamp    # vocab keyword search
"""

import argparse
import json
import os
import sys


def _load_json(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _summary_path(kdir):
    return os.path.join(kdir, "summary.json")


def _detail_path(kdir, category):
    return os.path.join(kdir, "tunable.json" if category == "tunable" else "readonly.json")


def query(kdir, name=None, category=None, path=None, keyword=None,
          detail=False):
    """Return matching items. Fast path: summary only. Full path: detail."""
    summary = _load_json(_summary_path(kdir))
    if summary is None:
        raise FileNotFoundError(
            f"summary.json not found under {kdir} — knowledge not produced?")

    items = summary.get("items", [])

    # ---- fast path: filter on summary only ----
    def matches_sum(it):
        if name and it.get("kernel_name") != name and it.get("name") != name:
            return False
        if category and it.get("category") != category:
            return False
        if keyword and keyword.lower() not in (it.get("summary", "") + " " +
                                              it.get("kernel_name", "")).lower():
            return False
        return True

    # path filtering needs the full record's path field, which is not in
    # summary; fall through to detail path for that. keyword also needs the
    # detail path: summary only carries kernel_name + a one-line summary, but a
    # consumer searching by symptom ("cache 命中率低") expects matches inside
    # when_to_*/good_for/bad_for, which live only in the detail files.
    need_detail = bool(path) or bool(name) or bool(keyword) or detail

    if not need_detail:
        hits = [it for it in items if matches_sum(it)]
        return {"_mode": "summary", "items": hits}

    # ---- full path: load tunable.json + readonly.json ----
    full = []
    for cat in ("tunable", "readonly"):
        if category and category != cat:
            continue
        rec = _load_json(_detail_path(kdir, cat))
        if not rec:
            continue
        for it in rec.get("items", []):
            if not _full_matches(it, name=name, path=path, keyword=keyword):
                continue
            full.append(it)

    return {"_mode": "detail", "items": full}


def _full_matches(it, name=None, path=None, keyword=None):
    if name and it.get("kernel_name") != name and it.get("name") != name:
        return False
    if path and path.lower() not in it.get("path", "").lower():
        return False
    if keyword:
        # ensure_ascii=False so Chinese (and other non-ASCII) in good_for/bad_for/
        # when_to_* is searchable — default json.dumps escapes to \uXXXX which makes
        # `--keyword "局部性"` silently miss items whose only match is in increasing.
        hay = (it.get("summary", "") + " " + it.get("kernel_name", "") + " " +
               json.dumps(it.get("when_to_increase", []), ensure_ascii=False) + " " +
               json.dumps(it.get("when_to_decrease", []), ensure_ascii=False) + " " +
               json.dumps(it.get("increasing", {}), ensure_ascii=False) + " " +
               json.dumps(it.get("decreasing", {}), ensure_ascii=False) + " " +
               it.get("use", ""))
        if keyword.lower() not in hay.lower():
            return False
    return True


def query_vocab(vocab_path, term=None, keyword=None):
    """Look up a term (or keyword) in knowledge/vocab.json."""
    data = _load_json(vocab_path)
    if data is None:
        raise FileNotFoundError(f"vocab.json not found at {vocab_path}")
    hits = []
    for t in data.get("terms", []):
        if term and t.get("term", "").lower() == term.lower():
            hits.append(t)
            continue
        if keyword and keyword.lower() in (t.get("term", "") + " " +
                                          t.get("definition", "")).lower():
            hits.append(t)
    return {"_mode": "vocab", "items": hits}


# ------------------------------------------------------------------ #
# Module selection (--modules) and diagnostic flow (--playbook)
# ------------------------------------------------------------------ #

_SUMMARY_LINE_PREFIX = ">"


def _extract_one_liner(readme_path):
    """Extract the one-line summary from a module README.md.

    The README's first blockquote line (starting with '>') after the H1 title
    is the consumer-facing one-liner used by --modules. Falls back to the H1
    title if no blockquote found.
    """
    try:
        with open(readme_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for ln in lines[1:]:  # skip the H1 title line
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith(_SUMMARY_LINE_PREFIX):
            return ln.lstrip(_SUMMARY_LINE_PREFIX + " \t").strip()
        # first non-empty, non-blockquote line ends the preamble — stop
        break
    return None


def _extract_scenarios(readme_path):
    """Extract the '我适合什么场景' section: the bold scenario keywords
    (each `- **X**：...` line) so a consumer with profiling symptoms can match
    them against module coverage directly in --modules output, without reading
    the full README first. Returns a list of scenario keywords.
    """
    try:
        with open(readme_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    import re
    # capture the section between '## 我适合什么场景' and the next '## '
    m = re.search(r"##\s*我适合什么场景[^\n]*\n(.*?)(?=\n##\s|\Z)", text, re.S)
    if not m:
        return []
    body = m.group(1)
    # each scenario is a line like '- **负载不均**：...'
    return re.findall(r"^\s*-\s*\*\*([^*：:]+)\*\*", body, re.M)


def _extract_excludes(readme_path):
    """Extract the '不属于本模块的情况→建议转哪个模块' line: a list of
    {from, to} so a consumer whose profiling doesn't fit this module knows
    where to go. Returns a list of dicts.
    """
    try:
        with open(readme_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    import re
    # the exclude line: '不属于 X 的情况（建议转模块）：A → B；C → D；...'
    # parentheses may be full-width; match the literal '建议转模块' then the colon
    m = re.search(r"不属于[^\n]*建议转模块[^\n]*[：:]\s*([^\n]+)", text)
    if not m:
        return []
    pairs = []
    for part in re.split(r"[；;]", m.group(1)):
        pm = re.search(r"([^→]+?)\s*→\s*([^；;]+)", part)
        if pm:
            pairs.append({"from": pm.group(1).strip(), "to": pm.group(2).strip()})
    return pairs


def list_modules(version_dir):
    """List all modules under knowledge/<version>/ with their intros +
    scenario keywords + exclude hints, so a consumer with profiling symptoms
    can match against module coverage and pick the right expert.

    Returns [{"module", "intro", "scenarios", "excludes", "has_playbook",
              "item_count"}]. A module is any subdir containing summary.json.
    """
    if not os.path.isdir(version_dir):
        raise FileNotFoundError(f"not a version directory: {version_dir}")
    out = []
    for name in sorted(os.listdir(version_dir)):
        mdir = os.path.join(version_dir, name)
        summ = os.path.join(mdir, "summary.json")
        if not (os.path.isdir(mdir) and os.path.isfile(summ)):
            continue
        readme = os.path.join(mdir, "README.md")
        rec = _load_json(summ) or {}
        out.append({
            "module": name,
            "intro": _extract_one_liner(readme),
            "scenarios": _extract_scenarios(readme),
            "excludes": _extract_excludes(readme),
            "has_playbook": os.path.isfile(os.path.join(mdir, "playbook.json")),
            "item_count": len(rec.get("items", [])),
        })
    return out


# For rule/intelligent mode, which playbook sections are relevant.
_MODE_SLICES = {
    "intelligent": ["modes", "capabilities", "observation_map", "thresholds",
                    "leverage_map", "interactions"],
    "rule": ["modes", "rules", "thresholds", "observation_map"],
    "hybrid": ["modes", "capabilities", "observation_map", "thresholds",
               "leverage_map", "interactions", "rules"],
}


def query_playbook(kdir, mode=None, capability=None):
    """Load a module's playbook.json, optionally sliced by mode/capability.

    mode in {intelligent, rule, hybrid} -> return only that mode's sections
    (plus module/version/_doc/_mode). capability -> return just that one
    capability object (after mode slicing). playbooks are OPTIONAL: returns
    None if playbook.json is absent.
    """
    pb_path = os.path.join(kdir, "playbook.json")
    data = _load_json(pb_path)
    if data is None:
        return None

    if mode:
        if mode not in _MODE_SLICES:
            raise ValueError(f"unknown mode {mode!r}; "
                             f"choose from {list(_MODE_SLICES)}")
        sliced = {"module": data.get("module"),
                  "version": data.get("version")}
        if "_doc" in data:
            sliced["_doc"] = data["_doc"]
        sliced["_mode"] = mode
        sliced["mode_description"] = data.get("modes", {}).get(mode, {})
        for sec in _MODE_SLICES[mode]:
            if sec in data:
                sliced[sec] = data[sec]
        data = sliced

    if capability:
        caps = data.get("capabilities", [])
        match = [c for c in caps if c.get("id") == capability]
        if not match:
            return {"_error": f"no capability with id={capability!r}; "
                               f"have {[c.get('id') for c in caps]}"}
        # return the capability + the lever refs it cites, for convenience
        return {"_mode": (mode or "intelligent"), "capability": match[0]}

    return data


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Query knowledge for a kernel module/version. Consumers "
                    "use this instead of cat-ing JSON files.")
    ap.add_argument("--knowledge",
                    help="knowledge dir: knowledge/<version>/<module>/")
    ap.add_argument("--vocab", metavar="PATH",
                    help="query the shared vocabulary file (knowledge/vocab.json)")
    ap.add_argument("--name", help="filter by kernel_name (or userspace name)")
    ap.add_argument("--category", choices=["tunable", "readonly"],
                    help="filter by category")
    ap.add_argument("--path", help="substring match on full path")
    ap.add_argument("--keyword", help="substring match on summary/kernel_name")
    ap.add_argument("--detail", action="store_true",
                    help="always return full records (loads tunable/readonly.json)")
    ap.add_argument("--list", action="store_true",
                    help="just list all items in summary (name, category, summary)")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON to stdout even with --list (default --list "
                         "prints a human table; scripts should pass --json)")
    ap.add_argument("--modules", action="store_true",
                    help="list all modules under knowledge/<version>/ with "
                         "one-line intros (consumer selects a module first)")
    ap.add_argument("--playbook", action="store_true",
                    help="emit a module's playbook.json (diagnostic flow: "
                         "capabilities/rules/thresholds for 3 modes)")
    ap.add_argument("--mode", choices=["intelligent", "rule", "hybrid"],
                    help="with --playbook: slice to the parts one mode needs "
                         "(rule=rules+thresholds, intelligent=capabilities+maps, "
                         "hybrid=all)")
    ap.add_argument("--capability", metavar="ID",
                    help="with --playbook: return just one capability by id "
                         "(e.g. load-imbalance)")
    args = ap.parse_args(argv)

    if args.vocab:
        try:
            result = query_vocab(args.vocab, term=args.name, keyword=args.keyword)
        except FileNotFoundError as e:
            sys.exit(str(e))
    elif args.modules:
        if not args.knowledge:
            sys.exit("need --knowledge <version-dir> for --modules")
        if not os.path.isdir(args.knowledge):
            sys.exit(f"not a directory: {args.knowledge}")
        try:
            mods = list_modules(args.knowledge)
        except FileNotFoundError as e:
            sys.exit(str(e))
        # text table if --list-ish, else JSON
        print(json.dumps({"version_dir": args.knowledge, "modules": mods},
                         indent=2, ensure_ascii=False))
        n = len(mods)
        pb = sum(1 for m in mods if m["has_playbook"])
        print(f"[modules] {n} module(s), {pb} with playbook", file=sys.stderr)
        return
    elif args.playbook:
        if not args.knowledge:
            sys.exit("need --knowledge <module-dir> for --playbook")
        if not os.path.isdir(args.knowledge):
            sys.exit(f"not a directory: {args.knowledge}")
        try:
            result = query_playbook(args.knowledge, mode=args.mode,
                                    capability=args.capability)
        except (FileNotFoundError, ValueError) as e:
            sys.exit(str(e))
        if result is None:
            sys.exit(f"no playbook.json under {args.knowledge} "
                     f"(playbook is optional; module may predate it)")
        if "_error" in result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        tag = result.get("_mode") or args.mode or "whole"
        print(f"[playbook:{tag}]", file=sys.stderr)
        return
    else:
        if not args.knowledge:
            sys.exit("need --knowledge <dir> or --vocab <path>")
        if not os.path.isdir(args.knowledge):
            sys.exit(f"not a directory: {args.knowledge}")
        try:
            result = query(args.knowledge, name=args.name, category=args.category,
                           path=args.path, keyword=args.keyword, detail=args.detail)
        except FileNotFoundError as e:
            sys.exit(str(e))

    items = result["items"]

    # E3: when a --name lookup hits nothing, the empty result is easily mistaken
    # for "param exists but empty". Surface a hint on stderr so the consumer notices.
    if args.name and not items:
        print(f"[hint] no item matched --name {args.name!r} (matches kernel_name "
              f"or userspace name); check spelling. Try --list or --keyword.",
              file=sys.stderr)

    if args.list and result["_mode"] == "summary" and not args.json:
        for it in items:
            print(f"{it.get('kernel_name'):40} {it.get('category',''):9} {it.get('summary','')}")
        print(f"\n{len(items)} items (summary)", file=sys.stderr)
        return

    # default: pretty-print JSON (--list --json also lands here for scripts)
    print(json.dumps({"items": items, "count": len(items),
                      "mode": result["_mode"]}, indent=2, ensure_ascii=False))
    print(f"[{result['_mode']}] {len(items)} item(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
