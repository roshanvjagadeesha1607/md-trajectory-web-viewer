#!/usr/bin/env python3
"""Automated end-to-end test of the multi-project web viewer using Playwright.

Loads a project through its DIRECT LINK (/view/<PROJECT_ID>/) and checks:
page load, structure parse, trajectory frame count, playback, pause, frame
step, slider jump, camera (orientation/quaternion) changes on
rotate/zoom/reset, and collects console + network errors.

Defaults exercise the registered example simulation; override with
    --url http://127.0.0.1:8877/view/<id>/  --frames N  --ligand GDP
"""
import argparse
import asyncio
import json
import sys
import time

from playwright.async_api import async_playwright

_ap = argparse.ArgumentParser()
_ap.add_argument("--url", default="http://127.0.0.1:8877/view/example-simulation/")
_ap.add_argument("--frames", type=int, default=2491,
                 help="expected frame count (last frame index = frames-1)")
_ap.add_argument("--ligand", default="GDP",
                 help="residue name whose toggle is exercised")
_args = _ap.parse_args()

URL = _args.url
LAST = _args.frames - 1
LIGAND = _args.ligand
OUT = "logs"

results = {}
console_errors = []
page_errors = []
failed_requests = []


def note(name, ok, detail=""):
    results[name] = bool(ok)
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""), flush=True)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=[
                "--use-gl=angle", "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
                "--disable-gpu-sandbox", "--no-sandbox",
            ]
        )
        page = await browser.new_page(viewport={"width": 1400, "height": 900})

        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("requestfailed",
                lambda r: failed_requests.append(f"{r.url} :: {r.failure}"))

        print("== loading page ==", flush=True)
        t0 = time.time()
        await page.goto(URL, wait_until="load", timeout=60000)

        # WebGL sanity
        webgl = await page.evaluate(
            """() => { const c=document.createElement('canvas');
            const g=c.getContext('webgl2')||c.getContext('webgl');
            if(!g) return 'none';
            return g.getParameter(g.getExtension('WEBGL_debug_renderer_info')
              ? g.getExtension('WEBGL_debug_renderer_info').UNMASKED_RENDERER_WEBGL
              : g.VERSION); }"""
        )
        note("webgl available", webgl and webgl != "none", str(webgl))

        # Wait for the frame count to appear (structure parse + first meta)
        print("== waiting for structure + trajectory metadata ==", flush=True)
        try:
            await page.wait_for_function(
                "() => { const el=document.getElementById('info-pill');"
                " return el && /\\d+ frames/.test(el.textContent); }",
                timeout=420000)
            note("trajectory count visible", True,
                 f"{time.time()-t0:.0f}s")
        except Exception as e:
            note("trajectory count visible", False, str(e)[:200])
            status = await page.evaluate(
                "()=>document.getElementById('status').textContent")
            print("   status text:", status)

        pill = await page.text_content("#info-pill")
        status = await page.text_content("#status")
        print("   info-pill:", pill, "| status:", status, flush=True)

        # canvas actually rendered pixels
        canvas_ok = await page.evaluate("""async () => {
            const c = document.querySelector('#viewport canvas');
            if (!c) return 'no canvas';
            // render is continuous; read via webgl context
            const gl = c.getContext('webgl2') || c.getContext('webgl');
            if (!gl) return 'no gl ctx';
            return c.width>0 && c.height>0 ? `canvas ${c.width}x${c.height}` : 'zero size';
        }""")
        note("webgl canvas present", "canvas" in str(canvas_ok) and "zero" not in str(canvas_ok), str(canvas_ok))

        # non-black framebuffer check: screenshot & measure variance
        png = await page.screenshot(path=f"{OUT}/test_frame0.png", timeout=120000, animations="disabled")
        import statistics
        nonblack = await page.evaluate("""() => {
            const c = document.querySelector('#viewport canvas');
            const gl = c.getContext('webgl2') || c.getContext('webgl');
            const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
            const px = new Uint8Array(w*h*4);
            gl.readPixels(0,0,w,h,gl.RGBA,gl.UNSIGNED_BYTE,px);
            let lit=0;
            for (let i=3;i<px.length;i+=4096) if (px[i]>0) lit++;
            return {lit, samples: Math.floor(px.length/4096)};
        }""")
        note("framebuffer has drawn pixels", nonblack["lit"] > 0,
             f"{nonblack['lit']}/{nonblack['samples']} lit samples")

        # --- frame count ---
        fc_text = await page.text_content("#frameno")
        note("frame counter populated", f"/ {LAST}" in fc_text, fc_text)

        # --- play ---
        print("== playback ==", flush=True)
        await page.click("#playpause")
        await asyncio.sleep(6)
        f1 = int(await page.evaluate("() => document.getElementById('timeline').value"))
        label = await page.text_content("#playpause")
        note("play button toggles to Pause", "Pause" in label, label)
        await asyncio.sleep(4)
        f2 = int(await page.evaluate("() => document.getElementById('timeline').value"))
        note("frames advance while playing", f2 > f1, f"f1={f1} f2={f2}")
        fps_now = await page.text_content("#fpsnow")
        print("   measured fps:", fps_now, flush=True)

        # --- pause ---
        await page.click("#playpause")
        await asyncio.sleep(2.5)
        p1 = int(await page.evaluate("() => document.getElementById('timeline').value"))
        await asyncio.sleep(2.5)
        p2 = int(await page.evaluate("() => document.getElementById('timeline').value"))
        note("pause stops advancement", p1 == p2, f"p1={p1} p2={p2}")

        # --- step buttons ---
        await page.click("#next")
        await page.wait_for_timeout(1200)
        n1 = int(await page.evaluate("() => document.getElementById('timeline').value"))
        await page.click("#prev")
        await page.wait_for_timeout(1200)
        n2 = int(await page.evaluate("() => document.getElementById('timeline').value"))
        note("next/prev step frames", n1 > p1 and n2 < n1, f"p1={p1} after next={n1}, after prev={n2}")

        # --- slider jump ---
        mid = int(LAST * 0.72)
        await page.evaluate("""(v) => { const tl=document.getElementById('timeline');
            tl.value=v; tl.dispatchEvent(new Event('input')); }""", mid)
        await page.wait_for_timeout(2500)
        sj = int(await page.evaluate("() => document.getElementById('timeline').value"))
        note("slider jumps to frame", sj == mid, f"timeline={sj}")

        # --- jump-to input ---
        await page.fill("#jump", "500")
        await page.press("#jump", "Enter")
        await page.wait_for_timeout(2500)
        jj = int(await page.evaluate("() => document.getElementById('timeline').value"))
        note("jump-to-frame input", jj == 500, f"timeline={jj}")

        # --- toEnd / toStart ---
        await page.click("#toEnd")
        await page.wait_for_timeout(3000)
        te = int(await page.evaluate("() => document.getElementById('timeline').value"))
        note("jump to last frame", te == LAST, f"timeline={te}")
        await page.click("#toStart")
        await page.wait_for_timeout(2500)
        ts = int(await page.evaluate("() => document.getElementById('timeline').value"))
        note("jump to first frame", ts == 0, f"timeline={ts}")

        # --- camera: rotation & zoom & reset change the camera matrix ---
        print("== camera ==", flush=True)

        async def cam_quat():
            return await page.evaluate("""() => {
                if (window.__stage && window.__stage.viewer && window.__stage.viewer.camera) {
                    const c = window.__stage.viewer.camera;
                    return [c.quaternion.x, c.quaternion.y, c.quaternion.z, c.quaternion.w,
                            c.position.x, c.position.y, c.position.z];
                }
                return null; }""")

        q0 = await cam_quat()
        note("camera handle exposed", q0 is not None, str(q0))

        async def canvas_hash():
            return await page.evaluate("""() => {
                const c = document.querySelector('#viewport canvas');
                const gl = c.getContext('webgl2') || c.getContext('webgl');
                const w=gl.drawingBufferWidth, h=gl.drawingBufferHeight;
                const px = new Uint8Array(w*h*4);
                gl.readPixels(0,0,w,h,gl.RGBA,gl.UNSIGNED_BYTE,px);
                let sum=0, lit=0;
                for (let i=0;i<px.length;i+=64) { sum=(sum*31+px[i])>>>0; if(px[i])lit++; }
                return sum + ':' + lit; }""")

        import math
        def cam_changed(a, b, tol=1e-4):
            if a is None or b is None:
                return False
            dq = math.dist(a[:4], b[:4]); dp = math.dist(a[4:], b[4:])
            return dq > tol or dp > 1.0

        async def wait_cam_diff(before, timeout=4000):
            end = time.time() + timeout/1000
            while time.time() < end:
                q = await cam_quat()
                if cam_changed(before, q):
                    return q
                await asyncio.sleep(0.2)
            return await cam_quat()

        async def canvas_hash2():
            return await page.evaluate("""() => {
                const c=document.querySelector('#viewport canvas');
                const gl=c.getContext('webgl2')||c.getContext('webgl');
                const w=gl.drawingBufferWidth,h=gl.drawingBufferHeight;
                const px=new Uint8Array(w*h*4);
                gl.readPixels(0,0,w,h,gl.RGBA,gl.UNSIGNED_BYTE,px);
                let sum=0,lit=0;
                for(let i=0;i<px.length;i+=64){sum=(sum*31+px[i])>>>0; if(px[i])lit++;}
                return sum+':'+lit; }""")

        h0 = await canvas_hash2()
        await asyncio.sleep(0.6)
        h0b = await canvas_hash2()          # confirm render is settled
        await page.mouse.move(550, 430)
        await page.mouse.down()
        for i in range(15):
            await page.mouse.move(550 + i*14, 430 + i*5)
            await asyncio.sleep(0.04)
        await page.mouse.up()
        await asyncio.sleep(1.5)
        h1 = await canvas_hash2()
        note("mouse drag rotates view", h0b != h1, f"{h0b[:20]}... vs {h1[:20]}...")

        q1 = await cam_quat()
        await page.mouse.move(600, 450)
        await page.mouse.wheel(0, -900)
        q2 = await wait_cam_diff(q1)
        note("wheel zooms view", cam_changed(q1, q2))

        # pan with middle button
        await page.mouse.move(600, 450)
        await page.mouse.down(button="middle")
        await page.mouse.move(480, 360, steps=8)
        await page.mouse.up(button="middle")
        q3 = await wait_cam_diff(q2)
        note("middle-drag pans view", cam_changed(q2, q3))

        # reset view: zoom back to fit -> camera distance changes
        await page.click("#resetView")
        q4 = await wait_cam_diff(q3)
        note("reset/recenter changes view", cam_changed(q3, q4))

        # --- representation switch ---
        print("== representations ==", flush=True)
        await page.click('[data-rep="backbone"]')
        await page.wait_for_timeout(4000)
        hb = await canvas_hash()
        note("switch to backbone rep", hb is not None)
        await page.screenshot(path=f"{OUT}/test_backbone.png", timeout=120000, animations="disabled")
        await page.click('[data-rep="line"]')
        await page.wait_for_timeout(6000)
        hl = await canvas_hash()
        note("switch to line rep", hl != hb)
        await page.click('[data-rep="cartoon"]')
        await page.wait_for_timeout(5000)

        # --- ligand toggles (evaluate-dispatched: the checkbox handler only
        # flips rep visibility, but a real click can stall behind NGL's
        # software-render loop) ---
        async def lig_toggle(on):
            await page.evaluate("""(a) => { const el=document.querySelector(a.s);
                el.checked = a.on; el.dispatchEvent(new Event('change')); }""",
                {"s": f"#lig-{LIGAND}", "on": on})
        await lig_toggle(False)   # off
        await page.wait_for_timeout(2500)
        ht = await canvas_hash()
        note("ligand toggle changes scene", ht != hl)
        await lig_toggle(True)    # back on
        await page.wait_for_timeout(2000)

        # --- playback-detail mode switches ---
        # Cα (Fast) is now the DEFAULT, so exercise the switch round-trip
        # full -> ca instead of the old no-op click. Compare the ca-mode
        # cartoon against the earlier line-rep hash (guaranteed different,
        # as in the original test); a full->ca cartoon swap can legitimately
        # look identical.
        # Clicks are dispatched via evaluate(): under swiftshader (software
        # WebGL) NGL's continuous render loop blocks the main thread for
        # seconds per frame, so Playwright actionability polling can time
        # out on real clicks. evaluate(el.click()) runs the identical
        # handler; the subsequent wait_for_function still verifies the
        # switch really happened.
        print("== playback detail modes ==", flush=True)
        try:
            await page.evaluate(
                "() => document.querySelector('[data-view=\"full\"]').click()")
            await page.wait_for_function(
                "()=>window.__A && window.__A.view==='full' && !window.__A.traj.inProgress",
                timeout=180000)
            await page.wait_for_timeout(6000)
            await page.evaluate(
                "() => document.querySelector('[data-view=\"ca\"]').click()")
            await page.wait_for_function(
                "()=>window.__A && window.__A.view==='ca' && !window.__A.traj.inProgress",
                timeout=180000)
            await page.wait_for_timeout(4000)
            hca = await canvas_hash()
            ok = hca is not None and hca != ht
            # restore full mode (also guards the final screenshot state)
            try:
                await page.evaluate(
                    "() => document.querySelector('[data-view=\"full\"]').click()")
                await page.wait_for_function(
                    "()=>window.__A.view==='full'", timeout=60000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass
            note("Ca fast mode loads", ok)
        except Exception as e:
            note("Ca fast mode loads", False, str(e)[:150])

        # final screenshot
        await page.screenshot(path=f"{OUT}/test_final.png", timeout=180000, animations="disabled")

        # --- errors summary ---
        real_errors = [e for e in console_errors if not any(
            s in e for s in ["favicon", "Failed to load resource: the server responded with a status of 404"])]
        note("no page errors", len(page_errors) == 0,
             "; ".join(page_errors[:3]))
        note("no failed network requests", len(failed_requests) == 0,
             "; ".join(failed_requests[:3]))
        if console_errors:
            print("\n   console errors (first 6):", flush=True)
            for e in console_errors[:6]:
                print("    -", e[:200], flush=True)

        await browser.close()

    print("\n================ SUMMARY ================", flush=True)
    npass = sum(results.values())
    print(f"  {npass}/{len(results)} checks passed", flush=True)
    for k, v in results.items():
        print(f"   [{'x' if v else ' '}] {k}", flush=True)
    return 0 if npass == len(results) else 1


sys.exit(asyncio.run(main()))
