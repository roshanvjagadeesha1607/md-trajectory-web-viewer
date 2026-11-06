// ======================================================================
// SELECTION PANEL UI — the VMD-like representation list.
//
// Builds the whole "Selections" panel (row list + controls + pick card +
// info/help) into a mount point and drives a RepManager. Nothing in this
// file touches trajectory streaming, the main protein reps, surface or
// Gaussian code; the only viewer inputs it reads are the ones RepManager
// already has (stage, engine ctx, frame).
//
// DOM contract kept stable for tests/console:
//   #selrep-list            representation rows
//   #seltext                expression input
//   #selrep / #selcolor     style / colour selects (of the ACTIVE row)
//   #seldynamic             dynamic checkbox (of the ACTIVE row)
//   #seladd                 + Add Representation
//   #selapply               Apply Changes (active row only)
//   #seldup / #seldeleterow / #selclearall
//   #selpick #selpickinfo #pickindex #pickserial #pickresid
//   #selinfo #selwarn #selhelp #selhelpbox
// ======================================================================
(function (global) {
  'use strict';

  var STYLE_LABEL = { 'licorice': 'Licorice', 'ball+stick': 'Ball+Stick',
                      'sphere': 'Sphere', 'line': 'Line', 'spacefill': 'Spacefill' };
  var COLOR_LABEL = { 'element': 'Element', 'name': 'Name',
                      'residue': 'Residue', 'chain': 'Chain', 'uniform': 'Uniform' };

  function escHtml(s) {
    return String(s).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function injectCss() {
    if (document.getElementById('sel-style')) return;
    var st = document.createElement('style');
    st.id = 'sel-style';
    st.textContent = [
      '#selrep-list { display:flex; flex-direction:column; gap:4px; margin-bottom:8px; }',
      '.sel-row { background:var(--panel2); border:1px solid var(--line); border-radius:6px;',
      '  padding:5px 7px; font-size:12px; cursor:pointer; }',
      '.sel-row.active { border-color:#6ea8ff; box-shadow:0 0 0 1px #6ea8ff55 inset; }',
      '.sel-row .l1 { display:flex; align-items:center; gap:6px; min-width:0; }',
      '.sel-row .expr { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;',
      '  white-space:nowrap; font-family:ui-monospace,Menlo,Consolas,monospace; }',
      '.sel-row .counts { color:var(--muted); font-size:11px; margin-top:2px;',
      '  font-variant-numeric:tabular-nums; }',
      '.sel-row button { padding:1px 6px; font-size:11px; line-height:1.4; }',
      '.sel-row .tags { color:var(--muted); font-size:10.5px; }'
    ].join('\n');
    document.head.appendChild(st);
  }

  function selOptions(sel, map, value) {
    sel.innerHTML = '';
    Object.keys(map).forEach(function (k) {
      var o = document.createElement('option');
      o.value = k; o.textContent = map[k];
      sel.appendChild(o);
    });
    sel.value = value;
  }

  // ------------------------------------------------------------------
  function buildPanel(mountId, mgr, hooks) {
    injectCss();
    var mount = document.getElementById(mountId);
    mount.innerHTML =
      '<h2>Selections / Representations</h2>' +
      '<div id="selrep-list"></div>' +
      '<div class="row"><input type="text" id="seltext" placeholder="e.g. resname SER" ' +
      '  style="flex:1; min-width:0; background:var(--panel2); color:var(--fg);' +
      '         border:1px solid var(--line); border-radius:6px; padding:7px 9px;' +
      '         font-size:13px; font-family:ui-monospace,Menlo,Consolas,monospace;"' +
      '  autocomplete="off" spellcheck="false"></div>' +
      '<div class="row">' +
      '  <span class="stat">style</span><select id="selrep"></select>' +
      '  <span class="stat">colour</span><select id="selcolor"></select>' +
      '  <label class="chk" title="Re-evaluate the spatial selection of this row (within / same residue as) on every frame">' +
      '    <input type="checkbox" id="seldynamic"> Dynamic</label>' +
      '</div>' +
      '<div class="row">' +
      '  <button class="primary" id="seladd" title="Add the text above as a NEW representation">+ Add Representation</button>' +
      '  <button id="selapply" title="Update the active row only">Apply Changes</button>' +
      '  <button id="seldup" title="Copy the active row">Duplicate</button>' +
      '  <button id="seldeleterow" title="Delete the active row">Delete</button>' +
      '  <button id="selclearall" title="Delete ALL representations">Clear All</button>' +
      '  <button id="selhelp" title="Selection syntax and examples">Examples</button>' +
      '</div>' +
      '<div id="selpick" hidden style="background:var(--panel2);border:1px solid var(--line);' +
      '     border-radius:6px;padding:6px 8px;margin:2px 0 8px;font-size:12px;">' +
      '  <div id="selpickinfo" style="font-variant-numeric:tabular-nums;"></div>' +
      '  <div class="row" style="margin:6px 0 0">' +
      '    <button id="pickindex" style="padding:3px 7px;font-size:12px">Use index</button>' +
      '    <button id="pickserial" style="padding:3px 7px;font-size:12px">Use serial</button>' +
      '    <button id="pickresid" style="padding:3px 7px;font-size:12px">Use resid</button>' +
      '  </div></div>' +
      '<div class="stat" id="selinfo">No representations. Type a selection and press ' +
      '<b>+ Add Representation</b>.</div>' +
      '<div class="stat warn" id="selwarn" hidden></div>' +
      '<details id="selhelpbox" hidden style="font-size:11.5px;color:var(--muted);line-height:1.6">' +
      '  <summary style="cursor:pointer">Selection syntax</summary>' +
      '  <div style="margin-top:6px; font-family:ui-monospace,Menlo,Consolas,monospace">' +
      '    <div>index 279 &nbsp;·&nbsp; index 100-200 &nbsp;·&nbsp; index 1 2 3</div>' +
      '    <div>serial 280 &nbsp;·&nbsp; serial 280 281 282</div>' +
      '    <div>resid 346 &nbsp;·&nbsp; resid 122 123 124</div>' +
      '    <div>resname SER &nbsp;·&nbsp; resname GTP GDP &nbsp;·&nbsp; name CA CB &nbsp;·&nbsp; chain A B</div>' +
      '    <div>protein &nbsp;·&nbsp; water</div>' +
      '    <div>within 3 of resid 346</div>' +
      '    <div>same residue as within 3 of index 444387</div>' +
      '    <div>resname GTP or resname GDP &nbsp;·&nbsp; protein and name CA &nbsp;·&nbsp; not water</div>' +
      '  </div>' +
      '  <div style="margin-top:6px">' +
      '    <b>index</b> = zero-based internal atom position (first atom is index 0). ' +
      '<b>serial</b> = the atom number written in the structure file (GRO/PDB), which is ' +
      '<i>not</i> necessarily index+1. They are separate selectors on purpose. ' +
      'Several values after one selector mean OR: <b>resname GTP GDP</b> ≡ ' +
      '<i>resname GTP or resname GDP</i>.</div>' +
      '  <div style="margin-top:6px">Each row is an independent representation (like VMD): ' +
      'click a row to edit it, tick the box to hide it (its selection is kept), ' +
      'Duplicate copies it, × deletes it only.</div>' +
      '</details>';

    var $ = function (id) { return document.getElementById(id); };
    var list = $('selrep-list'), text = $('seltext'),
        styleSel = $('selrep'), colorSel = $('selcolor'), dynChk = $('seldynamic'),
        info = $('selinfo'), warn = $('selwarn');
    selOptions(styleSel, STYLE_LABEL, mgr._defaultStyle);
    selOptions(colorSel, COLOR_LABEL, mgr._defaultColor);

    var ui = {
      mgr: mgr, pick: null, _sig: '', _editingOwn: false,
      el: { text: text, styleSel: styleSel, colorSel: colorSel, dynChk: dynChk }
    };

    // ---- rows --------------------------------------------------------
    function rowSignature(rs) {
      return rs.map(r => [r.id, r.visible ? 1 : 0, r.dynamic ? 1 : 0,
                          r.atomCount, r.residueCount, r.style, r.color,
                          r.expression, r.active ? 1 : 0].join(',')).join(';');
    }
    function renderRows() {
      var rs = mgr.summary();
      var sig = rowSignature(rs);
      if (sig === ui._sig) return;              // playing a static movie: no DOM churn
      ui._sig = sig;
      list.innerHTML = '';
      rs.forEach(function (r) {
        var row = document.createElement('div');
        row.className = 'sel-row' + (r.active ? ' active' : '');
        row.dataset.repId = String(r.id);
        var vis = document.createElement('input');
        vis.type = 'checkbox'; vis.checked = r.visible;
        vis.title = 'hide / show this representation (selection is kept)';
        vis.onchange = function (e) {
          e.stopPropagation();
          mgr.setVisible(r.id, vis.checked);
        };
        vis.onclick = function (e) { e.stopPropagation(); };
        var lab = document.createElement('span');
        lab.className = 'tags';
        lab.textContent = STYLE_LABEL[r.style] + ' | ' + COLOR_LABEL[r.color] +
                          (r.dynamic ? ' | dyn' : '');
        var expr = document.createElement('span');
        expr.className = 'expr'; expr.title = r.expression;
        expr.textContent = r.expression;
        var dup = document.createElement('button');
        dup.textContent = '⧉'; dup.title = 'duplicate this representation';
        dup.onclick = function (e) { e.stopPropagation(); mgr.duplicate(r.id); };
        var del = document.createElement('button');
        del.textContent = '×'; del.title = 'delete ONLY this representation';
        del.onclick = function (e) {
          e.stopPropagation();
          mgr.remove(r.id);
          if (mgr.activeId === null) syncControls();
        };
        var l1 = document.createElement('div');
        l1.className = 'l1';
        l1.appendChild(vis); l1.appendChild(lab); l1.appendChild(expr);
        l1.appendChild(dup); l1.appendChild(del);
        var counts = document.createElement('div');
        counts.className = 'counts';
        counts.textContent = r.atomCount.toLocaleString('en-US') + ' atoms / ' +
                             r.residueCount.toLocaleString('en-US') + ' residues';
        row.appendChild(l1); row.appendChild(counts);
        row.onclick = function () { mgr.setActive(r.id); };
        list.appendChild(row);
      });
      if (!rs.length) {
        var empty = document.createElement('div');
        empty.className = 'stat';
        empty.style.cssText = 'font-size:11.5px;color:var(--muted)';
        empty.textContent = 'none — add one below';
        list.appendChild(empty);
      }
    }

    // ---- active-row controls ------------------------------------------
    function syncControls() {
      var rep = mgr.active();
      if (!rep) {
        text.value = '';
        styleSel.value = mgr._defaultStyle;
        colorSel.value = mgr._defaultColor;
        dynChk.checked = false;
        renderInfo(null);
        return;
      }
      ui._editingOwn = true;
      text.value = rep.expression;
      styleSel.value = rep.style;
      colorSel.value = rep.color;
      dynChk.checked = !!rep.dynamic;
      ui._editingOwn = false;
      renderInfo(rep);
    }

    function renderInfo(rep) {
      warn.hidden = true;
      if (rep === null) {
        info.innerHTML = mgr.reps.length
          ? mgr.reps.length + ' representation(s). Click a row to edit it.'
          : 'No representations. Type a selection and press <b>+ Add Representation</b>.';
        return;
      }
      if (!rep.atomCount) {
        info.innerHTML = '<b>0 atoms</b> match <i>' + escHtml(rep.expression) + '</i>.';
        return;
      }
      var msg = '<b>rep ' + rep.id + '</b> — selected atoms: <b>' +
                rep.atomCount.toLocaleString('en-US') + '</b> · residues: <b>' +
                rep.residueCount.toLocaleString('en-US') + '</b>';
      if (rep.nNotStreamed) {
        var A = hooks.app();
        // Accuracy rule: static rows whose non-streamed atoms ARE covered by
        // the coordinate side-channel get live coordinates every frame — do
        // not claim GRO freeze for them. Dynamic rows and side-channel
        // overflow (>40k atoms) genuinely stay on GRO frame-0 coords.
        var sc = hooks.coords ? hooks.coords() : global.SEL && global.SEL.coords;
        var live = !rep.dynamic && sc && sc.count() > 0;
        msg += '<br><span style="color:var(--muted)">' + rep.nNotStreamed +
               ' of these atoms are outside the ' + A.view.toUpperCase() + ' stream — ' +
               (live
                 ? 'their live per-frame coordinates are fetched through the ' +
                   'selection side-channel.'
                 : 'they show GRO (frame-0) coordinates.') + '</span>';
      }
      if (rep.atom) {
        var a = rep.atom;
        msg += '<br>index <b>' + a.index + '</b> · serial <b>' + a.serial +
               '</b> · ' + escHtml(a.name) + ' · ' + escHtml(a.resname) +
               ' ' + a.resid + ' · chain ' + escHtml(a.chain) +
               '<br>coords: ' + a.x + ', ' + a.y + ', ' + a.z +
               ' Å (' + escHtml(a.element) + ')';
      }
      if (rep.spatial) {
        msg += '<br><span style="color:var(--muted)">spatial (within): plain ' +
               'Cartesian distance, current frame' +
               (rep.spatialPartial ? ' — not minimum-image; some atoms use GRO ' +
                'frame-0 coords in ' + hooks.app().view.toUpperCase() + ' mode' : '') +
               '.</span>';
      }
      if (rep.ambiguousSerial) {
        msg += '<br><span class="warn">⚠ a serial in this selection is ambiguous ' +
               '(GRO 5-digit wrap): it matches more than one atom. Use a more ' +
               'specific number or an index.</span>';
      }
      info.innerHTML = msg;
    }

    function showWarn(msg) { warn.hidden = false; warn.textContent = '✗ ' + msg; }

    // ---- control handlers ---------------------------------------------
    $('seladd').onclick = function () {
      var t = text.value.trim();
      if (!t) { text.focus(); return; }
      var r = mgr.add({ expression: t, style: styleSel.value,
                        color: colorSel.value, dynamic: dynChk.checked });
      if (!r.ok) showWarn(r.error);
      else { info && (warn.hidden = true); }
    };
    text.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); $('seladd').click(); }
    });
    $('selapply').onclick = function () {
      var rep = mgr.active();
      if (!rep) { showWarn('no active row — click a representation first, or Add one'); return; }
      var r = mgr.update(rep.id, { expression: text.value.trim(),
                                   style: styleSel.value, color: colorSel.value,
                                   dynamic: dynChk.checked });
      if (!r.ok) showWarn(r.error); else warn.hidden = true;
    };
    styleSel.onchange = function () {
      var rep = mgr.active();
      if (rep && !ui._editingOwn) mgr.update(rep.id, { style: styleSel.value });
    };
    colorSel.onchange = function () {
      var rep = mgr.active();
      if (rep && !ui._editingOwn) mgr.update(rep.id, { color: colorSel.value });
    };
    dynChk.onchange = function () {
      var rep = mgr.active();
      if (!rep || ui._editingOwn) return;
      mgr.update(rep.id, { dynamic: dynChk.checked });
      if (dynChk.checked) rep.lastDyn = -1;   // force re-eval at next frame
    };
    $('seldup').onclick = function () { mgr.duplicate(mgr.activeId); };
    $('seldeleterow').onclick = function () {
      if (mgr.activeId == null) return;
      mgr.remove(mgr.activeId);
    };
    $('selclearall').onclick = function () { mgr.clearAll(); };
    $('selhelp').onclick = function () { $('selhelpbox').hidden = !$('selhelpbox').hidden; };

    // ---- pick -----------------------------------------------------------
    ui.onPick = function (pk) {
      var el = $('selpick'), pi = $('selpickinfo');
      if (!pk) { el.hidden = true; return; }
      var at = null;
      if (pk.type === 'atom') at = pk.atom;
      else if (pk.type === 'bond') at = pk.closestBondAtom || (pk.bond && pk.bond.atom1);
      var A = hooks.app();
      if (!at || at.index === undefined || !A.comp) { el.hidden = true; return; }
      var st = A.comp.structure;
      if (at.index < 0 || at.index >= st.atomStore.count) { el.hidden = true; return; }
      var ss = hooks.engine().serialStore();
      var p = st.getAtomProxy(); p.index = at.index;
      ui.pick = { index: at.index, serial: ss.eff[at.index], serialParsed: ss.parsed[at.index],
                  name: String(p.atomname), resname: String(p.resname),
                  resid: p.resno, chain: String(p.chainname) };
      var q = ui.pick;
      pi.innerHTML =
        'index <b>' + q.index + '</b> · serial <b>' + q.serial + '</b>' +
        (q.serial !== q.serialParsed
          ? ' <span style="color:var(--muted)">(GRO field: ' + q.serialParsed +
            ', unwrapped)</span>' : '') +
        '<br>' + escHtml(q.name) + ' · ' + escHtml(q.resname) + ' ' + q.resid +
        ' · chain ' + escHtml(q.chain);
      el.hidden = false;
    };
    // Fill the selection INPUT only — the user then presses Add / Apply.
    // Picking must never destroy existing representations (§13).
    var fill = function (t) { text.value = t; text.focus(); };
    $('pickindex').onclick = function () { ui.pick && fill('index ' + ui.pick.index); };
    $('pickserial').onclick = function () { ui.pick && fill('serial ' + ui.pick.serial); };
    $('pickresid').onclick = function () { ui.pick && fill('resid ' + ui.pick.resid); };

    // ---- manager -> UI ---------------------------------------------------
    mgr.onChange = function (kind, rep) {
      renderRows();
      if (kind === 'active' || kind === 'add' || kind === 'remove' || kind === 'clear')
        syncControls();
      else if (kind === 'update' && rep && rep.id === mgr.activeId) renderInfo(rep);
      else if (kind === 'frame' || kind === 'view') {
        if (mgr.activeId != null) renderInfo(mgr.active());
      }
    };

    ui.renderRows = renderRows;
    ui.syncControls = syncControls;
    renderRows(); syncControls();
    return ui;
  }

  global.SelPanel = { build: buildPanel };
})(window);
