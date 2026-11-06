#!/usr/bin/env python
"""Bounded headless probe: prove selection overlays render REAL WebGL geometry.

Evidence hierarchy (never 'row exists'):
  1. renderer.info.render.calls / triangles from the live WebGLRenderer
     (__stage.viewer.renderer) — geometry present in the GL scene or not.
  2. PNG screenshot diffs — pixels actually changed.
  3. Component mounted in __stage.compList; per-buffer attribute rows.
"""
import sys, time, json, argparse
from playwright.sync_api import sync_playwright

_ap = argparse.ArgumentParser()
_ap.add_argument("--url", default="http://127.0.0.1:8877/view/example-simulation/")
_ap.add_argument("--budget", type=int, default=220)
A = _ap.parse_args()

RESULTS, ERRS = {}, []

def rec(k, v):
    RESULTS[k] = v
    print("PROBE %-34s %s" % (k, json.dumps(v)[:500]), flush=True)

def js(page, expr):
    return page.evaluate(expr)

MEASURE = """(function(){
  __stage.viewer.render();
  var r=__stage.viewer.renderer, i=r.info.render;
  return {calls:i.calls, tris:i.triangles};
})()"""

def measure(page):
    page.wait_for_timeout(500)
    return js(page, MEASURE)

def pngdiff(a, b):
    return sum(1 for x, y in zip(a, b) if x != y)

def inspect(page, rid):
    """Direct per-overlay inspection through real NGL object paths."""
    return js(page, """(function(){
      var rep=SEL.manager.get(%d); if(!rep) return {err:'missing'};
      var o=rep.overlay, out={id:%d, expr:rep.expression, style:rep.style, atoms:rep.atomCount};
      out.mounted = (function(){ var f=false;
        Object.keys(__stage.compList).forEach(function(k){ if(__stage.compList[k]===o.comp) f=true; });
        return f; })();
      out.inCompList = Object.keys(__stage.compList).length;
      out.vis = o.comp ? o.comp.reprList[0].repr.visible : null;
      out.bufs = o.state ? o.state.buffers.map(function(b){
        var g=b.buf.geometry, p=g.attributes.position;
        return {kind:b.kind, srcAtoms:b.buf.position? Math.floor(b.buf.position.length/3): null,
                glRows: p.array.length/3};
      }) : null;
      try{ out.bb = o.comp.reprList[0].repr.getBoundingBox().toArray().map(function(v){return +v.toFixed(1);}); }
      catch(e){ out.bb='repr-bbox-absent'; }
      return out;
    })()""" % (rid, rid))

def add_rep(page, expr, style, color="uniform", dynamic=False):
    r = js(page, "(function(){ return SEL.manager.add({expression:%s, style:%s, color:%s, dynamic:%s}); })()"
           % (json.dumps(expr), json.dumps(style), json.dumps(color), "true" if dynamic else "false"))
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    return {"ok": True, "id": r["rep"]["id"]}

