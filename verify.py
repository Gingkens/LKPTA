#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
Verify knowledge completeness against a scan workorder.

Runs scan.py to get the ground-truth parameter set for a recipe+kernel, then
diffs it against an existing knowledge directory's summary.json (and the
tunable/readonly files). Reports:

  missing  — in the workorder but NOT in knowledge  (producer under-produced)
  extra    — in knowledge but NOT in the workorder  (stale / wrong-version entries)
  matched  — present in both, kernel_name agrees (sanity count)
  drifted  — workorder row's PATH is in knowledge but its kernel_name differs.
             The parameter exists (so not missing), yet its stable identifier
             changed — a signal path-fallback used to mask. Cross-version diffs
             MUST surface these: a clean missing=0/extra=0 with drifted>0 is a
             false-clean (knowledge and workorder agree on path but disagree on
             the cross-version-stable kernel_name).

Same tool, two scenarios:
  * completeness check (same version):
        verify.py --recipe sched.json --ksrc /home/lgk/linux \
                  --against knowledge/v7.1.0-rc5/sched
  * incremental diff across versions (7.1 -> 7.2):
        verify.py --recipe sched.json --ksrc /home/lgk/linux-7.2 \
                  --against knowledge/v7.1.0-rc5/sched
    (missing = new-in-7.2 params the producer must add; extra = dropped)

Matching key: control_entry (path) primary, kernel_name fallback — both are
version-stable (no line numbers), so cross-version diffs are meaningful.
NOTE: only summary/tunable/readonly.json participate in the diff. README.md
and playbook.json (the selection/flow layers) are OPTIONAL module artifacts
and are NOT checked here — completeness is about parameter coverage only.

Usage:
    verify.py --recipe scan_recipes/sched.json --ksrc /home/lgk/linux \
              --against knowledge/v7.1.0-rc5/sched
    verify.py ... --format json   # machine-readable
    verify.py ... --missing-only   # just the gaps (for the producer to action)
