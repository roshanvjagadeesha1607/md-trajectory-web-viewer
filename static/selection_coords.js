// ======================================================================
// SELECTION COORDINATES — current-frame coordinates for selected atoms
// the playback mode does NOT stream.
//
// Cα / Backbone modes stream only the trace atoms (plus ticked components),
// so a static selection like `resname SER` contains atoms whose atomStore
// entries NGL never rewrites. The overlay must still MOVE them every frame
// (VMD semantics: fixed identities, live coordinates), and it must not drag
// the main trajectory into streaming more atoms than the user's mode asks
// for. So: a second, tiny client of the SAME frame endpoint the trajectory
// already uses. Nothing in serve.py, the NGL trajectory, the frame cache,
// the surface or the Gaussian pipeline changes — this only READS an
// existing endpoint the way any MDsrv client does.
//
//  * identity list = union of the non-streamed atoms of every live STATIC
//    representation (dynamic rows re-pick atoms per frame; making them move
//    would re-token the request every frame — they keep the documented
//    GRO-coords behaviour instead).
//  * first request POSTs the MDsrv range text and learns the X-Sel-Token;
//    every request after that POSTs 'tok=…' (~25 B). Same protocol, same
//    server-side parse cache as the main stream — one extra parse per row
//    edit, ~O(selection) gather per frame server-side.
//  * a small LRU (6 frames) plus a 2-frame prefetch keeps the data ready
//    before playback needs it, so the patch never lags behind the protein.
//  * frames arrive as Float32Array of the identity list's xyz (Å), exactly
//    the MDsrv payload tail: [i32 nframes | f32 t | 9×f32 box (3×3) | xyz…].
//    The header is 44 bytes — measured from the live endpoint: a frame for
//    N atoms is always 44 + 12N bytes. Reading it as 20 shifts every
//    coordinate by 2 atoms and draws bonds between strangers.
// ======================================================================
(function (global) {
  'use strict';

  var HEADER = 44;          // 4 B nframes + 4 B t_ns + 36 B triclinic box
  var CACHE_MAX = 6;
  var PREFETCH = 2;
  var MAX_ATOMS = 40000;    // beyond this, keep GRO coords (UI says so)

  function SelCoords(ctx) {
    this.ctx = ctx;                 // { comp(), frameUrl(f) }
    this.ids = null;                // Int32Array sorted atom indices
    this.slot = null;               // Int32Array natoms, slot id or -1
    this.text = '';                 // MDsrv range text for ids
    this.token = null;
    this.cache = new global.Map();  // frame -> Float32Array (ids.length*3)
    this.order = [];
    this.inflight = new global.Map();
    this.onChange = function () {}; // (frame, Float32Array)
    this.requested = 0; this.hits = 0;
  }

  // sorted unique Int32Array from a plain array
  function sortUnique(arr) {
    arr.sort(function (a, b) { return a - b; });
    var out = [], last = -1;
    for (var i = 0; i < arr.length; i++) if (arr[i] !== last) { out.push(arr[i]); last = arr[i]; }
    return Int32Array.from(out);
  }

  // Int32Array ids -> MDsrv half-open range text 's,e;s,e;…'
  // `e` is an ARRAY cursor, not an atom value: run ends when consecutive
  // VALUES stop being consecutive, and the emitted half-open end is the
  // last value in the run + 1.
  function rangeText(ids) {
    var parts = [], i = 0, n = ids.length;
    while (i < n) {
      var s = ids[i], e = i + 1;
      while (e < n && ids[e] === ids[e - 1] + 1) e++;
      parts.push(s + ',' + (ids[e - 1] + 1)); i = e;
    }
    return parts.join(';');
  }

  // Replace the identity list (call after any overlay/view change that can
  // move the non-streamed union). Returns the new atom count.
  SelCoords.prototype.setAtoms = function (idArray) {
    var ids = null, slot = null, text = '';
    if (idArray && idArray.length) {
      ids = sortUnique(idArray);
      if (ids.length > MAX_ATOMS) ids = null;      // too big: keep GRO coords
    }
    if (ids) {
      text = rangeText(ids);
      var nat = this.ctx.comp().structure.atomStore.count;
      slot = new Int32Array(nat).fill(-1);
      for (var i = 0; i < ids.length; i++) slot[ids[i]] = i;
    }
    var same = !!ids === !!this.ids && text === this.text;
    this.ids = ids; this.slot = slot; this.text = text;
    if (!same) { this.token = null; this.cache.clear(); this.order.length = 0; }
    return ids ? ids.length : 0;
  };

  SelCoords.prototype.count = function () { return this.ids ? this.ids.length : 0; };
  SelCoords.prototype.indexOf = function (atom) {
    return this.slot ? this.slot[atom] : -1;
  };

  SelCoords.prototype._evict = function () {
    while (this.order.length > CACHE_MAX) {
      var f = this.order.shift(); this.cache.delete(f);
    }
  };

  // Fetch one frame's coordinates for the identity list. Cheap when cached.
  SelCoords.prototype.ensure = function (f) {
    if (!this.ids || f < 0) return;
    if (this.cache.has(f) || this.inflight.has(f)) { this.hits++; return; }
    var self = this, url = this.ctx.frameUrl(f);
    var send = function (useToken) {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', url, true);
      xhr.responseType = 'arraybuffer';
      xhr.setRequestHeader('Content-Type', 'text/plain');
      self.inflight.set(f, xhr);
      xhr.onload = function () {
        self.inflight.delete(f);
        if (xhr.status === 422 && useToken) { self.token = null; send(false); return; }
        if (xhr.status !== 200) return;
        var t = xhr.getResponseHeader('X-Sel-Token');
        if (t) self.token = t;
        var buf = xhr.response;
        if (!buf || buf.byteLength < HEADER) return;
        var coords = new Float32Array(buf, HEADER);
        if (coords.length < self.ids.length * 3) return;
        self.cache.set(f, coords); self.order.push(f); self._evict();
        self.requested++;
        self.onChange(f, coords);
      };
      xhr.onerror = function () { self.inflight.delete(f); };
      xhr.send(useToken && self.token ? 'tok=' + self.token : self.text);
    };
    send(true);
  };

  SelCoords.prototype.prefetch = function (f) {
    for (var k = 1; k <= PREFETCH; k++) this.ensure(f + k);
  };

  SelCoords.prototype.get = function (f) { return this.cache.get(f) || null; };

  // Frame the overlays are about to be refreshed for. The data served must
  // never be NEWER than what the protein itself displays (getUpTo), and the
  // exact frame is served as soon as its fetch lands — the manager then
  // re-refreshes the overlays once via onChange.
  SelCoords.prototype.setFrame = function (f) {
    this.frame = f;
    this.ensure(f);
    this.prefetch(f);
  };
  SelCoords.prototype.current = function () {
    if (!this.ids) return null;
    if (this.frame === undefined || this.frame < 0) return null;
    var hit = this.cache.get(this.frame);
    if (hit) return hit;
    var best = -1, self = this;
    this.cache.forEach(function (v, k) { if (k <= self.frame && k > best) best = k; });
    return best >= 0 ? this.cache.get(best) : null;
  };

  global.SelCoords = SelCoords;
})(window);
