#!/usr/bin/env python3
"""Custom-FPS playback fix test (Playwright, bounded, hard timeouts).

Bug fixed: the playback interval was armed once at play(); play()
early-returns while playing, so Custom (or any) speed changes never
reached the scheduler. Fix: shared getRequestedFps() -> A.fps ->
rearmPlayTimer() keeps play/pause state and re-arms the interval.

Proofs are scheduler-level (count real interval callbacks with
A.traj.inProgress pinned) plus frame-progression spot checks — the
"actual: X fps" display stays a MEASUREMENT and is checked as such.

Usage: venv/bin/python tools/custom_fps_test.py [--url URL]
"""
import argparse
import asyncio
import json
import sys

from playwright.async_api import async_playwright

_ap = argparse.ArgumentParser()
_ap.add_argument("--url", default="http://127.0.0.1:8877/view/example-simulation/")
_a = _ap.parse_args()

R = {}


def note(name, ok, detail=""):
    R[name] = bool(ok)
    print(("  PASS  " if ok else "  FAIL  ") + name +
          (("   [" + str(detail) + "]") if detail else ""), flush=True)


# ---- page-side helpers -----------------------------------------------
INSTALL = """() => {
  window.__err = [];
  window.addEventListener('error', e => window.__err.push(String(e.message)));
  // Scheduler probe: wrap the GLOBAL step() that the interval closure
  // resolves at call time. Count firings; when __pin is set, swallow the
  // body so counts measure the pure timer period (no network dependence);
  // otherwise delegate to the real step (drives requestFrame as usual).
  if (!window.__origStep) {
    window.__origStep = step;
    window.__fires = 0; window.__pin = false;
    window.step = function () {
      window.__fires++;
      if (window.__pin) return;
      window.__origStep();
    };
  }
  window.__fires = 0; window.__pin = false;
  try { pause(); } catch (e) {}
  return {installed: true, frameCount: window.__A.frameCount};
}"""

# set speed exactly like a user: dropdown option + optional input value
SETFPS = """(o) => {
  const sel = document.getElementById('speed');
  sel.value = String(o.preset);
  sel.dispatchEvent(new Event('change'));
  if (o.val !== undefined) {
    const inp = document.getElementById('speedcustom');
    inp.value = String(o.val);
    inp.dispatchEvent(new Event('input'));
  }
  return {Afps: window.__A.fps, playing: window.__A.playing,
          ui: getRequestedFps(),
          inputVisible:
            getComputedStyle(document.getElementById('speedcustom')).display !== 'none'};
}"""

# measure interval firings over `sec` seconds; pin=true -> pure scheduler
MEASURE = """async (o) => {
  window.__pin = !!o.pin; window.__fires = 0;
  const t0 = performance.now();
  await new Promise(r => setTimeout(r, o.sec * 1000));
  const dt = (performance.now() - t0) / 1000;
  window.__pin = false;
  return {fires: window.__fires, sec: dt, rate: window.__fires / dt,
          playing: window.__A.playing, frame: window.__A.frame};
}"""

STEPCHK = """async (o) => {
  // drive N scheduler ticks deterministically through the ORIGINAL step();
  // A.frame only moves when the frame data lands, so wait for it to settle
  const seq = [];
  for (let i = 0; i < o.n; i++) {
    const prev = window.__A.frame;
    window.__origStep();
    for (let w = 0; w < 40 && window.__A.frame === prev; w++)
      await new Promise(r => setTimeout(r, 250));
    await new Promise(r => setTimeout(r, 150));
    seq.push(window.__A.frame);
  }
  return seq;
}"""

