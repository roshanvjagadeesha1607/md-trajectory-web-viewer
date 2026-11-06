// ----------------------------------------------------------------------
// Gaussian Surface (QuickSurf-like) — gaussian density envelope rendered
// inside the existing NGL scene.
//
// This is a genuine Gaussian DENSITY surface (not a molecular surface):
// every protein atom contributes a compact kernel to a regular grid,
// separable box filters widen the field (cubic B-spline approximation —
// the same splat+box idea behind VMD's QuickSurf, executed in a Web
// Worker instead of CUDA), and a closed isosurface is meshed with
// standard marching cubes. The mesh is uploaded into one persistent
// NGL MeshBuffer (one WebGL context; camera, lights, controls, other
// representations and opacity all keep working normally).
//
// Coordinates are read in-place from the ALREADY-STREAMED structure
// selection (Cα / backbone / full — whatever the current playback detail
// mode transfers), so no extra network requests are made and the frame
// pipeline is untouched. During playback the mesh updates as fast as the
// worker finishes (drop-latest); adaptive quality: coarse while playing,
// the selected quality when paused.
// ----------------------------------------------------------------------
window.GaussSurf = (function () {
  'use strict';

  // quality presets: spacing (Å grid), smoothing passes, isovalue (as a
  // fraction of one atom's peak density — mode-independent, see the worker),
  // and the splat-atom cap above which atoms are stride-subsampled.
  // iso is deliberately the SAME at every quality: calibrated on the real
  // reference structure coordinates it gives near-identical envelope extents at all three
  // resolutions, so pausing refines the surface WITHOUT it visibly changing
  // size.
  var QUALITY = {
    coarse: { spacing: 3.5, passes: 1, iso: 0.70, capN: 60000 },
    medium: { spacing: 2.5, passes: 2, iso: 0.70, capN: 150000 },
    high:   { spacing: 1.8, passes: 3, iso: 0.70, capN: 1000000 }
  };
  var PAUSE_REFRESH_MS = 550;   // re-mesh at full quality after playback stops

  var W = null;                 // worker
  var stage = null, getApp = null, getMeta = null;
  var active = false;
  var quality = 'medium';       // user selection (auto handled via playing)
  var playing = false;
  var colorMode = 'cyan';
  var includeWater = false, includeHets = false;

  // splat atom bookkeeping (rebuilt on rescan; mode switches)
  var splat = null;             // Int32Array of atom indices into the store
  var posArr = null;            // reusable read target (never transferred)
  var rescanKey = null;
  var nPolyRes = 0;             // polymer residues in the splat set
  var nHetAtoms = 0;            // included water/ligand atoms
  var lastStats = null;         // last worker reply (debug reporting)

  // NGL objects
  var shape = null, comp = null, rep = null, mb = null;

  // in-flight state
  var seq = 0, inflight = 0, lastDoneSeq = 0;
  var pendingReq = null;        // {frame}
  var needFrame = false;        // one-shot camera recenter (set on mode
                                // switch / activation; consumed by the next
                                // uploaded mesh — never during playback)
  var pauseTimer = null;
  var statsCb = null;
  var lastMs = 0, fpsSmooth = 0;

  function workerUrl() { return '/static/gsw_worker.js'; }

  function ensureWorker() {
    if (W) return;
    W = new Worker(workerUrl());
    W.onmessage = function (e) {
      var d = e.data;
      inflight--;
      lastMs = d.ok ? d.ms.total : 0;
      if (d.ok && d.id > lastDoneSeq) {
        lastDoneSeq = d.id;
        applyMesh(d);
      }
      // chain: newest pending wins; while playing never queue faster than
      // the worker drains (drop-latest policy).
      if (pendingReq && inflight === 0) {
        var f = pendingReq.frame; pendingReq = null;
        computeNow(f);
      }
    };
    W.onerror = function () { /* frame gap is fine; next frame re-arms */ };
  }

  // ---- atom set -----------------------------------------------------
  // Splat set mirrors the protein selection logic: every atom whose residue
  // is NOT a registered non-polymer component (category ligand/ion/water/
  // other) — i.e. the polymer envelope. Water/heterogenes enter only when
  // explicitly included AND part of the streamed selection (they are not
  // transferred in Cα/Backbone mode unless ticked, so they simply cannot
  // appear — the exclusion is enforced here independently of streaming).
  function rescan(force) {
    var A = getApp();
    var st = A.comp && A.comp.structure;
    if (!st) return false;
    var comps = (getMeta && getMeta() || {}).components || [];
    var cats = {}, vis = {};
    var visSig = '';
    for (var i = 0; i < comps.length; i++) {
      var rn = String(comps[i].resname);
      var upper = rn.toUpperCase();
      cats[upper] = comps[i].category;
      if (comps[i].category === 'protein' || comps[i].category === 'nucleic') continue;
      // checkbox ids are 'lig-'+resname (as-is from the server metadata)
      var el = document.getElementById('lig-' + rn);
      vis[upper] = el ? !!el.checked : false;
      visSig += upper + (vis[upper] ? '1' : '0') + ',';
    }
    var store = st.atomStore;
    var key = A.view + '|' + includeWater + '|' + includeHets + '|' + store.count + '|' + visSig;
    if (!force && key === rescanKey && splat) return true;
    rescanKey = key;
    var list = [];
    var p = st.getAtomProxy();
    var atomSel = A.view === 'ca' ? function (nm) { return nm === 'CA'; }
      : A.view === 'bb' ? function (nm) { return nm === 'N' || nm === 'CA' || nm === 'C' || nm === 'O'; }
      : function () { return true; };
    // Splat membership mirrors the app's selection semantics EXACTLY — the
    // same rule proteinRepSel() uses for the Cartoon/Backbone rep:
    //  - polymer residues (protein/nucleic): always the whole polymer,
    //    filtered by the mode's atom subset (.CA / backbone / all).
    //  - water: only when explicitly included AND actually transferred
    //    (Full mode always; Cα/BB only when its checkbox is ticked —
    //    unticked components are not streamed and would splat stale coords).
    //  - ligands/ions/other: only when included; in Cα/BB only when ticked.
    // nRes is reporting only: how many polymer residues the envelope covers.
    var nRes = 0, nHet = 0;
    var resSeen = Object.create(null);
    for (var k = 0; k < store.count; k++) {
      p.index = k;
      var rn2 = String(p.resname).toUpperCase();
      var cat = cats[rn2];
      if (cat === 'protein' || cat === 'nucleic' || cat === undefined) {
        if (A.view !== 'full' && !atomSel(String(p.atomname))) continue;
        list.push(k);
        // residue bookkeeping keyed per chain so equivalent residues in
        // different chains each count once
        var rk = p.chainname + '/' + p.resno + '/' + rn2;
        if (!resSeen[rk]) { resSeen[rk] = 1; nRes++; }
      } else if (cat === 'water') {
        if (!includeWater) continue;
        if (A.view !== 'full' && !vis[rn2]) continue;
        list.push(k); nHet++;
      } else {                                    // ligand / ion / other
        if (!includeHets) continue;
        if (A.view !== 'full' && !vis[rn2]) continue;
        list.push(k); nHet++;
      }
    }
    splat = Int32Array.from(list);
    posArr = new Float32Array(splat.length * 3);
    nPolyRes = nRes; nHetAtoms = nHet;
    lastStats = null;
    return true;
  }

  function readPos() {
    var st = getApp().comp.structure, a = st.atomStore, s = splat, o = posArr;
    for (var k = 0; k < s.length; k++) {
      var i = s[k];
      o[3 * k] = a.x[i]; o[3 * k + 1] = a.y[i]; o[3 * k + 2] = a.z[i];
    }
    return o;
  }

  // ---- compute scheduling --------------------------------------------
  function currentOpt() {
    var q = playing ? 'coarse' : quality;
    if (q === 'auto') q = playing ? 'coarse' : 'medium';
    return QUALITY[q] || QUALITY.medium;
  }

  function computeNow(frame) {
    if (!active) return;
    // Validate the splat set against the CURRENT atom store on every compute:
    // a selection re-stream (mode switch / ligand toggle) replaces the store,
    // which changes store.count and therefore the rescan key. Key comparison
    // is cheap (a handful of checkboxes); the O(n) sweep only re-runs when it
    // actually changed.
    if (!rescan(false) || !splat || !splat.length) return;
    ensureWorker();
    var opt = currentOpt();
    var src = readPos();
    var pos = new Float32Array(src.length);
    pos.set(src);
    var id = ++seq;
    inflight++;
    W.postMessage({ id: id, pos: pos, col: null,
                    opt: { spacing: opt.spacing, passes: opt.passes,
                           iso: opt.iso, capN: opt.capN, color: false } },
                  [pos.buffer]);
  }

  // called from onFrame (after NGL applied new coordinates)
  function onFrameSettled(frame) {
    if (!active) return;
    if (inflight === 0) computeNow(frame);
    else pendingReq = { frame: frame };
    if (!playing) schedulePauseRefresh();
  }

  function schedulePauseRefresh() {
    clearTimeout(pauseTimer);
    pauseTimer = setTimeout(function () {
      if (active && !playing) { if (inflight === 0) computeNow(-1); else pendingReq = { frame: -1 }; }
    }, PAUSE_REFRESH_MS);
  }

  // ---- camera framing ---------------------------------------------------
  // Center + zoom-to-fit on the MESH bbox. stage.autoView() is not usable
  // here: the viewer aggregates bounding boxes from representations, and a
  // raw MeshBuffer contributes none, so autoView frames the leftover stream
  // geometry instead of the envelope. Zoom reproduces NGL's own
  // Viewer.getZoomForBox() (a private method in this build) so the framing
  // matches what a normal autoView would have produced.
  function frameMeshBox(b) {
    if (!b || b.length !== 6) { try { stage.autoView(400); } catch (e) {} return; }
    try {
      var box = new NGL.Box3(new NGL.Vector3(b[0], b[1], b[2]),
                             new NGL.Vector3(b[3], b[4], b[5]));
      var sz = box.getSize(new NGL.Vector3());
      var mx = Math.max(sz.x, sz.y, sz.z), mn = Math.min(sz.x, sz.y, sz.z);
      var r = mx + Math.sqrt(mn);
      var fov = stage.viewer.perspectiveCamera.fov * Math.PI / 180;
      var w = stage.viewer.width, h = stage.viewer.height;
      var l = h < w ? 1 : w / h;
      var zoom = Math.abs(0.5 * r / l / Math.sin(fov / 2));
      try { zoom += stage.viewer.parameters.clipDist; } catch (e) {}
      stage.animationControls.zoomMove(
        box.getCenter(new NGL.Vector3()), -zoom, 400);
      window.__gsFraming = { zoom: -zoom, r: r, fov: fov, w: w, h: h, l: l };
    } catch (e) {
      window.__gsFraming = { err: String(e) };
      try { stage.autoView(400); } catch (e2) {}
    }
  }

  // ---- NGL upload ------------------------------------------------------
  function applyMesh(d) {
    var A = getApp();
    if (!A.comp || !active) return;
    if (!comp) {
      shape = new NGL.Shape('gaussian-surface');
      comp = stage.addComponentFromObject(shape);
      rep = comp.addRepresentation('buffer', {
        opacity: getOpacity(), transparent: true, color: colorMode
      });
    }
    var g, pa, na, ia;
    if (!mb) {
      mb = new NGL.MeshBuffer({
        position: d.position, index: d.index, normal: d.normal
      });
      shape.addBuffer(mb);
      g = mb.geometry;
      pa = g.attributes.position; na = g.attributes.normal; ia = g.index;
    } else {
      g = mb.geometry;
      pa = g.attributes.position; na = g.attributes.normal; ia = g.index;
      if (pa.array.length === d.position.length && na.array.length === d.normal.length &&
          ia.array.length === d.index.length) {
        pa.array.set(d.position); na.array.set(d.normal); ia.array.set(d.index);
        pa.needsUpdate = na.needsUpdate = ia.needsUpdate = true;
      } else {
        var Ctor = pa.constructor;
        g.setAttribute('position', new Ctor(d.position, 3));
        g.setAttribute('normal', new Ctor(d.normal, 3));
        var IC = ia.constructor;
        g.index = new IC(d.index, 1);
      }
    }
    lastStats = d;
    if (statsCb) statsCb(d);
    // One-shot camera recenter: consumed by the FIRST mesh that lands after
    // entering Gaussian mode or switching Cα/Backbone/Full. Never fires per
    // frame, so user rotate/zoom survives playback untouched.
    if (needFrame) {
      needFrame = false;
      window.__gsRecenter = (window.__gsRecenter || 0) + 1;
      frameMeshBox(d.meshBox);
    }
    try { stage.viewer.requestRender(); } catch (e) { /* next rAF renders */ }
  }

  function disposeMesh() {
    try { if (comp) stage.removeComponent(comp); } catch (e) {}
    comp = null; rep = null; mb = null; shape = null;
  }

  // ---- params ----------------------------------------------------------
  function getOpacity() {
    var el = document.getElementById('surfopacity');
    return el ? (+el.value) / 100 : 0.85;
  }
  function applyOpacity() {
    // 'transparent' is not a mesh-representation parameter in this NGL
    // build — opacity alone switches the material to a blended one, and
    // passing an unknown key would make setParameters reject the whole call.
    if (rep) { try { rep.setParameters({ opacity: getOpacity() }); } catch (e) {} }
  }
  function applyColor() {
    if (rep) { try { rep.setParameters({ colorValue: colorMode }); } catch (e) {} }
  }

  // ---- read-only introspection for the automated verification harness ----
  // Reports exactly what the user asked to be able to check: how many atoms
  // fed the Gaussian, the min/max/bbox of those atoms, and the mesh vertex
  // count — plus the OUTPUT mesh bbox, so a surface that fails to span the
  // protein is visible as a mismatch instead of a surprise on screen.
  window.__gsurfDebug = function () {
    var A = getApp();
    var st = A.comp && A.comp.structure;
    var g = mb && mb.geometry;
    var pa = g && g.attributes.position;
    var ia = g && g.index;
    // input extent recomputed here from the live splat set (Å)
    var inBox = null, nCA = 0, nBB = 0;
    if (st && splat && splat.length) {
      var a = st.atomStore, p = st.getAtomProxy();
      inBox = [1e30, 1e30, 1e30, -1e30, -1e30, -1e30];
      for (var i = 0; i < splat.length; i++) {
        var j = splat[i];
        var v = [a.x[j], a.y[j], a.z[j]];
        for (var k = 0; k < 3; k++) {
          if (v[k] < inBox[k]) inBox[k] = v[k];
          if (v[k] > inBox[3 + k]) inBox[3 + k] = v[k];
        }
      }
      inBox = inBox.map(function (v) { return +v.toFixed(1); });
      // how many of the store's protein atoms are CA / backbone, for the
      // "did we really take the whole protein" check
      for (var q = 0; q < st.atomStore.count; q++) {
        p.index = q;
        var nm = String(p.atomname);
        if (nm === 'CA') nCA++;
        if (nm === 'N' || nm === 'CA' || nm === 'C' || nm === 'O') nBB++;
      }
    }
    return {
      have: !!mb,
      verts: pa ? pa.array.length / 3 : 0,
      tris: ia ? ia.array.length / 3 : 0,
      visible: rep ? !!rep.visible : null,
      opacity: rep && rep.getParameters ? rep.getParameters().opacity
               : (rep && rep.repr ? rep.repr.parameters.opacity : null),
      quality: playing ? 'coarse' : quality,
      playing: !!playing,
      mode: A.view,
      splatAtoms: splat ? splat.length : 0,
      polyResidues: nPolyRes,
      hetAtomsIncluded: nHetAtoms,
      storeAtoms: st ? st.atomStore.count : 0,
      storeCA: nCA, storeBB: nBB,
      inputBox: inBox,
      meshBox: lastStats ? lastStats.meshBox : null,
      splatBox: lastStats ? lastStats.splatBox : null,
      workerMs: lastStats ? lastStats.ms : null,
      level: lastStats ? lastStats.level : null,
      inflight: inflight,
      seq: lastDoneSeq,
      recenters: window.__gsRecenter || 0
    };
  };
  window.__appFrame = function () { return getApp().frame; };
  // camera snapshot for the framing harness (center + zoom as plain data)
  window.__gsurfCam = function () {
    try {
      var c = stage.getCenter();
      return { cx: +c.x.toFixed(3), cy: +c.y.toFixed(3), cz: +c.z.toFixed(3),
               zoom: +stage.getZoom().toFixed(4) };
    } catch (e) { return { err: String(e) }; }
  };
  window.__appReady = function () {
    var A = getApp();
    return !!(A && A.ready && A.comp && A.comp.structure);
  };

  // ---- public API -------------------------------------------------------
  return {
    attach: function (stageRef, appGetter, metaGetter, stats) {
      stage = stageRef; getApp = appGetter; getMeta = metaGetter || null;
      statsCb = stats || null;
    },
    setActive: function (on) {
      if (on === active) return;
      active = on;
      if (on) { needFrame = true; rescan(true); if (inflight === 0) computeNow(-1); else pendingReq = { frame: -1 }; }
      else { disposeMesh(); }
    },
    isActive: function () { return active; },
    rescan: function (force) { rescan(force); },
    onFrame: function (frame) { onFrameSettled(frame); },
    setPlaying: function (p) {
      playing = p;
      if (!active) return;
      if (!p) { schedulePauseRefresh(); }   // refine after settling
      else if (inflight && currentOpt() !== QUALITY.coarse) {
        // let an in-flight fine mesh finish; new work is coarse
      }
    },
    setQuality: function (q) {
      quality = q;
      if (active && !playing && inflight === 0) computeNow(-1);
    },
    getQuality: function () { return quality; },
    setIncludeWater: function (v) { includeWater = v; if (active) { rescan(true); computeNow(-1); } },
    setIncludeHets: function (v) { includeHets = v; if (active) { rescan(true); computeNow(-1); } },
    setColor: function (c) { colorMode = c; applyColor(); },
    applyOpacity: applyOpacity,
    applyColor: applyColor,
    // re-send current geometry without recomputing (after mode switch the
    // splat set changed; call rescan + refresh)
    refresh: function () { if (active && inflight === 0) computeNow(-1); else pendingReq = { frame: -1 }; },
    // request a single camera recenter after the next mesh lands (used on
    // Cα/Backbone/Full switches, where the envelope extent changes once)
    armRecenter: function () { needFrame = true; },
    qualityNow: function () { return playing ? 'coarse' : quality; },
    playing: function () { return !!playing; },
    lastMs: function () { return lastMs; },
    presets: QUALITY
  };
})();
