#!/usr/bin/env python3
"""Project registry for the multi-project MD trajectory viewer.

`projects.json` (next to this file) stores ONLY metadata:

    {
      "version": 1,
      "base_url": null,                # e.g. "https://md.example.org" (optional)
      "port": 8877,
      "projects": [
        {
          "id": "example-simulation",     # stable URL slug; never auto-changes
          "name": "Example Simulation",   # display name (freely editable)
          "description": "optional text",
          "gro": "/path/to/structure.gro",
          "xtc": "/path/to/trajectory.xtc",
          "components": [ ...           # auto-discovered from the GRO
            {"resname": "LIG", "category": "ligand", "residues": 1,
             "atoms": 24, "representation": "ball+stick",
             "color": "#4ea1ff", "radius_scale": 1.5, "visible": true,
             "bulk": false, "recognized": false}, ...],
          "component_stats": {"total_atoms": ..., "by_category_atoms": {...}},
          "added": "YYYY-MM-DDTHH:MM:SS",
          "metadata": {"natoms": ..., "nframes": ...,
                       "first_time_ns": ..., "last_time_ns": ...,
                       "interval_ps": ..., "identity": "..."}
        }, ...
      ]
    }

Simulation files are NEVER copied, moved or modified by anything in this
module — only read. Absolute paths stay server-side; the HTTP layer must
only ever expose `public()` output (basenames + stats, no paths).
"""

import fcntl
import hashlib
import json
import os
import re
import time

BASE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(BASE, "projects.json")
LOCK_PATH = REGISTRY_PATH + ".lock"
META_DIR = os.path.join(BASE, "meta")

# Project IDs are conservative URL slugs: lowercase, no traversal bytes,
# no dots, no separators beyond '-'/'_'.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Stable, path-derived ids are capped at this many characters (see
# project_token / path_token): readable, URL-safe, never an absolute path.
_ID_CAP = 48
LIGAND_PALETTE = ["#4ea1ff", "#37c98b", "#ffd166", "#ff6b6b", "#c792ea",
                  "#89ddff", "#f78c6c", "#a1efe4"]


class RegistryError(Exception):
    """Raised for any user-facing registration/validation problem."""


# ----------------------------------------------------------------------
# low-level registry I/O (cross-process safe: CLI + server may overlap)
# ----------------------------------------------------------------------

def _default():
    return {"version": 1, "base_url": None,
            "port": int(os.environ.get("MTV_PORT", "8877")),
            "projects": []}


def load():
    """Return the registry dict (empty default when the file is absent).

    Registries written before the folder feature are migrated in RAM on
    read (a missing 'folders' list becomes []); the file itself is only
    rewritten the next time something writes — no forced churn."""
    if not os.path.exists(REGISTRY_PATH):
        d = _default()
        d["folders"] = []
        return d
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot read {REGISTRY_PATH}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise RegistryError(f"{REGISTRY_PATH} is malformed (need "
                            f"{{'projects': [...]}})")
    if not isinstance(data.get("folders"), list):
        data["folders"] = []
    return data


def save(data):
    """Atomic write under an exclusive lock file (readers never see partials)."""
    os.makedirs(os.path.dirname(REGISTRY_PATH) or ".", exist_ok=True)
    with open(LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            tmp = REGISTRY_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, REGISTRY_PATH)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


class locked_registry:
    """Context manager: read-modify-write the registry with the lock held."""

    def __enter__(self):
        self._lf = open(LOCK_PATH, "w")
        fcntl.flock(self._lf, fcntl.LOCK_EX)
        return load()

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                pass  # caller saved via save_locked()
        finally:
            fcntl.flock(self._lf, fcntl.LOCK_UN)
            self._lf.close()
        return False

    @staticmethod
    def save(data):
        # Already holding the lock: write without re-locking.
        tmp = REGISTRY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, REGISTRY_PATH)


# ----------------------------------------------------------------------
# slugs
# ----------------------------------------------------------------------

def slugify(name):
    """'Example Simulation' -> 'example-simulation'."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:64].strip("-")
    if not s:
        raise RegistryError(
            f"cannot derive a URL id from {name!r} — use --id or a name "
            f"containing letters/digits")
    return s


def unique_id(data, wanted, pool="projects"):
    """First free id: `wanted`, then wanted-2, wanted-3 … (ids stay stable
    afterwards — renaming a project never changes its id). `pool` selects
    which registry collection to check for collisions ('projects' or
    'folders')."""
    taken = {p["id"] for p in data.get(pool) or []}
    if wanted not in taken:
        return wanted
    for i in range(2, 1000):
        cand = f"{wanted[:58]}-{i}"
        if cand not in taken:
            return cand
    raise RegistryError("too many id collisions")


# ----------------------------------------------------------------------
# trajectory metadata (read-only). Frame counting is the expensive part,
# cached per FILE IDENTITY (dev:inode:size:mtime) so two projects using the
# same XTC share one count, and any change to the file invalidates it.
# ----------------------------------------------------------------------

def file_identity(path):
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def meta_cache_path(identity):
    return os.path.join(META_DIR,
                        hashlib.sha1(identity.encode()).hexdigest()[:16] + ".npz")


def gro_natoms(path):
    """Atom count from the GRO header (line 2). Cheap — never reads the body."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.readline()
        line = fh.readline().strip()
    try:
        n = int(line)
    except ValueError:
        raise RegistryError(f"{os.path.basename(path)} does not look like a "
                            f"GRO file (line 2 is not an atom count)")
    if n <= 0:
        raise RegistryError(f"{os.path.basename(path)}: atom count is {n}")
    return n


