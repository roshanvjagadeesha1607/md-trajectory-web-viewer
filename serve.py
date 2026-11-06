#!/usr/bin/env python3
"""
Multi-project molecular-dynamics trajectory web viewer -- backend server.

Streams frames from GROMACS XTC trajectories to NGL Viewer in the browser,
speaking the MDsrv HTTP protocol (the protocol NGL 2.x implements natively
via NGL.MdsrvDatasource), so the browser only downloads ONE frame of ONE
selection at a time -- never the whole multi-GB trajectory.

Binary frame response layout (exactly what NGL's remote-Trajectory expects):
    [0]  int32   number of frames
    [4]  float32 frame time (ns)
    [8]  float32[9]  3x3 box matrix, row-major, Angstrom
    [44] float32[n*3] coordinates, Angstrom

Every registered simulation lives at its own URL namespace:

    /                          project library (searchable)
    /view/<PROJECT_ID>/        interactive viewer for that one project
    /view/<PROJECT_ID>/file/           its structure (read-only)
    /view/<PROJECT_ID>/traj/numframes/ frame count
    /view/<PROJECT_ID>/traj/frame/<f>/ one frame

Security model: a visitor can only ever reach projects present in
projects.json, addressed by a validated slug id. Filesystem paths come from
the registry only -- never from a URL -- so path traversal and arbitrary file
access are impossible by construction. There is no `dir/` listing endpoint
(NGL's MdsrvDatasource would use one), no download endpoints, and no
absolute path is ever sent to a client. Simulation files are only ever
opened read-only.

All the single-project performance work is preserved: parsed-selection cache
+ selection tokens, byte-bounded decoded-frame LRU (now one GLOBAL budget
shared across projects), flat coordinate gather, in-place client updates.
"""

import hashlib
import os
import sys
import gzip
import time
import threading
from collections import OrderedDict

import numpy as np
from flask import Flask, Response, request, send_from_directory, jsonify, abort

import registry

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
VENDOR = os.path.join(BASE, "vendor")

# Cache budgets (bounded so the server can never be pushed into swap).
# The frame budget is GLOBAL across every project -- not per project -- so
# registering more simulations never grows total memory.
SEL_CACHE_MAX = 32         # parsed atom selections per trajectory (tiny)
XYZ_CACHE_BYTES = int(float(os.environ.get("MTV_CACHE_GB", "3"))
                      * 1024 * 1024 * 1024)

# Filename NGL sees for structure/trajectory. Purely cosmetic: the routes
# ignore it and resolve the file through the registry, so it cannot be used
# to probe the filesystem.
GRO_NAME = "structure.gro"
XTC_NAME = "track.xtc"

app = Flask(__name__)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------
# registry access (hot-reloaded on change, so add/remove/edit need no restart)
# ----------------------------------------------------------------------

_REG = {"mtime": None, "data": None}
_REG_LOCK = threading.Lock()


def reg():
    with _REG_LOCK:
        try:
            mtime = os.stat(registry.REGISTRY_PATH).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != _REG["mtime"]:
            try:
                _REG["data"] = registry.load()
            except registry.RegistryError as exc:
                log(f"warning: {exc} (keeping previous registry)")
            _REG["mtime"] = mtime
        return _REG["data"] or {"projects": []}


def project(pid):
    """Registry entry for a validated project id, or None.

    Also resolves TEMPORARY folder-view session tokens: a folder-selected
    GRO+XTC pair becomes a project-shaped dict with the session token as
    its id, so every /view/<pid>/ route below (structure, numframes,
    frame streaming, metadata) serves it verbatim -- one viewer, no
    second implementation. Sessions live in memory, keyed per browser
    choice, so different users browsing the same folder stay isolated."""
    if not pid or not (registry.ID_RE.match(pid)
                       or registry.SESSION_ID_RE.match(pid)):
        return None
    for p in reg()["projects"]:
        if p["id"] == pid:
            return p
    return registry.session_as_project(pid)


# ----------------------------------------------------------------------
# GLOBAL decoded-frame cache, shared by every trajectory.
#
# Key is (absolute path of the XTC, frame index): project A frame 100 can
# never collide with project B frame 100 for a different file, while two
# projects registered against the same XTC share one entry. One LRU, one
# byte budget, so total memory stays bounded as projects are added.
# ----------------------------------------------------------------------

