#!/usr/bin/env python3
"""Browser-side stage profile (hang-proof: in-page loop + XHR patch + watchdog).

Per view mode, per frame, splits:
  net_ms   : XHR send->load (server extract + transfer)
  apply_ms : setFrame() -> frameChanged dispatch (network + NGL coord update)
  redraw_ms: frameChanged -> next rendered tick (representation redraw)
  rep_ms   : explicit updateRepresentations(onlyCoordUpdate) -> rendered
"""
import asyncio, statistics as st, sys
from playwright.async_api import async_playwright

URL = "http://127.0.0.1:8877/"
FRAMES = [10, 11, 12, 13, 14]

XHR_PATCH = """
() => {
  if (window.__xhrLog) return;
  window.__xhrLog = [];
  const O = XMLHttpRequest.prototype.open, S = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__u = u; this.__t0 = 0;
      return O.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (b) {
    if (String(this.__u).includes('/traj/frame')) {
      this.__t0 = performance.now();
      const n = b ? String(b).length : 0;
      this.addEventListener('load', () => window.__xhrLog.push(
        {u: String(this.__u).split('/').slice(-2).join('/'),
         ms: performance.now() - this.__t0, body: n,
         bytes: this.response ? this.response.byteLength : 0}));
    }
    return S.apply(this, arguments);
  };
}
"""

RUN = """
async (frames) => {
  const A = window.__A, stage = window.__stage;
  const rsig = (stage.signals && stage.signals.rendered)
             ? stage.signals : stage.viewer.signals;
  const out = [];
  const oneFrame = f => new Promise(res => {
    const t0 = performance.now();
    let done = false;
    const finish = (apply) => {
      if (done) return; done = true;
      A.traj.signals.frameChanged.remove(onF);
      rsig.rendered.remove(onR);
      res({f, cached: preC, apply_ms: apply, redraw_ms: performance.now() - tA});
    };
    const preC = !!A.traj.frameCache[f];
    let tA = 0;
    const onF = fr => { if (fr === f) { tA = performance.now(); } };
    const onR = () => { if (tA) finish(tA - t0); };
    A.traj.signals.frameChanged.add(onF);
    rsig.rendered.add(onR);
    A.traj.setFrame(f);
    setTimeout(() => finish(-1), 90000);        // watchdog
  });
  for (const f of frames) out.push(await oneFrame(f));
  return out;
}
"""

REDRAW = """
async () => {
  const A = window.__A, stage = window.__stage;
  const rsig = (stage.signals && stage.signals.rendered)
             ? stage.signals : stage.viewer.signals;
  return await new Promise(res => {
    const t0 = performance.now();
    const onR = () => { rsig.rendered.remove(onR); res(performance.now() - t0); };
    rsig.rendered.add(onR);
    A.comp.updateRepresentations({onlyCoordUpdate: true});
    setTimeout(() => res(-1), 60000);
  });
}
"""

def mean(v):
    v = [x for x in v if x >= 0]
    return f"{st.mean(v):7.1f}" if v else "    n/a"

async def section(page, label, frames):
    await page.evaluate("() => { window.__xhrLog.length = 0; }")
    try:
        res = await asyncio.wait_for(page.evaluate(RUN, frames), timeout=600)
    except Exception as e:
        print(f"[{label}] RUN FAILED: {type(e).__name__} {str(e)[:120]}", flush=True)
        return
    net = await page.evaluate("() => window.__xhrLog.map(x => x.ms)")
    byt = await page.evaluate("() => window.__xhrLog.map(x => x.bytes)")
    body = await page.evaluate("() => window.__xhrLog.map(x => x.body)")
    try:
        rep = await asyncio.wait_for(page.evaluate(REDRAW), timeout=120)
    except Exception:
        rep = -1
    print(f"[{label:14s}] apply(new) mean {mean([r['apply_ms'] for r in res])} ms | "
          f"apply(cached) mean {mean([r['apply_ms'] for r in res if r['cached']])} ms | "
          f"redraw mean {mean([r['redraw_ms'] for r in res])} ms | "
          f"rep-only {rep:6.1f} ms", flush=True)
    print(f"                 xhr net mean {mean(net)} ms | payload "
          f"{mean([b/1e6 for b in byt])} MB | POST body "
          f"{mean([b/1e3 for b in body])} kB | frames {len(res)}", flush=True)

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader",
                                          "--enable-unsafe-swiftshader",
                                          "--disable-gpu-sandbox", "--no-sandbox"])
        page = await b.new_page(viewport={"width": 1200, "height": 800})
        page.on("pageerror", lambda e: print("PAGEERROR", str(e)[:200], flush=True))
        await page.goto(URL, wait_until="load", timeout=60000)
        await page.evaluate(XHR_PATCH)
        await page.wait_for_function(
            "() => { const el=document.getElementById('info-pill');"
            " return el && /\\d+ frames/.test(el.textContent); }", timeout=420000)
        await page.wait_for_function("() => window.__A && window.__A.ready",
                                     timeout=60000)
        await page.wait_for_timeout(4000)
        print(f"ready; renderer=swiftshader (software)", flush=True)
        await section(page, "full", FRAMES)
        await page.click('[data-view="ca"]')
        await page.wait_for_timeout(10000)
        await section(page, "ca", FRAMES)
        await b.close()

asyncio.run(main())