def resnames_in_gro(path, limit=None):
    """Unique residue names present in a GRO file. GRO lines are
    %5d resid, %-5s resname, %5s atom name, %5s atom id ..., so the residue
    name lives in columns 5-9. Reads the file once (~1 s for 700k atoms)."""
    names = []
    seen = set()
    n = gro_natoms(path)
    for rn in gro_resnames(path, n):
        if rn not in seen:
            seen.add(rn)
            names.append(rn)
            if limit and len(names) >= limit:
                break
    return names


def gro_resnames(path, n=None):
    """Yield the (stripped) residue name of each atom line."""
    n = n if n is not None else gro_natoms(path)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.readline()
        fh.readline()
        for i, line in enumerate(fh):
            if i >= n:
                break
            yield line[5:10].strip()


def _xtc():
    from mdtraj.formats import XTCTrajectoryFile
    return XTCTrajectoryFile


def xtc_natoms(path):
    """Atom count from XTC frame 0 (authoritative; decodes one frame)."""
    XTCTrajectoryFile = _xtc()
    with XTCTrajectoryFile(path, "r") as fh:
        out = fh.read(n_frames=1)
    return int(out[0][0].shape[0])


def ensure_meta(xtc_path, identity=None, progress=None):
    """Return (nframes, times_ns, identity). Counts frames with a one-time
    sequential pass (reading only atom 0 per frame — bandwidth only, ~1 min
    per 6 GB), cached under meta/<hash>.npz keyed by file identity."""
    import numpy as np
    identity = identity or file_identity(xtc_path)
    cache = meta_cache_path(identity)
    if os.path.exists(cache):
        try:
            z = np.load(cache)
            return int(z["nframes"]), z["times_ns"].astype(np.float32), identity
        except Exception:
            pass  # corrupt cache -> recount
    if progress:
        progress("counting trajectory frames (one-time pass over the XTC; "
                 "about a minute per few GB)...")
    t0 = time.time()
    times = []
    XTCTrajectoryFile = _xtc()
    fh = XTCTrajectoryFile(xtc_path, "r")
    try:
        fh.seek(0)
        while True:
            try:
                out = fh.read(n_frames=50, atom_indices=[0])
            except Exception:
                break
            if out is None or len(out) == 0 or out[0].shape[0] == 0:
                break
            times.extend(np.asarray(out[1], dtype=np.float32).tolist())
            if progress:
                progress(f"  ... {len(times)} frames", end="\r")
    finally:
        try:
            fh.close()
        except Exception:
            pass
    if progress:
        progress("")
    nframes = len(times)
    if nframes == 0:
        raise RegistryError(f"{os.path.basename(xtc_path)}: no frames found "
                            f"(is it a valid GROMACS XTC?)")
    times_ns = np.asarray(times, dtype=np.float32) / 1000.0  # ps -> ns
    try:
        os.makedirs(META_DIR, exist_ok=True)
        tmp = cache + ".tmp.npz"
        np.savez(tmp, nframes=np.int64(nframes), times_ns=times_ns)
        os.replace(tmp, cache)
    except OSError:
        pass  # cache is an optimisation only
    if progress:
        progress(f"  counted {nframes} frames in {time.time()-t0:.0f} s")
    return nframes, times_ns, identity


def compute_metadata(gro_path, xtc_path, progress=None):
    """Full registration metadata: sizes, GRO/XTC atom-count compatibility,
    frame count, real trajectory times, frame spacing."""
    import numpy as np
    for label, p in (("GRO", gro_path), ("XTC", xtc_path)):
        if not os.path.isabs(p):
            raise RegistryError(f"{label} path must be absolute: {p}")
        if not os.path.exists(p):
            raise RegistryError(f"{label} file not found: {p}")
        if not os.path.isfile(p):
            raise RegistryError(f"{label} path is not a regular file: {p}")
        if not os.access(p, os.R_OK):
            raise RegistryError(f"{label} file is not readable: {p}")
    ng = gro_natoms(gro_path)
    nx = xtc_natoms(xtc_path)
    if ng != nx:
        raise RegistryError(
            f"atom-count mismatch: GRO has {ng:,} atoms, XTC frames have "
            f"{nx:,} — these files do not belong together "
            f"(use --skip-checks only if you are sure)")
    nframes, times_ns, identity = ensure_meta(xtc_path, progress=progress)
    first = float(times_ns[0])
    last = float(times_ns[nframes - 1])
    if nframes > 1:
        # times_ns is nanoseconds; report the spacing in picoseconds.
        interval_ps = float(np.median(np.diff(times_ns))) * 1000.0
    else:
        interval_ps = 0.0
    st = os.stat(xtc_path)
    return {
        "natoms": int(nx),
        "natoms_gro": int(ng),
        "nframes": int(nframes),
        "first_time_ns": round(first, 4),
        "last_time_ns": round(last, 4),
        "duration_ns": round(last - first, 4),
        "interval_ps": round(interval_ps, 3),
        "identity": identity,
        "xtc_bytes": int(st.st_size),
    }


# ----------------------------------------------------------------------
# AUTOMATIC COMPONENT DISCOVERY
#
# The GRO file is the authoritative structure definition, so the viewer is
# never told which ligands/ions a system contains: every residue name in the
# GRO is inventoried and classified here, once, at registration/rescan.
#
# The known sets below cover only what is genuinely universal (standard
# amino acids, standard nucleic acids, common water models, and a handful of
# ion names used purely for colour/size hints). EVERYTHING ELSE IS KEPT.
# An unrecognised residue name (XYZ, GMPCPP, a custom cofactor, ...) is
# preserved as a component and shown in the viewer -- classification
# uncertainty never discards data.
# ----------------------------------------------------------------------