class FrameCache:
    def __init__(self, budget_bytes):
        self.budget = budget_bytes
        self._lock = threading.Lock()
        self._data = OrderedDict()
        self._bytes = 0

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is not None:
                self._data.move_to_end(key)
            return hit

    def put(self, key, entry):
        nbytes = entry[0].nbytes
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= old[0].nbytes
            self._data[key] = entry
            self._bytes += nbytes
            while self._bytes > self.budget and len(self._data) > 1:
                _, (old, _) = self._data.popitem(last=False)
                self._bytes -= old.nbytes

    def stats(self):
        with self._lock:
            return {"frames": len(self._data), "bytes": self._bytes,
                    "budget_bytes": self.budget}

    def drop_file(self, path):
        """Free entries for a trajectory (used when a project is removed)."""
        with self._lock:
            for k in [k for k in self._data if k[0] == path]:
                self._bytes -= self._data.pop(k)[0].nbytes


CACHE = FrameCache(XYZ_CACHE_BYTES)


# ----------------------------------------------------------------------
# XTC access (one file handle per thread per trajectory; seek per frame)
# ----------------------------------------------------------------------

class XtcSource:
    """Thread-safe random access into one XTC trajectory.

    One instance per distinct XTC file, so two projects pointing at the same
    trajectory share handles, metadata and cached frames.
    """

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self._local = threading.local()
        self._meta_lock = threading.Lock()
        self._nframes = None
        self._times_ns = None  # np.ndarray float32, or None
        # Parsed-selection cache: token -> (flat_coord_idx, n_atoms). The
        # C-alpha selection repeats identically across all frames, so this is
        # tiny. Tokens are salted with the project id (see resolve_selection)
        # so a token learned in one project is never honoured in another.
        self._sel_lock = threading.Lock()
        self._sel_cache = OrderedDict()
        self._natoms = None

    @property
    def natoms(self):
        if self._natoms is None:
            with self._meta_lock:
                if self._natoms is None:
                    self._natoms = self._read_natoms()
        return self._natoms

    def _read_natoms(self):
        # GROMACS .gro companion not required: read frame 0 fully once.
        fh = self._fh()
        fh.seek(0)
        out = fh.read(n_frames=1)
        return int(out[0][0].shape[0])

    # -- file handles -------------------------------------------------
    def _fh(self):
        fh = getattr(self._local, "fh", None)
        if fh is None:
            from mdtraj.formats import XTCTrajectoryFile
            fh = XTCTrajectoryFile(self.path, "r")
            self._local.fh = fh
        return fh

    # -- metadata -----------------------------------------------------
    def _ensure_meta(self):
        if self._nframes is not None:
            return
        with self._meta_lock:
            if self._nframes is not None:
                return
            try:
                n, times, _ = registry.ensure_meta(self.path)
            except registry.RegistryError as exc:
                log(f"warning: {exc}")
                raise
            self._nframes = n
            self._times_ns = times
            log(f"{self.name}: {n} frames ready")

    def warm(self):
        """Background warm-up (registry cache hit is instant; a cold file
        costs one sequential pass and must never block server start)."""
        try:
            self._ensure_meta()
        except Exception as exc:  # noqa: BLE001
            log(f"warning: warm-up failed for {self.name}: {exc}")

    @property
    def nframes(self):
        self._ensure_meta()
        return self._nframes

    def time_ns(self, idx):
        self._ensure_meta()
        if self._times_ns is not None and idx < len(self._times_ns):
            return float(self._times_ns[idx])
        return idx * 0.01  # fallback: assume 10 ps spacing

    # -- selection parsing (cached) ----------------------------------
    # NGL re-POSTs the same ~0.6-1.1 MB atom-range text on EVERY frame in a
    # partial (C-alpha) selection. Parsing it (split/int over ~46k ranges) is
    # ~60 ms/frame -- the single biggest server cost in C-alpha mode. We parse
    # a selection once, cache the resulting flat coordinate-index array, and
    # from then on only do a ~0.4 ms gather. A client that has seen a token
    # may send just 'tok=<token>' instead of re-uploading the whole body.
    def _parse_ranges(self, text):
        """Parse MDsrv range text -> (flat_coord_indices, n_atoms).

        Returns a 1-D intp array of positions into xyz.reshape(-1) so the
        per-frame gather is a single fancy-index (fastest measured variant,
        ~0.4 ms for the C-alpha selection vs ~3 ms for a boolean mask).
        """
        try:
            rngs = [[int(y) for y in x.split(",")]
                    for x in text.strip().split(";") if x]
        except (ValueError, IndexError):
            raise ValueError("bad atomIndices")
        n = self.natoms
        # full-structure request: single [0, >=natoms) range
        if len(rngs) == 1 and rngs[0][0] == 0 and rngs[0][1] >= n:
            return None, n
        if len(rngs) > 4096:
            mask = np.zeros(n, dtype=bool)
            for s, e in rngs:
                mask[min(s, n):min(e, n)] = True
            atoms = np.flatnonzero(mask)
        else:
            atoms = np.concatenate(
                [np.arange(min(s, n), min(e, n)) for s, e in rngs
                 if e > s]) if rngs else np.empty(0, dtype=np.int64)
        flat = np.repeat(atoms.astype(np.intp) * 3, 3) \
            + np.tile(np.array([0, 1, 2], dtype=np.intp), atoms.size)
        return flat, int(atoms.size)

    def resolve_selection(self, pid, text):
        """Return (token, flat, n_atoms), caching the parse. Thread-safe.

        The token hash mixes in the project id AND the cache key is
        (project, token): a token issued to one project is never honoured
        for another, even when two projects share this trajectory file.
        """
        key = hashlib.sha256(f"{pid}\n{text}".encode()).hexdigest()[:20]
        ck = (pid, key)
        with self._sel_lock:
            hit = self._sel_cache.get(ck)
            if hit is not None:
                self._sel_cache.move_to_end(ck)
                return key, hit[0], hit[1]
        flat, nat = self._parse_ranges(text)
        with self._sel_lock:
            self._sel_cache[ck] = (flat, nat)
            while len(self._sel_cache) > SEL_CACHE_MAX:
                self._sel_cache.popitem(last=False)
        return key, flat, nat

    _MISS = object()

    def selection(self, pid, key):
        """Look up a previously issued selection token for this project.
        -> (flat, natoms) or _MISS. flat may be None (full structure)."""
        with self._sel_lock:
            hit = self._sel_cache.get((pid, key))
            if hit is None:
                return self._MISS
            self._sel_cache.move_to_end((pid, key))
            return hit

    def cache_stats(self):
        with self._sel_lock:
            s = len(self._sel_cache)
        return {"selection_cache_entries": s}

    # -- frames -------------------------------------------------------
    def _xyz(self, frame_idx):
        """Decoded frame (xyz nm, box Angstrom) from the global LRU.

        Decode is ~25 ms and mode-independent; both full and C-alpha requests
        for the same frame share it, so client prefetch warms every mode.
        """
        key = (self.path, frame_idx)
        hit = CACHE.get(key)
        if hit is not None:
            return hit
        fh = self._fh()
        fh.seek(frame_idx)
        out = fh.read(n_frames=1)
        xyz = np.ascontiguousarray(out[0][0], dtype=np.float32)
        # mdtraj read() returns (xyz, time_ps, step, cell); cell is (1,3,3)
        # nm, or None when the XTC stores a zero box (hand-made test files)
        # — the viewer never uses PBC, so zeros are honest and harmless.
        if out[3] is None:
            cell = np.zeros((3, 3), dtype=np.float32)
        else:
            cell = np.asarray(out[3][0], dtype=np.float32).reshape(3, 3) * 10.0
        entry = (xyz, cell)
        CACHE.put(key, entry)
        return entry

    def frame(self, frame_idx, flat=None):
        """Return payload bytes in the MDsrv binary frame format (Angstrom).

        flat: None (all atoms) or a cached flat coordinate-index array from
        resolve_selection().
        """
        frame_idx = max(0, min(frame_idx, self.nframes - 1))
        xyz, cell = self._xyz(frame_idx)
        t_ns = self.time_ns(frame_idx)

        if flat is None:
            coords = xyz.reshape(-1)
        else:
            coords = xyz.reshape(-1)[flat]

        payload = b"".join([
            np.int32(self.nframes).tobytes(),
            np.float32(t_ns).tobytes(),
            np.ascontiguousarray(cell, dtype=np.float32).tobytes(),
            np.ascontiguousarray(coords * np.float32(10.0),
                                 dtype=np.float32),
        ])
        return payload


