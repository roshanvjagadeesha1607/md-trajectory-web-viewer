// ======================================================================
// VMD-like atom / residue SELECTION engine — fully client-side.
//
// A self-contained overlay for the existing viewer. It adds NO new server
// endpoint, NO new dependency and NO change to the streaming, component,
// surface, Gaussian, folder or portability behaviour it runs alongside.
//
// Everything runs against the REAL NGL Structure that is already in the
// browser (comp.structure). Measured facts this design rests on (probed on
// the viewer):
//
//   * comp.structure.atomStore holds EVERY atom of the structure in EVERY
//     playback mode — Cα mode streams only the trace coordinates, but the
//     parsed GRO metadata (atomname, resname, resno, chain, serial, element)
//     for all 41 532 atoms is present. So serial/index/resid/resname/name/
//     chain/protein/water, boolean logic, residue expansion, click-picking,
//     counts and the info panel are exact and cheap in Cα, Backbone AND Full.
//
//   * atomStore.serial IS the GRO atom-number column (NGL parses cols 15-20
//     verbatim), i.e. the structure's real serial number — never index+1.
//     serial and index are kept strictly distinct (see SerialStore).
//
//   * structureView.atomSet.isSet(i) reports which atoms the CURRENT mode
//     transfers coordinates for. structureView.atomCount = number streamed.
//
//   * structure.spatialHash.within(x,y,z,R) is an O(result) cell-hash query
//     over the atoms' CURRENT coordinates (0.1 ms on the full store). This is
//     the neighbor search for `within` — never an O(N²) scan, and it follows
//     the trajectory because NGL rewrites atomStore x/y/z every frame.
//
// Grammar implemented (recursive descent, deliberately small — the goal is
// stability, not full VMD coverage):
//
//   or_expr   := and_expr  ( 'or' and_expr )*
//   and_expr  := not_expr  ( 'and' not_expr | NOT-term* )*      // implicit AND
//   not_expr  := 'not' not_expr | primary
//   primary   := '(' or_expr ')'
//              | 'same' 'residue' 'as' primary
//              | 'within' NUMBER 'of' primary
//              | selector
//   selector  := serial|index|resi|resid|resname|name|atomname|chain
//                ( ':' NUMBER | ':' NUMBER '-' NUMBER | NUMBER )
//              | protein | water | all | none
//
// `within` uses plain (non-minimum-image) Cartesian distance in Å — see
// pbcMode(); this is documented rather than silently claimed as PBC-correct.
// ======================================================================
(function (global) {
  'use strict';

  // ------------------------------------------------------------------
  // SerialStore — the structure-file atom number (GRO / PDB serial) mapped
  // to the zero-based internal atom index, and back.
  //
  // NGL stores the parsed serial in atomStore.serial; for GROMACS GRO that is
  // the 5-digit atom-number field, which WRAPS at 99999 and restarts at 0.
  // So within one chain of consecutive atoms the serial is monotonic until a
  // large drop, which marks a wrap. We count wraps and reconstruct the
  // "effective" serial = parsed + 100000*wraps, exactly the scheme VMD applies
  // to GROMACS numbering. Two consequences are honoured, not hidden:
  //   * the parsed value is preserved verbatim on .serialParsed;
  //   * a serial that lands on >1 internal atom after unwrapping is AMBIGUOUS
  //     and reported as such (resolve returns {ambiguous: true}) rather than
  //     silently matching the wrong atom.
  // ------------------------------------------------------------------
  function SerialStore(store) {
    this.store = store;
    var n = store.count;
    var parsed = store.serial;               // Int32Array, GRO/PDB field
    if (!parsed) {                            // fallback: 1-based (never silent)
      parsed = new Int32Array(n);
      for (var i = 0; i < n; i++) parsed[i] = i + 1;
      this.synthesized = true;
    }
    this.n = n;
    this.parsed = parsed;
    this.eff = new Float64Array(n);
    // walk in internal-index order (== file order == GRO order) counting wraps
    var wraps = 0, prev = -1;
    for (var j = 0; j < n; j++) {
      var v = parsed[j];
      if (v < 0) v += 100000;                 // defensive: negative field
      if (prev >= 0 && v < prev - 50000) wraps++;   // big drop -> 5-digit rollover
      this.eff[j] = v + wraps * 100000;
      prev = v;
    }
    // maps, built once (typed-array pass; ~O(n))
    this.byEff = new global.Map();            // effective serial -> index | AMBIG
    this.byParsed = new global.Map();         // parsed serial -> index | AMBIG
    this.ambigEff = new global.Set();
    this.ambigParsed = new global.Set();
    for (var k = 0; k < n; k++) this._add(this.byEff, this.ambigEff, this.eff[k], k);
    for (var m = 0; m < n; m++) this._add(this.byParsed, this.ambigParsed, parsed[m], m);
  }
  var AMBIG = {};                             // sentinel meaning "matches >1 atom"
  SerialStore.prototype._add = function (map, ambigSet, key, idx) {
    var e = map.get(key);
    if (e === undefined) map.set(key, idx);
    else if (e !== AMBIG) { map.set(key, AMBIG); ambigSet.add(key); }
  };
  // Resolve a serial number -> { indices:[..], ambiguous:bool } (indices is 0
  // normally; may be empty). Prefers the unwrapped (effective) domain, then
  // falls back to the literal parsed value (e.g. asking for a wrapped "0").
  SerialStore.prototype.resolve = function (val) {
    var out = this._lookup(this.byEff, val);
    if (out) return out;
    out = this._lookup(this.byParsed, val);
    if (out) return out;
    return { indices: [], ambiguous: false };
  };
  SerialStore.prototype._lookup = function (map, key) {
    var e = map.get(key);
    if (e === undefined) return null;
    if (e === AMBIG) return { indices: [], ambiguous: true };
    return { indices: [e], ambiguous: false };
  };
  // metadata for one internal index (for the info panel / picking)
  SerialStore.prototype.infoFor = function (idx) {
    return { index: idx, serial: this.eff[idx], serialParsed: this.parsed[idx] };
  };

  // ------------------------------------------------------------------
  // Selector engine, bound to one NGL Structure + its live StructureView.
  // ------------------------------------------------------------------
  function Selector(ctx) {
    this.ctx = ctx;
    this._serial = null;
    this._serialStructure = null;
  }

  Selector.prototype.structure = function () { return this.ctx.comp().structure; };

  Selector.prototype.serialStore = function () {
    var st = this.structure();
    if (this._serial && this._serialStructure === st) return this._serial;
    this._serial = new SerialStore(st.atomStore);
    this._serialStructure = st;
    return this._serial;
  };

  // isSet(i): does the CURRENT playback mode transfer coordinates for atom i?
  // Cα/Backbone -> a subset; Full -> all. In Cα/BB the not-streamed atoms keep
  // their GRO (frame-0) coordinates in atomStore (NGL only rewrites the
  // streamed subset), which is the caveat `within` documents.
  Selector.prototype.streamedMask = function () {
    var sv = this.ctx.comp().structureView;
    var n = this.structure().atomStore.count;
    var m = new Uint8Array(n);
    if (!sv || !sv.atomSet) { m.fill(1); return { mask: m, count: n }; }
    var set = sv.atomSet, cnt = 0;
    for (var i = 0; i < n; i++) { if (set.isSet(i)) { m[i] = 1; cnt++; } }
    return { mask: m, count: cnt };
  };

  Selector.prototype.isStreamed = function (i) {
    var sv = this.ctx.comp().structureView;
    return !(sv && sv.atomSet) ? true : !!sv.atomSet.isSet(i);
  };

  // ---- tokeniser --------------------------------------------------
  // Residue / element / atom-name tokens are whitespace-delimited, so they are
  // safe; a stray character that cannot form a token is a parse error, never a
  // silent match. Numeric selectors take ranges; resname/name/chain are exact
  // (case-insensitive) strings.
  var OPERATORS = { 'AND': 1, 'OR': 1, 'NOT': 1 };
  var KEYWORDS = {
    'SERIAL': 1, 'INDEX': 1, 'RESID': 1, 'RESI': 1, 'RESNAME': 1,
    'NAME': 1, 'ATOMNAME': 1, 'CHAIN': 1, 'PROTEIN': 1, 'WATER': 1,
    'SOLVENT': 1, 'ALL': 1, 'NONE': 1
  };
  // Words that can never be a multi-value: they are operators, selectors or
  // parts of the 'within N of' / 'same residue as' phrases. (VMD likewise
  // reserves these; a residue literally named 'OR' would need quoting —
  // documented limitation.)
  var RESERVED = {
    'AND': 1, 'OR': 1, 'NOT': 1, 'WITHIN': 1, 'SAME': 1, 'RESIDUE': 1,
    'AS': 1, 'OF': 1,
    'SERIAL': 1, 'INDEX': 1, 'RESID': 1, 'RESI': 1, 'RESNAME': 1,
    'NAME': 1, 'ATOMNAME': 1, 'CHAIN': 1, 'PROTEIN': 1, 'WATER': 1,
    'SOLVENT': 1, 'ALL': 1, 'NONE': 1
  };

  function tokenize(str) {
    var out = [];
    var i = 0, L = str.length;
    while (i < L) {
      var c = str[i];
      if (c === '(' || c === ')') { out.push({ t: c }); i++; continue; }
      if (c === ':' || c === '-') { out.push({ t: c }); i++; continue; }
      if (/\s/.test(c)) { i++; continue; }
      // A token is a maximal run of name/value characters. '-' and ':' stay
      // separators so ranges (346-347) parse, while names that START with a
      // digit (GTP's 1MG, 2DG, 5GC ...) stay one whole token — a plain
      // number-first regex would split '1MG' into '1' + 'MG'.
      var w = /^[A-Za-z0-9+@'.]+/.exec(str.slice(i));
      if (w) {
        var txt = w[0];
        if (/^\d+$/.test(txt)) out.push({ t: 'NUM', v: parseInt(txt, 10), s: txt });
        else if (/^\d*\.\d+$/.test(txt)) out.push({ t: 'FLOAT', v: parseFloat(txt), s: txt });
        else out.push({ t: 'WORD', v: txt, u: txt.toUpperCase() });
        i += txt.length; continue;
      }
      throw new Error("unexpected character '" + c + "' in selection");
    }
    return out;
  }

  // ---- parser -----------------------------------------------------
  function Parser(tokens) { this.tk = tokens; this.p = 0; }
  Parser.prototype.peek = function () { return this.tk[this.p]; };
  Parser.prototype.next = function () { return this.tk[this.p++]; };
  Parser.prototype.atEnd = function () { return this.p >= this.tk.length; };
  Parser.prototype.isOp = function (u) { var t = this.peek(); return t && t.t === 'WORD' && t.u === u; };

  Parser.prototype.parse = function () {
    if (this.atEnd()) throw new Error('empty selection');
    var e = this.parseOr();
    if (!this.atEnd()) {
      var t = this.peek();
      throw new Error("unexpected token '" + (t.v || t.t) + "'");
    }
    return e;
  };
  Parser.prototype.parseOr = function () {
    var left = this.parseAnd();
    while (this.isOp('OR')) { this.next(); var right = this.parseAnd(); left = { kind: 'or', a: left, b: right }; }
    return left;
  };
  Parser.prototype.parseAnd = function () {
    var left = this.parseNot();
    for (;;) {
      if (this.isOp('AND')) { this.next(); var r = this.parseNot(); left = { kind: 'and', a: left, b: r }; continue; }
      // implicit AND: a selector / '(' / 'not' / 'same' directly follows
      var t = this.peek();
      if (t && (t.t === '(' || t.t === 'NUM' || (t.t === 'WORD' &&
          (KEYWORDS[t.u] || t.u === 'WITHIN' || t.u === 'SAME')))) {
        var r2 = this.parseNot(); left = { kind: 'and', a: left, b: r2 }; continue;
      }
      break;
    }
    return left;
  };
  Parser.prototype.parseNot = function () {
    if (this.isOp('NOT')) { this.next(); return { kind: 'not', a: this.parseNot() }; }
    return this.parsePrimary();
  };
  Parser.prototype.parsePrimary = function () {
    var t = this.peek();
    if (!t) throw new Error('unexpected end of selection');
    if (t.t === '(') {
      this.next();
      var e = this.parseOr();
      var cl = this.peek();
      if (!cl || cl.t !== ')') throw new Error("missing ')'");
      this.next();
      return e;
    }
    if (t.t === 'WORD' && t.u === 'SAME') return this.parseSameResidue();
    if (t.t === 'WORD' && t.u === 'WITHIN') return this.parseWithin();
    return this.parseSelector();
  };

  // same residue as <expr>
  Parser.prototype.parseSameResidue = function () {
    this.next();                                  // consume 'same'
    var r = this.peek();
    if (!r || r.t !== 'WORD' || r.u !== 'RESIDUE') throw new Error("expected 'residue' after 'same'");
    this.next();
    var a = this.peek();
    if (!a || a.t !== 'WORD' || a.u !== 'AS') throw new Error("expected 'as' after 'same residue'");
    this.next();
    var inner = this.parsePrimary();              // binds like 'not' / a modifier
    return { kind: 'sameResidue', a: inner };
  };

  // within <R> of <expr>
  Parser.prototype.parseWithin = function () {
    this.next();                                  // consume 'within'
    var rn = this.peek();
    if (!rn || (rn.t !== 'NUM' && rn.t !== 'FLOAT')) throw new Error("'within' needs a distance in A");
    this.next();
    var radius = rn.v;
    if (!(radius > 0)) throw new Error("'within' radius must be > 0");
    var of = this.peek();
    if (!of || of.t !== 'WORD' || of.u !== 'OF') throw new Error("expected 'of' after within distance");
    this.next();
    var target = this.parsePrimary();
    return { kind: 'within', radius: radius, a: target };
  };

  // a number, possibly a trailing '-' range
  Parser.prototype.parseNumberOrRange = function () {
    var n = this.peek();
    if (!n || n.t !== 'NUM') throw new Error('expected an integer');
    this.next();
    var lo = n.v;
    if (this.peek() && this.peek().t === '-') {
      this.next();
      var m = this.peek();
      if (!m || m.t !== 'NUM') throw new Error('expected an integer after -');
      this.next();
      return { lo: lo, hi: m.v };
    }
    return { lo: lo, hi: lo };
  };

  Parser.prototype.parseSelector = function () {
    var t = this.next();
    if (!t || t.t !== 'WORD') throw new Error("expected a selector, got '" + (t ? (t.v || t.t) : 'end') + "'");
    var u = t.u;
    switch (u) {
      case 'ALL': return { kind: 'all' };
      case 'NONE': return { kind: 'none' };
      case 'PROTEIN': return { kind: 'protein' };
      case 'WATER':
      case 'SOLVENT': return { kind: 'water' };
      case 'SERIAL':
      case 'INDEX':
      case 'RESID':
      case 'RESI':
      case 'RESNAME':
      case 'NAME':
      case 'ATOMNAME':
      case 'CHAIN':
        return this.parseValueSelector(u);
      default:
        throw new Error("unknown selector '" + t.v + "'");
    }
  };

  // VMD-style multi-value shorthand: one selector may be followed by several
  // values, e.g. `resname GTP GDP`, `resid 122 123 124`, `name CA CB CG`,
  // `chain A B`, `index 10 11 12`, `serial 280-282 290`. Each extra value
  // means the same selector OR-ed in. This is purely additive: explicit
  // `resname GTP or resname GDP` keeps parsing as before. Chaining stops at
  // reserved words ('and'/'or'/'not'/'within'/'same'/selectors) and at
  // '(' / end, so an ordinary implicit AND (e.g. `protein and name CA`,
  // `resname GTP and name CA`) is unaffected: 'and'/'or'/'not' are keywords
  // that end the value list, and bare selectors never continue a list.
  Parser.prototype.parseValueSelector = function (u) {
    var kind = u === 'CHAIN' ? 'chain'
             : (u === 'NAME' || u === 'ATOMNAME') ? 'name'
             : u === 'RESNAME' ? 'resname'
             : u === 'SERIAL' ? 'serial'
             : u === 'INDEX' ? 'index' : 'resid';

    function valueContinues(tok) {                       // another value next?
      if (!tok) return false;
      if (tok.t === 'WORD') return !RESERVED[tok.u];     // names like SER, GTP
      return tok.t === 'NUM';                            // numbers / ranges
    }
    function oneValue() {
      if (kind === 'resname' || kind === 'name' || kind === 'chain') {
        var t = this.peek();
        if (!t || t.t !== 'WORD') throw new Error("'" + kind + "' needs a name");
        this.next();
        return { kind: kind, value: t.v };
      }
      // numeric selector: NUMBER or NUMBER-NUMBER (optionally ':'-prefixed)
      if (this.peek() && this.peek().t === ':') this.next();
      var rng = this.parseNumberOrRange();
      return { kind: kind, lo: rng.lo, hi: rng.hi };
    }

    // A leading ':' is the VMD/NGL field syntax (`chain:A`, `resid:5`) and is
    // consumed once up front for every selector kind.
    if (this.peek() && this.peek().t === ':') this.next();
    var head = this.peek();
    if (!head) throw new Error("selector '" + u.toLowerCase() + "' needs a value");
    if ((kind === 'resname' || kind === 'name' || kind === 'chain') &&
        head.t !== 'WORD') throw new Error("'" + kind + "' needs a name");
    if ((kind === 'serial' || kind === 'index' || kind === 'resid') &&
        head.t !== 'NUM') throw new Error("'" + kind + "' needs a number");
    if (kind === 'resname' || kind === 'name' || kind === 'chain') {
      if (head.t === 'WORD' && RESERVED[head.u])
        throw new Error("'" + kind + "' needs a name");
    }

    var node = oneValue.call(this);
    // consume trailing values while the next token is clearly another value.
    // ':' starts a new value ONLY for numeric selectors (`resid 5 :7`), so a
    // VMD-style `chain:A` never mis-chains into the next token.
    for (;;) {
      var t2 = this.peek();
      if (kind !== 'resname' && kind !== 'name' && kind !== 'chain' &&
          t2 && t2.t === ':' && this.tk[this.p + 1] &&
          (this.tk[this.p + 1].t === 'NUM' || this.tk[this.p + 1].t === '-')) {
        node = { kind: 'or', a: node, b: oneValue.call(this) };
        continue;
      }
      if (!valueContinues(t2)) break;
      node = { kind: 'or', a: node, b: oneValue.call(this) };
    }
    return node;
  };

  Selector.prototype.parse = function (text) {
    return new Parser(tokenize(text)).parse();
  };

  // ---- evaluation -------------------------------------------------
  // All primitives produce a Uint8Array(n) over the STRUCTURE atom indices.
  // residueIndex (store.residueIndex) groups atoms into residues; two residues
  // are distinct when (chainIndex, resno) differ, so the dimer's two "resid
  // 346" (chain A TRP, chain D PRO) are handled per residue, not merged by
  // resno alone.
  Selector.prototype._newMask = function () { return new Uint8Array(this.structure().atomStore.count); };

  Selector.prototype._eval = function (node, ctx) {
    switch (node.kind) {
      case 'all': { var a = this._newMask(); a.fill(1); return a; }
      case 'none': return this._newMask();
      case 'or': {
        var l = this._eval(node.a, ctx), r = this._eval(node.b, ctx);
        for (var i = 0; i < l.length; i++) l[i] |= r[i];
        return l;
      }
      case 'and': {
        var l2 = this._eval(node.a, ctx), r2 = this._eval(node.b, ctx);
        for (var j = 0; j < l2.length; j++) l2[j] &= r2[j];
        return l2;
      }
      case 'not': {
        var m = this._eval(node.a, ctx);
        for (var k = 0; k < m.length; k++) m[k] ^= 1;
        return m;
      }
      case 'protein': return this._proteinMask(ctx);
      case 'water': return this._waterMask(ctx);
      case 'resname': return this._resnameMask(node.value);
      case 'name': return this._nameMask(node.value);
      case 'chain': return this._chainMask(node.value);
      case 'index': return this._indexMask(node.lo, node.hi, ctx);
      case 'serial': return this._serialMask(node.lo, node.hi, ctx);
      case 'resid': return this._residMask(node.lo, node.hi);
      case 'within': return this._withinMask(node, ctx);
      case 'sameResidue': return this._sameResidueMask(this._eval(node.a, ctx));
      default: throw new Error('cannot evaluate node ' + node.kind);
    }
  };

  Selector.prototype._proteinMask = function (ctx) {
    // protein / nucleic residue names come from the server component table;
    // fall back to a standard-residue set when it is unavailable.
    var names = new global.Set();
    var comps = (this.ctx.meta && this.ctx.meta()) || {};
    var list = comps.components || [];
    if (list.length) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].category === 'protein' || list[i].category === 'nucleic')
          names.add(String(list[i].resname).toUpperCase());
      }
    } else {
      for (var q = 0; q < STD_RES.length; q++) names.add(STD_RES[q]);
    }
    var st = this.structure(), a = st.atomStore, p = st.getAtomProxy();
    var m = this._newMask();
    for (var j = 0; j < a.count; j++) {
      p.index = j;
      if (names.has(String(p.resname).toUpperCase())) m[j] = 1;
    }
    return m;
  };

  // solvent names: server 'water' category, else a standard set.
  Selector.prototype._waterMask = function () {
    var names = new global.Set();
    var comps = (this.ctx.meta && this.ctx.meta()) || {};
    var list = comps.components || [];
    for (var i = 0; i < list.length; i++)
      if (list[i].category === 'water') names.add(String(list[i].resname).toUpperCase());
    if (!names.size) for (var w = 0; w < WATER_RES.length; w++) names.add(WATER_RES[w]);
    var st = this.structure(), a = st.atomStore, p = st.getAtomProxy();
    var m = this._newMask();
    for (var j = 0; j < a.count; j++) {
      p.index = j;
      var rn = String(p.resname).toUpperCase();
      if (names.has(rn) || rn.indexOf('TIP') === 0 || rn.indexOf('SPC') === 0) m[j] = 1;
    }
    return m;
  };

  Selector.prototype._resnameMask = function (value) {
    var want = String(value).toUpperCase();
    var st = this.structure(), a = st.atomStore, p = st.getAtomProxy();
    var m = this._newMask();
    for (var j = 0; j < a.count; j++) { p.index = j; if (String(p.resname).toUpperCase() === want) m[j] = 1; }
    return m;
  };

  Selector.prototype._nameMask = function (value) {
    var want = String(value).toUpperCase();
    var st = this.structure(), a = st.atomStore, p = st.getAtomProxy();
    var m = this._newMask();
    for (var j = 0; j < a.count; j++) { p.index = j; if (String(p.atomname).toUpperCase() === want) m[j] = 1; }
    return m;
  };

  // chain: matches chainname (letter) OR chainid (number), case-insensitive.
  Selector.prototype._chainMask = function (value) {
    var want = String(value).toUpperCase();
    var st = this.structure(), a = st.atomStore, p = st.getAtomProxy();
    var m = this._newMask();
    for (var j = 0; j < a.count; j++) {
      p.index = j;
      if (String(p.chainname).toUpperCase() === want || String(p.chainid) === want) m[j] = 1;
    }
    return m;
  };

  Selector.prototype._indexMask = function (lo, hi, ctx) {
    var m = this._newMask(), n = m.length;
    var a = Math.max(0, lo), b = Math.min(n - 1, hi);
    for (var i = a; i <= b; i++) m[i] = 1;
    ctx.indexOutOfRange = (lo < 0 || hi >= n) ? true : ctx.indexOutOfRange;
    return m;
  };

  Selector.prototype._serialMask = function (lo, hi, ctx) {
    var ss = this.serialStore();
    var m = this._newMask();
    // NOTE: never reset the flag here — an OR-chain of several serial
    // selectors must preserve an ambiguity raised by ANY of them.
    for (var s = lo; s <= hi; s++) {
      var r = ss.resolve(s);
      if (r.ambiguous) { ctx.ambiguousSerial = true; continue; }
      for (var k = 0; k < r.indices.length; k++) m[r.indices[k]] = 1;
    }
    return m;
  };

  // resid: select whole residues whose resno == N. Two residues sharing a
  // resno in different chains both match (each expanded fully).
  Selector.prototype._residMask = function (lo, hi) {
    var st = this.structure(), a = st.atomStore, rs = st.residueStore;
    var ridx = a.residueIndex;
    var m = this._newMask();
    // mark matching residues, then expand to their atoms (residueStore gives a
    // contiguous [atomOffset, +atomCount) span per residue — GRO keeps residue
    // atoms contiguous, which the server relies on too)
    for (var r = 0; r < rs.count; r++) {
      var no = rs.resno[r];
      if (no >= lo && no <= hi) {
        var off = rs.atomOffset[r], cnt = rs.atomCount[r];
        for (var k = 0; k < cnt; k++) m[off + k] = 1;
      }
    }
    return m;
  };

  // within R of <target>: every atom whose CURRENT coords are within R Å of
  // any target atom. Neighbors via the structure's live spatial hash — cost
  // ~ O(#targets + #found), so large systems remain practical.
  Selector.prototype._withinMask = function (node, ctx) {
    var st = this.structure(), a = st.atomStore;
    var target = this._eval(node.a, ctx);
    var R = node.radius, R2 = R * R;
    var m = this._newMask();
    var hash = st.spatialHash;
    if (!hash || !hash.within) throw new Error('spatial hash unavailable — cannot evaluate within');

    // Pre-empt the known blind spot: in a partial mode the not-streamed atoms
    // keep their GRO (frame-0) coordinates — NGL only rewrites the streamed
    // subset every frame. So `within` in Cα/Backbone mixes current coords
    // (streamed) with stale frame-0 coords (not streamed). Flag any within in a
    // partial mode so the UI documents it rather than pretending exactness.
    var sv = this.ctx.comp().structureView;
    var notStreamed = 0, touched = 0;
    for (var i = 0; i < target.length; i++) {
      if (!target[i]) continue;
      touched++;
      if (sv && sv.atomSet && !sv.atomSet.isSet(i)) notStreamed++;
    }
    // spatialExact = every atom the query could touch has CURRENT coords.
    if (this.ctx.view() !== 'full') ctx.spatialPartial = true;
    ctx.spatialTargetAtoms = (ctx.spatialTargetAtoms || 0) + touched;
    ctx.spatialTargetNotStreamed = (ctx.spatialTargetNotStreamed || 0) + notStreamed;

    var r2 = Math.ceil(R);
    var self = this;
    function addNear(ax, ay, az) {
      var res = hash.within(ax, ay, az, r2);
      for (var q = 0; q < res.length; q++) {
        var idx = res[q];
        if (m[idx]) continue;
        var dx = a.x[idx] - ax, dy = a.y[idx] - ay, dz = a.z[idx] - az;
        if (dx * dx + dy * dy + dz * dz <= R2) m[idx] = 1;
      }
    }
    for (var j = 0; j < target.length; j++) {
      if (!target[j]) continue;
      addNear(a.x[j], a.y[j], a.z[j]);
    }
    return m;
  };

  // same residue as <mask>: expand every set atom to its whole residue.
  Selector.prototype._sameResidueMask = function (mask) {
    var st = this.structure(), a = st.atomStore, rs = st.residueStore;
    var ridx = a.residueIndex;
    var m = this._newMask();
    for (var i = 0; i < mask.length; i++) {
      if (!mask[i]) continue;
      var r = ridx[i];
      var off = rs.atomOffset[r], cnt = rs.atomCount[r];
      for (var k = 0; k < cnt; k++) m[off + k] = 1;
    }
    return m;
  };

  // ---- public API -------------------------------------------------
  // evaluate(text) -> result object, or throws on a parse error.
  Selector.prototype.evaluate = function (text) {
    var ast = this.parse(text);
    var ctx = { ambiguousSerial: false, spatialPartial: false, indexOutOfRange: false };
    var mask = this._eval(ast, ctx);
    var st = this.structure(), a = st.atomStore;
    var sv = this.ctx.comp().structureView;
    var ss = this.serialStore();
    var ridx = a.residueIndex, rs = st.residueStore;
    var p = st.getAtomProxy();

    // indices + residue bookkeeping (residues keyed by chainIndex|resno so the
    // dimer's repeated resno counts as distinct residues)
    var indices = [];
    var resSeen = new global.Map();
    var atomResKey = new Int32Array(a.count); atomResKey.fill(-1);
    for (var i = 0; i < mask.length; i++) {
      if (!mask[i]) continue;
      indices.push(i);
      var r = ridx[i];
      var key = rs.chainIndex[r] * 1000000 + rs.resno[r];
      var id = resSeen.get(key);
      if (id === undefined) { id = resSeen.size; resSeen.set(key, id); }
      atomResKey[i] = id;
    }

    // coverage vs the CURRENT mode
    var streamed = 0, notStreamed = 0;
    for (var s = 0; s < indices.length; s++) {
      var ii = indices[s];
      if (sv && sv.atomSet && !sv.atomSet.isSet(ii)) notStreamed++; else streamed++;
    }

    // single-atom metadata
    var atom = null;
    if (indices.length === 1) {
      var q = indices[0]; p.index = q;
      atom = {
        index: q,
        serial: ss.eff[q],
        serialParsed: ss.parsed[q],
        name: String(p.atomname),
        resname: String(p.resname),
        resid: p.resno,
        chain: String(p.chainname),
        element: String(p.element),
        x: +a.x[q].toFixed(3), y: +a.y[q].toFixed(3), z: +a.z[q].toFixed(3)
      };
    }

    return {
      ok: true, text: text,
      mask: mask, indices: indices, atomResKey: atomResKey,
      nAtoms: indices.length, nResidues: resSeen.size,
      nStreamed: streamed, nNotStreamed: notStreamed,
      spatial: nodeHasSpatial(ast),
      ambiguousSerial: !!ctx.ambiguousSerial,
      // spatialPartial: spatial query touched atoms the mode does not stream
      spatialPartial: !!ctx.spatialPartial,
      pbc: this.pbcMode(),
      atom: atom
    };
  };

  function nodeHasSpatial(n) {
    if (!n || typeof n !== 'object') return false;
    if (n.kind === 'within') return true;
    return nodeHasSpatial(n.a) || nodeHasSpatial(n.b);
  }

  // Honest PBC statement (see §10): `within` is plain Cartesian /
  // non-minimum-image. Box vectors ARE available per frame (trajectory
  // boxCache), but the spatial hash ignores them, so we do not claim PBC.
  Selector.prototype.pbcMode = function () {
    var A = this.ctx.app && this.ctx.app();
    var hasBox = !!(A && A.traj && A.traj.boxCache && A.traj.boxCache[this.ctx.frame()] !== undefined);
    return {
      mode: 'cartesian',
      minimumImage: false,
      boxAvailable: hasBox,
      note: hasBox
        ? 'within uses plain Cartesian distance (no minimum image); box vectors are ' +
          'present but not applied.'
        : 'within uses plain Cartesian distance; no box vectors for this frame.'
    };
  };

  // Per-atom element from an atom name, cached by (atomTypeId|resname). GRO
  // stores no element column, so NGL infers it from the name; this mirrors that
  // for uniform overlay colouring.
  var ELEMENT_BY_TYPE = {};
  Selector.prototype.elementFor = function (idx) {
    var st = this.structure(), a = st.atomStore, p = st.getAtomProxy();
    p.index = idx;
    var key = a.atomTypeId[idx] + '|' + p.resname;
    var e = ELEMENT_BY_TYPE[key];
    if (e === undefined) { e = inferElement(String(p.atomname), String(p.resname)); ELEMENT_BY_TYPE[key] = e; }
    return e;
  };

  // Standard CPK element colours (hex 0xRRGGBB), fallback pink.
  var CPK = {
    H: 0xffffff, C: 0x909090, N: 0x3050f8, O: 0xff0d0d, F: 0x90e050,
    S: 0xffff30, P: 0xff8000, CL: 0x1ff01f, NA: 0xab5cf2, MG: 0x8aff00,
    K: 0x8f40d4, CA: 0x3dff00, ZN: 0x49c0c0, FE: 0xe06633, MN: 0x9c7ac7,
    BR: 0xa62929, I: 0x940094, HE: 0xd9ffff, AR: 0x80d1e3, CU: 0xc88033,
    NI: 0x50d050
  };
  Selector.prototype.elementColor = function (sym) {
    var c = CPK[String(sym).toUpperCase()];
    return c === undefined ? 0xff6699 : c;
  };

  // deterministic per-chain colour for the 'Chain' scheme
  var CHAIN_PALETTE = [0xff5d5d, 0x4ea1ff, 0x37c98b, 0xf4d35e, 0xb388ff,
                       0xff8f5e, 0x26c6da, 0xf06292, 0xaed581, 0xffd54f];
  Selector.prototype.chainColor = function (chainname) {
    var s = String(chainname); var h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return CHAIN_PALETTE[h % CHAIN_PALETTE.length];
  };

  // Residue scheme: a smooth per-residue rainbow (element-agnostic).
  Selector.prototype.residueColor = function (resKey) {
    var h = (resKey * 2654435761) >>> 0;
    var hue = (h % 360) / 360;
    return hslToHex(hue, 0.62, 0.58);
  };

  // 'Name' scheme: one stable colour per atom name (CA, CB, N, O, …). Matches
  // the VMD "Name" label idea — atoms sharing an atom name share a colour —
  // using the same deterministic hash as the chain palette so it is stable
  // across frames and across representations.
  Selector.prototype.nameColor = function (atomname) {
    var s = String(atomname); var h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 131 + s.charCodeAt(i)) >>> 0;
    return CHAIN_PALETTE[h % CHAIN_PALETTE.length];
  };

  // ---- helpers ----------------------------------------------------
  function hslToHex(h, s, l) {
    function f(n) {
      var k = (n + h * 12) % 12;
      var a = s * Math.min(l, 1 - l);
      return l - a * Math.max(-1, Math.min(k - 3, Math.min(9 - k, 1)));
    }
    var r = Math.round(f(0) * 255), g = Math.round(f(8) * 255), b = Math.round(f(4) * 255);
    return (r << 16) | (g << 8) | b;
  }

  var ELEMENT_SYMBOL = /^(CL|BR|MG|NA|K|CA|FE|ZN|MN|CU|NI|HE|AR|I|F|S|P|O|N|C|H)(\d|[A-Z])?$/;
  function inferElement(name, resname) {
    var n = name.trim().toUpperCase();
    var m = ELEMENT_SYMBOL.exec(n);
    if (m) return m[1];
    var RN = resname.trim().toUpperCase();
    if (RN === 'MG' || RN === 'NA' || RN === 'CL' || RN === 'K' || RN === 'CA' ||
        RN === 'ZN' || RN === 'FE' || RN === 'MN' || RN === 'CU' || RN === 'NI') return RN;
    return n[0] || '?';
  }

  var STD_RES = ['ALA','ARG','ASN','ASP','CYS','GLN','GLU','GLY','HIS','ILE',
    'LEU','LYS','MET','PHE','PRO','SER','THR','TRP','TYR','VAL','DA','DC','DG',
    'DT','A','C','G','U','HIE','HID','HIP','CYX','MSE'];
  var WATER_RES = ['HOH','W','SOL','TIP3','TIP4','TIP5','TIP','SPC','SPCE','OH','WAT','H2O'];

  global.SelectionEngine = {
    Selector: Selector,
    SerialStore: SerialStore,
    tokenize: tokenize,
    parse: function (text) { return new Selector({}).parse(text); }
  };
})(window);
