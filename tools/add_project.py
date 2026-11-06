#!/usr/bin/env python3
"""Register a simulation with the MD trajectory viewer.

    ./add_project.sh "Display Name" /abs/path/npt.gro /abs/path/md.xtc
    ./add_project.sh                      # interactive prompts

That is all it ever needs: a name, a GRO and an XTC. Every ligand, ion,
cofactor, water and unknown residue is discovered automatically from the
GRO file and appears in the viewer with sensible defaults (bulk solvent
hidden, ligands shown). The GRO/XTC files are only ever opened read-only.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry  # noqa: E402
from registry import RegistryError  # noqa: E402


def tty_progress(msg, end="\n"):
    sys.stderr.write(msg + end)
    sys.stderr.flush()


def prompt(label, required=True, exists=False):
    while True:
        try:
            v = input(f"{label}: ").strip()
        except EOFError:
            print()
            raise SystemExit(1)
        if not v:
            if required:
                print("  (required)")
                continue
            return ""
        if exists:
            v = os.path.abspath(os.path.expanduser(v))
            if not os.path.isfile(v):
                print(f"  not found: {v}")
                continue
            return v
        return v


def print_components(proj):
    """Print what GRO discovery found -- the whole point of the tool."""
    comps = proj.get("components") or []
    if not comps:
        return
    stats = proj.get("component_stats") or {}
    total = stats.get("total_atoms") or sum(c["atoms"] for c in comps)
    print(f"  Components    : {len(comps)} distinct residue names "
          f"({total:,} atoms)")
    by_cat = {}
    for c in comps:
        by_cat.setdefault(c["category"], []).append(c)
    for cat in ("protein", "nucleic", "ligand", "ion", "water"):
        group = by_cat.get(cat)
        if not group:
            continue
        n_atoms = sum(c["atoms"] for c in group)
        label = registry.CATEGORY_LABELS.get(cat, cat)
        print(f"    {label} ({n_atoms:,} atoms):")
        # Show the most informative members; collapse huge name lists.
        show = group if len(group) <= 8 else group[:7]
        for c in show:
            flags = []
            if not c["recognized"]:
                flags.append("non-standard")
            if c["bulk"]:
                flags.append("bulk")
            if not c["visible"]:
                flags.append("hidden by default")
            extra = f"  [{', '.join(flags)}]" if flags else ""
            print(f"      {c['resname']:<6} {c['residues']:>8,} residues  "
                  f"{c['atoms']:>10,} atoms{extra}")
        if len(group) > len(show):
            rest = sum(c["atoms"] for c in group[len(show):])
            print(f"      … +{len(group) - len(show)} more "
                  f"({rest:,} atoms)")


def print_urls(proj, port):
    base = registry.base_url() or f"http://localhost:{port}"
    lan_ip = registry.lan_ip()
    direct = f"/view/{proj['id']}/"
    print()
    print(f"  Registered    : {proj['name']}")
    print(f"  Project ID    : {proj['id']}")
    m = proj.get("metadata") or {}
    if m.get("natoms"):
        print(f"  Atoms         : {m['natoms']:,}")
    if m.get("nframes"):
        print(f"  Frames        : {m['nframes']:,}")
        print(f"  Trajectory    : {m.get('first_time_ns', 0)} - "
              f"{m.get('last_time_ns')} ns "
              f"({m.get('interval_ps')} ps spacing)")
    print_components(proj)
    print()
    print(f"  This machine  : {base}{direct}")
    if lan_ip:
        print(f"  LAN           : http://{lan_ip}:{port}{direct}")
    print(f"  Library       : {base}/")
    print()


def main():
    ap = argparse.ArgumentParser(
        description="Register a GRO+XTC simulation with the web viewer. "
                    "Ligands/ions/waters are discovered from the GRO "
                    "automatically — no need to name them.")
    ap.add_argument("name", nargs="?", help="display name")
    ap.add_argument("gro", nargs="?", help="absolute path to the .gro file")
    ap.add_argument("xtc", nargs="?", help="absolute path to the .xtc file")
    ap.add_argument("--description", default=None,
                    help="optional text shown in the library/viewer")
    ap.add_argument("--ligands", default=None,
                    help="DEPRECATED advanced option: comma-separated "
                         "residue names that force default visibility "
                         "(prefix with '-' to hide, e.g. 'GTP,-K'). "
                         "Discovery already finds everything; this only "
                         "overrides which boxes start ticked.")
    ap.add_argument("--id", default=None,
                    help="explicit URL id (default: derived from the name)")
    ap.add_argument("--skip-checks", action="store_true",
                    help="register without opening the files (no metadata, "
                         "no component discovery until a rescan)")
    ap.add_argument("--port", default=None,
                    help="port printed in the URLs (default: $MTV_PORT or "
                         "the port stored in projects.json)")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    args = ap.parse_args()

    interactive = not args.name
    if interactive:
        print("Register a new simulation (Ctrl-C to abort)")
        print("Components (ligands/ions/water) are detected automatically.")
        name = prompt("Display name")
        gro = prompt("GRO file", exists=True)
        xtc = prompt("XTC file", exists=True)
        desc = prompt("Description (optional)", required=False)
        vis = ""
    else:
        name, gro, xtc = args.name, args.gro, args.xtc
        if not xtc:
            ap.error("an XTC path is required")
        desc = args.description or ""
        vis = args.ligands or ""
        gro = os.path.abspath(os.path.expanduser(gro))
        xtc = os.path.abspath(os.path.expanduser(xtc))

    try:
        proj = registry.add_project(
            name=name, gro=gro, xtc=xtc, description=desc,
            visibility=registry.normalize_ligand_overrides(
                [x.strip() for x in vis.split(",") if x.strip()]),
            project_id=args.id, skip_checks=args.skip_checks,
            progress=None if args.json else tty_progress)
    except RegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\naborted")
        return 130

    if args.json:
        import json
        print(json.dumps(proj, indent=2))
        return 0

    try:
        port = int(args.port or os.environ.get("MTV_PORT")
                   or registry.load().get("port") or 8877)
    except (TypeError, ValueError):
        port = 8877
    print_urls(proj, port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
