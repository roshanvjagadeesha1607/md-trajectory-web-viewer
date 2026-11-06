// QuickSurf-like Gaussian density surface — compute worker.
//
// Per frame: every splat atom deposits a compact kernel into a regular grid;
// separable sliding-window box filters widen the field (a cubic B-spline
// approximation — the same splat+box idea VMD's QuickSurf uses, on CPU
// instead of CUDA); an isosurface is extracted with standard marching cubes
// (three.js tables, MIT) and per-edge vertex merging. The isosurface LEVEL is
// calibrated by enclosed VOLUME (see the level section), which is what keeps
// the envelope wrapped around the whole molecule instead of eroding it down to
// its densest cores. Output: flat position/normal/color/index arrays for GPU
// upload.
//
// In:  {id, pos:Float32Array(n*3), col:Uint8Array(n*3)|null,
//       opt:{spacing, passes, iso, unitVol, capN, color}}
//         iso      = envelope bulge factor (enclosed volume =
//                    iso × Σ unitVol over the splat atoms)
//         unitVol  = volume each splat atom stands for (Å³) — side-chain
//                    atoms ≈20, backbone atoms ≈35, one Cα per residue ≈140
// Out: {id, ok, position, normal, color, index, verts, tris, ms, dims, ...}
'use strict';
importScripts('mc_tables.js');
var EDGE = self.MC_EDGE, TRI = self.MC_TRI;
var W_FACE = 0.55, W_EDGE = 0.30, W_CORNER = 0.15;

// scratch float pool (density + 3 colour channels + one swap each)
var pool = new Float32Array(0), poolB = new Float32Array(0);

// growable triangle-buffer output
var CAP0 = 1 << 15;
var bPos = new Float32Array(CAP0 * 3), bNor = new Float32Array(CAP0 * 3),
    bCol = new Uint8Array(CAP0 * 4), bIdx = new Uint32Array(CAP0);
var bNV = 0, bNI = 0;
function ensureVerts(k) {
  if (k * 3 <= bPos.length) return;
  var cap = bPos.length / 3;
  while (cap < k) cap *= 2;
  var p = new Float32Array(cap * 3); p.set(bPos.subarray(0, bNV * 3)); bPos = p;
  var n = new Float32Array(cap * 3); n.set(bNor.subarray(0, bNV * 3)); bNor = n;
  var c = new Uint8Array(cap * 4); c.set(bCol.subarray(0, bNV * 4)); bCol = c;
}
function ensureIdx(k) {
  if (k <= bIdx.length) return;
  var cap = bIdx.length;
  while (cap < k) cap *= 2;
  var i = new Uint32Array(cap); i.set(bIdx.subarray(0, bNI)); bIdx = i;
}

// Peak density of ONE isolated atom's splat after `passes` box filters,
// computed with the real splat + boxAxis code on a small grid and cached per
// pass count. The isosurface level is a fixed fraction of this, which is what
// keeps the envelope the same physical size in Cα / Backbone / Full mode and
// under splat subsampling (it depends on the kernel only, never on how many
// atoms the molecule has).
var PEAK1 = {};
function peak1(passes) {
  if (PEAK1[passes] !== undefined) return PEAK1[passes];
  var N = 21, nxy = N * N, nv = N * N * N, c = (N >> 1);
  var a = new Float32Array(nv), b = new Float32Array(nv);
  var o = c + N * c + nxy * c;
  a[o] += 1;
  for (var dz = -1; dz <= 1; dz++) for (var dy = -1; dy <= 1; dy++) for (var dx = -1; dx <= 1; dx++) {
    if (!dx && !dy && !dz) continue;
    var nz1 = (dx ? 0 : 1) + (dy ? 0 : 1) + (dz ? 0 : 1);
    a[o + dx + N * dy + nxy * dz] += nz1 === 3 ? W_CORNER : (nz1 === 2 ? W_EDGE : W_FACE);
  }
  var s = a, t = b;
  for (var p = 0; p < passes; p++) {
    for (var ax = 0; ax < 3; ax++) { boxAxis(s, t, N, N, N, ax); var sw = s; s = t; t = sw; }
  }
  PEAK1[passes] = s[o];
  return PEAK1[passes];
}