AMINO_ACIDS = frozenset("""
ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL
HIS ASP GLU LYS MSE SEC PYL ACE NME NH2
""".split())

NUCLEIC_ACIDS = frozenset("""
A C G U I DA DC DG DT DI DU RA RC RG RU RI
5MC 5MU 7MG 1MA 2MA MIA HUA PSU OMG OMU 4SU PSE
""".split())

WATER_NAMES = frozenset("""
SOL HOH WAT TIP3 TIP3P TIP4 TIP4P TIP5 SPC SPC/E TP4 OH2 OW TIP HO4
""".split())

# Ions whose name is known: only affects colour/size defaults and the
# 'recognized' flag. Detection never depends on this list -- any single-atom
# non-polymer residue is treated as an ion (see classify_components).
KNOWN_IONS = frozenset("""
H LI NA K RB CS FR BE MG CA SR BA RA
ZN CU CU1 FE FE2 FE3 MN CO NI CD CR MO AG AU HG PT TL PB AL GA IN SN
F CL BR I OX CN N3
""".split())

# Monovalent bulk salts: visually cluttering in large amounts, so they start
# hidden (still one click away). Divalent/transition metals start visible.
BULK_IONS = frozenset("NA K CL BR I F LI RB CS OH".split())

# Categories that form the polymer/biopolymer body of the structure. These are
# always rendered (cartoon/backbone) and are not offered as individual
# on/off toggles.
POLYMER_CATEGORIES = ("protein", "nucleic")

# "Bulk" flags a component as large enough that enabling it is noticeably
# slower to stream. Both bounds must hold: a sizeable share of the system AND
# an absolute minimum — otherwise tiny test systems would brand their lone
# ligand "bulk" while a real 700k-atom solvated box stays unflagged.
BULK_FRACTION = 0.05
BULK_MIN_ATOMS = 10000

COMPONENT_CATEGORIES = ("protein", "nucleic", "water", "ion", "ligand")
CATEGORY_LABELS = {"protein": "Protein", "nucleic": "Nucleic acid",
                   "water": "Water / solvent", "ion": "Ions",
                   "ligand": "Ligands / cofactors"}

# Deterministic colour per residue name so a component keeps its colour
# across rescans (rather than depending on discovery order).
def component_color(resname):
    h = int(hashlib.md5(resname.encode()).hexdigest()[:6], 16)
    # pleasant, saturated-but-not-neon hues, stepped around the wheel
    hue = (h % 360)
    return _hsl_to_hex(hue, 0.62, 0.62)


