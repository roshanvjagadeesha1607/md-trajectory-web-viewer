// ======================================================================
// Selection OVERLAY — one overlay per representation.
//
// The VMD "Graphical Representations" model needs MANY overlays alive at
// once. An Overlay is therefore an INSTANTIABLE object (window.SelectionOverlay
// is the class), each owning its own NGL Shape + ShapeComponent + render
// state, so adding / editing / hiding / deleting one representation touches
// no other one. The main protein reps, component reps, surface and Gaussian
// pipeline are never touched (each overlay lives on its own component, the
// pattern gaussiansurf.js already uses).
//
// Why not comp.addRepresentation('licorice', {sele:'@1,2,3…'}):
//   1. A huge '@' string would be re-parsed by NGL on every selection change
//      and structure refresh — O(N) parser work, precisely what performance
//      rules forbid.
//   2. comp representations also subscribe to trajectory position updates, so
//      a huge atomSet adds per-frame cost inside NGL's hot path.
//
// Each Overlay builds NGL GPU buffers directly (SphereBuffer /
// CylinderBuffer, exported by the bundled build):
//
//   * creation: one typed-array pass over the selected indices.
//   * animation: every trajectory frame rewrites position attributes IN PLACE
//     from the live atomStore (buffer.setAttributes — the impostor sphere and
//     cylinder buffers re-expand their per-primitive arrays internally), so all
//     representations follow the movie with zero geometry rebuild and zero
//     string parsing.
//   * atoms the current mode does not stream keep their GRO (frame-0)
//     coordinates, so their overlay atoms sit still while streamed ones move —
//     the honest consequence of the Cα/Backbone streaming design, surfaced by
//     the UI, not hidden.
//
// Bonds (Licorice / Ball+Stick): ORIGINAL loaded topology ONLY. The parsed
// structure carries NGL's covalent-radius bonds for EVERY atom (bondStore is
// not limited to the streamed subset); a cylinder is drawn for a topology
// bond (i,j) iff both ends are selected, and its endpoints always read the
// coordinates of those same GLOBAL indices. Nothing is inferred from spatial
// proximity or selection adjacency — that is how cross-protein phantom
// diagonals get drawn.
//
// Memory discipline (§15): an Overlay stores its atom-identity list (Int32Array)
// plus per-primitive display buffers only. It never copies the coordinate
// store; every frame reads positions straight from the shared atomStore.
// ======================================================================
(function (global) {
  'use strict';

  var VDW_SCALE = 1.0, SPHERE_SCALE = 0.5, BALL_SCALE = 0.35,
      LINE_RADIUS = 0.12, STICK_RADIUS = 0.24, LICORICE_RADIUS = 0.24;
  // coarse element radius table (Å); unknowns → 1.7
  var RAD = { H: 0.31, C: 1.7, N: 1.55, O: 1.52, F: 1.47, S: 1.8, P: 1.8,
              CL: 1.75, BR: 1.85, I: 1.98, MG: 1.73, NA: 2.27, K: 2.75,
              CA: 2.31, ZN: 1.39, FE: 1.56, MN: 1.61, CU: 1.4, NI: 1.63 };

  function radiusFor(eng, idx) {
    return RAD[String(eng.elementFor(idx)).toUpperCase()] || 1.7;
  }
  function isHydrogen(eng, at) {
    var e = String(eng.elementFor(at)); return e === 'H' || e === 'D';
  }

  // ------------------------------------------------------------------
  // Overlay — an independently addressable selection representation.
  // ------------------------------------------------------------------
  function Overlay(stage, ctx, id) {
    this.stage = stage;
    this.ctx = ctx;            // { comp(), meta(), app(), view(), frame(), engine() }
    this.id = id;
    this.shape = null;
    this.comp = null;
    this.state = null;         // see _build
    this.refreshes = 0;
  }

  Overlay.prototype._detach = function () {
    if (this.comp) {
      try { this.stage.removeComponent(this.comp); } catch (e) {}
    }
    this.comp = null; this.shape = null; this.state = null;
  };
  // Mount a freshly built overlay. ORDER MATTERS and is load-bearing: this
  // NGL bundle's buffer representation snapshots the shape's buffer list at
  // addRepresentation() time — it exposes no bufferAdded/bufferUpdated signal
  // (verified against vendor/ngl.min.js: 5× addBuffer, 0× bufferAdded), so a
  // buffer pushed AFTER addRepresentation is silently never rendered
  // (component exists, atom count correct, zero triangles in the scene).
  // Therefore: create Shape → addBuffer(every buffer) → THEN the component +
  // representation. Verified: buffers added before the rep contribute real
  // triangles to the WebGL render; after the rep contribute none.
  Overlay.prototype._mount = function (buffers) {
    this.shape = new NGL.Shape('selection-' + this.id);
    for (var i = 0; i < buffers.length; i++) this.shape.addBuffer(buffers[i].buf);
    this.comp = this.stage.addComponentFromObject(this.shape);
    this.comp.addRepresentation('buffer', {});
  };

  // ---- construction -------------------------------------------------
  // Build one render state from a selection result. One pass over the
  // selected indices; typed arrays only; no strings, no NGL selection parsing.
  Overlay.prototype._build = function (res, rep, colorMode) {
    var st = this.ctx.comp().structure;
    var store = st.atomStore;
    var eng = this.ctx.engine();
    var idx = res.indices, n = idx.length;
    var i, k, c3;

    var position = new Float32Array(n * 3);
    var color = new Float32Array(n * 3);
    var radius = new Float32Array(n);
    var chainCache = Object.create(null), resCache = Object.create(null),
        nameCache = Object.create(null);
    var proxy = st.getAtomProxy();

    function setCol(i, hex) {
      color[3 * i] = ((hex >> 16) & 255) / 255;
      color[3 * i + 1] = ((hex >> 8) & 255) / 255;
      color[3 * i + 2] = (hex & 255) / 255;
    }
    function fillRadius(scale) {
      for (var i2 = 0; i2 < n; i2++) radius[i2] = radiusFor(eng, idx[i2]) * scale;
    }

    var useVdw = rep === 'spacefill';
    for (i = 0; i < n; i++) {
      var a = idx[i];
      position[3 * i] = store.x[a];
      position[3 * i + 1] = store.y[a];
      position[3 * i + 2] = store.z[a];
      if (colorMode === 'element') setCol(i, eng.elementColor(eng.elementFor(a)));
      else if (colorMode === 'chain') {
        proxy.index = a; var ch = String(proxy.chainname);
        var cc = chainCache[ch]; if (cc === undefined) { cc = eng.chainColor(ch); chainCache[ch] = cc; }
        setCol(i, cc);
      } else if (colorMode === 'residue') {
        var rk = res.atomResKey[a];
        var rc = resCache[rk]; if (rc === undefined) { rc = eng.residueColor(rk); resCache[rk] = rc; }
        setCol(i, rc);
      } else if (colorMode === 'name') {
        proxy.index = a; var nm = String(proxy.atomname);
        var nc = nameCache[nm]; if (nc === undefined) { nc = eng.nameColor(nm); nameCache[nm] = nc; }
        setCol(i, nc);
      } else setCol(i, 0xffe066);          // uniform warm yellow — reads on any protein colour
      radius[i] = useVdw ? radiusFor(eng, a) * VDW_SCALE
                : rep === 'sphere' ? radiusFor(eng, a) * SPHERE_SCALE
                : rep === 'line' ? LINE_RADIUS
                : LICORICE_RADIUS;
    }

    // identity list: Int32Array, not a copied coordinate array (§15)
    var idxArr = new Int32Array(n);
    for (i = 0; i < n; i++) idxArr[i] = idx[i];

    // slot lookup for bond ends
    var slotOf = new global.Map();
    for (i = 0; i < n; i++) slotOf.set(idxArr[i], i);

    // bonds for licorice / ball+stick — ORIGINAL TOPOLOGY ONLY. A cylinder
    // is drawn for a bondStore pair (global i, global j) iff BOTH ends are
    // selected; nothing is ever inferred from spatial proximity or from
    // adjacency in the selection array (an inferred bond between two
    // neighbouring selected atoms in different parts of the molecule renders
    // as a long diagonal line across the protein).
    var bonds = null;
    if ((rep === 'licorice' || rep === 'ball+stick') && n > 1) {
      var b1 = [], b2 = [];
      var bs = st.bondStore, sel = res.mask;
      for (k = 0; k < bs.count; k++) {
        var u = bs.atomIndex1[k], v = bs.atomIndex2[k];
        if (sel[u] && sel[v] && !isHydrogen(eng, u) && !isHydrogen(eng, v)) { b1.push(u); b2.push(v); }
      }
      if (b1.length) {
        var nb = b1.length;
        var su = new Int32Array(nb), sv = new Int32Array(nb);
        for (k = 0; k < nb; k++) { su[k] = slotOf.get(b1[k]); sv[k] = slotOf.get(b2[k]); }
        bonds = { su: su, sv: sv, n: nb };
      }
    }

    // buffers
    var buffers = [];
    function atomBuffer(radArr) {
      return new NGL.SphereBuffer({ position: position, color: color, radius: radArr, picking: false });
    }
    if (rep === 'sphere' || rep === 'spacefill' || rep === 'line') {
      buffers.push({ kind: 'atoms', buf: atomBuffer(radius.slice()) });
    } else {                                  // licorice / ball+stick
      if (rep === 'ball+stick') fillRadius(BALL_SCALE);
      buffers.push({ kind: 'atoms', buf: atomBuffer(radius.slice()) });
      if (bonds) {
        var pos1 = new Float32Array(bonds.n * 3), pos2 = new Float32Array(bonds.n * 3);
        var col1 = new Float32Array(bonds.n * 3), col2 = new Float32Array(bonds.n * 3);
        var rad = new Float32Array(bonds.n).fill(rep === 'licorice' ? LICORICE_RADIUS : STICK_RADIUS);
        for (k = 0; k < bonds.n; k++) {
          for (c3 = 0; c3 < 3; c3++) {
            pos1[3 * k + c3] = position[3 * bonds.su[k] + c3];
            pos2[3 * k + c3] = position[3 * bonds.sv[k] + c3];
            col1[3 * k + c3] = color[3 * bonds.su[k] + c3];
            col2[3 * k + c3] = color[3 * bonds.sv[k] + c3];
          }
        }
        // NGL's cylinder API names the per-endpoint colours `color`/`color2`
        // (there is no `color1` — the impostor constructor rejects it).
        var cb = new NGL.CylinderBuffer({
          position1: pos1, position2: pos2, color: col1, color2: col2,
          radius: rad, picking: false
        });
        buffers.push({ kind: 'bonds', buf: cb, bonds: bonds,
                       p1: new Float32Array(bonds.n * 3), p2: new Float32Array(bonds.n * 3) });
      }
    }
    return { buffers: buffers, idx: idxArr, position: position, res: res,
             rep: rep, colorMode: colorMode, visible: true, lastRefreshMs: 0 };
  };

  // ---- per-frame animation -----------------------------------------
  // Rewrite position attributes in place from the live structure. Never
  // rebuilds geometry, never parses a string. The bundled NGL build uses
  // impostor (GPU-instanced) buffers, and buffer.setAttributes re-expands the
  // rendered `position` attribute from the per-atom source arrays internally
  // (verified against this exact bundle: source midpoint -> expanded rows),
  // so one O(selection) pass per frame is all it takes.
  //
  // FRAME CONSISTENCY (fixes the long-diagonal-bond bug): streamed atoms read
  // atomStore (current frame F). Non-streamed atoms must come from the
  // side channel's data for EXACTLY frame F — never a cached older/newer frame
  // and never the GRO entry in atomStore — because this trajectory is PBC
  // wrapped: a bonded pair drawn with one end at frame F and the other at
  // frame 0 can sit 100 Å apart, which renders as a giant cylinder across the
  // molecule. While the exact frame has not landed yet, a covered overlay
  // stays mounted but hidden; SelCoords.onChange re-refreshes (and un-hides)
  // it the moment the frame arrives. Fetch latency is one frame, so this is
  // invisible in playback.
  Overlay.prototype._applyVisibility = function () {
    if (!this.comp) return;
    var on = !!(this.state && this.state.visible) && !this.state._pending;
    try { this.comp.setVisibility(!!on); } catch (e) {}
  };
  Overlay.prototype.refresh = function () {
    var state = this.state;
    if (!state || !this.comp) return;
    var store = this.ctx.comp().structure.atomStore;
    var idx = state.idx, n = idx.length, pos = state.position;
    var coords = this.ctx.coords && this.ctx.coords();
    var cd = null;
    if (coords && coords.count()) {
      // covered = at least one of my atoms is in the side-channel union,
      // i.e. it must move but atomStore cannot follow it. Recomputed per
      // refresh: the union grows/shrinks as rows are added/removed.
      var covered = false;
      for (var i = 0; i < n; i++) {
        if (coords.indexOf(idx[i]) >= 0) { covered = true; break; }
      }
      state._scCovered = covered;
      if (covered) {
        cd = coords.get(this.ctx.frame());
        if (!cd) {
          // Wait for exactly this frame. If nothing has been painted yet the
          // component stays hidden (no half-frame geometry); once one
          // consistent frame is on screen it simply holds — one stale frame
          // beats flicker, and it never mixes two frames into a cylinder.
          if (!state._everPainted && !state._pending) {
            state._pending = true; this._applyVisibility();
          }
          return;
        }
        if (state._pending) { state._pending = false; this._applyVisibility(); }
      }
      // else (dynamic row / side-channel overflow): not covered — those
      // atoms stay on their atomStore entry, the documented GRO behaviour.
      // Patching from cached older frames would mix frames and redraw the
      // PBC-jump diagonals, so no patch at all.
    }
    for (var i2 = 0; i2 < n; i2++) {
      var a = idx[i2];
      pos[3 * i2] = store.x[a];
      pos[3 * i2 + 1] = store.y[a];
      pos[3 * i2 + 2] = store.z[a];
    }
    if (cd) {
      for (var i3 = 0; i3 < n; i3++) {
        var slot = coords.indexOf(idx[i3]);
        if (slot >= 0) {
          pos[3 * i3] = cd[3 * slot];
          pos[3 * i3 + 1] = cd[3 * slot + 1];
          pos[3 * i3 + 2] = cd[3 * slot + 2];
        }
      }
    }
    var t0 = performance.now();
    try {
      for (var b = 0; b < state.buffers.length; b++) {
        var e = state.buffers[b];
        if (e.kind === 'atoms') {
          e.buf.setAttributes({ position: pos });
        } else {
          var nb = e.bonds.n, su = e.bonds.su, sv = e.bonds.sv;
          for (var k = 0; k < nb; k++) {
            for (var c3 = 0; c3 < 3; c3++) {
              e.p1[3 * k + c3] = pos[3 * su[k] + c3];
              e.p2[3 * k + c3] = pos[3 * sv[k] + c3];
            }
          }
          e.buf.setAttributes({ position1: e.p1, position2: e.p2 });
        }
      }
    } catch (err) {
      if (!state._warned) { state._warned = true;
        console.warn('selection overlay: in-place update failed', err); }
      return;                              // leave last frame on screen
    }
    state.lastRefreshMs = +(performance.now() - t0).toFixed(1);
    if (!state._everPainted) { state._everPainted = true; this._applyVisibility(); }
    this.refreshes++;
    try { this.stage.viewer.requestRender(); } catch (e2) {}
  };

  // ---- public -------------------------------------------------------
  // apply(res, rep, colorMode) — render an ALREADY-EVALUATED selection.
  // The manager owns expression evaluation; the overlay is pure display, so
  // the same engine result could feed several styles.
  Overlay.prototype.apply = function (res, rep, colorMode) {
    this._detach();
    if (!res || !res.nAtoms) return;
    // build FIRST (pure typed arrays, no scene objects), then mount with the
    // buffers already on the shape — see the ordering note on _mount().
    this.state = this._build(res, rep, colorMode);
    this._mount(this.state.buffers);
    // Re-patch from the CURRENT frame before the first paint: _build read
    // atomStore, which mid-movie holds frame F only for streamed atoms —
    // refresh() either produces a fully consistent buffer or defers (hidden).
    this.refresh();
    try { this.stage.viewer.requestRender(); } catch (e) {}
  };
  Overlay.prototype.clear = function () { this._detach(); };
  Overlay.prototype.setVisible = function (on) {
    if (this.state) this.state.visible = !!on;
    this._applyVisibility();             // honours the pending-frame gate
  };
  Overlay.prototype.active = function () { return !!this.state; };
  Overlay.prototype.stats = function () {
    var s = this.state;
    if (!s) return null;
    return { atoms: s.idx.length, buffers: s.buffers.length,
             rep: s.rep, color: s.colorMode, visible: !!s.visible,
             lastRefreshMs: s.lastRefreshMs, refreshes: this.refreshes };
  };

  global.SelectionOverlay = Overlay;
})(window);