// sliding-window box filter along one axis, clamped borders, src -> dst
function boxAxis(src, dst, nx, ny, nz, axis) {
  var i, j, k;
  if (axis === 0) {
    var nxy = nx * ny;
    for (k = 0; k < nz; k++) for (j = 0; j < ny; j++) {
      var base = k * nxy + j * nx;
      for (i = 0; i < nx; i++) {
        var a = src[base + (i > 0 ? i - 1 : 0)] + src[base + i] +
                src[base + (i < nx - 1 ? i + 1 : nx - 1)];
        dst[base + i] = a / 3;
      }
    }
  } else if (axis === 1) {
    for (k = 0; k < nz; k++) for (i = 0; i < nx; i++) {
      for (j = 0; j < ny; j++) {
        var p = k * nx * ny + j * nx + i;
        var a1 = src[p] +
          src[k * nx * ny + (j > 0 ? j - 1 : 0) * nx + i] +
          src[k * nx * ny + (j < ny - 1 ? j + 1 : ny - 1) * nx + i];
        dst[p] = a1 / 3;
      }
    }
  } else {
    var nxy2 = nx * ny;
    for (j = 0; j < ny; j++) for (i = 0; i < nx; i++) {
      for (k = 0; k < nz; k++) {
        var p2 = k * nxy2 + j * nx + i;
        var a2 = src[p2] +
          src[(k > 0 ? k - 1 : 0) * nxy2 + j * nx + i] +
          src[(k < nz - 1 ? k + 1 : nz - 1) * nxy2 + j * nx + i];
        dst[p2] = a2 / 3;
      }
    }
  }
}