def _hsl_to_hex(h, s, l):
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = l - c / 2
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x),
               (0, x, c), (x, 0, c), (c, 0, x)][int(h // 60) % 6]
    return "#%02x%02x%02x" % (round((r + m) * 255),
                              round((g + m) * 255), round((b + m) * 255))


def inventory_gro(path, n=None):
    """Single pass over the GRO atom lines -> {resname: (n_atoms, n_residues,
    max_atoms_per_residue)}. ~2 s for a 700k-atom structure; registration and
    rescan only, never per frame."""
    n = n if n is not None else gro_natoms(path)
    stats = {}                                    # rn -> [n_atoms, {resid: n}]
    for line in gro_lines(path, n):
        rn = line[5:10].strip() or "?"
        resid = line[0:5].strip()
        s = stats.get(rn)
        if s is None:
            stats[rn] = [1, {resid: 1}]
        else:
            s[0] += 1
            s[1][resid] = s[1].get(resid, 0) + 1
    return {rn: (v[0], len(v[1]), max(v[1].values()))
            for rn, v in stats.items()}


def classify_name(resname, max_atoms_per_residue):
    """-> category, recognized. Never returns 'discard'."""
    rn = resname.upper()
    if rn in AMINO_ACIDS:
        return "protein", True
    if rn in NUCLEIC_ACIDS:
        return "nucleic", True
    if rn in WATER_NAMES or rn.startswith(("TIP", "SPC")):
        return "water", True
    if max_atoms_per_residue <= 1:
        # A residue of one atom outside the polymer/solvent sets is an ion --
        # whether or not we recognise the name (unknown names are kept).
        return "ion", rn in KNOWN_IONS
    return "ligand", False


def default_visibility(category, resname, n_atoms, total_atoms, recognized):
    """Sensible starting point; everything stays reachable either way."""
    if category in POLYMER_CATEGORIES:
        return True                      # the body of the structure
    if category == "water":
        return False                     # bulk solvent: opt-in
    if category == "ion":
        return resname.upper() not in BULK_IONS
    # ligands/cofactors/unknown: show them, that is why people load a system
    return True


def discover_components(gro_path, total_atoms=None, overrides=None):
    """Parse the GRO and return the full component list + per-category sums.

    `overrides` optionally forces `visible` for specific residue names
    (the deprecated --ligands / --hide / --show flags), which is the only
    remaining way a user can influence this without editing the files.
    """
    n = total_atoms or gro_natoms(gro_path)
    inv = inventory_gro(gro_path, n)
    overrides = overrides or {}
    comps = []
    for rn, (n_atoms, n_res, max_per) in inv.items():
        cat, recognized = classify_name(rn, max_per)
        vis = default_visibility(cat, rn, n_atoms, n, recognized)
        o = overrides.get(rn)
        if o and "visible" in o:
            vis = bool(o["visible"])
        comps.append({
            "resname": rn,
            "label": rn,
            "category": cat,
            "recognized": recognized,
            "residues": n_res,
            "atoms": n_atoms,
            "atoms_per_residue": max_per,
            # rendering defaults, derived from the residue's own shape
            "representation": _default_representation(cat, max_per),
            "radius_scale": 2.4 if max_per <= 1 else 1.5,
            "color": component_color(rn),
            "visible": vis,
            "bulk": (n_atoms >= BULK_MIN_ATOMS
                     and n_atoms > BULK_FRACTION * max(n, 1)),
        })
    # Largest first inside each category, categories in a stable reading order.
    order = {c: i for i, c in enumerate(
        ("ligand", "ion", "water", "protein", "nucleic"))}
    comps.sort(key=lambda c: (order.get(c["category"], 9), -c["atoms"]))
    counts = {}
    for c in comps:
        counts[c["category"]] = counts.get(c["category"], 0) + c["atoms"]
    return comps, {"total_atoms": n, "by_category_atoms": counts,
                   "n_components": len(comps)}


def _default_representation(category, atoms_per_residue):
    """Reasonable starting representation, chosen from what the residue IS."""
    if atoms_per_residue <= 1:
        return "sphere"                  # mono-atomic: ions
    if category == "water":
        return "point"                   # lightweight if enabled at all
    return "ball+stick"                  # ligands, cofactors, unknowns


def normalize_ligand_overrides(spec):
    """Validate user-supplied residue names (advanced override only)."""
    out = {}
    for item in spec or []:
        show, rn = True, item
        if item.startswith("-"):
            show, rn = False, item[1:]
        rn = rn.strip().upper()
        if not re.match(r"^[A-Z0-9+/@.-]{1,6}$", rn):
            raise RegistryError(f"bad residue name: {item!r}")
        out[rn] = {"visible": show}
    return out


def gro_lines(path, n):
    """Yield the raw text of the n atom lines of a GRO file."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.readline()
        fh.readline()
        for i, line in enumerate(fh):
            if i >= n:
                break
            yield line


# ----------------------------------------------------------------------
# public operations
# ----------------------------------------------------------------------

def validate_existing(pid=None, name=None):
    if pid is not None and not ID_RE.match(pid):
        raise RegistryError(
            f"invalid project id {pid!r} (expected {ID_RE.pattern})")
    if name is not None and not name.strip():
        raise RegistryError("project name must not be empty")


def add_project(name, gro, xtc, description=None, visibility=None,
                project_id=None, skip_checks=False, progress=None):
    """Validate, discover components, compute metadata and register.

    `visibility` is an OPTIONAL dict of per-residue visibility overrides
    (advanced use, e.g. {"K": {"visible": False}}). Discovery itself needs
    nothing but the GRO file. Returns the project dict.
    """
    validate_existing(project_id, name)
    gro = os.path.realpath(os.path.expanduser(gro))
    xtc = os.path.realpath(os.path.expanduser(xtc))
    if skip_checks:
        metadata = {"natoms": None, "nframes": None, "identity": None}
        components, component_stats = [], None
    else:
        metadata = compute_metadata(gro, xtc, progress=progress)
        if progress:
            progress("scanning GRO structure for components...")
        components, component_stats = discover_components(
            gro, metadata.get("natoms"), overrides=visibility)
    with locked_registry() as data:
        # Stable id derived from the GRO/XTC RELATIVE PATH (never the
        # display name): renaming a project later never changes the URL;
        # an explicit --id still wins.
        pid = project_id or project_token(gro, xtc, _taken(data, "projects"))
        proj = {
            "id": pid,
            "name": name.strip(),
            "description": (description or "").strip() or None,
            "gro": gro,
            "xtc": xtc,
            "components": components,
            "component_stats": component_stats,
            "added": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "metadata": metadata,
        }
        data["projects"].append(proj)
        locked_registry.save(data)
    return proj


def rescan_project(pid, progress=None):
    """Re-run GRO component discovery for a project (e.g. after upgrading
    the classification logic). Registry + GRO only — XTC is untouched."""
    validate_existing(pid)
    with locked_registry() as data:
        proj = next((p for p in data["projects"] if p["id"] == pid), None)
        if proj is None:
            raise RegistryError(f"no project with id {pid!r}")
        if not os.path.isfile(proj["gro"]):
            raise RegistryError(f"GRO file missing: {proj['gro']}")
        # Preserve any explicit visibility choices across the rescan.
        overrides = {c["resname"]: {"visible": c["visible"]}
                     for c in proj.get("components") or []}
        if progress:
            progress(f"{pid}: scanning {os.path.basename(proj['gro'])}...")
        components, stats = discover_components(
            proj["gro"], (proj.get("metadata") or {}).get("natoms"),
            overrides=overrides)
        proj["components"] = components
        proj["component_stats"] = stats
        locked_registry.save(data)
    return proj


def remove_project(pid):
    """Delete the registry entry ONLY. The GRO/XTC files are never touched."""
    validate_existing(pid)
    with locked_registry() as data:
        proj = next((p for p in data["projects"] if p["id"] == pid), None)
        if proj is None:
            raise RegistryError(f"no project with id {pid!r}")
        data["projects"] = [p for p in data["projects"] if p["id"] != pid]
        locked_registry.save(data)
    return proj


def update_project(pid, name=None, description=None, gro=None, xtc=None,
                   new_id=None, skip_checks=False, progress=None):
    """Edit registry fields. Renaming NEVER changes the id (URLs are
    stable). Changing the GRO/XTC path regenerates the path-derived id —
    ids depend ONLY on the path (see path_token) — and the caller prints
    the new one; the old URL then stops resolving. Components are
    re-discovered automatically whenever the structure file changes."""
    validate_existing(pid)
    if new_id is not None:
        validate_existing(new_id)
    if gro is not None:
        gro = os.path.realpath(os.path.expanduser(gro))
    if xtc is not None:
        xtc = os.path.realpath(os.path.expanduser(xtc))
    with locked_registry() as data:
        proj = next((p for p in data["projects"] if p["id"] == pid), None)
        if proj is None:
            raise RegistryError(f"no project with id {pid!r}")
        if name is not None:
            validate_existing(None, name)
            proj["name"] = name.strip()
        if description is not None:
            proj["description"] = description.strip() or None
        if new_id is not None:
            taken = {p["id"] for p in data["projects"]} - {pid}
            if new_id in taken:
                raise RegistryError(f"project id {new_id!r} already exists")
            proj["id"] = new_id

        files_changed = gro is not None or xtc is not None
        if files_changed:
            g = gro or proj["gro"]
            x = xtc or proj["xtc"]
            if skip_checks:
                proj["metadata"] = {}
            else:
                proj["metadata"] = compute_metadata(g, x, progress=progress)
            old_gro, old_xtc = proj["gro"], proj["xtc"]
            proj["gro"], proj["xtc"] = g, x
            if progress:
                progress("re-discovering components from the new GRO...")
            proj["components"], proj["component_stats"] = \
                discover_components(g, proj["metadata"].get("natoms"))
            # Path-derived id: only a real path change moves the id (a
            # rename never does — that path is above). An explicit --id
            # override still wins over regeneration.
            if new_id is None and (g, x) != (old_gro, old_xtc):
                proj["id"] = project_token(g, x, _taken(data, "projects",
                                                        exclude_id=pid))
        locked_registry.save(data)
    return proj


def get(pid):
    if not pid or not ID_RE.match(pid):
        return None
    for p in load()["projects"]:
        if p["id"] == pid:
            return p
    return None


def all_projects():
    return load()["projects"]


# ----------------------------------------------------------------------
# sanitised payloads for the HTTP layer — NEVER include absolute paths
# ----------------------------------------------------------------------

def human_size(n):
    if n is None:
        return None
    n = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def public(p):
    m = p.get("metadata") or {}
    try:
        gro_bytes = os.path.getsize(p["gro"])
    except OSError:
        gro_bytes = None
    try:
        xtc_bytes = os.path.getsize(p["xtc"])
    except OSError:
        xtc_bytes = None
    nframes = m.get("nframes")
    first = m.get("first_time_ns")
    last = m.get("last_time_ns")
    return {
        "id": p["id"],
        "name": p["name"],
        "description": p.get("description") or "",
        "atoms": m.get("natoms"),
        "frames": nframes,
        "first_time_ns": first,
        "last_time_ns": last,
        "duration_ns": m.get("duration_ns")
        if m.get("duration_ns") is not None
        else (round(last - first, 3) if first is not None and last is not None
              else None),
        "frame_interval_ps": m.get("interval_ps"),
        "gro_filename": os.path.basename(p["gro"]),
        "xtc_filename": os.path.basename(p["xtc"]),
        "gro_size": human_size(gro_bytes),
        "xtc_size": human_size(xtc_bytes),
        # Auto-discovered from the GRO. Public keys only — no paths, no
        # coordinates, no per-atom data of any kind.
        "components": [public_component(c) for c in (p.get("components") or [])],
        "component_stats": p.get("component_stats") or {},
        "added": p.get("added"),
        # True for temporary folder-view sessions (lets the viewer offer
        # "Save to Library"); never set for registry projects.
        "temp": bool(p.get("_temp")),
    }


# The only component fields the frontend is allowed to see.
_COMPONENT_PUBLIC_KEYS = (
    "resname", "label", "category", "recognized", "residues", "atoms",
    "atoms_per_residue", "representation", "radius_scale", "color",
    "visible", "bulk",
)


def public_component(c):
    return {k: c.get(k) for k in _COMPONENT_PUBLIC_KEYS}


def base_url(port=None):
    """Single knob for future public HTTPS: env MTV_BASE_URL (or
    projects.json base_url) overrides everything; otherwise a host-relative
    URL is used and links stay relative to whatever host/port serves the app."""
    b = os.environ.get("MTV_BASE_URL")
    if not b:
        try:
            b = load().get("base_url")
        except RegistryError:
            b = None
    if b:
        return str(b).rstrip("/")
    return None  # -> callers build relative links


def lan_ip():
    """Best-effort primary LAN address (no external network calls)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # no packets sent; picks the route
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None


# ======================================================================
# REGISTERED FOLDERS  (Part B of the folder-browsing feature)
#
# A folder is an ALLOWED ROOT: an absolute server-side directory that the
# web folder browser may list inside of (read-only). The registry stores
# ONLY {id, name, root, added} per folder — contents are listed on demand
# and NEVER indexed. Absolute paths never leave the server; the HTTP layer
# exposes public_folder() output plus per-directory listings built from
# safe_path(), which resolves every request with realpath and refuses
# anything that lands outside the resolved root (symlinks included).
# ======================================================================

# Trajectory-viewing for folder browsing supports the established path:
# GRO structure + XTC trajectory. Other extensions are listed as
# informational only (never openable through a temporary session).
FOLDER_FILE_EXTS = {".gro", ".xtc", ".pdb", ".trr", ".tpr"}

# Temporary-view session tokens: exactly 24 lowercase hex chars, generated
# with secrets.token_hex(12) in create_session (12 random bytes). The
# validator is strict (no length wiggle-room) so a token can only ever be
# what that generator produces.
SESSION_ID_RE = re.compile(r"^[a-f0-9]{24}$")

# IDs are stable, readable URL tokens derived from the SERVER-SIDE relative
# path (project: <gro stem>_<xtc stem>; folder: the folder path under the
# user's home). No absolute path is ever embedded, and the token is capped
# at 48 chars. IDs stay immutable: editing a display name never changes an
# id; a path edit regenerates it (the old URL then simply 404s, and the
# CLI prints the new one).


def path_token(seed, taken=frozenset(), maxlen=_ID_CAP):
    """Readable URL slug from a relative-path seed. Collision-safe against
    `taken` (deterministic for the same inputs); a short sha1 suffix of the
    seed guarantees uniqueness even when two different paths share a
    truncated slug."""
    s = (seed or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        s = "item"
    s = s[:maxlen].strip("-") or "item"
    if s not in taken:
        return s
    cand = s
    h = hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()
    i = 2
    while cand in taken:
        # first retry: stem + path hash so two same-named paths under
        # different roots never alias; then counter for further clashes
        cand = f"{s[:maxlen - 6]}-{h[:5]}" if i == 2 else \
               f"{s[:maxlen - 8]}-{h[:7]}-{i}"
        i += 1
    return cand


def _taken(data, pool, exclude_id=None):
    ids = {p["id"] for p in data.get(pool) or []}
    if exclude_id:
        ids.discard(exclude_id)
    return ids


# ----------------------------------------------------------------------
# stable, readable, path-derived IDs
#
# A project id comes from its GRO/XTC paths, a folder id from its path
# relative to the user's home directory. Absolute paths NEVER appear in an
# id or URL. ids are immutable after creation: renaming a project or folder
# never changes its id; editing the PATH regenerates it (the CLI prints the
# new id; the old URL stops resolving). Collisions fall back to readable
# variants, then to short sha1 suffixes of the path seed.
# ----------------------------------------------------------------------

def _rel_under_home(path):
    """Short stable seed for a path: relative to $HOME when inside it,
    otherwise the path minus its leading '/'. The absolute path itself is
    never used as (or inside) an id."""
    home = os.path.expanduser("~")
    try:
        rel = os.path.relpath(path, home)
        if not rel.startswith(".."):
            return rel
    except ValueError:
        pass
    return path.lstrip(os.sep)


def project_token(gro, xtc, taken=frozenset()):
    """Stable URL id for a registered project, derived from its GRO/XTC
    paths (server-side, relative — never absolute):

        <home>/projects/a01/run1/structure.gro  +  trajectory.xtc
        ->  projects-a01-run1-structure_trajectory

    Directory path and GRO stem are slugified (separators -> single '-'),
    the XTC stem is appended after '_'. Capped at 48 chars; collisions
    across the existing ids fall back to readable variants, then a short
    sha1 suffix of the full seed (never the full absolute path)."""
    d = _slug(_rel_under_home(os.path.dirname(gro)) or "root")
    g = _slug(os.path.splitext(os.path.basename(gro))[0] or "structure")
    x = _slug_xtc(os.path.splitext(os.path.basename(xtc))[0] or "trajectory")
    seed = f"{d}-{g}_{x}"
    if len(seed) <= _ID_CAP and seed not in taken:
        return seed
    return path_token(seed, taken)


def _slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s


def _slug_xtc(s):
    # XTC stem keeps its underscores (matches the example); only
    # characters illegal in ids are replaced.
    s = re.sub(r"[^a-z0-9_-]+", "-", (s or "").lower()).strip("-_")
    return s


def _folder_seed(root):
    return _rel_under_home(root)


# ----------------------------------------------------------------------
# folder CRUD (registration only — real directories are never touched)
# ----------------------------------------------------------------------

def _require_under_home(root, what="Folder"):
    """Folder browsing is restricted to the user's own home tree. $HOME
    itself resolves via realpath (which follows /home/user -> /data/home on
    symlinked-home setups), and the allowed roots are realpath-canonical
    downstream, so containment is judged between resolved paths."""
    home = os.path.realpath(os.path.expanduser("~"))
    if root != home and not root.startswith(home + os.sep):
        raise RegistryError(
            f"{what} path must be inside your home directory ({home}); "
            f"this tool only serves folders from your own home tree")


def add_folder(name, root):
    """Register an allowed-root folder. Validates existence, readability,
    home-directory containment, and stores the canonicalised absolute path.
    Copies nothing. The id is derived from the folder path RELATIVE to
    $HOME — never from the display name, never an absolute path."""
    name = (name or "").strip()
    if not name:
        raise RegistryError("folder display name must not be empty")
    root = os.path.realpath(os.path.expanduser((root or "").strip()))
    if not os.path.exists(root):
        raise RegistryError(f"folder does not exist: {root}")
    if not os.path.isdir(root):
        raise RegistryError(f"not a directory: {root}")
    if not os.access(root, os.R_OK | os.X_OK):
        raise RegistryError(f"folder is not readable: {root}")
    _require_under_home(root)
    with locked_registry() as data:
        if any(f.get("root") == root for f in data["folders"]):
            raise RegistryError(f"folder already registered: "
                                f"{f_display_for(data, root)} ({root})")
        fid = path_token(_folder_seed(root), _taken(data, "folders"))
        folder = {
            "id": fid,
            "name": name,
            "root": root,
            "added": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        data["folders"].append(folder)
        locked_registry.save(data)
    return folder


def _f_display_for(data, root):
    f = next((f for f in data["folders"] if f.get("root") == root), None)
    return f["name"] if f else "?"


f_display_for = _f_display_for  # keep the name used above readable


def get_folder(fid):
    if not fid or not ID_RE.match(fid):
        return None
    for f in load().get("folders") or []:
        if f.get("id") == fid:
            return f
    return None


def all_folders():
    return load().get("folders") or []


def remove_folder(fid):
    """Delete the folder REGISTRY ENTRY ONLY. The directory and every file
    inside it are never touched."""
    with locked_registry() as data:
        folder = next((f for f in data["folders"] if f.get("id") == fid), None)
        if folder is None:
            raise RegistryError(f"no folder with id {fid!r}")
        data["folders"] = [f for f in data["folders"] if f.get("id") != fid]
        locked_registry.save(data)
    return folder


def update_folder(fid, name=None, root=None):
    with locked_registry() as data:
        folder = next((f for f in data["folders"] if f.get("id") == fid), None)
        if folder is None:
            raise RegistryError(f"no folder with id {fid!r}")
        if name is not None:
            folder["name"] = name.strip()
        if root is not None:
            root = os.path.realpath(os.path.expanduser(root.strip()))
            if not os.path.isdir(root):
                raise RegistryError(f"not a readable directory: {root}")
            if not os.access(root, os.R_OK | os.X_OK):
                raise RegistryError(f"folder is not readable: {root}")
            _require_under_home(root)
            if any(f.get("root") == root and f.get("id") != fid
                   for f in data["folders"]):
                raise RegistryError(f"folder already registered for {root}")
            # A root change regenerates the path-derived id (per the id
            # rule: ids depend only on the path). The caller prints the
            # new id; the old URL stops resolving.
            if root != folder["root"]:
                folder["root"] = root
                folder["id"] = path_token(_folder_seed(root),
                                          _taken(data, "folders",
                                                 exclude_id=fid))
        locked_registry.save(data)
    return folder


def public_folder(f):
    """Sanitised folder payload: display name + id only. NEVER the root."""
    return {"id": f["id"], "name": f["name"], "added": f.get("added")}


# ----------------------------------------------------------------------
# safe path resolution + on-demand directory listing
# ----------------------------------------------------------------------

def folder_root(folder):
    """Resolved allowed root for a folder entry (realpath, symlink-free)."""
    root = os.path.realpath(folder["root"])
    if not os.path.isdir(root):
        raise RegistryError("registered folder is currently unavailable")
    return root


def safe_path(folder, rel):
    """Resolve `rel` (a URL-supplied relative path) inside the folder's
    allowed root. Returns the canonical absolute path, or None when the
    request escapes the root in ANY way: '..' segments (also after URL
    decoding), absolute-path injection, duplicate separators, symlink
    escapes, Unicode oddities — the containment check runs on fully
    resolved realpaths, never on strings. The root itself is rel=''."""
    root = folder_root(folder)
    # Normalise Unicode (NFC) so visually identical names resolve the same,
    # then join under the root WITHOUT honouring embedded absolute paths
    # (os.path.join would discard the root — ntpath-style tricks on POSIX
    # come down to a leading '/', which normpath of the joined form below
    # still keeps inside because we strip it first).
    try:
        import unicodedata
        rel = unicodedata.normalize("NFC", rel)
    except (ImportError, ValueError):
        pass
    rel = rel.lstrip("/")
    cand = os.path.realpath(os.path.join(root, rel))
    # Containment by path components — string prefix matching would accept
    # '/home/u/proj2' for root '/home/u/proj'.
    return cand if (cand == root or
                    cand.startswith(root + os.sep)) else None


def list_folder_dir(folder, rel=""):
    """On-demand, read-only listing of ONE directory inside an allowed root.
    Never recursive, never walks the whole tree. Returns
    {rel, dirs, files, truncated, note?} with only basenames + sizes —
    absolute paths never appear in the payload. Raises RegistryError for
    escapes (caller maps them to 403/404)."""
    full = safe_path(folder, rel)
    if full is None:
        raise RegistryError("path escapes the registered folder")
    if not os.path.isdir(full):
        raise RegistryError("not a directory")
    root = folder_root(folder)
    dirs, files = [], []
    truncated = False
    MAX_ITEMS = 2000  # bounded listing for very large directories
    try:
        entries = sorted(os.scandir(full),
                         key=lambda e: (not e.is_dir(follow_symlinks=False),
                                        e.name.lower()))
    except OSError:
        # No exception detail: OSError text embeds the absolute path, and
        # absolute paths must never reach the HTTP layer.
        raise RegistryError("cannot read this directory")
    # relative path of this dir under the root, for response breadcrumbs
    rel_out = os.path.relpath(full, root).replace(os.sep, "/")
    if rel_out == ".":
        rel_out = ""
    shown = 0
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            is_symlink = e.is_symlink()
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            continue
        # A symlink that resolves OUTSIDE the allowed root is invisible in
        # the listing (not merely un-openable): the browser never learns the
        # name exists. Symlinks pointing within the root stay visible.
        if is_symlink and safe_path(folder,
                                    os.path.relpath(e.path, root)) is None:
            continue
        if is_dir:
            dirs.append({"name": e.name})
        else:
            ext = os.path.splitext(e.name)[1].lower()
            if ext not in FOLDER_FILE_EXTS:
                continue  # only MD-relevant files are exposed
            try:
                size = e.stat(follow_symlinks=False).st_size
            except OSError:
                size = None
            files.append({"name": e.name, "ext": ext, "size": human_size(size)})
        shown += 1
        if shown >= MAX_ITEMS:
            truncated = True
            break
    # Parent link for breadcrumbs: relative path of the parent dir (clamped
    # at the root; '..' is a UI concept, not a filesystem hop).
    if rel_out:
        parent = "/".join(rel_out.split("/")[:-1])
    else:
        parent = None
    return {"rel": rel_out, "parent": parent, "dirs": dirs, "files": files,
            "truncated": truncated}


def folder_pair_exists(folder, gro_rel, xtc_rel):
    """Validate a (gro, xtc) relative pair selected in the web browser.

    Both files must exist, be regular readable files, sit INSIDE the
    resolved allowed root (safe_path — symlink/.. tricks rejected), carry
    the right extensions, and have matching atom counts. Returns
    (gro_abs, xtc_abs, metadata) with full registration-grade metadata
    (natoms, nframes, times, interval) computed by the SAME code path
    add_project.sh uses. Raises RegistryError with a user-facing message.
    """
    if not gro_rel or not xtc_rel:
        raise RegistryError("select one .gro structure and one .xtc trajectory")
    gro = safe_path(folder, gro_rel)
    xtc = safe_path(folder, xtc_rel)
    if gro is None or xtc is None:
        raise RegistryError("selection escapes the registered folder")
    if os.path.splitext(gro)[1].lower() != ".gro":
        raise RegistryError("structure file must be a .gro")
    if os.path.splitext(xtc)[1].lower() != ".xtc":
        raise RegistryError("trajectory file must be an .xtc")
    for label, p in (("structure", gro), ("trajectory", xtc)):
        if not os.path.isfile(p):
            raise RegistryError(f"{label} file not found")
        if not os.access(p, os.R_OK):
            raise RegistryError(f"{label} file is not readable")
    # compute_metadata checks GRO/XTC atom-count compatibility and produces
    # exactly the metadata a registered project carries (identity-keyed
    # frame counts are cached under meta/, so re-opening is instant).
    # Its error text can embed the absolute path (only on a rare race now
    # that we pre-validated), so scrub the allowed root out before it can
    # ever surface over HTTP.
    root = folder_root(folder)
    try:
        metadata = compute_metadata(gro, xtc)
    except RegistryError as exc:
        raise RegistryError(str(exc).replace(root, os.path.basename(root)))
    return gro, xtc, metadata


# ----------------------------------------------------------------------
# temporary folder-view sessions (in-memory, per trajectory choice)
#
# Browsing a folder and opening a GRO+XTC pair creates a SESSION keyed by a
# random token: {folder id, gro, xtc, metadata, components}. serve.py turns
# a session into a project-shaped dict that is fed to the EXISTING viewer
# routes verbatim — same /view/<id>/ page, same streaming endpoints, same
# caches (frame cache keys on the XTC's absolute path, so temporary views
# and saved projects never collide, and two sessions of different files are
# fully isolated). Sessions die with the server process by design: they are
# convenience views, not saved data.
# ----------------------------------------------------------------------

import secrets as _secrets
import threading as _threading

_SESSIONS = {}
_SESSIONS_LOCK = _threading.Lock()
SESSION_TTL_S = 24 * 3600
_SESSIONS_MAX = 64


def create_session(folder_id, gro, xtc, metadata, components,
                   component_stats):
    """Register a temporary trajectory view; returns the session id."""
    sid = _secrets.token_hex(12)
    with _SESSIONS_LOCK:
        # opportunistic expiry — no background thread needed
        now = time.time()
        for stale in [s for s, v in _SESSIONS.items()
                      if now - v["ts"] > SESSION_TTL_S]:
            _SESSIONS.pop(stale, None)
        while len(_SESSIONS) >= _SESSIONS_MAX:
            _SESSIONS.pop(next(iter(_SESSIONS)), None)
        _SESSIONS[sid] = {
            "folder_id": folder_id,
            "gro": gro,
            "xtc": xtc,
            "metadata": metadata,
            "components": components,
            "component_stats": component_stats,
            "ts": now,
        }
    return sid


def get_session(sid):
    if not sid or not SESSION_ID_RE.match(sid):
        return None
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
        if s is not None:
            s["ts"] = time.time()  # active views never expire under use
        return s


def session_as_project(sid):
    """Registry-project-shaped dict for the shared viewer routes. The id is
    the session token so every existing /view/<pid>/... route works
    unchanged; 'name' carries folder display + basenames (never paths)."""
    s = get_session(sid)
    if s is None:
        return None
    folder = get_folder(s["folder_id"])
    fname = folder["name"] if folder else "folder"
    return {
        "id": sid,
        "name": f"{fname} · {os.path.basename(s['gro'])} + "
                f"{os.path.basename(s['xtc'])}",
        "description": "temporary folder view (not saved to the library)",
        "gro": s["gro"],
        "xtc": s["xtc"],
        "components": s["components"],
        "component_stats": s["component_stats"],
        "metadata": s["metadata"],
        "added": None,
        "_temp": sid,
    }


def session_folder_id(sid):
    s = get_session(sid)
    return s["folder_id"] if s else None
