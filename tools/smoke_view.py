#!/usr/bin/env python3
"""Viewer page checks against one project.

    ./venv/bin/python tools/smoke_view.py [PROJECT_ID] [RES_A RES_B ...]

Verifies the project API populated the UI, component checkboxes were built
dynamically (including non-standard residue names), the Cα selection is a
complement (protein atoms survive even when every component is hidden),
hidden components leave the streamed selection, and Full mode streams all
atoms. Also drives one frame in each mode without JS errors.
"""
import asyncio
import json
import sys
import urllib.request

from playwright.async_api import async_playwright

PID = sys.argv[1] if len(sys.argv) > 1 else "synthetic-xyz-test"
# Optional explicit list of non-polymer resnames that must appear as
# components. With no args the expectation is recomputed from the project's
# own API metadata, so the tool is project-agnostic.
EXPECT_RES = sys.argv[2:] or None
URL = f"http://127.0.0.1:8877/view/{PID}/"
ok = True


def note(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"   [{detail}]" if detail else ""), flush=True)


def cbid(res):
    return "#lig-" + res.replace(" ", "%20")


async def main():
    meta = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:8877/view/{PID}/api/project", timeout=10))
    natoms = meta["atoms"]
    global EXPECT_RES
    if EXPECT_RES is None:
        # every non-polymer component (protein/nucleic are rendered as the
        # cartoon, not as checkboxes) must surface in the component panel
        EXPECT_RES = [c["resname"] for c in meta.get("components") or []
                      if c.get("category") not in ("protein", "nucleic")]

    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        page = await b.new_page(viewport={"width": 1280, "height": 800})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        await page.goto(URL, wait_until="load", timeout=60000)
        try:
            await page.wait_for_function(
                "() => window.__A && window.__A.comp", timeout=240000)
        except Exception:
            st = await page.evaluate(
                "document.getElementById('status').textContent")
            note("structure loads", False, st)
            await b.close()
            return 1
        note("structure loads", True)

        # --- dynamic component panel --------------------------------
        ids = await page.evaluate(
            "() => CMP.map(c => c.resname)")
        for rn in EXPECT_RES:
            note(f"component {rn!r} auto-discovered", rn in ids, str(ids))
        boxes = await page.evaluate(
            "() => [...document.querySelectorAll('#comproll input[type=checkbox]')]"
            ".map(e => e.id)")
        note("checkbox per non-polymer component", len(boxes) >= len(EXPECT_RES),
             str(boxes))
        vis = await page.evaluate(
            "() => CMP.filter(c => !c.polymer)"
            ".map(c => c.resname + '=' + c.el.checked).join(',')")
        vmap = dict(kv.split("=") for kv in vis.split(",") if "=" in kv)
        checks_ok = True
        why = []
        for rn in ("XYZ", "MG"):        # small ligands/ions: expect visible
            if rn in vmap:
                checks_ok &= vmap[rn] == "true"; why.append(f"{rn}={vmap[rn]}")
        for rn in ("K", "SOL"):         # bulk salt + water: expect hidden
            if rn in vmap:
                checks_ok &= vmap[rn] == "false"; why.append(f"{rn}={vmap[rn]}")
        note("default visibility matches the spec", checks_ok, vis)

        # --- streamed selection in Cα mode ---------------------------
        def eval_sel(expr):
            return page.evaluate(
                """expr => { try { return window.__A.comp.structure
                        .getAtomIndices(new NGL.Selection(expr)).length;
                      } catch (e) { return 'ERR ' + e.message; } }""", expr)

        sel_now = await page.evaluate("window.__curSel")
        n_all = await eval_sel("")
        note("Cα selection is a complement, not bare .CA",
             "not [" in (sel_now or ""), sel_now)
        n_ca = await eval_sel(".CA")
        note("protein Cα atoms present even with all comps ticked",
             n_ca > 0, f"{n_ca}")

        # uncheck everything, then wait for the debounced re-issue to land.
        # On 700k-atom systems under software WebGL the setSelection rebuild
        # can take tens of seconds, so POLL __curSel instead of sleeping.
        async def wait_sel_change(before, timeout=120000):
            end = asyncio.get_event_loop().time() + timeout / 1000
            while asyncio.get_event_loop().time() < end:
                cur = await page.evaluate("window.__curSel")
                if cur != before:
                    return cur
                await page.wait_for_timeout(500)
            return await page.evaluate("window.__curSel")

        await page.evaluate("""() => {
          CMP.filter(c => !c.polymer).forEach(c => {
            c.el.checked = false; c.el.dispatchEvent(new Event('change'));
          });
        }""")
        sel_off = await wait_sel_change(sel_now)
        n_off = await eval_sel(sel_off)
        note("all components hidden: protein Cα still stream",
             n_off > 0, f"{n_off} atoms streamed")
        # The NOT-subtractor legitimately names every component (so they
        # cannot leak into the protein term); what must be gone is the
        # *union* term — nothing may be added back after ' and <atomSel>'.
        union = (sel_off or "").split(" or ")
        note("hidden components are not added back to the stream",
             len(union) == 1, sel_off)

        # re-check MG only
        await page.evaluate("""() => {
          const c = CMP.find(x => x.resname === 'MG');
          c.el.checked = true; c.el.dispatchEvent(new Event('change'));
        }""")
        sel_mg = await wait_sel_change(sel_off)
        note("re-showing MG adds it to the streamed selection",
             sel_mg and sel_mg.endswith("or [MG]"), sel_mg)

        # --- component reps exist (non-polymer only) -------------------
        rep_ok = await page.evaluate("""() => {
          let names = [];
          window.__A.comp.eachRepresentation(r => {
            if (r.__mtvComp) names.push(r.__mtvComp);
          });
          return JSON.stringify(names);
        }""")
        rep_names = json.loads(rep_ok)
        expect_reps = await page.evaluate(
            "() => CMP.filter(c => !c.polymer).map(c => c.resname)")
        note("own rep for every non-polymer component",
             all(r in rep_names for r in expect_reps),
             f"reps={rep_names}")

        # pathological component (if this project has one): non-empty
        # selection via @-indices
        ab_sel = await page.evaluate("""() => {
          const c = CMP.find(x => !x.polymer && !/^[A-Za-z0-9][A-Za-z0-9+/@.\\-]{0,4}$/.test(x.resname));
          return c ? compSele(c) + '||' + c.resname : null;
        }""")
        if ab_sel is None:
            note("pathological-name handling (none in this project)", True)
        else:
            sel_txt, rn = ab_sel.split("||")
            note(f"pathological name {rn!r} uses @-index selector",
                 sel_txt.startswith("@"), sel_txt[:60])
            n_ab = await eval_sel(sel_txt)
            n_expect = await page.evaluate(
                "r => (CMP.find(c => c.resname === r) || {}).atoms || -1", rn)
            note(f"{rn} atoms selected by index ({n_expect} expected)",
                 n_ab == n_expect, str(n_ab))

        # --- frames reachable in both modes ---------------------------
        for view in ("ca", "full"):
            await page.evaluate(
                "v => document.querySelector('[data-view=' + v + ']').click()",
                view)
            await page.wait_for_timeout(4000)
            await page.evaluate("""() => { const tl =
                document.getElementById('timeline');
                tl.value = 1; tl.dispatchEvent(new Event('input')); }""")
            await page.wait_for_timeout(4000)
            f = await page.evaluate(
                "() => document.getElementById('timeline').value")
            note(f"frame jump works in {view} mode", f == "1", f"frame={f}")
        sel_full = await page.evaluate("window.__curSel")
        note("Full mode selection transfers every atom",
             sel_full == "", repr(sel_full))
        n_full = await eval_sel("")
        note("full structure holds every atom", n_full == natoms,
             f"{n_full}/{natoms}")

        # --- protein complement excludes components, keeps protein -----
        # Build the subtractor exactly like the app does: grammar-safe
        # names only. Protein atoms (incl. non-standard ones) must survive.
        info = await page.evaluate("""() => {
          const safe = /^[A-Za-z0-9][A-Za-z0-9+/@.\\-]{0,4}$/;
          const np = CMP.filter(c => !c.polymer);
          const sub = np.filter(c => safe.test(c.resname)).map(c => c.resname);
          const subAtoms = np.filter(c => safe.test(c.resname))
                             .reduce((s, c) => s + c.atoms, 0);
          const patho = np.filter(c => !safe.test(c.resname)).map(c => c.resname);
          return JSON.stringify({sel: 'not [' + sub.join(',') + ']',
                                 expect: natomsPlaceholder0, subAtoms, patho});
        }""".replace("natomsPlaceholder0", str(natoms)))
        info = json.loads(info)
        n_notpoly = await eval_sel(info["sel"])
        # Full mode keeps every atom in the structure, so the complement
        # should equal total minus the subtracted component atoms (within a
        # couple of atoms for shared altlocs — compare exactly).
        note("complement excludes components, keeps everything else",
             n_notpoly == info["expect"] - info["subAtoms"],
             f"{n_notpoly} vs {info['expect'] - info['subAtoms']}")
        if info["patho"]:
            note("pathological names excluded from bracket subtractor",
                 all(p not in info["sel"] for p in info["patho"]),
                 str(info["patho"]))
        else:
            note("pathological names excluded from bracket subtractor "
                 "(none in this project)", True)

        note("no JS errors", not errs, "; ".join(errs[:3]))
        await page.screenshot(path=f"logs/smoke_{PID}.png")
        await b.close()
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