def main():
    t0 = time.time()
    def deadline(tag=""):
        if time.time() - t0 > A.budget:
            raise TimeoutError("budget " + tag)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
            "--disable-gpu-sandbox", "--no-sandbox"])
        page = browser.new_page(viewport={"width": 1000, "height": 700})
        page.set_default_timeout(60000)
        page.on("pageerror", lambda e: ERRS.append(str(e)[:160]))
        page.on("console", lambda m: ERRS.append("c:" + m.text[:160]) if m.type == "error" else None)

        page.goto(A.url, wait_until="load")
        page.wait_for_function(
            "typeof A!=='undefined'&&A.comp&&A.comp.structure&&A.comp.structure.atomStore.count>0&&SEL.manager&&A.frame>=0",
            timeout=90000)
        page.wait_for_timeout(1500)
        js(page, "(function(){ if(typeof pause==='function') pause(); __stage.autoView(); })()")
        page.wait_for_timeout(800)
        rec("load", js(page, "({atoms:A.comp.structure.atomStore.count, bonds:A.comp.structure.bondStore.count, view:A.view, frame:A.frame, frames:A.frameCount})"))

        base_m = measure(page); base_png = page.screenshot(type="png")
        base_tris = base_m["tris"]
        rec("cartoon_only", base_m)

        # ---------- 1. SER spacefill ----------
        a = add_rep(page, "resname SER", "spacefill"); rec("SER_spacefill_add", a)
        m1 = measure(page); png1 = page.screenshot(type="png")
        rec("SER_spacefill_render", {"tri_delta": m1["tris"] - base_tris, "calls_delta": m1["calls"] - base_m["calls"],
                                     "png_diff": pngdiff(base_png, png1), **inspect(page, a["id"])})
        deadline("sf")

        # ---------- 2. SER licorice ----------
        js(page, "SEL.manager.update(%d,{style:'licorice'})" % a["id"])
        m2 = measure(page); png2 = page.screenshot(type="png")
        rec("SER_licorice_render", {"tri_delta": m2["tris"] - base_tris, "calls_delta": m2["calls"] - base_m["calls"],
                                    "png_diff_vs_spacefill": pngdiff(png1, png2), "png_diff_vs_cartoon": pngdiff(base_png, png2),
                                    **inspect(page, a["id"])})
        deadline("lic")

        # ---------- 3. SER ball+stick ----------
        js(page, "SEL.manager.update(%d,{style:'ball+stick'})" % a["id"])
        m3 = measure(page); png3 = page.screenshot(type="png")
        rec("SER_ballstick_render", {"tri_delta": m3["tris"] - base_tris,
                                     "png_diff_vs_licorice": pngdiff(png2, png3), **inspect(page, a["id"])})

        # ---------- 4. SER line ----------
        js(page, "SEL.manager.update(%d,{style:'line'})" % a["id"])
        m4 = measure(page); png4 = page.screenshot(type="png")
        rec("SER_line_render", {"tri_delta": m4["tris"] - base_tris,
                                "png_diff_vs_ballstick": pngdiff(png3, png4), **inspect(page, a["id"])})
        js(page, "SEL.manager.update(%d,{style:'spacefill'})" % a["id"])
        deadline("bs/ln")

        # ---------- 5. three overlays simultaneously ----------
        b1 = add_rep(page, "resid 122", "licorice"); rec("resid122_add", b1)
        c1 = add_rep(page, "resname GTP GDP", "ball+stick"); rec("GTPGDP_add", c1)
        m5 = measure(page); png5 = page.screenshot(type="png")
        rec("multi_render", {"tri_delta": m5["tris"] - base_tris, "calls_delta": m5["calls"] - base_m["calls"],
                             "png_diff_vs_cartoon": pngdiff(base_png, png5),
                             "rows": js(page, "SEL.manager.summary()"),
                             "mounted": js(page, "(function(){var n=0;Object.keys(__stage.compList).forEach(function(k){});return Object.keys(__stage.compList).length;})()")})
        rec("resid122_geom", inspect(page, b1["id"]))
        rec("GTPGDP_geom", inspect(page, c1["id"]))
        deadline("multi")

        # ---------- hide / show SER ----------
        js(page, "SEL.manager.setVisible(%d,false)" % a["id"])
        mh = measure(page); pngh = page.screenshot(type="png")
        js(page, "SEL.manager.setVisible(%d,true)" % a["id"])
        ms = measure(page); pngs = page.screenshot(type="png")
        rec("hide_show", {"tris_multi": m5["tris"], "tris_hidden": mh["tris"], "tris_shown": ms["tris"],
                          "png_diff_hidden": pngdiff(png5, pngh), "png_diff_shown_vs_multi": pngdiff(png5, pngs)})

        # ---------- style change GTP/GDP -> spacefill ----------
        p_before = page.screenshot(type="png"); t_before = measure(page)["tris"]
        js(page, "SEL.manager.update(%d,{style:'spacefill'})" % c1["id"])
        p_after = page.screenshot(type="png"); t_after = measure(page)["tris"]
        rec("gtp_style_change", {"tri_before": t_before, "tri_after": t_after, "png_diff": pngdiff(p_before, p_after)})
        js(page, "SEL.manager.update(%d,{style:'ball+stick'})" % c1["id"])

        # ---------- delete resid 122 ----------
        p_before = page.screenshot(type="png"); t_before = measure(page)["tris"]
        js(page, "SEL.manager.remove(%d)" % b1["id"])
        p_after = page.screenshot(type="png"); t_after = measure(page)["tris"]
        rec("delete_resid122", {"tri_before": t_before, "tri_after": t_after, "png_diff": pngdiff(p_before, p_after),
                                "rows_left": js(page, "SEL.manager.reps.map(function(r){return r.id;})"),
                                "comps_left": js(page, "Object.keys(__stage.compList).length")})
        deadline("edit-ops")

        # ---------- trajectory: static selection follows frame ----------
        # Choose the SER atom with the LARGEST frame0→frame500 source displacement
        # (a mid-index atom can be a rigid-core atom with ~0 motion — that is
        # physics, not a bug), then check overlay buf + GPU rows follow it.
        traj = js(page, """(function(){ return new Promise(function(res){
          var o=SEL.manager.reps.filter(function(r){return r.expression==='resname SER';})[0].overlay;
          var st=A.comp.structure.atomStore, idx=o.state.idx;
          var target=Math.min(500, A.frameCount-1);
          // snapshot current (frame 0) source coords
          var n=idx.length, p0=new Float32Array(n*3);
          for(var i=0;i<n;i++){ p0[3*i]=st.x[idx[i]]; p0[3*i+1]=st.y[idx[i]]; p0[3*i+2]=st.z[idx[i]]; }
          var geom=o.state.buffers[0].buf.geometry;
          var bufBefore=o.state.position.slice();
          var glBefore=Float32Array.from(geom.attributes.position.array);
          requestFrame(target);
          var tries=0;
          var iv=setInterval(function(){ tries++;
            if(A.frame===target || tries>150){ clearInterval(iv);
              setTimeout(function(){
                // pick atom with max source displacement, compare buf + GL rows
                var best=0, bestD=-1;
                for(var i=0;i<n;i++){
                  var dx=st.x[idx[i]]-p0[3*i], dy=st.y[idx[i]]-p0[3*i+1], dz=st.z[idx[i]]-p0[3*i+2];
                  var d=dx*dx+dy*dy+dz*dz; if(d>bestD){bestD=d; best=i;} }
                function d3(arr,i){ return [arr[3*i],arr[3*i+1],arr[3*i+2]]; }
                var g=geom.attributes.position.array;
                // impostor geometry may be 4 rows/atom (quad) — check both interpretations
                var glRowSame = (glBefore[12*best]===g[12*best] && glBefore[12*best+1]===g[12*best+1]
                              && glBefore[12*best+2]===g[12*best+2] && glBefore[12*best+3]===g[12*best+3]);
                var maxSrcDisp=Math.sqrt(bestD).toFixed(3);
                var movedBuf=0, movedSrc=0, staleBuf=0;
                for(var i=0;i<n;i++){
                  var sv=bufBefore[3*i]!==o.state.position[3*i]||bufBefore[3*i+1]!==o.state.position[3*i+1]||bufBefore[3*i+2]!==o.state.position[3*i+2];
                  var sm=p0[3*i]!==st.x[idx[i]]||p0[3*i+1]!==st.y[idx[i]]||p0[3*i+2]!==st.z[idx[i]];
                  if(sm) movedSrc++;
                  if(sv) movedBuf++;
                  if(sm && !sv) staleBuf++;
                }
                res({probeAtom:idx[best], maxSrcDisp:maxSrcDisp, frames:[0,A.frame],
                     atomsWithMovedSrc:movedSrc, atomsWithMovedBuf:movedBuf, staleBufCount:staleBuf,
                     bufMatchesSrc: d3(o.state.position,best).map(function(v){return +v.toFixed(3);}),
                     srcNow: [ +st.x[idx[best]].toFixed(3), +st.y[idx[best]].toFixed(3), +st.z[idx[best]].toFixed(3) ],
                     glRowsChangedAtProbe: !glRowSame, glRows: g.length/3, refreshes:o.refreshes});
              }, 300);
            }
          }, 150);
        }); })()""")
        rec("trajectory_static", traj)
        deadline("traj")

        # ---------- dynamic selection ----------
        dyn = js(page, """(function(){ return new Promise(function(res){
          SEL.manager.add({expression:'within 6 of resname GTP', style:'licorice', color:'uniform', dynamic:true});
          var id=SEL.manager.reps[SEL.manager.reps.length-1].id;
          var c0=SEL.manager.get(id).atomCount;
          requestFrame(1);
          var tries=0; var iv=setInterval(function(){ tries++;
            if(A.frame===1||tries>100){ clearInterval(iv);
              setTimeout(function(){ var rep=SEL.manager.get(id);
                var r={ok:true, atoms0:c0, atomsNow:rep.atomCount, dynamic:rep.dynamic,
                       refreshes:rep.overlay.refreshes, recomputedAtFrame:rep.lastDyn, curFrame:A.frame};
                SEL.manager.remove(id); res(r);
              }, 300);
            }
          }, 150);
        }); })()""")
        rec("dynamic", dyn)
        deadline("dyn")

        # ---------- Ca mode: non-CA selected atoms move ----------
        camode = js(page, """(function(){ return new Promise(function(res){
          var st=A.comp.structure, s=st.atomStore, proxy=st.getAtomProxy();
          var o=SEL.manager.reps.filter(function(r){return r.expression==='resname SER';})[0].overlay;
          var slot=-1, at=-1;
          for(var i=0;i<o.state.idx.length;i++){ var a=o.state.idx[i]; proxy.index=a;
            if(String(proxy.atomname)!=='CA'){ slot=i; at=a; break; } }
          if(at<0){ res({skip:'no non-CA SER atom'}); return; }
          function buf(){ return [+o.state.position[3*slot].toFixed(3),+o.state.position[3*slot+1].toFixed(3),+o.state.position[3*slot+2].toFixed(3)]; }
          function src(){ return [+s.x[at].toFixed(3),+s.y[at].toFixed(3),+s.z[at].toFixed(3)]; }
          var t0={buf:buf(), src:src(), view:A.view, f:A.frame};
          var target = A.frame>100 ? 5 : Math.min(400, A.frameCount-1);
          requestFrame(target);
          var tries=0; var iv=setInterval(function(){ tries++;
            if(A.frame===target||tries>120){ clearInterval(iv);
              setTimeout(function(){
                var t1={buf:buf(), src:src(), f:A.frame};
                res({atom:at, name:String((proxy.index=at, proxy.atomname)), view:t0.view,
                     frames:[t0.f,t1.f], before:t0, after:t1,
                     srcMoved:t0.src.join()!==t1.src.join(),
                     bufMoved:t0.buf.join()!==t1.buf.join(),
                     bufMatchesSrcAtEnd:t1.buf.join()===t1.src.join()});
              }, 300);
            }
          }, 150);
        }); })()""")
        rec("camode_non_ca", camode)
        deadline("camode")

        rec("js_errors", ERRS[:8])
        browser.close()
    print("\n=== DONE %.0fs ===" % (time.time() - t0))

try:
    main()
except TimeoutError as e:
    print("TIMEOUT:", e, flush=True)
    sys.exit(2)
finally:
    print("\n=== RESULTS ===")
    print(json.dumps(RESULTS, indent=1))