"""

import argparse
import json
import os
import subprocess
import sys


def _run_scan(recipe, ksrc):
    """Run scan.py, return its parsed rows (list of dicts)."""
    here = os.path.dirname(os.path.abspath(__file__))
    scan = os.path.join(here, "scan_recipes", "scan.py")
    proc = subprocess.run(
        [sys.executable, scan, "--recipe", recipe, "--ksrc", ksrc,
         "--format", "json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"scan.py failed:\n{proc.stderr}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.exit(f"scan.py produced non-JSON:\n{proc.stdout[:500]}")
    return result


def _load_knowledge(kdir):
    """Load summary.json from a knowledge dir. Returns list of items, each
    augmented with its source file (summary only has 3 fields)."""
    summary_path = os.path.join(kdir, "summary.json")
    if not os.path.isfile(summary_path):
        sys.exit(f"no summary.json under {kdir} — knowledge not produced?")
    with open(summary_path) as fh:
        data = json.load(fh)
    return data.get("items", []), data.get("module"), data.get("version")


def _key(item):
    """Stable matching key: control_entry (path) preferred, kernel_name fallback.

    summary.json items only carry kernel_name + category + summary — no path.
    So for summary-based matching we use kernel_name; when the detail files
    are loaded (path available), we prefer path.
    """
    path = item.get("path") or item.get("control_entry")
    if path:
        return ("path", path)
    kn = item.get("kernel_name", "")
    return ("kn", kn)


def verify(recipe, ksrc, kdir):
    scan_result = _run_scan(recipe, ksrc)
    rows = scan_result["rows"]
    if scan_result["summary"]["errors"]:
        # surface scan errors but keep going
        pass

    ksum, kmodule, kversion = _load_knowledge(kdir)

    # knowledge index by key; summary has no path, so key by kernel_name only
    # unless we also load the detail files for richer matching.
    kn_index = {}
    path_index = {}
    for it in ksum:
        kn = it.get("kernel_name", "")
        if kn:
            kn_index.setdefault(kn, []).append(it)
    # also load detail files so we can match by path (more precise)
    for fname in ("tunable.json", "readonly.json"):
        fpath = os.path.join(kdir, fname)
        if not os.path.isfile(fpath):
            continue
        with open(fpath) as fh:
            for it in json.load(fh).get("items", []):
                p = it.get("path", "")
                if p:
                    path_index.setdefault(p, []).append(it)

    # Detect aggregated control_entries: paths shared by multiple workorder
    # rows (e.g. 'kernel cmdline' — every __setup() param sits there).
    # For these, path is NOT a per-param identity, so path-only matching would
    # let one knowledge entry mask every param under that path. We must match
    # those rows by kernel_name only.
    from collections import Counter
    ce_counts = Counter(r.get("control_entry", "") for r in rows)
    aggregated = {ce for ce, n in ce_counts.items() if n > 1}

    missing, extra, matched, drifted = [], [], [], []

    # --- workorder -> knowledge: find missing ---
    # matched  : workorder row found in knowledge (kn or path agree)
    # drifted  : workorder row's PATH is in knowledge but KERNEL_NAME differs —
    #            the parameter exists (not missing) yet its stable identifier
    #            changed. This is the signal path-fallback used to mask: a
    #            cross-version diff would silently report matched=0 missing
    #            while kernel_names actually drifted. Report it separately so
    #            producers can fix the knowledge (or scan) instead of trusting
    #            a false-clean.
    kn_seen = set()
    path_seen = set()
    for r in rows:
        kn = r.get("kernel_name", "")
        path = r.get("control_entry", "")
        if kn and kn in kn_index:
            matched.append(("kn", r, kn_index[kn][0]))
            kn_seen.add(kn)
        elif path and path not in aggregated and path in path_index:
            # path fallback ONLY for non-aggregated paths (1 row <-> 1 path).
            # Before declaring matched, check kernel_name agreement: if the
            # knowledge entry at this path has a DIFFERENT kn, it's a drift,
            # not a clean match.
            k_entry = path_index[path][0]
            k_kn = k_entry.get("kernel_name", "")
            if kn and k_kn and k_kn != kn:
                drifted.append({
                    "path": path,
                    "workorder_kn": kn,
                    "knowledge_kn": k_kn,
                    "userspace_name": r.get("userspace_name", ""),
                    "type": r.get("type", ""),
                })
                path_seen.add(path)
            else:
                matched.append(("path", r, k_entry))
                path_seen.add(path)
        else:
            missing.append(r)

    # --- knowledge -> workorder: find extra ---
    wk_paths = {r.get("control_entry", "") for r in rows}
    wk_kns = {r.get("kernel_name", "") for r in rows}
    for it in ksum:
        # we matched summary entries by kn above; anything in summary whose kn
        # wasn't hit by a workorder row (and has no path in detail matching a
        # workorder path) is extra.
        kn = it.get("kernel_name", "")
        if kn in kn_seen:
            continue
        # does any detail path for this kn match a workorder path?
        detail_hit = False
        for fname in ("tunable.json", "readonly.json"):
            fpath = os.path.join(kdir, fname)
            if not os.path.isfile(fpath):
                continue
            with open(fpath) as fh:
                for dit in json.load(fh).get("items", []):
                    if dit.get("kernel_name") == kn and dit.get("path") in wk_paths:
                        detail_hit = True
                        break
            if detail_hit:
                break
        if not detail_hit:
            extra.append(it)

    return {
        "module": kmodule,
        "version": kversion,
        "scan_total": len(rows),
        "knowledge_total": len(ksum),
        "matched": len(matched),
        "missing": missing,
        "extra": extra,
        "drifted": drifted,
        "scan_errors": scan_result["summary"]["errors"],
    }


def _fmt_row(r, kind="workorder"):
    if kind == "workorder":
        return (f"  {r.get('kernel_name','?'):30} {r.get('userspace_name',''):25} "
                f"{r.get('control_entry',''):50} [{r.get('type','')}]")
    return (f"  {r.get('kernel_name','?'):30} {r.get('summary','')[:50]}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Verify knowledge completeness vs a scan workorder.")
    ap.add_argument("--recipe", required=True, help="recipe JSON path")
    ap.add_argument("--ksrc", required=True, help="kernel source root")
    ap.add_argument("--against", required=True,
                    help="knowledge dir to compare: knowledge/<ver>/<module>/")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--missing-only", action="store_true",
                    help="report only missing (for producer action)")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.against):
        sys.exit(f"not a directory: {args.against}")

    r = verify(args.recipe, args.ksrc, args.against)

    if args.format == "json":
        out = {
            "module": r["module"], "version": r["version"],
            "scan_total": r["scan_total"], "knowledge_total": r["knowledge_total"],
            "matched": r["matched"],
            "missing_count": len(r["missing"]),
            "extra_count": len(r["extra"]),
            "drifted_count": len(r["drifted"]),
            "scan_errors": r["scan_errors"],
        }
        if not args.missing_only:
            out["extra"] = r["extra"]
        out["missing"] = r["missing"]
        out["drifted"] = r["drifted"]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"[{r['module']} {r['version']}] "
              f"scan={r['scan_total']} knowledge={r['knowledge_total']} "
              f"matched={r['matched']} missing={len(r['missing'])} "
              f"extra={len(r['extra'])} drifted={len(r['drifted'])}")
        if r["scan_errors"]:
            print(f"\n!! scan.py had {len(r['scan_errors'])} errors (these "
                  f"params won't be in the workorder):")
            for e in r["scan_errors"]:
                print(f"   {e.get('mechanism')} {e.get('locator')}: {e.get('error')}")
        if r["missing"]:
            print(f"\n=== MISSING ({len(r['missing'])}) — in scan, NOT in knowledge ===")
            for m in r["missing"]:
                print(_fmt_row(m))
        if not args.missing_only and r["extra"]:
            print(f"\n=== EXTRA ({len(r['extra'])}) — in knowledge, NOT in scan ===")
            for e in r["extra"]:
                print(_fmt_row(e, "knowledge"))
        if r["drifted"]:
            print(f"\n=== DRIFTED ({len(r['drifted'])}) — path matches but kernel_name changed ===")
            for d in r["drifted"]:
                print(f"  {d['path']}")
                print(f"    workorder kn: {d['workorder_kn']}  →  knowledge kn: {d['knowledge_kn']}")
                print(f"    (param exists, not missing; stable identifier drifted — fix knowledge or scan)")
        if not r["missing"] and not r["extra"] and not r["drifted"]:
            print("\n✓ knowledge matches scan workorder exactly")


if __name__ == "__main__":
    main()
