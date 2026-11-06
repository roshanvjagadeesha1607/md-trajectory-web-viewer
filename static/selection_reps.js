// ======================================================================
// SELECTION REPRESENTATION MANAGER — the VMD "Graphical Representations"
// list, client-side.
//
// Conceptually (per the requirement):
//
//   reps = [ { id, expression, atomIndices, style, color, visible, dynamic,
//              atomCount, residueCount, overlay }, ... ]
//
// There is NO single global selection: every row is independently
// addressable, owns its own SelectionOverlay (own NGL Shape/ShapeComponent)
// and its own atom identity list. Adding, editing, hiding, duplicating or
// deleting one representation cannot affect any other.
//
// Performance (§15): the manager evaluates expressions through the shared
// engine (one SerialStore, the structure's single live spatialHash — the same
// index serves every `within` of the same frame; nothing is rebuilt per rep).
// An invisible dynamic rep is NOT re-evaluated per frame (nothing would see
// it); it is brought current once when switched back on. Each overlay stores
// its atom identities as an Int32Array and reads coordinates straight from
// the shared atomStore — coordinate arrays are never duplicated per rep.
// ======================================================================
(function (global) {
  'use strict';

  var STYLES = ['licorice', 'ball+stick', 'sphere', 'line', 'spacefill'];
  var COLORS = ['element', 'name', 'residue', 'chain', 'uniform'];
  var STYLE_LABEL = { 'licorice': 'Licorice', 'ball+stick': 'Ball+Stick',
                      'sphere': 'Sphere', 'line': 'Line', 'spacefill': 'Spacefill' };
  var COLOR_LABEL = { 'element': 'Element', 'name': 'Name',
                      'residue': 'Residue', 'chain': 'Chain', 'uniform': 'Uniform' };

  function RepManager(stage, ctx, opts) {
    this.stage = stage;
    this.ctx = ctx;                  // { comp(), meta(), app(), view(), frame(), engine() }
    opts = opts || {};
    this.onChange = opts.onChange || function () {};   // row-list / status refresh
    this.warn = opts.warn || function (m) { console.warn(m); };
    this.reps = [];
    this.activeId = null;
    this._nextId = 1;
    this._defaultStyle = 'licorice';
    this._defaultColor = 'element';
    this._defaultDynamic = false;
  }

  RepManager.prototype.STYLES = STYLES;
  RepManager.prototype.COLORS = COLORS;
  RepManager.prototype.STYLE_LABEL = STYLE_LABEL;
  RepManager.prototype.COLOR_LABEL = COLOR_LABEL;

  RepManager.prototype.defaults = function (d) {
    if (d.style !== undefined) this._defaultStyle = d.style;
    if (d.color !== undefined) this._defaultColor = d.color;
    if (d.dynamic !== undefined) this._defaultDynamic = !!d.dynamic;
  };

  RepManager.prototype.get = function (id) {
    for (var i = 0; i < this.reps.length; i++) if (this.reps[i].id === id) return this.reps[i];
    return null;
  };
  RepManager.prototype.active = function () { return this.get(this.activeId); };

  // evaluate + render one rep. Returns the rep, or throws on a parse error.
  RepManager.prototype._recompute = function (rep) {
    var res = this.ctx.engine().evaluate(rep.expression);
    rep.result = res;
    rep.atomIndices = res.indices;
    rep.atomCount = res.nAtoms;
    rep.residueCount = res.nResidues;
    rep.spatial = !!res.spatial;
    rep.spatialPartial = !!res.spatialPartial;
    rep.nNotStreamed = res.nNotStreamed;
    rep.ambiguousSerial = !!res.ambiguousSerial;
    rep.atom = res.atom;
    rep.overlay.apply(res, rep.style, rep.color);
    rep.overlay.setVisible(rep.visible);
    rep.lastDyn = this.ctx.frame();
    this._syncCoords();
    return rep;
  };

  // ---- non-streamed coordinate side channel -------------------------
  // The union of every live STATIC row's not-currently-streamed atoms is
  // handed to SelCoords (ctx.coords(), optional), which fetches exactly
  // those atoms' current-frame coordinates from the existing frame
  // endpoint. Dynamic rows deliberately stay out of the union (their
  // identity list changes per frame; re-tokenising a server selection every
  // frame is exactly the O(N) parse the design forbids) — the UI already
  // labels their non-streamed atoms GRO-frozen.
  RepManager.prototype._syncCoords = function () {
    var coords = this.ctx.coords && this.ctx.coords();
    if (!coords) return;
    var out = [];
    for (var i = 0; i < this.reps.length; i++) {
      var rep = this.reps[i];
      if (!rep.dynamic && rep.overlay.active()) {
        var idx = rep.overlay.state.idx;
        for (var k = 0; k < idx.length; k++) out.push(idx[k]);
      }
    }
    if (!out.length) { coords.setAtoms([]); return; }
    // only atoms the CURRENT mode does not stream need the side channel
    var sv = this.ctx.comp().structureView;
    var set = sv && sv.atomSet ? sv.atomSet : null;
    if (set) {
      var filtered = [];
      for (var m = 0; m < out.length; m++) if (!set.isSet(out[m])) filtered.push(out[m]);
      out = filtered;
    }
    coords.setAtoms(out);
    coords.setFrame(this.ctx.frame());
  };

  // ---- CRUD ----------------------------------------------------------
  // add({expression, style, color, dynamic}) -> {ok, rep} | {ok:false, error}
  RepManager.prototype.add = function (spec) {
    spec = spec || {};
    var expr = String(spec.expression || '').trim();
    if (!expr) return { ok: false, error: 'empty selection' };
    var style = spec.style !== undefined ? spec.style : this._defaultStyle;
    var color = spec.color !== undefined ? spec.color : this._defaultColor;
    if (STYLES.indexOf(style) < 0) style = this._defaultStyle;
    if (COLORS.indexOf(color) < 0) color = this._defaultColor;
    var id = this._nextId++;
    var rep = {
      id: id,
      expression: expr,
      style: style, color: color,
      visible: true,
      dynamic: spec.dynamic !== undefined ? !!spec.dynamic : false,
      atomIndices: null, atomCount: 0, residueCount: 0,
      spatial: false, spatialPartial: false, nNotStreamed: 0,
      ambiguousSerial: false, atom: null, result: null,
      lastDyn: -1,
      overlay: new global.SelectionOverlay(this.stage, this.ctx, id)
    };
    try { this._recompute(rep); }
    catch (e) {
      rep.overlay.clear();
      return { ok: false, error: (e && e.message) || String(e) };
    }
    this.reps.push(rep);
    this.activeId = rep.id;
    // _recompute's _syncCoords ran before this rep was in the list — grow the
    // side-channel union now that it is.
    this._syncCoords();
    this.onChange('add', rep);
    return { ok: true, rep: rep };
  };

  // update(id, {expression?, style?, color?, dynamic?, visible?})
  // Touches exactly one representation.
  RepManager.prototype.update = function (id, patch) {
    var rep = this.get(id);
    if (!rep) return { ok: false, error: 'no such representation' };
    patch = patch || {};
    var newExpr = patch.expression !== undefined ? String(patch.expression).trim() : rep.expression;
    var needRecompute = (newExpr !== rep.expression) ||
                        (patch.style !== undefined && patch.style !== rep.style) ||
                        (patch.color !== undefined && patch.color !== rep.color);
    if (patch.style !== undefined && STYLES.indexOf(patch.style) >= 0) rep.style = patch.style;
    if (patch.color !== undefined && COLORS.indexOf(patch.color) >= 0) rep.color = patch.color;
    if (patch.expression !== undefined) rep.expression = newExpr;
    if (patch.dynamic !== undefined) rep.dynamic = !!patch.dynamic;
    if (needRecompute) {
      try { this._recompute(rep); }
      catch (e) {
        // expression broke: keep the previous visuals, report the error
        rep.overlay.apply(rep.result, rep.style, rep.color);
        rep.overlay.setVisible(rep.visible);
        return { ok: false, error: (e && e.message) || String(e) };
      }
    }
    if (patch.visible !== undefined) { rep.visible = !!patch.visible; rep.overlay.setVisible(rep.visible); }
    // dynamic / identity-list changes alter the non-streamed union
    if (patch.dynamic !== undefined && !needRecompute) this._syncCoords();
    this.onChange('update', rep);
    return { ok: true, rep: rep };
  };

  RepManager.prototype.setVisible = function (id, on) {
    var rep = this.get(id);
    if (!rep) return false;
    rep.visible = !!on;
    rep.overlay.setVisible(rep.visible);
    this.onChange('visible', rep);
    return true;
  };

  RepManager.prototype.duplicate = function (id) {
    var src = this.get(id) || this.active();
    if (!src) return { ok: false, error: 'nothing to duplicate' };
    return this.add({ expression: src.expression, style: src.style,
                      color: src.color, dynamic: src.dynamic });
  };

  RepManager.prototype.remove = function (id) {
    var i = -1;
    for (var k = 0; k < this.reps.length; k++) if (this.reps[k].id === id) { i = k; break; }
    if (i < 0) return false;
    this.reps[i].overlay.clear();
    var rep = this.reps.splice(i, 1)[0];
    if (this.activeId === id) this.activeId = null;
    this._syncCoords();                  // shrink the side-channel union
    this.onChange('remove', rep);
    return true;
  };

  RepManager.prototype.clearAll = function () {
    while (this.reps.length) { this.reps.pop().overlay.clear(); }
    this.activeId = null;
    this._syncCoords();
    this.onChange('clear', null);
  };

  RepManager.prototype.setActive = function (id) {
    this.activeId = this.get(id) ? id : null;
    this.onChange('active', this.get(this.activeId));
  };

  // ---- frame / view hooks ---------------------------------------------
  // Refresh every live overlay; re-evaluate per-rep dynamic spatial ones.
  RepManager.prototype.onFrame = function (f) {
    // Advance the side channel FIRST: if the exact frame is already cached
    // (prefetch) every overlay patches from it in this same pass; otherwise
    // it shows the newest cached frame <= f (never newer than the protein)
    // and onChange refreshes once more when the fetch lands.
    var coords = this.ctx.coords && this.ctx.coords();
    if (coords) coords.setFrame(f);
    for (var i = 0; i < this.reps.length; i++) {
      var rep = this.reps[i];
      if (!rep.overlay.active()) continue;
      rep.overlay.refresh();
      if (rep.dynamic && rep.spatial && rep.visible && rep.lastDyn !== f) {
        rep.lastDyn = f;
        try { this._recompute(rep); }
        catch (e) { this.warn('dynamic selection "' + rep.expression + '" failed: ' + e); }
      }
    }
    if (this.reps.length) this.onChange('frame', null);
  };

  // Mode (Cα/Backbone/Full) or component toggling changed which atoms carry
  // current coordinates: refresh statics' display and re-evaluate spatial
  // reps against the new coordinate domain (matches the previous single-sel
  // behaviour, applied per row).
  RepManager.prototype.onViewChange = function () {
    for (var i = 0; i < this.reps.length; i++) {
      var rep = this.reps[i];
      if (!rep.overlay.active()) continue;
      try {
        if (rep.spatial) this._recompute(rep);
        else rep.overlay.refresh();
      } catch (e) { /* keep last render */ }
    }
    this.onChange('view', null);
  };

  // compact row snapshot for the DOM list
  RepManager.prototype.summary = function () {
    var out = [];
    for (var i = 0; i < this.reps.length; i++) {
      var r = this.reps[i];
      out.push({ id: r.id, expression: r.expression, style: r.style, color: r.color,
                 visible: r.visible, dynamic: r.dynamic,
                 atomCount: r.atomCount, residueCount: r.residueCount,
                 spatial: r.spatial, active: r.id === this.activeId });
    }
    return out;
  };

  global.RepManager = RepManager;
})(window);
