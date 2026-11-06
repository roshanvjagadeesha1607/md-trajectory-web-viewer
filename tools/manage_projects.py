#!/usr/bin/env python3
"""List, edit, rescan and remove registered simulations.

    ./list_projects.sh
    ./remove_project.sh PROJECT_ID
    ./edit_project.sh PROJECT_ID --name "New name" --description "..."
    ./edit_project.sh PROJECT_ID --gro /abs/new.gro --xtc /abs/new.xtc
    ./rescan_projects.sh                 # all projects
    ./rescan_projects.sh PROJECT_ID      # one project

Removing a project deletes its REGISTRY ENTRY ONLY. The GRO and XTC files
are never touched. Editing paths re-validates, recomputes metadata and
re-discovers components; a rescan re-reads the GRO component inventory. The
simulation files themselves are only ever read.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry  # noqa: E402
from registry import RegistryError  # noqa: E402


def base(port=None):
    b = registry.base_url()
    if b:
        return b
    try:
        port = int(port or os.environ.get("MTV_PORT")
                   or registry.load().get("port") or 8877)
    except (TypeError, ValueError):
        port = 8877
    return f"http://localhost:{port}"


def component_summary(p, limit=8):
    """One-line-per-category summary of the discovered components."""
    comps = p.get("components") or []
    if not comps:
        return []
    by_cat = {}
    for c in comps:
        by_cat.setdefault(c["category"], []).append(c)
    lines = []
    for cat in ("ligand", "ion", "water", "protein", "nucleic"):
        group = by_cat.get(cat)
        if not group:
            continue
        n_atoms = sum(c["atoms"] for c in group)
        label = registry.CATEGORY_LABELS.get(cat, cat)
        names = []
        for c in group[:limit]:
            n = c["resname"] + ("" if c["visible"] else "*")
            names.append(n)
        if len(group) > limit:
            names.append(f"+{len(group) - limit} more")
        lines.append(f"    {label.lower()} ({n_atoms:,} atoms): "
                     + ", ".join(names))
    return lines


def do_list(_args):
    projects = registry.all_projects()
    if not projects:
        print("No projects registered yet.")
        print()
        print('  ./add_project.sh "My Simulation" '
              "/abs/path/npt.gro /abs/path/md.xtc")
        return 0
    b = base()
    print(f"{len(projects)} registered simulation(s)\n")
    for p in projects:
        m = p.get("metadata") or {}
        print(f"  {p['id']}")
        print(f"    name        : {p['name']}")
        if p.get("description"):
            print(f"    description : {p['description']}")
        if m.get("natoms"):
            print(f"    atoms       : {m['natoms']:,}")
        if m.get("nframes"):
            print(f"    frames      : {m['nframes']:,}  "
                  f"({m.get('first_time_ns', 0)}-{m.get('last_time_ns')} ns, "
                  f"{m.get('interval_ps')} ps spacing)")
        print(f"    structure   : {os.path.basename(p['gro'])}")
        print(f"    trajectory  : {os.path.basename(p['xtc'])}")
        for line in component_summary(p):
            print(line)
        if p.get("components"):
            print("    (* = starts hidden; everything stays toggleable)")
        print(f"    url         : {b}/view/{p['id']}/")
        print()
    print(f"  library       : {b}/")
    return 0


def do_remove(args):
    try:
        proj = registry.remove_project(args.project_id)
    except RegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Removed {proj['id']} from the library registry.")
    print("These files were NOT touched and still exist on disk:")
    print(f"  {proj['gro']}")
    print(f"  {proj['xtc']}")
    return 0


def do_edit(args):
    nothing = (args.name is None and args.description is None
               and args.gro is None and args.xtc is None
               and args.new_id is None)
    if nothing:
        # No flags -> prompt through the current values. Pressing Enter keeps
        # the current value; type '-' to clear an optional field. Components
        # are not edited by hand any more: edit --gro triggers a re-scan.
        p = registry.get(args.project_id)
        if p is None:
            print(f"ERROR: no project with id {args.project_id!r}",
                  file=sys.stderr)
            return 2
        print(f"Editing {p['id']} — Enter keeps the current value, '-' clears.")
        try:
            v = input(f"  name        [{p['name']}]: ").strip()
            args.name = v or None
            cur = p.get("description") or ""
            v = input(f"  description [{cur}]: ").strip()
            args.description = None if v == "" else (
                "" if v == "-" else v)
            v = input(f"  GRO         "
                      f"[{os.path.basename(p['gro'])}]: ").strip()
            args.gro = v or None
            v = input(f"  XTC         "
                      f"[{os.path.basename(p['xtc'])}]: ").strip()
            args.xtc = v or None
        except EOFError:
            print()
            return 1

    def progress(msg, end="\n"):
        sys.stderr.write(msg + end)
        sys.stderr.flush()

    try:
        proj = registry.update_project(
            args.project_id, name=args.name, description=args.description,
            gro=args.gro, xtc=args.xtc,
            new_id=args.new_id, skip_checks=args.skip_checks,
            progress=progress)
    except RegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\naborted")
        return 130
    print(f"\nUpdated {proj['id']}")
    if args.new_id:
        print("  NOTE: the id changed, so the old URL no longer resolves.")
    print(f"  url: {base()}/view/{proj['id']}/")
    return 0


def do_rescan(args):
    """Re-run GRO component discovery (after upgrading the classifier, or to
    migrate projects registered before auto-discovery existed). Files are
    read-only; visibility choices made earlier are preserved."""
    def progress(msg, end="\n"):
        sys.stderr.write(msg + end)
        sys.stderr.flush()

    if args.project_id:
        pids = [args.project_id]
    else:
        pids = [p["id"] for p in registry.all_projects()]
        if not pids:
            print("No projects registered.")
            return 0
    failed = 0
    for pid in pids:
        try:
            proj = registry.rescan_project(pid, progress=progress)
        except RegistryError as exc:
            print(f"  {pid}: FAILED — {exc}", file=sys.stderr)
            failed += 1
            continue
        comps = proj.get("components") or []
        stats = proj.get("component_stats") or {}
        n_hidden = sum(1 for c in comps if not c["visible"]
                       and c["category"] not in registry.POLYMER_CATEGORIES)
        unknown = [c["resname"] for c in comps
                   if not c["recognized"]
                   and c["category"] not in registry.POLYMER_CATEGORIES]
        print(f"  {pid}: {len(comps)} components, "
              f"{stats.get('total_atoms', '?')} atoms, "
              f"{n_hidden} non-polymer hidden by default"
              + (f", non-standard: {', '.join(unknown)}" if unknown else ""))
        for line in component_summary(proj, limit=6):
            print(line)
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    li = sub.add_parser("list", help="list registered projects")
    li.set_defaults(func=do_list)

    rm = sub.add_parser("remove", help="remove a registry entry (files stay)")
    rm.add_argument("project_id")
    rm.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt")
    rm.set_defaults(func=do_remove)

    ed = sub.add_parser("edit", help="edit display name/description/paths")
    ed.add_argument("project_id")
    ed.add_argument("--name")
    ed.add_argument("--description")
    ed.add_argument("--gro")
    ed.add_argument("--xtc")
    ed.add_argument("--id", dest="new_id",
                    help="change the URL id (breaks the old URL)")
    ed.add_argument("--skip-checks", action="store_true")
    ed.set_defaults(func=do_edit)

    rs = sub.add_parser(
        "rescan", help="re-discover components from each project's GRO")
    rs.add_argument("project_id", nargs="?", default=None,
                    help="project id (default: all projects)")
    rs.set_defaults(func=do_rescan)

    li = sub.add_parser("ls", help="list registered projects")
    li.set_defaults(func=do_list)

    args = ap.parse_args()
    if args.cmd == "remove" and not args.yes:
        p = registry.get(args.project_id)
        if p is None:
            print(f"ERROR: no project with id {args.project_id!r}",
                  file=sys.stderr)
            return 2
        print(f"About to remove this entry from projects.json:")
        print(f"  id          : {p['id']}")
        print(f"  name        : {p['name']}")
        print(f"  structure   : {p['gro']}")
        print(f"  trajectory  : {p['xtc']}")
        print("The GRO and XTC files will NOT be deleted.")
        try:
            if input("Confirm? [y/N] ").strip().lower() not in ("y", "yes"):
                print("cancelled")
                return 1
        except EOFError:
            print("\ncancelled")
            return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