LIGCOL = """() => {
  // Component reps are proxy reps on the MAIN component, tagged
  // __mtvComp = resname (see ensureCompReps). Read per-atom colours from
  // their GPU buffers + the scheme identifier NGL actually attached.
  const byName = {};
  for (const c of CMP) byName[c.resname] = c;
  const out = {};
  A.comp.eachRepresentation(rep => {
    const rn = rep.__mtvComp;
    if (!rn) return;
    const c = byName[rn] || {};
    // proxy rep (Jp): params live on .parameters, GPU buffers on .repr.bufferList
    const bufs = (rep.repr && rep.repr.bufferList) || rep.buffers || [];
    const cols = new Set();
    const f = v => v > 1 ? v / 255 : v;   // Uint8-normalised or float attrs
    for (const b of bufs) {
      const at = b.geometry && b.geometry.attributes && b.geometry.attributes.color;
      if (!at) continue;
      for (let i = 0; i < at.count; i++)
        cols.add(f(at.array[i*3]).toFixed(2) + ',' + f(at.array[i*3+1]).toFixed(2) +
                 ',' + f(at.array[i*3+2]).toFixed(2));
    }
    out[rn] = {cat: c.category, n: cols.size, cols: [...cols].slice(0, 8),
               scheme: rep.parameters && String(rep.parameters.colorScheme || '')};
  });
  return out;
}"""

