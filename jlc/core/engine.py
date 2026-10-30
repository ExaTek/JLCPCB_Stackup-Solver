"""Stack search: requirements -> ranked orderable JLC stackups.

A requirement is a dict:
  name   label, e.g. "DDR S40"
  kind   "se" | "diff"
  z      target ohms;  tol  allowed relative deviation (0.05 = 5 %)
  where  "inner_all" | "inner_any" | "outer" | "all" | "any"
  mode   "synth" (find the smallest W [and S]) | "fixed" (check given w [and s])
  w, s   fixed geometry for mode="fixed" (mils)

Synthesis picks the smallest manufacturable geometry that hits the target:
  SE   : W from Z; below wmin/above wmax it is clamped and accepted only if
         Z stays within tol.
  diff : W = wmin and S solved for Z; if S < smin, S = smin and W is solved
         instead; if S > smax, S = smax and accepted only within tol.
Widths and gaps are rounded to `step` and the reported Z is the one at the
rounded geometry.

Layer plan: one role per copper layer, "S" signal, "G" ground, "P" power.
A signal layer references the nearest plane above and below it.
"""
import concurrent.futures as cf
import json
import multiprocessing
import os
import sys
import threading
import time

from scipy.optimize import brentq

from .. import paths as P
from . import stackup as ST
from .solver import impedance

CACHE_PATH = os.path.join(P.CACHE, "engine_cache.json")
_lock = threading.Lock()
_cache = None
FINE = 1.0


def _load():
    global _cache
    if _cache is None:
        _cache = json.load(open(CACHE_PATH, encoding="utf-8")) if os.path.exists(CACHE_PATH) else {}
    return _cache


def save_cache():
    with _lock:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        json.dump(_load(), open(tmp, "w", encoding="utf-8"), separators=(",", ":"))
        os.replace(tmp, CACHE_PATH)


# ------------------------------------------------------------------ physics
def _params(geo, W, S=None):
    p = {k: v for k, v in geo.items() if k != "d"}
    p["W1"], p["W2"] = W, max(W - geo["d"], 0.05)
    if S is not None:
        p["S1"] = S
    return p


def zval(model, geo, W, S=None):
    return impedance(model, _params(geo, W, S), FINE)[0]


def _rnd(x, step):
    return round(round(x / step) * step, 4)


def solve_req(model, geo, req, lim):
    """One requirement on one geometry -> result dict."""
    Z0, tol = float(req["z"]), float(req.get("tol", 0.05))
    step = lim["step"]
    wmin, wmax, smin, smax = lim["wmin"], lim["wmax"], lim["smin"], lim["smax"]
    within = lambda z: abs(z - Z0) <= tol * Z0 + 1e-9
    out = {"ok": False, "W": None, "S": None, "Z": None, "note": ""}
    if req.get("mode") == "fixed":
        W = float(req["w"])
        S = float(req["s"]) if req["kind"] == "diff" else None
        z = zval(model, geo, W, S)
        out.update(W=W, S=S, Z=z, ok=within(z), note="fixed geometry")
        return out
    if req["kind"] == "se":
        f = lambda w: zval(model, geo, w) - Z0
        lo = max(geo["d"] + 0.3, 1.0)
        try:
            W = brentq(f, lo, 60.0, xtol=0.003)
        except ValueError:
            out["note"] = "target out of range"
            return out
        note = ""
        if W < wmin:
            W, note = wmin, f"needs {W:.2f} < min width"
        elif W > wmax:
            W, note = wmax, f"needs {W:.2f} > max width"
        W = _rnd(W, step)
        z = zval(model, geo, W)
        out.update(W=W, Z=z, ok=within(z), note=note)
        return out
    # differential
    W = wmin
    g = lambda s: zval(model, geo, W, s) - Z0
    note = ""
    if g(60.0) < 0:
        out["note"] = f"unreachable even with S=60 at W={wmin}"
        return out
    S = brentq(g, 0.3, 60.0, xtol=0.003) if g(0.3) < 0 else 0.3
    if S < smin:
        S = smin
        h = lambda w: zval(model, geo, w, S) - Z0
        if h(wmin) < 0:
            note = "min gap gives lower Z"
        else:
            try:
                W = brentq(h, wmin, 60.0, xtol=0.003)
            except ValueError:
                out["note"] = "no width fits"
                return out
            if W > wmax:
                W, note = wmax, f"needs W {W:.2f} > max width"
    elif S > smax:
        S, note = smax, f"needs S {S:.2f} > max gap"
    W, S = _rnd(W, step), _rnd(S, step)
    z = zval(model, geo, W, S)
    out.update(W=W, S=S, Z=z, ok=within(z), note=note)
    return out


def _key(model, geo, req, lim):
    r = {k: req.get(k) for k in ("kind", "z", "tol", "mode", "w", "s")}
    return json.dumps([model, geo, r, lim], sort_keys=True)