_SOURCES = {}
_SRC_LOCK = threading.Lock()


def get_source(pid):
    """XtcSource for a registered project (created on first use)."""
    p = project(pid)
    if p is None:
        return None
    path = p.get("xtc")
    if not path or not os.path.exists(path):
        return None
    with _SRC_LOCK:
        src = _SOURCES.get(path)
        if src is None:
            src = _SOURCES[path] = XtcSource(path)
        return src


# ----------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC, "library.html")


@app.route("/view/<pid>")
def view_noslash(pid):
    return Response(status=302, headers={"Location": f"/view/{pid}/"})


@app.route("/view/<pid>/")
def view(pid):
    """The interactive viewer for one project (404 for unknown ids)."""
    if project(pid) is None:
        abort(404, description="unknown project")
    return send_from_directory(STATIC, "index.html")


@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC, fname)


@app.route("/vendor/<path:fname>")
def vendor_files(fname):
    return send_from_directory(VENDOR, fname)


@app.route("/api/projects")
def api_projects():
    """Library listing. Deliberately path-free: only basenames + stats."""
    return jsonify({
        "projects": [registry.public(p) for p in reg()["projects"]],
        "base_url": registry.base_url(),
    })


@app.route("/api/project/<pid>")
@app.route("/view/<pid>/api/project")          # viewer page's relative call
def api_project(pid):
    p = project(pid)
    if p is None:
        abort(404, description="unknown project")
    body = registry.public(p)
    src = get_source(pid)
    body["server_frame_cache"] = CACHE.stats()
    body["server_selection_cache"] = src.cache_stats() if src else {}
    return jsonify(body)


