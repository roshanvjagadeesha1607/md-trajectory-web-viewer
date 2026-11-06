# MD Trajectory Web Viewer

A reusable, **multi-project** web viewer for GROMACS molecular-dynamics
trajectories. Each simulation is one GRO structure + one XTC trajectory; the
server streams **original** XTC frames on demand to NGL in the browser — no
converted/trimmed copy, no down-sampling, and the trajectory never leaves the
server.

* **One library page** at `/` lists every registered simulation.
* **One direct link per simulation** at `/view/<PROJECT_ID>/` opens the full
  interactive viewer for just that simulation.

Adding a simulation is a single command — no editing of Python/HTML/JS.

---

## Installation

Works on Linux with Python 3.12+ (tested with Python 3.12). No root
access needed, no system packages touched:

```bash
git clone https://github.com/roshanvjagadeesha1607/md-trajectory-web-viewer.git molecular-viewer      # or: extract the ZIP/TAR
cd molecular-viewer
./install.sh                                # creates ./venv, installs deps
./start_viewer.sh                           # starts on port 8877
```

Then open **http://localhost:8877/** — the library starts empty and tells
you how to register your own simulations:

```bash
./add_project.sh                            # one GRO + one XTC
#   (interactive, or: ./add_project.sh "Name" /abs/path.md.gro /abs/md.xtc)

./add_folder.sh "My Runs" /home/you/simulations   # whole folder, browsable
                                                  # in the web UI (read-only)
```

Other commands:

```bash
./list_projects.sh      ./edit_project.sh ID ...   ./remove_project.sh ID
./list_folders.sh       ./edit_folder.sh ID ...    ./remove_folder.sh ID
./rescan_projects.sh                               # re-detect components
./stop_viewer.sh
```

Every script resolves its own directory — the tool runs from wherever you
extracted it (`~/mdviewer`, `/opt/mdviewer`, …). Settings come from
environment variables, all optional: `MTV_PORT` (default **8877**),
`MTV_HOST` (default `0.0.0.0`, LAN-visible; set `127.0.0.1` for local-only),
`MTV_BASE_URL` (behind a public HTTPS proxy), `MTV_CACHE_GB` (server frame
cache budget, default 3).

Registered folders are **allowed roots** for the read-only web browser:
they may only live inside your home directory, the browser can never leave
one, never sees absolute paths, and never offers downloads or writes.
Removing a folder or project removes only the registry entry — your
simulation files are never modified, moved or deleted.

---

## Quick start

```bash
./start_viewer.sh
```

Then open the library:

* Local: <http://localhost:8877/>
* LAN:   <http://YOUR_LAN_IP:8877/>   (IP detected dynamically)

Stop with `./stop_viewer.sh`. Port overrides: `MTV_PORT=9000 ./start_viewer.sh`.

---

## Adding a simulation

You only ever provide three things: a **display name**, a **GRO path** and an
**XTC path**. Everything else — URL slug, atom count, frame count, time range,
spacing, **and the full component inventory (ligands, ions, water, cofactors,
unknown residues)** — is discovered automatically from the GRO file. You never
have to name a ligand or ion, for this system or any future one.

```bash
./add_project.sh "Descriptive Name" \
    /abs/path/to/structure.gro \
    /abs/path/to/trajectory.xtc
```

Example (using generic simulation files):

```bash
./add_project.sh "Example Simulation" /path/to/structure.gro /path/to/trajectory.xtc
```

Components are discovered automatically from the input files.

Optional flags:

| Flag | Meaning |
| --- | --- |
| `--description "..."` | Free text shown on the library card and viewer header. |
| `--ligands GTP,-K` | **Deprecated.** Discovery already finds everything; this only forces which checkboxes start ticked (prefix `-` to hide). |
| `--id my-slug` | Force a specific URL id instead of the auto-generated one. |
| `--skip-checks` | Register without opening the files (no metadata or discovery until `rescan_projects.sh`). |

Run it with **no arguments** for an interactive prompt (asks for name, GRO,
XTC, description — nothing else).

### Component auto-discovery

The GRO is the authoritative structure definition. At registration every
residue name is inventoried and classified:

| Category | Rule | Default |
| --- | --- | --- |
| Protein / nucleic acid | standard residue-name sets | always rendered (cartoon) |
| Water / solvent | known water names (SOL, HOH, WAT, TIP*, SPC…) | checkbox **off**, atom count + ⚠ shown |
| Ion | any other **single-atom** residue (MG, K, NA, CL, ZN, …) | visible — except bulk monovalent salt (NA/K/CL/…), which starts hidden but stays one click away |
| Ligand / cofactor | any other multi-atom residue — **including completely unknown names** | visible |