def _worker_init(parent_pid):
    """Worker processes exit as soon as the app that started them is gone,
    even if it crashed (Windows does not kill children with their parent)."""
    def watch():
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x00100000, False, parent_pid)      # SYNCHRONIZE
            if h:
                k32.WaitForSingleObject(h, 0xFFFFFFFF)
        else:
            while os.getppid() == parent_pid:
                time.sleep(2)
        os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def make_pool():
    # spawn everywhere: no fork of a process running Qt threads, and the same path as
    # Windows for the packaged build (multiprocessing.freeze_support() in the entry script)
    n = max(1, min(12, (os.cpu_count() or 2) - 1))
    return cf.ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"),
                                  initializer=_worker_init, initargs=(os.getpid(),))


def _worker(task):
    return [solve_req(m, g, r, lim) for (m, g, r, lim) in task]


# ------------------------------------------------------------------ plans
def refs_for(plan, k):
    """Nearest plane indices above/below copper index k (None if absent)."""
    up = next((i for i in range(k - 1, -1, -1) if plan[i] in "GP"), None)
    dn = next((i for i in range(k + 1, len(plan)) if plan[i] in "GP"), None)
    return up, dn


def signal_layers(stack, plan):
    """[(layer_name, kind 'outer'|'inner', up_name, down_name, warnings)]"""
    names = stack.layer_names()
    n = len(names)
    out = []
    for k, role in enumerate(plan):
        if role != "S":
            continue
        up, dn = refs_for(plan, k)
        outer = k in (0, n - 1)
        warn = []
        if outer:
            ref = dn if k == 0 else up
            if ref is None:
                continue
            if plan[ref] == "P":
                warn.append("outer layer references a power plane")
            if abs(ref - k) > 1:
                warn.append("reference is not the adjacent layer")
            out.append((names[k], "outer", names[up] if up is not None else None,
                        names[dn] if dn is not None else None, warn))
        else:
            if up is None or dn is None:
                out.append((names[k], "inner", None, None, ["no plane on one side - not computed"]))
                continue
            if abs(up - k) > 1 or abs(dn - k) > 1:
                warn.append("adjacent signal layer (broadside crosstalk)")
            out.append((names[k], "inner", names[up], names[dn], warn))
    return out


def dominant_ref(stack, layer, up, dn, plan):
    names = stack.layer_names()
    hu, _ = stack.between(up, layer)
    hd, _ = stack.between(layer, dn)
    ref = up if hu <= hd else dn
    return ref, plan[names.index(ref)], min(hu, hd), max(hu, hd)


# ------------------------------------------------------------------ search
DEFAULT_LIM = {"wmin": 3.5, "wmax": 10.0, "smin": 3.5, "smax": 10.0, "step": 0.05}


def candidate_stacks(layers, thicknesses=None, inner=None, outer=1.0, max_fee=None):
    out = []
    for s in ST.stacks(layers=[layers]):
        if not s.order or s.default_build:
            continue
        if thicknesses and s.thickness not in thicknesses:
            continue
        if inner and s.inner_oz not in inner:
            continue
        if s.outer_oz != outer:
            continue
        if max_fee is not None and (s.surcharge is None or s.surcharge > max_fee):
            continue
        out.append(s)
    return out


