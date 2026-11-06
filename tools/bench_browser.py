#!/usr/bin/env python3
"""Browser-side profile: NGL coordinate update vs redraw vs network wait.

Measures, per view mode, for a handful of frames:
  net_ms        : /traj/frame request duration seen by the browser (server+net)
  bytes         : decoded body size
  apply_ms      : frameChanged -> next rendered tick (coord update + redraw)
  cached_ms     : same, for a frame already in NGL's frameCache (no network)
  rep_ms        : explicit updateRepresentations() -> rendered (redraw only)
Uses the app's own UI so it profiles the real code path.
"""
import asyncio, json, statistics as st, sys
from playwright.async_api import async_playwright

URL = "http://127.0.0.1:8877/"
N = 6

PROF = """
async (frames) => {
  const A = window.__A || A;
  const stage = window.__stage;
  const out = [];
  for (const f of frames) {
    const cached = !!A.traj.frameCache[f];
    const e = await new Promise(res => {
      let t0 = 0, done = false;
      const onFrame = fr => { if (fr !== f) return; t0 = performance.now(); go(); };
      const onRender = () => { if (!done) { done = true; fin(performance.now()); } };
      const go = () => stage.signals.rendered.add(onRender);
      const fin = t => {
        A.traj.signals.frameChanged.remove(onFrame);
        stage.signals.rendered.remove(onRender);
        res({f, cached, ms: t - t0});
      };
      A.traj.signals.frameChanged.add(onFrame);
      A.traj.setFrame(f);
    });
    out.push(e);
  }
  return out;
}
"""

REP = """
async () => {
  const A = window.__A, stage = window.__stage;
  const t = await new Promise(res => {
    const t0 = performance.now();
    const onRender = () => { stage.signals.rendered.remove(onRender); res(performance.now()-t0); };
    stage.signals.rendered.add(onRender);
    A.comp.updateRepresentations({onlyCoordUpdate: true});
  });
  return t;
}
"""

NETSTAT = """
() => {
  const e = performance.getEntriesByType('resource')
    .filter(r => r.name.includes('/traj/frame'))
    .slice(-1)[0];
  return e ? {dur: e.duration, dec: e.decodedBodySize, enc: e.encodedBodySize,
              wait: e.responseStart - e.requestStart} : null;
}
"""

SETUP = """
() => { performance.setResourceTimingBufferSize(5000);
        performance.clearResourceTimings(); }
"""


async def mode(page, label, frames):
    await page.evaluate(SETUP)
    res = await page.evaluate(PROF, frames)
    net, cached, fresh = [], [], []
    bytes_ = 0
    for r in res:
        ns = await page.evaluate(NETSTAT)
        if ns:
            bytes_ = ns["dec"]
            (cached if r["cached"] else net).append(ns["dur"])
        (cached if r["cached"] else fresh).append(r["ms"])
    rep = await page.evaluate(REP)
    def m(v):
        return f"{st.mean(v):7.1f}" if v else "    n/a"
    print(f"[{label:12s}] payload {bytes_/1e6:6.2f} MB | "
          f"net mean {m(net)} ms | coord+redraw(new) mean {m(fresh)} ms | "
          f"coord+redraw(cached) mean {m(cached)} ms | "
          f"rep-only-update {rep:6.1f} ms", flush=True)
    fps = [1000/(a+b) for a, b in zip(net or [0], fresh or [0]) if (a or b)]
    if fps:
        print(f"               sequential fps ceiling ~{st.mean(fps):.2f}",
              flush=True)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader",
                                          "--enable-unsafe-swiftshader",
                                          "--disable-gpu-sandbox", "--no-sandbox"])
        page = await b.new_page(viewport={"width": 1200, "height": 800})
        page.on("pageerror", lambda e: print("PAGEERROR", e))
        await page.goto(URL, wait_until="load", timeout=60000)
        await page.wait_for_function(
            "() => { const el=document.getElementById('info-pill');"
            " return el && /\\d+ frames/.test(el.textContent); }",
            timeout=420000)
        await page.wait_for_function("() => A && A.ready && A.frameCount > 0",
                                     timeout=60000)
        await page.wait_for_timeout(3000)
        print(f"renderer: swiftshader (software WebGL; real GPU faster)", flush=True)
        fr = list(range(10, 10 + N))
        await mode(page, "full", fr)
        await page.click('[data-view="ca"]')
        await page.wait_for_timeout(8000)
        await mode(page, "ca", fr)
        # timings summary
        print("\nstructure parse time (page):", flush=True)
        print(await page.evaluate(
            "()=>performance.getEntriesByType('resource')"
               ".filter(r=>r.name.includes('.gro'))"
               ".map(r=>({ms:Math.round(r.duration),mb:+(r.decodedBodySize/1e6).toFixed(1)}))"),
            flush=True)
        await b.close()

asyncio.run(main())