# NGL's MdsrvDatasource asks for `${baseUrl}dir/<path>` to list trajectories.
# Serving a listing would expose the filesystem, so the route simply does not
# exist; our viewer never calls it (it addresses one known trajectory).
@app.route("/dir/<path:_path>", methods=["GET", "POST"])
@app.route("/view/<pid>/dir/<path:_path>", methods=["GET", "POST"])
def no_listing(_path, pid=None):
    return abort(404, description="directory listings are not available")


@app.route("/view/<pid>/file/<path:fname>")
def serve_file(pid, fname):
    """Serve a project's structure file.

    The path comes from the registry, never from the URL (`fname` is
    ignored), so this cannot be pointed at anything but a registered GRO.
    No download affordance is offered by the UI; NGL fetches it to render.
    """
    p = project(pid)
    if p is None:
        abort(404, description="unknown project")
    path = p.get("gro")
    if not path or not os.path.exists(path):
        abort(404, description="structure file is missing on the server")
    gz = path + ".gz"
    if (os.path.exists(gz) and
            "gzip" in request.headers.get("Accept-Encoding", "")):
        with open(gz, "rb") as f:
            body = f.read()
        return Response(body, mimetype="text/plain",
                        headers={"Content-Encoding": "gzip",
                                 "Content-Type": "text/plain"})
    return send_from_directory(os.path.dirname(path),
                               os.path.basename(path),
                               mimetype="text/plain")


@app.route("/view/<pid>/traj/numframes/<path:name>")
def numframes(pid, name):
    src = get_source(pid)
    if src is None:
        abort(404, description="unknown project")
    return str(src.nframes), 200, {"Content-Type": "text/plain"}


@app.route("/view/<pid>/traj/frame/<int:frame>/<path:name>",
           methods=["POST", "GET"])
def mdsrv_frame(pid, frame, name):
    """MDsrv-compatible frame endpoint for one project.

    Request body (all optional):
      * empty                    -> all atoms
      * 'start,end;start,end;…'  -> MDsrv text atom ranges (what NGL posts
        for partial selections, e.g. C-alpha-only mode). Parsed once and
        remembered; the response carries 'X-Sel-Token: <key>' (and an
        'X-Sel-Retain: 1' hint). Subsequent requests may then send just
        'tok=<key>' (~25 bytes) instead of the full ~0.6-1.1 MB range list.
        Tokens are project-salted: one project's token is unknown to another.
      * 'tok=<key>'              -> reuse a cached selection
      * 'full'                   -> all atoms (fast path for prefetch)
    """
    src = get_source(pid)
    if src is None:
        abort(404, description="unknown project")

    body = request.get_data()
    flat = None
    token = None
    if body:
        text = body.decode("ascii", errors="replace")
        if text.startswith("atomIndices="):
            text = text[len("atomIndices="):]
        if text.startswith("tok="):
            key = text[4:].strip()
            hit = src.selection(pid, key)
            if hit is src._MISS:
                return "unknown selection token", 422
            flat = hit[0]
            token = key
        elif text == "full":
            flat = None
        else:
            try:
                token, flat, _ = src.resolve_selection(pid, text)
            except ValueError:
                return "bad atomIndices", 400
            except Exception as exc:  # noqa: BLE001
                log(f"selection error: {exc}")
                return str(exc), 500

    try:
        payload = src.frame(frame, flat)
    except Exception as exc:  # noqa: BLE001
        log(f"frame error: {exc}")
        return str(exc), 500

    headers = {"Cache-Control": "no-store"}
    if token:
        headers["X-Sel-Token"] = token
        # Advise the app to keep this selection's parse alive while the
        # matching view mode is active (server keeps an LRU either way).
        headers["X-Sel-Retain"] = "1"
    return Response(payload, mimetype="application/octet-stream",
                    headers=headers)


