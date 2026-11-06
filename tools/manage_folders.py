#!/usr/bin/env python3
"""Register, list, edit and remove ALLOWED-ROOT folders for web browsing.

    ./add_folder.sh "Display Name" /absolute/path/to/folder
    ./list_folders.sh
    ./edit_folder.sh FOLDER_ID [--name "..."] [--root /abs/path]
    ./remove_folder.sh FOLDER_ID

A registered folder is an allowed root for the read-only web folder
browser (browse <FOLDER_ID>/ on the server). Only the registration is
stored — folder contents are never copied or indexed, and removing a
folder removes the REGISTRY ENTRY ONLY: the real directory and every file
inside it are never touched.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry  # noqa: E402
from registry import RegistryError  # noqa: E402


def cmd_add(args):
    name, root = args.name, args.path
    if not name:
        name = input("Folder display name: ").strip()
    if not root:
        root = input("Folder path: ").strip()
    if not name or not root:
        raise RegistryError("a display name and a folder path are required")
    folder = registry.add_folder(name, root)
    print(f"Registered folder '{folder['name']}' (id: {folder['id']}).")
    print("It is now browsable from the library page — read-only, inside "
          "that directory only.")
    print("No files were copied.")
    return 0


def cmd_list(args):
    folders = registry.all_folders()
    if not folders:
        print("No folders registered yet.")
        print("Add one with:  ./add_folder.sh \"Display Name\" /abs/path")
        return 0
    print(f"{len(folders)} registered folder(s):")
    for f in folders:
        print(f"  {f['id']:<28} {f['name']}")
    return 0


def cmd_edit(args):
    folder = registry.update_folder(args.folder_id, name=args.name,
                                    root=args.root)
    print(f"Updated folder {folder['id']}: name='{folder['name']}'.")
    return 0


def cmd_remove(args):
    folder = registry.remove_folder(args.folder_id)
    print(f"Removed folder registration '{folder['name']}' "
          f"(id: {folder['id']}).")
    print("The directory itself and all of its files were NOT touched.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="manage_folders.py",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="register an allowed-root folder")
    p.add_argument("name", nargs="?", help="display name (prompted if omitted)")
    p.add_argument("path", nargs="?", help="absolute folder path (prompted)")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("list", help="list registered folders")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("edit", help="edit a folder registration")
    p.add_argument("folder_id")
    p.add_argument("--name")
    p.add_argument("--root", help="new absolute root path")
    p.set_defaults(fn=cmd_edit)

    p = sub.add_parser("remove", help="remove a registration (never the dir)")
    p.add_argument("folder_id")
    p.set_defaults(fn=cmd_remove)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
