#!/usr/bin/env python3
"""Bond-endpoint diagnostic: every rendered cylinder maps back to REAL atoms.

For a given selection/style, reads the overlay's bond buffer, maps each
cylinder's slot pair back through state.idx to GLOBAL atom indices, and
reports bond lengths (min/median/max + every offender >3 Å with atom
metadata). Runs the same audit at frame 0 and at a far frame so a frame
mapping bug shows up too.

Usage: venv/bin/python tools/bond_audit.py [--url URL] [--expr "resname SER"]
       [--style licorice] [--frame 500]
"""
import argparse
import asyncio
import json
import statistics

from playwright.async_api import async_playwright

_ap = argparse.ArgumentParser()
_ap.add_argument("--url", default="http://127.0.0.1:8877/view/example-simulation/")
_ap.add_argument("--expr", default="resname SER")
_ap.add_argument("--style", default="licorice")
_ap.add_argument("--frame", type=int, default=500)
_a = _ap.parse_args()

AUDIT = """(a) => {
  const rep = SEL.manager.reps[0];
  if (!rep || !rep.overlay.state) return {error: 'no overlay'};
  const st = window.__A.comp.structure, S = st.atomStore, pr = st.getAtomProxy();
  const idx = rep.overlay.state.idx;
  const pos = rep.overlay.state.position;
  const e = rep.overlay.state.buffers.find(x => x.kind === 'bonds');
  const out = {style: rep.style, frame: window.__A.frame, bonds: 0, lens: [], bad: []};
  if (!e) return out;
  const su = e.bonds.su, sv = e.bonds.sv, n = e.bonds.n;
  out.bonds = n;
  // authoritative current coords per GLOBAL atom: streamed atoms read the
  // atomStore (NGL rewrote them this frame); non-streamed must be checked
  // against the side-channel cache if present, else atomStore.
  const sc = window.SEL.coords;
  const scCur = sc && sc.count() ? sc.current() : null;
  const streamSet = window.__A.comp.structureView.atomSet;
  function meta(g) { pr.index = g;
    return {g, name: String(pr.atomname), res: String(pr.resname) + pr.resno,
            chain: String(pr.chainname)}; }
  for (let k = 0; k < n; k++) {
    const g1 = idx[su[k]], g2 = idx[sv[k]];
    const x1 = pos[3*su[k]], y1 = pos[3*su[k]+1], z1 = pos[3*su[k]+2];
    const x2 = pos[3*sv[k]], y2 = pos[3*sv[k]+1], z2 = pos[3*sv[k]+2];
    const d = Math.hypot(x2-x1, y2-y1, z2-z1);
    out.lens.push(+d.toFixed(2));
    if (d > 3.0) {
      let auth = null;
      // what should each endpoint be at THIS frame?
      function authCoord(g) {
        if (streamSet && !streamSet.isSet(g) && scCur && sc.ids) {
          const slot = sc.indexOf(g);
          if (slot >= 0) return [scCur[3*slot], scCur[3*slot+1], scCur[3*slot+2]];
        }
        return [S.x[g], S.y[g], S.z[g]];
      }
      const c1 = authCoord(g1), c2 = authCoord(g2);
      auth = {dAuth: +Math.hypot(c2[0]-c1[0], c2[1]-c1[1], c2[2]-c1[2]).toFixed(2)};
      out.bad.push({a1: meta(g1), a2: meta(g2), d: +d.toFixed(2), auth});
    }
  }
  // is each rendered endpoint the SAME coordinate the global atom should
  // have at this frame? (streamed -> atomStore; non-streamed -> side channel)
  let mism = 0, maxDev = 0, checked = 0;
  for (let i = 0; i < idx.length; i++) {
    const g = idx[i];
    let exp;
    if (streamSet && !streamSet.isSet(g) && scCur && sc.ids) {
      const slot = sc.indexOf(g);
      if (slot < 0) continue;
      exp = [scCur[3*slot], scCur[3*slot+1], scCur[3*slot+2]];
    } else if (streamSet && !streamSet.isSet(g)) {
      continue;   // genuinely GRO-frozen; nothing to compare against
    } else {
      exp = [S.x[g], S.y[g], S.z[g]];
    }
    const d = Math.hypot(pos[3*i]-exp[0], pos[3*i+1]-exp[1], pos[3*i+2]-exp[2]);
    checked++;
    if (d > 0.01) mism++;
    if (d > maxDev) maxDev = d;
  }
  out.coordCheck = {checked, mism, maxDev: +maxDev.toFixed(4)};
  return out;
}"""


async def audit(page, label):
    r = await page.evaluate(AUDIT)
    if "error" in r:
        print(f"[{label}] ERROR {r}")
        return
    lens = r["lens"]
    med = statistics.median(lens) if lens else 0
    print(f"[{label}] style={r['style']} frame={r['frame']} bonds={r['bonds']} "
          f"min={min(lens) if lens else '-'} median={med:.2f} "
          f"max={max(lens) if lens else '-'} >3A: {len(r['bad'])}")
    for b in r["bad"][:15]:
        print(f"    >3A: {b['a1']['g']:>7} {b['a1']['name']:<4} {b['a1']['res']:<8} "
              f"ch{b['a1']['chain']}  <->  {b['a2']['g']:>7} {b['a2']['name']:<4} "
              f"{b['a2']['res']:<8} ch{b['a2']['chain']}  d={b['d']} "
              f"(authoritative {b['auth']['dAuth'] if b['auth'] else '?'})")
    if len(r["bad"]) > 15:
        print(f"    ... and {len(r['bad'])-15} more")
    print(f"    coord mapping: {r['coordCheck']}")
    return r


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader",
                                           "--enable-unsafe-swiftshader"])
        p = await b.new_page(viewport={"width": 1100, "height": 720})
        await p.goto(_a.url, wait_until="load", timeout=90000)
        await p.wait_for_function(
            "() => window.SEL && window.SEL.manager && window.__A && window.__A.comp"
            " && window.__A.comp.structure && window.__A.comp.structure.atomStore.count>100",
            timeout=420000)
        await p.evaluate("""() => {
          SEL.manager.clearAll();
          if (window.pause) window.pause();
          const r = SEL.manager.add({expression: %s, style: %s, color: 'element'});
          window.__addErr = r.ok ? null : r.error;
        }""" % (json.dumps(_a.expr), json.dumps(_a.style)))
        await p.wait_for_timeout(2500)
        err = await p.evaluate("() => window.__addErr")
        if err:
            print(f"ADD FAILED: {err}")
            await b.close()
            return
        await audit(p, f"{_a.expr} / {_a.style} @ f0")
        await p.evaluate("(a) => requestFrame(a)", _a.frame)
        await p.wait_for_timeout(4000)
        await p.evaluate("() => window.__stage.viewer.requestRender()")
        await p.wait_for_timeout(500)
        await audit(p, f"{_a.expr} / {_a.style} @ f{_a.frame}")
        await b.close()

asyncio.run(main())