@app.route("/api/info")
def info():
    """Server status (polled by start_viewer.sh for readiness). Path-free."""
    data = reg()
    return jsonify({
        "projects": len(data["projects"]),
        "project_ids": [p["id"] for p in data["projects"]],
        "server_frame_cache": CACHE.stats(),
        "server_selection_cache": {
            "trajectories": len(_SOURCES),
            "entries": sum(s.cache_stats()["selection_cache_entries"]
                           for s in _SOURCES.values()),
        },
        "base_url": registry.base_url(),
    })


# ----------------------------------------------------------------------
# Legacy single-project URLs -> the first registered project
# ----------------------------------------------------------------------

_LEGACY_TRAJ = "md_center.xtc"
_LEGACY_GRO = "npt_nowater_web.gro"


def _legacy_pid():
    """First project whose XTC is the original one, else the first project."""
    projects = reg()["projects"]
    want = os.path.realpath(os.path.join(BASE, "data", _LEGACY_TRAJ))
    for p in projects:
        try:
            if os.path.realpath(p["xtc"]) == want:
                return p["id"]
        except (KeyError, OSError):
            continue
    return projects[0]["id"] if projects else None


@app.route("/file/<path:fname>")
def legacy_file(fname):
    pid = _legacy_pid()
    if pid is None or (fname != _LEGACY_GRO and fname != GRO_NAME):
        abort(404)
    return serve_file(pid, fname)


@app.route("/traj/numframes/<path:name>")
def legacy_numframes(name):
    pid = _legacy_pid()
    if pid is None:
        abort(404)
    return numframes(pid, name)


@app.route("/traj/frame/<int:frame>/<path:name>", methods=["POST", "GET"])
def legacy_frame(frame, name):
    pid = _legacy_pid()
    if pid is None:
        abort(404)
    return mdsrv_frame(pid, frame, name)


# ----------------------------------------------------------------------
# REGISTERED-FOLDER BROWSING  (Part B)
#
# A folder registration is an ALLOWED ROOT. These routes can only ever list
# directories and MD-relevant file NAMES inside that root, and can only
# open a GRO+XTC pair that lives inside it. Paths come from registry.
# safe_path(), which resolves with realpath and checks component-wise
# containment, so '..', URL-encoded traversal, absolute-path injection,
# duplicate separators and symlink escapes all fail closed. There is no
# download, upload, rename, move or delete route — the browser is strictly
# read-only, and only basenames + folder display names are ever serialised.
#
# Opening a pair creates an in-memory TEMPORARY SESSION (see
# registry.session_as_project); its token works as a <pid> in every
# existing /view/<pid>/ route, so the temp viewer IS the normal viewer.
# ----------------------------------------------------------------------

def _folder_or_404(fid):
    f = registry.get_folder(fid)
    if f is None:
        abort(404, description="unknown folder")
    try:
        registry.folder_root(f)
    except registry.RegistryError:
        abort(404, description="folder is currently unavailable")
    return f


@app.route("/api/folders")
def api_folders():
    """Registered-folder listing. Path-free by construction."""
    return jsonify({"folders": [registry.public_folder(f)
                                for f in registry.all_folders()]})


@app.route("/browse/<fid>")
@app.route("/browse/<fid>/")
def browse_page(fid):
    _folder_or_404(fid)
    return send_from_directory(STATIC, "browse.html")


@app.route("/browse/<fid>/<path:rel>")
def browse_page_sub(fid, rel):
    _folder_or_404(fid)
    return send_from_directory(STATIC, "browse.html")