Nothing is ever silently discarded: an unrecognised residue (XYZ, a modified
residue, a drug) appears as a non-standard ligand with its own checkbox, its
own licence-stable colour and a licorice/ball-and-stick representation. The
classification sets are only ever hints for colour/default — never filters.

What `add_project.sh` does, and does not, do:

* ✅ Checks the GRO and XTC exist, are regular files, and are readable.
* ✅ Verifies the **GRO and XTC atom counts match** (rejects a mismatch —
  these files must belong together).
* ✅ Scans the GRO once and stores per-component residue/atom counts and a
  category in `projects.json` (counts only — never coordinates).
* ✅ Extracts atom count, frame count, first/last frame time and the frame
  spacing from the files themselves (never assumed).
* ✅ Generates a safe, unique, URL-friendly id from the name
  (`Example Simulation` → `example-simulation`; collisions get `-2`, `-3`…).
* ✅ Prints the direct URL (localhost + LAN) and the library URL.
* ❌ Does **not** copy the XTC (it can be many GB).
* ❌ Does **not** modify, move or delete the original GRO/XTC — they are only
  opened read-only.

The first registration of a brand-new XTC counts its frames with a one-time
sequential pass (~1 min per few GB); this is cached under `meta/` keyed by the
file's identity, so re-registering the same trajectory (or a second project
pointing at the same file) is instant, and any change to the file is detected.

---

## Managing simulations

```bash
./list_projects.sh                       # show every registered project + URLs
./remove_project.sh PROJECT_ID           # delete the registry entry ONLY
./edit_project.sh PROJECT_ID --name "New display name"
./edit_project.sh PROJECT_ID --description "..."
./edit_project.sh PROJECT_ID --gro /abs/new.gro --xtc /abs/new.xtc   # re-validates + re-discovers
./edit_project.sh PROJECT_ID             # interactive (Enter keeps, '-' clears)
./rescan_projects.sh                     # re-discover components from every GRO
./rescan_projects.sh PROJECT_ID          # …or from one project's GRO
```

`rescan_projects.sh` re-reads each project's GRO structure file (read-only)
and rebuilds the component table — use it after upgrading the classifier, or
to migrate a project registered by an older version. Checkbox choices already
made in `projects.json` are preserved. `edit --gro/--xtc` re-discovers
automatically; the server hot-reloads the new metadata (no restart).

* **Removing a project deletes only its entry in `projects.json`.** The GRO
  and XTC files are never touched and remain on disk. A confirmation prompt
  runs first (`--yes` to skip).
* **Renaming never changes the URL.** The id (and therefore `/view/ID/`) stays
  stable unless you pass `--id` explicitly, so links you have shared keep
  working.

Administration is **command-line only** — there is deliberately no web admin
interface.

---

## Where projects are registered

Everything lives in **`projects.json`** (created next to `serve.py`):

```json
{
  "version": 1,
  "base_url": null,
  "port": 8877,
  "projects": []
}
```


You can edit this file directly if you prefer; the server hot-reloads it on
change (no restart needed). Absolute paths live **only** here and in the
server — they are never sent to a browser (the API returns basenames +
stats). The `meta/` directory holds the frame-count cache.

---

## Security model

* **Only registered projects are reachable.** A project is addressed by a
  validated slug (`[a-z0-9][a-z0-9_-]{0,63}`); an unknown or malformed id is a
  plain 404.
* **No path traversal, by construction.** Data URLs never carry a filesystem
  path — `.../file/<anything>` and `.../traj/frame/<n>/<anything>` ignore the
  trailing name and resolve the file from the registry entry. There is no
  route that joins user input to the filesystem.
* **No unrestricted directory listings.** Registered simulation folders can be browsed read-only. NGL's `MdsrvDatasource` would happily call
  `dir/...`; that endpoint intentionally does not exist.
* **No arbitrary files, no downloads.** `/file/` serves a registered project's
  GRO for NGL to render and nothing else; there is no download UI for the GRO
  or XTC; the trajectory is only ever served frame-by-frame to the canvas.
* **GRO/XTC are read-only.** Nothing in the app writes to a simulation file.
* Verified by `tools/multi_project_test.py` (see Verification below).

---

## Architecture & performance

```
Browser (NGL 2.4.0)                         Server (Flask + waitress)
  /view/<ID>/  index.html   ─── GET ───────▶  /view/<ID>/                project page
     api/project          ─── GET ───────▶  /view/<ID>/api/project      metadata (no paths)
     file/structure.gro   ─── GET ───────▶  /view/<ID>/file/...         registered GRO
     traj/numframes/...   ─── GET ───────▶  /view/<ID>/traj/numframes/  frame count
     traj/frame/<n>/...   ─── POST ──────▶  /view/<ID>/traj/frame/<n>/  one MDsrv frame
```