self.onmessage = function (e) {
  var msg = e.data, id = msg.id, opt = msg.opt;
  var pos = msg.pos, col = msg.col;
  var spacing = opt.spacing, passes = opt.passes | 0, isoQ = opt.iso;
  var wantColor = !!opt.color && !!col;
  var n = pos.length / 3 | 0;

  // stride-subsample huge splat sets (dense polymer envelope is unchanged
  // at these grid resolutions; caps worker cost for 700k-atom systems)
  var stride = 1;
  if (opt.capN && n > opt.capN) stride = Math.ceil(n / opt.capN);

  var t0 = performance.now();
  var mnx = 1e30, mny = 1e30, mnz = 1e30, mxx = -1e30, mxy = -1e30, mxz = -1e30;
  for (var i = 0; i < pos.length; i += 3) {
    var x = pos[i], y = pos[i + 1], z = pos[i + 2];
    if (x < mnx) mnx = x; if (y < mny) mny = y; if (z < mnz) mnz = z;
    if (x > mxx) mxx = x; if (y > mxy) mxy = y; if (z > mxz) mxz = z;
  }
  // Padding must exceed the maximum envelope bulge beyond the outermost atom,
  // otherwise the isosurface runs out of grid and closes itself with a hole
  // (visible as open boundary edges). 5 spacings is comfortably more than the
  // kernel + box support at any quality preset.
  var pad = spacing * 5;
  var nx = ((mxx - mnx + 2 * pad) / spacing | 0) + 2;
  var ny = ((mxy - mny + 2 * pad) / spacing | 0) + 2;
  var nz = ((mxz - mnz + 2 * pad) / spacing | 0) + 2;
  if (!(nx > 2 && ny > 2 && nz > 2)) { self.postMessage({ id: id, ok: false, err: 'degenerate' }); return; }
  var nxy = nx * ny, nv = nxy * nz;
  var chans = wantColor ? 4 : 1;
  if (pool.length < nv * chans) {
    pool = new Float32Array(nv * chans);
    poolB = new Float32Array(nv * chans);
  }
  var d = pool.subarray(0, nv), ds = poolB.subarray(0, nv);
  var cr, cg, cb, crs, cgs, cbs;
  if (wantColor) {
    cr = pool.subarray(nv, nv * 2); cg = pool.subarray(nv * 2, nv * 3); cb = pool.subarray(nv * 3, nv * 4);
    crs = poolB.subarray(nv, nv * 2); cgs = poolB.subarray(nv * 2, nv * 3); cbs = poolB.subarray(nv * 3, nv * 4);
    cr.fill(0); cg.fill(0); cb.fill(0);
  }
  d.fill(0);

  // ---- splat ----
  for (var a = 0; a < n; a += stride) {
    var ax = pos[3 * a], ay = pos[3 * a + 1], az = pos[3 * a + 2];
    var gx = (ax - mnx + pad) / spacing | 0;
    var gy = (ay - mny + pad) / spacing | 0;
    var gz = (az - mnz + pad) / spacing | 0;
    if (gx < 1 || gy < 1 || gz < 1 || gx >= nx - 1 || gy >= ny - 1 || gz >= nz - 1) continue;
    var o = gx + nx * gy + nxy * gz;
    var r = 0, g = 0, b = 0;
    if (wantColor) { r = col[3 * a] * 0.00392156; g = col[3 * a + 1] * 0.00392156; b = col[3 * a + 2] * 0.00392156; }
    d[o] += 1;
    if (wantColor) { cr[o] += r; cg[o] += g; cb[o] += b; }
    for (var dz = -1; dz <= 1; dz++) {
      for (var dy = -1; dy <= 1; dy++) {
        for (var dx = -1; dx <= 1; dx++) {
          if (!dx && !dy && !dz) continue;
          var nz1 = (dx ? 0 : 1) + (dy ? 0 : 1) + (dz ? 0 : 1);
          var w = nz1 === 3 ? W_CORNER : (nz1 === 2 ? W_EDGE : W_FACE);
          var j2 = o + dx + nx * dy + nxy * dz;
          d[j2] += w;
          if (wantColor) { cr[j2] += r * w; cg[j2] += g * w; cb[j2] += b * w; }
        }
      }
    }
  }

  // ---- box smoothing (per axis, `passes` times, all channels) ----
  for (var p = 0; p < passes; p++) {
    for (var ax = 0; ax < 3; ax++) { boxAxis(d, ds, nx, ny, nz, ax); var t = d; d = ds; ds = t; }
    if (wantColor) {
      for (var ax2 = 0; ax2 < 3; ax2++) { boxAxis(cr, crs, nx, ny, nz, ax2); var t2 = cr; cr = crs; crs = t2; }
      for (var ax3 = 0; ax3 < 3; ax3++) { boxAxis(cg, cgs, nx, ny, nz, ax3); var t3 = cg; cg = cgs; cgs = t3; }
      for (var ax4 = 0; ax4 < 3; ax4++) { boxAxis(cb, cbs, nx, ny, nz, ax4); var t4 = cb; cb = cbs; cbs = t4; }
    }
  }
  var tSplat = performance.now() - t0;

  // ---- isosurface level: a fixed fraction of ONE atom's peak density ----
  // This is the QuickSurf way to set the isovalue and it is what makes the
  // envelope behave: the level is defined by the kernel alone (how many box
  // passes ran), never by how dense or extended the molecule happens to be.
  // Every atom therefore carries a skin of the same thickness, tips included,
  // and Cα / Backbone / Full all wrap the SAME molecule (an atom-count or
  // volume-percentile level instead cuts inward as the atom count grows —
  // that erosion is what left only a small blob inside the protein).
  // peak1 = center value of one isolated atom's splat after the same box
  // passes; depends only on `passes`, so it is computed once and cached.
  var maxv = 0, occ = 0;
  for (var q = 0; q < nv; q++) {
    var v0 = d[q];
    if (v0 <= 0) continue;
    occ++;
    if (v0 > maxv) maxv = v0;
  }
  if (!(maxv > 0) || !occ) { self.postMessage({ id: id, ok: false, err: 'empty density' }); return; }
  var p1 = peak1(passes | 0);
  var level = isoQ * p1;
  if (!(level > 0) || level >= maxv) level = maxv * 0.25;
  var enclosedVox = 0;
  for (var qe = 0; qe < nv; qe++) if (d[qe] >= level) enclosedVox++;
  var voxVol = spacing * spacing * spacing;
  var tIso = performance.now() - t0 - tSplat;

  // ---- marching cubes ----
  var tMC0 = performance.now();
  bNV = 0; bNI = 0;
  var vex = new Int32Array((nx - 1) * ny * nz);
  var vey = new Int32Array(nx * (ny - 1) * nz);
  var vez = new Int32Array(nxy * (nz - 1));
  // Edge -> vertex with per-edge caching (this is what makes the mesh a
  // closed manifold: each edge contributes exactly one vertex, shared by
  // every cell that uses it). t() clamps the interpolation to [0,1] so an
  // edge whose endpoints straddle the level by less than float precision can
  // never emit a coordinate a trillion ångström away.
  function interp(va, vb) {
    var dv = vb - va;
    if (!(dv > 1e-9) && !(dv < -1e-9)) return 0.5;
    var f = (level - va) / dv;
    return f < 0 ? 0 : (f > 1 ? 1 : f);
  }
  function vidX(k) {
    var e = vex[k]; if (e) return e - 1;
    var kx = k % (nx - 1), ky = (k / (nx - 1) | 0) % ny, kz = k / ((nx - 1) * ny) | 0;
    var i0 = kx + nx * ky + nxy * kz;
    var v = mkVert(kx + interp(d[i0], d[i0 + 1]), ky, kz);
    vex[k] = v + 1; return v;
  }
  function vidY(k) {
    var e = vey[k]; if (e) return e - 1;
    var kx = k % nx, ky = (k / nx | 0) % (ny - 1), kz = k / (nx * (ny - 1)) | 0;
    var i0 = kx + nx * ky + nxy * kz;
    var v = mkVert(kx, ky + interp(d[i0], d[i0 + nx]), kz);
    vey[k] = v + 1; return v;
  }
  function vidZ(k) {
    var e = vez[k]; if (e) return e - 1;
    var kx = k % nx, ky = (k / nx | 0) % ny, kz = k / nxy | 0;
    var i0 = kx + nx * ky + nxy * kz;
    var v = mkVert(kx, ky, kz + interp(d[i0], d[i0 + nxy]));
    vez[k] = v + 1; return v;
  }
  function mkVert(fx, fy, fz) {
    ensureVerts(bNV + 1);
    var o = bNV * 3;
    bPos[o] = (mnx - pad) + fx * spacing;
    bPos[o + 1] = (mny - pad) + fy * spacing;
    bPos[o + 2] = (mnz - pad) + fz * spacing;
    if (wantColor) {
      var x0 = fx | 0, y0 = fy | 0, z0 = fz | 0;
      if (x0 > nx - 2) x0 = nx - 2; if (y0 > ny - 2) y0 = ny - 2; if (z0 > nz - 2) z0 = nz - 2;
      var tx = fx - x0, ty = fy - y0, tz = fz - z0;
      var b0 = x0 + nx * y0 + nxy * z0;
      var ar = 0, ag = 0, ab = 0, ad = 0;
      for (var cz = 0; cz < 2; cz++) for (var cy = 0; cy < 2; cy++) for (var cx = 0; cx < 2; cx++) {
        var wt = (cx ? tx : 1 - tx) * (cy ? ty : 1 - ty) * (cz ? tz : 1 - tz);
        var ii = b0 + cx + nx * cy + nxy * cz;
        ar += wt * cr[ii]; ag += wt * cg[ii]; ab += wt * cb[ii]; ad += wt * d[ii];
      }
      var k255 = ad > 1e-6 ? 255 / ad : 0;
      var co = bNV * 4;
      var rr = ar * k255, gg = ag * k255, bb = ab * k255;
      bCol[co] = rr < 0 ? 0 : rr > 255 ? 255 : rr;
      bCol[co + 1] = gg < 0 ? 0 : gg > 255 ? 255 : gg;
      bCol[co + 2] = bb < 0 ? 0 : bb > 255 ? 255 : bb;
      bCol[co + 3] = 255;
    }
    return bNV++;
  }
  function edgeV(ev, x, y, z) {
    switch (ev) {
      case 0: return vidX(x + (nx - 1) * (y + ny * z));
      case 1: return vidY(x + 1 + nx * (y + (ny - 1) * z));
      case 2: return vidX(x + (nx - 1) * (y + 1 + ny * z));
      case 3: return vidY(x + nx * (y + (ny - 1) * z));
      case 4: return vidX(x + (nx - 1) * (y + ny * (z + 1)));
      case 5: return vidY(x + 1 + nx * (y + (ny - 1) * (z + 1)));
      case 6: return vidX(x + (nx - 1) * (y + 1 + ny * (z + 1)));
      case 7: return vidY(x + nx * (y + (ny - 1) * (z + 1)));
      case 8: return vidZ(x + nx * (y + ny * z));
      case 9: return vidZ(x + 1 + nx * (y + ny * z));
      case 10: return vidZ(x + 1 + nx * (y + 1 + ny * z));
      default: return vidZ(x + nx * (y + 1 + ny * z));
    }
  }
  for (var z = 0; z < nz - 1; z++) {
    for (var y = 0; y < ny - 1; y++) {
      var row = nxy * z + nx * y;
      for (var x = 0; x < nx - 1; x++) {
        var i0 = row + x;
        var ci = 0;
        if (d[i0] >= level) ci |= 1;
        if (d[i0 + 1] >= level) ci |= 2;
        if (d[i0 + 1 + nx] >= level) ci |= 4;
        if (d[i0 + nx] >= level) ci |= 8;
        if (d[i0 + nxy] >= level) ci |= 16;
        if (d[i0 + 1 + nxy] >= level) ci |= 32;
        if (d[i0 + 1 + nx + nxy] >= level) ci |= 64;
        if (d[i0 + nx + nxy] >= level) ci |= 128;
        if (EDGE[ci] === 0) continue;
        // MC_TRI is FLAT: 256 rows x 16 entries (three.js triTable). Indexing
        // it as TRI[ci] yields a NUMBER, so tt[k] was undefined for every k,
        // the -1 terminator never fired, and each active cell emitted its full
        // 5 placeholder triangles of garbage — which is exactly why the mesh
        // collapsed into one small crumpled object (tris/verts was pinned at
        // 6.0 instead of the ~2.0 of a closed surface).
        var tb = ci << 4;
        for (var t3i = 0; t3i < 16; t3i += 3) {
          var e0 = TRI[tb + t3i];
          if (e0 === -1) break;
          ensureIdx(bNI + 3);
          bIdx[bNI] = edgeV(e0, x, y, z);
          bIdx[bNI + 1] = edgeV(TRI[tb + t3i + 1], x, y, z);
          bIdx[bNI + 2] = edgeV(TRI[tb + t3i + 2], x, y, z);
          bNI += 3;
        }
      }
    }
  }
  var tMC = performance.now() - tMC0;

  // ---- normals + copy out ----
  var posOut = bPos.subarray(0, bNV * 3).slice();
  var idxOut = bIdx.subarray(0, bNI).slice();
  var norOut = new Float32Array(bNV * 3);
  for (var f = 0; f < bNI; f += 3) {
    var ia = idxOut[f] * 3, ib = idxOut[f + 1] * 3, ic = idxOut[f + 2] * 3;
    var ux = posOut[ib] - posOut[ia], uy = posOut[ib + 1] - posOut[ia + 1], uz = posOut[ib + 2] - posOut[ia + 2];
    var wx = posOut[ic] - posOut[ia], wy = posOut[ic + 1] - posOut[ia + 1], wz = posOut[ic + 2] - posOut[ia + 2];
    var fx2 = uy * wz - uz * wy, fy2 = uz * wx - ux * wz, fz2 = ux * wy - uy * wx;
    norOut[ia] += fx2; norOut[ia + 1] += fy2; norOut[ia + 2] += fz2;
    norOut[ib] += fx2; norOut[ib + 1] += fy2; norOut[ib + 2] += fz2;
    norOut[ic] += fx2; norOut[ic + 1] += fy2; norOut[ic + 2] += fz2;
  }
  for (var s = 0; s < norOut.length; s += 3) {
    var ln = Math.sqrt(norOut[s] * norOut[s] + norOut[s + 1] * norOut[s + 1] + norOut[s + 2] * norOut[s + 2]) || 1;
    norOut[s] /= ln; norOut[s + 1] /= ln; norOut[s + 2] /= ln;
  }
  var colOut = wantColor ? bCol.subarray(0, bNV * 4).slice() : null;
  var trs = { splat: +(tSplat).toFixed(1), iso: +tIso.toFixed(1), mc: +tMC.toFixed(1),
              total: +(performance.now() - t0).toFixed(1) };
  // ---- debug boxes: the input (splat atom) extent and the OUTPUT mesh
  // extent, in Å. Equal extents mean the envelope really wraps the whole
  // molecule; a much smaller mesh box means the isosurface eroded inward.
  var meshBox = [1e30, 1e30, 1e30, -1e30, -1e30, -1e30];
  for (var mb2 = 0; mb2 < posOut.length; mb2 += 3) {
    if (posOut[mb2] < meshBox[0]) meshBox[0] = posOut[mb2];
    if (posOut[mb2 + 1] < meshBox[1]) meshBox[1] = posOut[mb2 + 1];
    if (posOut[mb2 + 2] < meshBox[2]) meshBox[2] = posOut[mb2 + 2];
    if (posOut[mb2] > meshBox[3]) meshBox[3] = posOut[mb2];
    if (posOut[mb2 + 1] > meshBox[4]) meshBox[4] = posOut[mb2 + 1];
    if (posOut[mb2 + 2] > meshBox[5]) meshBox[5] = posOut[mb2 + 2];
  }
  if (!bNV) meshBox = [0, 0, 0, 0, 0, 0];
  var out = { id: id, ok: true, position: posOut, normal: norOut, color: colOut, index: idxOut,
              verts: bNV, tris: bNI / 3 | 0, ms: trs, level: +level.toFixed(4),
              dims: [nx, ny, nz], splatUsed: Math.ceil(n / stride),
              nAtoms: n,
              splatBox: [+mnx.toFixed(1), +mny.toFixed(1), +mnz.toFixed(1),
                         +mxx.toFixed(1), +mxy.toFixed(1), +mxz.toFixed(1)],
              meshBox: [+meshBox[0].toFixed(1), +meshBox[1].toFixed(1),
                        +meshBox[2].toFixed(1), +meshBox[3].toFixed(1),
                        +meshBox[4].toFixed(1), +meshBox[5].toFixed(1)],
              volEnclosed: +(enclosedVox * voxVol).toFixed(0) };
  var toTransfer = [posOut.buffer, norOut.buffer, idxOut.buffer];
  if (colOut) toTransfer.push(colOut.buffer);
  self.postMessage(out, toTransfer);
};
var hist = null;