# render + wait for a specific frame to be applied
async def goto_frame(page, f, timeout_ms=25000):
    await page.evaluate(f"() => requestFrame({f})")
    await page.wait_for_function(
        f"() => window.__A.frame === {f} && window.__A.ready",
        timeout=timeout_ms)
    await page.wait_for_timeout(120)


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(
            args=["--use-gl=angle", "--use-angle=swiftshader",
                  "--enable-unsafe-swiftshader"])
        page = await b.new_page(viewport={"width": 1100, "height": 720})
        page.set_default_timeout(60000)
        console_errs = []
        page.on("pageerror", lambda e: console_errs.append(str(e)))

        await page.goto(_a.url, wait_until="load", timeout=90000)
        await page.wait_for_function(
            "() => window.__A && window.__A.ready && window.__A.comp",
            timeout=120000)
        await page.wait_for_timeout(1500)

        nfr = await page.evaluate("() => window.__A.frameCount")
        print(f"[info] frameCount={nfr}", flush=True)
        await page.evaluate(INSTALL)

        # ---------------- UI association --------------------------------
        vis = {}
        for p in ("1", "15"):
            vis[p] = (await page.evaluate(SETFPS, {"preset": p}))["inputVisible"]
        vis["custom"] = (await page.evaluate(
            SETFPS, {"preset": "custom", "val": 10}))["inputVisible"]
        note("Custom FPS input visible (hidden for presets)",
             vis["custom"] and not vis["1"] and not vis["15"], vis)
        # the field lives in the SPEED row, not a second orphan control
        same_row = await page.evaluate(
            "() => { const s = document.getElementById('speed');"
            "const i = document.getElementById('speedcustom');"
            "return s.parentElement === i.parentElement; }")
        dupes = await page.evaluate(
            "() => [...document.querySelectorAll('input[type=number]')]"
            ".filter(x => x.id === 'speedcustom').length")
        note("single number field, in the speed row", same_row and dupes == 1,
             {"sameRow": same_row, "count": dupes})

        # ---------------- presets feed the scheduler --------------------
        preset_rates = {}
        for p in (1, 2, 4, 8, 15):
            await page.evaluate(SETFPS, {"preset": p})
            await page.evaluate("() => { pause(); play(); }")
            m = await page.evaluate(MEASURE, {"sec": 2.2, "pin": True})
            await page.evaluate("() => pause()")
            want = 1000.0 / p
            got = 1000.0 / max(m["rate"], 1e-6)
            preset_rates[p] = round(got, 1)
            # measured period within 25% of 1000/preset
            if abs(got - want) > 0.25 * want:
                note(f"preset {p} fps scheduler period", False, m)
        note("FPS presets 1/2/4/8/15 control scheduler", True, preset_rates)

        # ---------------- custom values 3/6/10/20/50 --------------------
        custom_rates = {}
        ok_all = True
        for v in (3, 6, 10, 20, 50):
            r = await page.evaluate(SETFPS, {"preset": "custom", "val": v})
            if r["Afps"] != v or r["ui"] != v:
                ok_all = False; note(f"custom {v} resolves", False, r)
            await page.evaluate("() => { pause(); play(); }")
            m = await page.evaluate(MEASURE, {"sec": 2.2, "pin": True})
            await page.evaluate("() => pause()")
            got = 1000.0 / max(m["rate"], 1e-6)
            custom_rates[v] = round(got, 1)
            want = 1000.0 / v
            if abs(got - want) > 0.25 * want or not m["playing"]:
                ok_all = False
                note(f"custom {v} fps scheduler period", False, m)
        note("Custom FPS 3/6/10/20/50 control scheduler", ok_all, custom_rates)

        # frames actually advance while playing custom (unpinned)
        await page.evaluate(SETFPS, {"preset": "custom", "val": 20})
        await goto_frame(page, 10)
        await page.evaluate("() => play()")
        await page.wait_for_timeout(1600)
        adv = await page.evaluate("() => { const f = window.__A.frame;"
                                  " pause(); return f; }")
        note("frames advance with Custom 20", adv > 10, f"frame -> {adv}")

        # ---------------- § PLAY/PAUSE state matrix ----------------------
        # 1. Paused -> Custom 10 -> Play plays
        await page.evaluate("() => pause()")
        r1 = await page.evaluate(SETFPS, {"preset": "custom", "val": 10})
        paused_ok = (not r1["playing"]) and r1["Afps"] == 10
        await page.evaluate("() => play()")
        m1 = await page.evaluate(MEASURE, {"sec": 2.0, "pin": True})
        await page.evaluate("() => pause()")
        note("Paused -> Custom 10 -> Play plays",
             paused_ok and m1["playing"] and m1["fires"] >= 14,
             f"fires/s={m1['rate']:.1f}")

        # 2. Playing 15 -> Custom 10: keeps playing, new period
        await page.evaluate(SETFPS, {"preset": "15"})
        await page.evaluate("() => { pause(); play(); }")
        r2a = await page.evaluate(SETFPS, {"preset": "custom", "val": 10})
        m2 = await page.evaluate(MEASURE, {"sec": 2.2, "pin": True})
        still = await page.evaluate("() => window.__A.playing")
        await page.evaluate("() => pause()")
        got2 = 1000.0 / max(m2["rate"], 1e-6)
        note("Playing 15fps -> Custom 10 continues at 10",
             r2a["playing"] and still and r2a["Afps"] == 10
             and abs(got2 - 100) <= 25,
             f"period={got2:.0f}ms playing={still}")

        # 3. Playing Custom 10 -> 20 continues faster
        await page.evaluate("() => { pause(); play(); }")
        await page.evaluate(SETFPS, {"preset": "custom", "val": 10})
        r3 = await page.evaluate(SETFPS, {"preset": "custom", "val": 20})
        m3 = await page.evaluate(MEASURE, {"sec": 2.2, "pin": True})
        await page.evaluate("() => pause()")
        got3 = 1000.0 / max(m3["rate"], 1e-6)
        note("Playing Custom 10 -> 20 continues at 20",
             r3["playing"] and r3["Afps"] == 20 and abs(got3 - 50) <= 15,
             f"period={got3:.0f}ms")

        # 4. Playing Custom -> back to preset 4 fps
        await page.evaluate("() => { pause(); play(); }")
        r4 = await page.evaluate(SETFPS, {"preset": "custom", "val": 20})
        r4b = await page.evaluate(SETFPS, {"preset": "4"})
        m4 = await page.evaluate(MEASURE, {"sec": 2.2, "pin": True})
        await page.evaluate("() => pause()")
        got4 = 1000.0 / max(m4["rate"], 1e-6)
        note("Playing Custom -> preset 4 continues at 4",
             r4b["playing"] and r4b["Afps"] == 4
             and abs(got4 - 250) <= 60, f"period={got4:.0f}ms")

        # 5. Paused Custom 10 -> preset 4 stays paused
        await page.evaluate("() => pause()")
        await page.evaluate(SETFPS, {"preset": "custom", "val": 10})
        r5 = await page.evaluate(SETFPS, {"preset": "4"})
        await page.wait_for_timeout(500)
        still_paused = await page.evaluate(
            "() => !window.__A.playing && window.__A.timer === null")
        note("Paused Custom -> preset stays paused",
             (not r5["playing"]) and still_paused, r5)

        # ---------------- invalid values ---------------------------------
        # numeric-but-invalid CLAMP into 1–120 (field text updated to match
        # what was applied); blank / non-numeric keep the last good value.
        # The custom field RETAINS its text across preset switches — that
        # leftover is the "last good", so the running sequence matters:
        #   0 -> 1 (clamp),  -1 -> 1 (clamp),
        #   '' -> 1 (last good is case-2's clamped 1, NOT the preset 4),
        #   'abc' -> 4 (case-3 blanked the field; preset 4 was last good),
        #   999999 -> 120 (clamp)
        inv_ok = True
        details = {}
        for bad, expect in ((0, 1), (-1, 1), ("", 1), ("abc", 4),
                            (999999, 120)):
            # last good = 4 fps (preset) before each bad custom value
            await page.evaluate(SETFPS, {"preset": "4"})
            r = await page.evaluate(SETFPS, {"preset": "custom", "val": bad})
            details[str(bad)] = r["Afps"]
            if r["Afps"] != expect or not (1 <= r["Afps"] <= 120):
                inv_ok = False
            # clamped values must be reflected in the field itself
            if bad in (0, -1, 999999):
                fb = await page.evaluate("() => document.getElementById"
                                         "('speedcustom').value")
                if fb != str(expect):
                    inv_ok = False; details[str(bad) + "_field"] = fb
            # and playback must still work right after
            await page.evaluate("() => { pause(); play(); }")
            m = await page.evaluate(MEASURE, {"sec": 1.4, "pin": True})
            await page.evaluate("() => pause()")
            if not m["playing"] or m["fires"] < 1:
                inv_ok = False; details[str(bad) + "_fires"] = m["fires"]
        note("Invalid custom values keep playback alive",
             inv_ok and not console_errs,
             details if not inv_ok else f"kept-fps={details}")

        # ---------------- actual: X fps stays a measurement ---------------
        await page.evaluate(SETFPS, {"preset": "custom", "val": 50})
        await goto_frame(page, 5)
        await page.evaluate("() => { document.getElementById('fpsnow')"
                            ".textContent = '–'; play(); }")
        try:
            await page.wait_for_function(
                "() => document.getElementById('fpsnow').textContent !== '–'",
                timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        act = await page.evaluate("() => { pause();"
                                  "return document.getElementById('fpsnow')"
                                  ".textContent; }")
        actv = float(act) if act.replace('.', '', 1).isdigit() else -1
        note("actual fps is measured (not the requested 50)",
             0 < actv < 50, f"actual={act} requested=50")

        # ---------------- custom playback step regression -----------------
        await page.evaluate(SETFPS, {"preset": "4"})
        await page.evaluate("""() => {
          const s = document.getElementById('framestep');
          s.value = 'custom'; s.dispatchEvent(new Event('change'));
          const i = document.getElementById('stepcustom');
          i.value = '7'; i.dispatchEvent(new Event('input'));
        }""")
        st = await page.evaluate("() => window.__A.step")
        # loop ON, 100 -> 107 -> 114 -> 121
        await goto_frame(page, 100)
        seq = await page.evaluate(STEPCHK, {"n": 3})
        note("Custom step 7: 100->107->114->121",
             st == 7 and seq == [107, 114, 121], f"step={st} seq={seq}")
        # near end: loop ON wraps
        loop_on = await page.evaluate("() => window.__A.loop")
        await goto_frame(page, nfr - 3)
        seq2 = await page.evaluate(STEPCHK, {"n": 1})
        want2 = (nfr - 3 + 7) % nfr
        note("Custom step near end (loop ON wraps)",
             seq2 == [want2], f"from={nfr-3} -> {seq2} want [{want2}] loop={loop_on}")
        # loop OFF clamps + pauses
        if loop_on:
            await page.evaluate("() => { document.getElementById('loop')"
                                ".click(); }")
        await goto_frame(page, nfr - 3)
        seq3 = await page.evaluate(STEPCHK, {"n": 1})
        clamped = await page.evaluate(
            "() => ({f: window.__A.frame, playing: window.__A.playing})")
        note("Custom step near end (loop OFF clamps+pauses)",
             seq3 == [nfr - 1] and not clamped["playing"],
             f"{clamped}")
        if not loop_on:
            await page.evaluate("() => { document.getElementById('loop')"
                                ".click(); }")

        # slider / jump / prev / next still hit every frame (step=7 active)
        await goto_frame(page, 200)
        await page.evaluate("""() => {
          const j = document.getElementById('jump');
          j.value = '201'; j.dispatchEvent(new Event('change'));
        }""")
        await page.wait_for_function("() => window.__A.frame === 201", timeout=30000)
        nx = await page.evaluate("() => { pause(); requestFrame(window.__A.frame + 1);"
                                 " return null; }")
        await page.wait_for_function("() => window.__A.frame === 202", timeout=30000)
        await page.evaluate("""() => {
          const tl = document.getElementById('timeline');
          tl.value = '203'; tl.dispatchEvent(new Event('input'));
        }""")
        await page.wait_for_function("() => window.__A.frame === 203", timeout=30000)
        await page.evaluate("() => { pause(); requestFrame(window.__A.frame - 1); }")
        await page.wait_for_function("() => window.__A.frame === 202", timeout=30000)
        note("jump/next/slider/prev exact with step=7", True,
             "201,202,203,202 hit individually")
        # restore step preset
        await page.evaluate("""() => {
          const s = document.getElementById('framestep');
          s.value = '1'; s.dispatchEvent(new Event('change'));
        }""")

        # ---------------- ligand element colours + independence -----------
        cats = await page.evaluate("""() => {
          const by = {};
          for (const [n, c] of Object.entries(CMP))
            (by[c.category] = by[c.category] || []).push(n);
          return by;
        }""")
        print("[info] components:", json.dumps(
            {k: v for k, v in cats.items()}), flush=True)

        lig0 = await page.evaluate(LIGCOL)
        ligs = {k: v for k, v in lig0.items()
                if v["cat"] in ("ligand", "other")}
        # C green (0.20,0.80,0.20) present, N/O/P distinct
        GREEN = "0.20,0.80,0.20"
        ok_col = True
        for k, v in ligs.items():
            if v["n"] < 2:
                ok_col = False
            if v["scheme"] and "mtv-element" not in v["scheme"]:
                ok_col = False
        # carbon must be green in every carbon-containing ligand
        green_ligs = {k for k, v in ligs.items() if GREEN in v["cols"]}
        note("Ligand element colours (multi-colour, element scheme)",
             ok_col and len(ligs) > 0 and len(green_ligs) == len(ligs),
             {k: (v["n"], v["cols"][:3]) for k, v in ligs.items()})

        # protein colour change must not recolour ligands
        for scheme in ("chainindex", "resname", "uniform", "chainid"):
            try:
                await page.evaluate(
                    "(s) => { const c = document.getElementById('colorScheme');"
                    " c.value = s; c.dispatchEvent(new Event('change')); }",
                    scheme)
                await page.wait_for_timeout(1200)
            except Exception:
                pass
        lig1 = await page.evaluate(LIGCOL)
        same = all(lig1.get(k, {}).get("cols") == v["cols"]
                   for k, v in ligs.items())
        note("Ligand colours independent of protein colour mode", same,
             {k: lig1.get(k, {}).get("cols", [])[:2] for k in ligs})

        # ions keep their previous colouring path (server c.color)
        ioncols = {k: v for k, v in lig1.items() if v["cat"] == "ion"}
        note("Ions untouched (single server colour)",
             all(v["n"] <= 1 for v in ioncols.values()) or not ioncols,
             {k: v["cols"][:2] for k, v in ioncols.items()})

        # ---------------- regression: overlays / Surface / Gaussian -------
        await page.goto(_a.url, wait_until="load", timeout=90000)
        await page.wait_for_function(
            "() => window.__A && window.__A.ready && window.__A.comp",
            timeout=120000)
        await page.wait_for_timeout(1200)

        overlay_ok = await page.evaluate("""async () => {
          const r = SEL.manager.add({expression: 'resname SER',
                                     style: 'licorice', color: 'green'});
          if (!r.ok) return {ok: false, err: r.error};
          const rep = SEL.manager.reps[0];
          const ov = rep && rep.overlay;
          // wait for the first consistent-paint (the gate may hold the
          // overlay hidden until exact-frame coords land)
          for (let i = 0; i < 40 && !(ov && ov.comp && rep.overlay.state
                                      && ov.state._everPainted); i++)
            await new Promise(x => setTimeout(x, 250));
          if (!ov || !ov.comp) return {ok: false, err: 'no overlay comp'};
          const rend = window.__stage.viewer.renderer;
          ov.comp.setVisibility(false);
          window.__stage.viewer.requestRender();
          await new Promise(x => setTimeout(x, 500));
          const hid = rend.info.render.triangles;
          ov.comp.setVisibility(true);
          window.__stage.viewer.requestRender();
          await new Promise(x => setTimeout(x, 500));
          const shown = rend.info.render.triangles;
          return {ok: shown - hid > 100, delta: shown - hid,
                  atoms: rep.atomCount, painted: !!ov.state._everPainted};
        }""")
        note("Selection overlay still renders (regression)",
             overlay_ok.get("ok"), overlay_ok)

        reps_ok = await page.evaluate("""async () => {
          const out = {};
          for (const r of ['cartoon', 'backbone', 'line']) {
            const s = document.getElementById('rep' );
            // rep switch goes through the rep buttons
            const btn = [...document.querySelectorAll('[data-rep]')]
              .find(b => b.dataset.rep === r);
            if (!btn) { out[r] = 'no-button'; continue; }
            btn.click();
            await new Promise(x => setTimeout(x, 1500));
            out[r] = window.__A.rep === r;
          }
          return out;
        }""")
        note("Cartoon/Backbone/Line reps still switch",
             all(v is True for v in reps_ok.values()), reps_ok)

        for rep in ("surface", "gsurf"):
            sw = await page.evaluate(
                """async (r) => {
              const btn = [...document.querySelectorAll('[data-rep]')]
                .find(b => b.dataset.rep === r);
              if (!btn) return 'no-button';
              btn.click();
              await new Promise(x => setTimeout(x, r === 'gsurf' ? 12000 : 8000));
              return {rep: window.__A.rep,
                      vis: document.querySelector('#viewport canvas') !== null};
            }""", rep)
            note(f"{rep} still loads (unchanged, no crash)",
                 isinstance(sw, dict) and sw["rep"] == rep, sw)

        note("No page errors during suite", not console_errs,
             console_errs[:3])

        await b.close()

    print("\n==== SUMMARY ====")
    print(json.dumps(R, indent=2))
    sys.exit(0 if all(R.values()) else 1)


asyncio.run(main())