Because NGL composes every request from the datasource `baseUrl`, serving the
page at `/view/<ID>/` scopes all of a project's traffic to its own namespace
with **no patching of the vendored NGL build** — and two visitors on two
different projects are fully independent.

All the single-project optimizations are preserved and are now
project-aware:

* **Playback detail modes** — *Cα (Fast)* (default), *Backbone*, *Full atoms*.
  Only the visible atoms are transferred (unticked components are not in
  the selection): transfer size depends on the selected atoms.
* **Selection tokens** — NGL re-POSTs the atom-range selection text every
  frame; the server parses it once, returns `X-Sel-Token`, and the browser
  POSTs `tok=<token>` (25 B) afterwards. Tokens are salted with the project id,
  so one project's token is never honoured by another (an unknown token is a
  422, which the client's watchdog recovers from).
* **Bounded server frame cache — GLOBAL, not per project.** Decoded frames live
  in one byte-bounded LRU (default 3 GB, `MTV_CACHE_GB` to change). The key is
  `(trajectory file, frame index)`, so *project A frame 100 can never collide
  with project B frame 100*, while two projects sharing a file share one entry.
  Adding more projects never grows total memory. Parsed selections are cached
  per `(project, token)` (LRU 32).
* **Client prefetch + bounded cache** — 5–16 future frames fetched ahead
  (batches of 3, chained); the browser frame cache is evicted at ~160 MB.
* **Playback frame step** — Play can skip every 2nd/5th frame; slider, ⏪/⏩ and
  jump-to-frame always reach every original frame.
* **In-place coordinate updates** — NGL copies new coords into existing typed
  arrays each frame (no rep rebuild); component toggles flip rep visibility.
* **Stale-token / stall watchdog** — a 2 s-interval watchdog (20 s stuck
  threshold) clears the loader and re-learns tokens after a server restart or
  network blip.

### Surface representation

Alongside **Cartoon / Backbone / Line** there is a fourth protein
representation, **Surface**: NGL's native molecular-surface mesh
(`surfaceType: 'ms'`, 1.4 Å probe, coarse `scaleFactor 1` / `smooth 1` grid —
low/medium quality so builds stay tractable, computed in NGL's molsurf
worker). It is *not* VMD's QuickSurf; it is NGL's own algorithm. A slider
(20–100 %) controls surface opacity, and the surface is coloured per residue
by the same scheme as the cartoon, using exactly the atoms the current detail
mode transfers (in Cα/Backbone mode that is the trace only — switch to *Full
atoms* for a true molecular surface).

The mesh is **rebuilt from scratch whenever the frame or selection changes**
— dozens of full mesh builds per second is what playback would demand, so
**Play is disabled while Surface is active** (the button explains why; the
viewer shows a note when you enter Surface mode). Surface is therefore a
paused-frame inspection tool: step with ⏪/⏩ or the slider — each step
rebuilds the mesh (time depending on system size and hardware) — and switch back
to Cartoon/Backbone/Line to play again. Playback and all transfer
optimizations are untouched by this representation: nothing new is
streamed; the mesh is built entirely client-side.

---

## Serving behind a future public HTTPS URL

The whole app builds URLs from a single knob. Set a base URL and every link
the tools print and the API returns follows it:

```bash
export MTV_BASE_URL="https://md.example.org"      # or set "base_url" in projects.json
```

Links are otherwise host-relative (they follow whatever host/port serves the
app), so a reverse proxy or tunnel can be added later with **no code change**.
Public exposure/tunneling is intentionally **not** configured here.

---

## Layout

* `serve.py` — HTTP server: library page, per-project routes, frame streaming.
* `registry.py` — project registry + metadata extraction (read-only re: data).
* `projects.json` — the registry (created on first add; the source of truth).
* `static/` — `library.html` (homepage) and `index.html` (project viewer).
* `add_project.sh`, `list_projects.sh`, `remove_project.sh`, `edit_project.sh`
  — thin wrappers over `tools/add_project.py` and `tools/manage_projects.py`.
* `tools/` — tests + utilities.
* `logs/`, `venv/`, `vendor/`, `meta/` — runtime, deps, bundled JS, frame-count cache.

Legacy single-project routes remain available for compatibility.

---

## Verification

The repository contains automated checks and development utilities in `tools/`.

For a functional test:

1. Install dependencies using `./install.sh`.
2. Start the viewer using `./start_viewer.sh`.
3. Register your own compatible GRO and XTC files.
4. Open the registered simulation in the browser.
5. Verify structure loading, frame navigation, playback, representations and component visibility.

No simulation trajectory files are distributed with this repository.

## Third-party components

- NGL Viewer: molecular visualization library, MIT license.
  License notice: `vendor/LICENSE-NGL`.
- Three.js: source of marching-cubes lookup tables, MIT license.
  License notice: `vendor/LICENSE-threejs`.

The project uses these components according to their respective licenses.