@app.route("/api/browse/<fid>")
@app.route("/api/browse/<fid>/<path:rel>")
def api_browse(fid, rel=""):
    """One directory inside an allowed root, on demand (never recursive)."""
    f = registry.get_folder(fid)
    if f is None:
        return jsonify(description="unknown folder"), 404
    try:
        listing = registry.list_folder_dir(f, rel)
    except registry.RegistryError as exc:
        # Escape attempts and bad paths both land here -> 403, no detail
        # beyond the fixed message (never the resolved absolute path).
        return jsonify(description=str(exc)), 403
    listing["folder"] = registry.public_folder(f)
    return jsonify(listing)


@app.route("/api/browse/<fid>/open", methods=["POST"])
def api_browse_open(fid):
    """Validate a selected GRO+XTC pair and open a temporary view.

    Body JSON: {"gro": "<rel>", "xtc": "<rel>"} — RELATIVE to the folder
    root, never server paths. Returns a session id usable as /view/<sid>/.
    Frame counting runs through the same cached metadata path as
    add_project.sh, so the first open of a cold XTC can take a minute.
    """
    f = _folder_or_404(fid)
    body = request.get_json(silent=True) or {}
    gro_rel = body.get("gro") or ""
    xtc_rel = body.get("xtc") or ""
    if not isinstance(gro_rel, str) or not isinstance(xtc_rel, str):
        return jsonify(description="gro and xtc must be strings"), 400
    try:
        gro, xtc, metadata = registry.folder_pair_exists(f, gro_rel, xtc_rel)
        components, stats = registry.discover_components(
            gro, metadata.get("natoms"))
    except registry.RegistryError as exc:
        return jsonify(description=str(exc)), 400
    except Exception as exc:  # noqa: BLE001 — corrupt file etc.: no path, no trace
        log(f"folder open failed ({f['id']}): {exc}")
        return jsonify(description="cannot read these files "
                                   "(corrupt GRO or XTC?)"), 400
    sid = registry.create_session(f["id"], gro, xtc, metadata,
                                  components, stats)
    log(f"opened folder view {sid}: {f['id']} / "
        f"{os.path.basename(gro)} + {os.path.basename(xtc)}")
    return jsonify({"session": sid, "url": f"/view/{sid}/"})


@app.route("/api/session/<sid>/save", methods=["POST"])
def api_session_save(sid):
    """Save a temporary folder view as a permanent project (B9).

    Registers the SAME files by path — nothing is copied. Returns the new
    stable project id/url. The temporary session keeps working either way.
    """
    s = registry.get_session(sid)
    if s is None:
        abort(404, description="unknown or expired session")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, description="a project name is required")
    folder = registry.get_folder(s["folder_id"])
    if folder is None:
        abort(400, description="the source folder is no longer registered")
    try:
        proj = registry.add_project(name, s["gro"], s["xtc"],
                                    description=body.get("description"))
    except registry.RegistryError as exc:
        abort(400, description=str(exc))
    return jsonify({"id": proj["id"], "url": f"/view/{proj['id']}/"})


# ----------------------------------------------------------------------
# startup
# ----------------------------------------------------------------------

def warmup():
    """Load every registered project's cached metadata up front; anything
    cold is counted on a single background worker so boot stays fast."""
    data = reg()
    cold = []
    for p in data["projects"]:
        path = p.get("xtc")
        if not path or not os.path.exists(path):
            log(f"warning: project {p['id']}: trajectory missing: "
                f"{os.path.basename(path or '')}")
            continue
        with _SRC_LOCK:
            src = _SOURCES.get(path)
            if src is None:
                src = _SOURCES[path] = XtcSource(path)
        # Registry metadata is authoritative and instant; a cache hit in
        # meta/ makes _ensure_meta a no-op read of a tiny npz.
        md = p.get("metadata") or {}
        if md.get("nframes") and md.get("identity"):
            try:
                n, times, _ = registry.ensure_meta(path, md["identity"])
                src._nframes, src._times_ns = n, times
            except Exception:  # noqa: BLE001
                cold.append(src)
        else:
            cold.append(src)
    if cold:
        threading.Thread(target=lambda: [s.warm() for s in cold],
                         daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("MTV_PORT", registry.load().get("port", "8877")))
    host = os.environ.get("MTV_HOST", "0.0.0.0")
    warmup()
    from waitress import serve
    log(f"serving {len(reg()['projects'])} project(s) on "
        f"http://{host}:{port}")
    # In C-alpha mode NGL POSTs an atom-index list (~46k ranges, ~0.5-1 MB
    # of text), which trips waitress's 1 MB default body limit -> raise it.
    serve(app, host=host, port=port, threads=8,
          max_request_body_size=16 * 1024 * 1024,
          channel_timeout=120)