def search(spec, pool=None, progress=None):
    """spec: {layers, thicknesses, inner, outer, plan, reqs, lim, max_fee, top}"""
    lim = dict(DEFAULT_LIM, **(spec.get("lim") or {}))
    plan = spec["plan"]
    reqs = spec["reqs"]
    stacks = candidate_stacks(spec["layers"], spec.get("thicknesses"), spec.get("inner"),
                              spec.get("outer", 1.0), spec.get("max_fee"))
    if spec.get("only"):
        stacks = [s for s in stacks if s.name == spec["only"]]
    cache = _load()
    jobs = {}           # key -> (model, geo, req, lim)
    layout = []         # per stack: [(layer info, [(req_index, key)])]
    for s in stacks:
        rows = []
        for (L, kind, up, dn, warn) in signal_layers(s, plan):
            if kind == "inner" and up is None:
                rows.append({"layer": L, "kind": kind, "warn": warn, "keys": None})
                continue
            mask = spec.get("mask")
            gse = s.geometry(L, up, dn, mask=mask)
            gdf = s.geometry(L, up, dn, diff=True, mask=mask)
            keys = []
            for r in reqs:
                m, g = gdf if r["kind"] == "diff" else gse
                # a requirement may carry its own W/S range
                lr = dict(lim, **{x: float(r[x]) for x in ("wmin", "wmax", "smin", "smax") if r.get(x)})
                k = _key(m, g, r, lr)
                keys.append(k)
                if k not in cache and k not in jobs:
                    jobs[k] = (m, g, r, lr)
            dom = dominant_ref(s, L, up, dn, plan) if kind == "inner" else None
            if dom and dom[1] == "P":
                warn = warn + [f"closest reference {dom[0]} is a power plane"]
            rows.append({"layer": L, "kind": kind, "up": up, "down": dn, "warn": warn, "keys": keys,
                         "geo": gse[1], "dom": dom})
        layout.append((s, rows))
    if jobs:
        items = list(jobs.items())
        # group by geometry so a worker reuses nothing heavy; chunk evenly
        tasks = [[v] for _, v in items]
        own = pool is None
        pool = pool or make_pool()
        try:
            done = 0
            if progress:
                progress(0, len(items))
            for (k, _), res in zip(items, pool.map(_worker, tasks, chunksize=4)):
                cache[k] = res[0]
                done += 1
                if progress:
                    progress(done, len(items))
        finally:
            if own:
                pool.shutdown()
        save_cache()
    results = []
    for s, rows in layout:
        per_req = []
        fails = 0
        size = 0.0
        for i, r in enumerate(reqs):
            target = [x for x in rows if x["keys"] is not None and _applies(r["where"], x["kind"])]
            res = [(x["layer"], cache[x["keys"][i]]) for x in target]
            oks = [ok for _, v in res for ok in [v["ok"]]]
            need_all = r["where"] in ("inner_all", "outer", "all")
            sat = bool(oks) and (all(oks) if need_all else any(oks))
            fails += 0 if sat else 1
            best = [v for _, v in res if v["ok"]]
            if best:
                size += min((v["W"] or 0) + (v["S"] or 0) for v in best)
            per_req.append({"name": r["name"], "sat": sat, "layers": {L: v for L, v in res}})
        o = s.order
        results.append({
            "name": s.name, "note": s.note(), "thickness": s.thickness, "finished": s.finished,
            "inner_oz": s.inner_oz, "outer_oz": s.outer_oz, "fee": s.surcharge, "cu_fee": s.inner_cu_fee,
            "expedited": bool(o["expeditedFlag"]), "summary": s.summary(), "fails": fails,
            "size": round(size, 2), "reqs": per_req,
            "layers": [{k: v for k, v in x.items() if k != "keys"} for x in rows],
            "items": [{kk: vv for kk, vv in it.items()} for it in s.items],
        })
    pref = spec.get("thickness_pref")
    results.sort(key=lambda r: (r["fails"], (r["fee"] if r["fee"] is not None else 999) + r["cu_fee"],
                                not r["expedited"], r["size"],
                                abs(r["thickness"] - pref) if pref else 0))
    return results[: spec.get("top", 25)]


def find_stack(layers, name, thickness, inner_oz, outer_oz=1.0):
    return next(s for s in ST.stacks(layers=[layers]) if s.name == name and s.thickness == thickness
                and s.inner_oz == inner_oz and s.outer_oz == outer_oz and s.order)


def all_layers(stack, result, reqs, lim=None, mask=None):
    """Per requirement {layer: cell} for every computable signal layer: the search cell
    where it exists with a width, otherwise the smallest geometry synthesized on that
    layer (e.g. an "inner_any" or fixed requirement on TOP/BOTTOM). Used for the Altium
    impedance profiles, which need a width on every signal layer."""
    lim = dict(DEFAULT_LIM, **(lim or {}))
    cache = _load()
    out = []
    for req, rr in zip(reqs, result["reqs"]):
        cells = {}
        for row in result["layers"]:
            L = row["layer"]
            v = rr["layers"].get(L)
            if (v is None or v.get("W") is None) and "up" in row:
                q = dict(req, mode="synth", w=None, s=None)
                m, g = stack.geometry(L, row["up"], row["down"], diff=req["kind"] == "diff", mask=mask)
                lr = dict(lim, **{x: float(req[x]) for x in ("wmin", "wmax", "smin", "smax") if req.get(x)})
                k = _key(m, g, q, lr)
                if k not in cache:
                    v = solve_req(m, g, q, lr)
                    with _lock:                 # not while save_cache() is dumping
                        cache[k] = v
                v = cache[k]
            if v is not None and v.get("W") is not None:
                cells[L] = v
        out.append(cells)
    return out


def jlc_check(stack, result, reqs, mask=None):
    """JLC's own Si9000 for every computed cell: {req_name: {layer: Z}}."""
    from ..jlcpcb import cache as C
    rows = {x["layer"]: x for x in result["layers"]}
    jobs, where = [], []
    for req, rr in zip(reqs, result["reqs"]):
        diff = req["kind"] == "diff"
        for L, v in rr["layers"].items():
            if v["W"] is None:
                continue
            row = rows[L]
            m, g = stack.geometry(L, row.get("up"), row.get("down"), diff=diff, mask=mask)
            jobs.append((m, _params(g, v["W"], v["S"] if diff else None)))
            where.append((req["name"], L))
    out = {}
    for (name, L), z in zip(where, C.online(jobs)):
        out.setdefault(name, {})[L] = z
    return out


def _applies(where, kind):
    return {"inner_all": kind == "inner", "inner_any": kind == "inner", "outer": kind == "outer",
            "all": True, "any": True}[where]
