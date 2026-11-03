"""Command line (offline by default, --online = JLC's Si9000). `python -m jlc` alone opens the window.

  python -m jlc list     --layers 8 --thick 1.6 [--inner 0.5] [--free]
  python -m jlc show     JLC081611-1080G
  python -m jlc widths   JLC081611-1080G [--se 40,50] [--diff 80,90,100] [--online]
  python -m jlc z        JLC081611-1080G --layer L3 --w 5 [--s 4.2] [--refs L2,L4] [--online]
  python -m jlc rank     --layers 8 --thick 1.6,2.0 [--inner 0.5,1] [--se 40,50] [--diff 80,90,100]
  python -m jlc update                      refresh the stack database from JLCPCB (~7 min)
  python -m jlc update   --fab rezonit      Rezonit's typical builds from rezonit.ru (seconds)
  python -m jlc prices   [--layers 4,6,8,10]   only re-price the stackup surcharges (~2 min)
  python -m jlc validate [--new 200]        offline solver vs JLC's Si9000 (cached answers [+ new online])

list/rank/show/widths/z/update take --fab jlcpcb (default) or --fab rezonit: Rezonit's typical
builds (4/6/8 layers, foil 18 or 35 um = --outer 1 or 1.5, TG150/TG170).
"""
import argparse
import concurrent.futures as cf
import json
import random
import statistics
import sys
import time

from . import paths as P
from .core import design as D
from .core import stackup as S
from .core.solver import impedance


def floats(s):
    return [float(x) for x in s.split(",")] if s else []


def select(a):
    st = S.stacks(layers=[a.layers] if a.layers else None, include_hdi=a.hdi)
    th = floats(getattr(a, "thick", None))
    inn = floats(getattr(a, "inner", None))
    out = [s for s in st if (not th or s.thickness in th) and (not inn or s.inner_oz in inn)
           and s.outer_oz == getattr(a, "outer", 1.0) and not s.default_build]
    if getattr(a, "free", False):
        out = [s for s in out if s.free]
    if not getattr(a, "all", False):
        out = [s for s in out if s.order]
    return sorted(out, key=lambda s: (s.n, s.thickness, s.inner_oz, s.name))


def cost(s):
    return s.cost()


# ------------------------------------------------------------------ commands
def _progress(stage, done, total):
    if total and (done == total or done % 50 == 0):
        print(f"  {stage}: {done}/{total}", file=sys.stderr)
    elif not total:
        print(f"  {stage}...", file=sys.stderr)


def cmd_update(a):
    t0 = time.time()
    if a.fab == "rezonit":
        from .rezonit import update as R
        m = R.refresh(_progress)
        print(f"{m['templates']} typical builds saved in {time.time() - t0:.0f}s; Rezonit's own 50/100 Ohm "
              f"widths through our solver: worst {100 * m['check_max_dev']:.1f}%")
        return
    from .jlcpcb import update as U
    m = U.refresh(_progress)
    print(f"{m['templates']} orderable stackups, {m['options']} order options saved in {time.time() - t0:.0f}s")


def cmd_prices(a):
    from .jlcpcb import update as U
    m = U.reprice([int(x) for x in a.layers.split(",")], _progress)
    print(f"saved: {m['options']} order options")


def _random_case(rng):
    model = rng.choice(["OffsetStripline1B1A", "DiffOffsetStripline1B1A",
                        "CoatedMicrostrip1B", "DiffEdgeCoupledCoatedMicrostrip1B"])
    inner = "Stripline" in model
    cu = rng.choice([(0.6, 0.5), (1.2, 0.8)]) if inner else (1.6, 0.5)
    W1 = round(rng.uniform(3.0, 10.0), 2)
    p = {"H1": round(rng.uniform(2.5, 16), 3), "Er1": round(rng.uniform(3.9, 4.6), 2),
         "W1": W1, "W2": round(W1 - cu[1], 3), "T1": cu[0]}
    if inner:
        p.update(H2=round(rng.uniform(3.5, 26), 3) + cu[0], Er2=round(rng.uniform(3.9, 4.6), 2))
    else:
        p.update(C1=1.0, C2=0.6, CEr=3.8)
    if model.startswith("Diff"):
        p["S1"] = round(rng.uniform(3.5, 14), 2)
        if not inner:
            p["C3"] = 1.0
    return model, p


def cmd_validate(a):
    """Compare the offline solver with JLC's Si9000 on random geometries."""
    from .jlcpcb import cache as C
    cache = C.Cache()
    if a.new:
        rng = random.Random(len(cache.d))
        C.online([_random_case(rng) for _ in range(a.new)], cache)
    by = {}
    for k, z in cache.d.items():
        if z is None:
            continue
        model, p = C.parse_key(k)
        zl, _ = impedance(model, p, fine=a.fine)
        by.setdefault(model, []).append(100 * (zl - z) / z)
    for m, errs in sorted(by.items()):
        print(f"{m:36s} n={len(errs):4d}  mean {statistics.mean(errs):+.2f}%  "
              f"sd {statistics.pstdev(errs):.2f}%  worst {max(errs, key=abs):+.2f}%")


def cmd_list(a):
    for s in select(a):
        print(f"{s.name:18s} {s.thickness:4.2f}mm fin={s.finished or '?':<5} in={s.inner_oz:g}oz "
              f"{cost(s):4s} | {s.summary()}")


def cmd_show(a):
    s = S.find(a.name)
    print(f"{s.name}  {s.note()}\n  layers={s.n} nominal={s.thickness}mm inner={s.inner_oz:g}oz "
          f"outer={s.outer_oz:g}oz cost={cost(s)}")
    for it in s.items:
        if it["k"] == S.CU:
            print(f"  {it['name']:5s} Cu {it['t']:.2f} mil")
        else:
            print(f"        {it['k']:4s} {it['mat']:28s} {it['h']:6.2f} mil  Dk {it['er']}")
    print("geometry per signal layer (adjacent references):")
    for L in s.layer_names():
        try:
            m, g = s.geometry(L)
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        print(f"  {L:4s} {m:20s} {g}")


def solve_layer(s, L, se, df, online=False, wmin=4.0, smin=4.0):
    ms, g = s.geometry(L)
    md, gd = s.geometry(L, diff=True)
    res = {"layer": L, "geo": g, "se": {}, "diff": {}}
    for Z in se:
        res["se"][Z] = D.se_width(ms, g, Z)
    for Z in df:
        opts = []
        for W in (3.5, 4.0, 4.5, 5.0, 5.5, 6.0):
            gap = D.diff_gap(md, gd, Z, W)
            if gap is not None:
                opts.append((W, gap))
        res["diff"][Z] = opts
    if online:
        from .jlcpcb import cache as C
        jobs = [(ms, D._p(g, round(w, 2))) for Z, w in res["se"].items() if w]
        jobs += [(md, D._p(gd, w, round(sg, 2))) for opts in res["diff"].values() for w, sg in opts]
        try:
            zs = C.online(jobs)
        except OSError as e:
            print(f"  (JLC unreachable: {e.__class__.__name__}; offline values only)")
            zs = [None] * len(jobs)
        res["jlc"] = {f"{p['W1']}/{p.get('S1', '-')}": z for (m, p), z in zip(jobs, zs)}
    return res


def cmd_widths(a):
    s = S.find(a.name)
    se, df = floats(a.se), floats(a.diff)
    layers = a.layers_sel.split(",") if a.layers_sel else s.layer_names()
    print(f"{s.name}  {s.note()}  inner {s.inner_oz:g}oz  cost={cost(s)}")
    print("SE: W1 (bottom width, the CAD width). Diff: W1/S1 at the base, pitch = W+S. mil.")
    for L in layers:
        try:
            r = solve_layer(s, L, se, df, a.online)
        except (IndexError, ValueError):
            continue
        g = r["geo"]
        hdr = f"H1={g['H1']:.2f} Er1={g['Er1']}" + (f" H2={g['H2']:.2f} Er2={g['Er2']}" if "H2" in g else "")
        print(f"\n{L}  ({hdr}, T={g['T1']}, etch {g['d']})")
        for Z, w in r["se"].items():
            line = f"  SE {Z:>5g} Ohm: " + (f"W={w:.2f}" if w else "n/a")
            if a.online and w:
                line += f"   JLC@{round(w, 2)}: {r['jlc'].get(f'{round(w, 2)}/-')}"
            print(line)
        for Z, opts in r["diff"].items():
            cells = []
            for w, sg in opts:
                c = f"{w:g}/{sg:.2f}"
                if a.online:
                    c += f"({r['jlc'].get(f'{w}/{round(sg, 2)}')})"
                cells.append(c)
            print(f"  D  {Z:>5g} Ohm: " + ("  ".join(cells) if cells else "n/a"))


def cmd_z(a):
    s = S.find(a.name)
    up, down = (a.refs.split(",") + [None])[:2] if a.refs else (None, None)
    diff = a.s is not None
    m, g = s.geometry(a.layer, up, down, diff)
    p = D._p(g, a.w, a.s)
    z, ereff = impedance(m, p, 1.5)
    print(f"{s.name} {a.layer} {m} {p}\n  offline: Z={z:.2f} Ohm  Er_eff={ereff:.3f}")
    if a.online:
        from .jlcpcb import cache as C
        try:
            print(f"  JLC Si9000: Z={C.online([(m, p)])[0]}")
        except OSError as e:
            print(f"  JLC Si9000: unreachable ({e.__class__.__name__}), offline value above stands")


# ------------------------------------------------------------------ ranking
def _layer_eval(job):
    """Grid-solve one geometry (runs in a worker process)."""
    ms, md, g = job
    zse, zdf = D.tables(ms, md, g)
    return zse.tolist(), zdf.tolist()


def grade_layer(zse, zdf, se, df, a):
    import numpy as np
    zse, zdf = np.array(zse), np.array(zdf)
    out, score = {}, 0
    for Z in se:
        w = D.interp_w(zse, Z)
        gr = 0
        if w and 3.5 <= w <= a.wmax_ok:
            gr = 2 if a.wmin <= w <= a.wmax else 1
        out[f"S{Z:g}"] = (w, None, gr)
        score += gr
    for Z in df:
        opts = D.diff_options(zdf, Z)
        best = D.pick_pair(opts, a.wmin, a.smin, a.pmax)
        gr = 2
        if not best:
            best, gr = D.pick_pair(opts, 3.5, 3.5, a.pmax_ok), 1
        if not best:
            gr = 0
        out[f"D{Z:g}"] = (best[0] if best else None, best[1] if best else None, gr)
        score += gr
    return out, score


def cmd_rank(a):
    st = select(a)
    se, df = floats(a.se), floats(a.diff)
    ose, odf = floats(a.outer_se), floats(a.outer_diff)
    jobs, keys = {}, {}
    for s in st:
        for L in s.layer_names()[1:-1]:
            try:
                ms, g = s.geometry(L)
                md, _ = s.geometry(L, diff=True)
            except (IndexError, ValueError, KeyError, ZeroDivisionError):
                continue
            k = json.dumps(g, sort_keys=True)
            jobs[k] = (ms, md, g)
            keys[(s.name, s.display, L)] = k
        ms, g = s.geometry("L1")
        md, _ = s.geometry("L1", diff=True)
        k = json.dumps(g, sort_keys=True)
        jobs[k] = (ms, md, g)
        keys[(s.name, s.display, "L1")] = k
    t0 = time.time()
    print(f"{len(st)} stacks, {len(jobs)} unique geometries, solving...", file=sys.stderr)
    with cf.ProcessPoolExecutor() as ex:
        res = dict(zip(jobs, ex.map(_layer_eval, jobs.values(), chunksize=2)))
    print(f"solved in {time.time() - t0:.0f}s", file=sys.stderr)
    rows = []
    for s in st:
        inner = {}
        for L in s.layer_names()[2:-2]:
            k = keys.get((s.name, s.display, L))
            if k:
                inner[L] = grade_layer(*res[k], se, df, a)
        outer = grade_layer(*res[keys[(s.name, s.display, "L1")]], ose, odf, a)
        # best pair of inner signal layers separated by at least one plane each side
        names = list(inner)
        plans = []
        for i, x in enumerate(names):
            for y in names[i + 2:]:
                plans.append(((x, y), inner[x][1] + inner[y][1]))
        if not plans:
            continue
        classic = (names[0], names[-1])     # L3 + L(n-2): planes L2, L4..L(n-3), L(n-1)
        plan, pscore = max(plans, key=lambda p: (p[1], p[0] == classic))
        rows.append((s, plan, pscore, inner, outer))
    rows.sort(key=lambda r: (-r[2], -r[4][1], not r[0].free, r[0].inner_oz, abs(r[0].thickness - 1.6)))
    mx = 4 * (len(se) + len(df))

    def cell(v):
        w, sg, gr = v
        mark = {2: " ", 1: "~", 0: "x"}[gr]
        if w is None:
            return f"{'--':>9s}{mark}"
        return (f"{w:4.1f}/{sg:<4.1f}" if sg is not None else f"{w:9.2f}") + mark

    head = " ".join(f"{k:>10s}" for k in [f"S{z:g}" for z in se] + [f"D{z:g}" for z in df])
    ohead = " ".join(f"{k:>10s}" for k in [f"S{z:g}" for z in ose] + [f"D{z:g}" for z in odf])
    print(f"grades: ' '=comfortable (W>={a.wmin}, S>={a.smin}, W<={a.wmax}, pitch<={a.pmax}), "
          f"'~'=JLC minimum 3.5/3.5, 'x'=not feasible")
    for s, plan, pscore, inner, outer in rows[:a.top]:
        print(f"\n{s.name:18s} {s.note()}  inner {s.inner_oz:g}oz  {cost(s)}  score {pscore}/{mx}"
              f"  plan: signals {plan[0]}+{plan[1]}\n    {s.summary()}")
        print(f"    {'':5s}{head}")
        for L in plan:
            print(f"    {L:5s}" + " ".join(cell(v) for v in inner[L][0].values()))
        print(f"    {'L1':5s}{ohead}\n    {'':5s}" + " ".join(cell(v) for v in outer[0].values()))


def main():
    ap = argparse.ArgumentParser(prog="python -m jlc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    fab = argparse.ArgumentParser(add_help=False)
    fab.add_argument("--fab", choices=sorted(P.FABS), default="jlcpcb", help="stack database (factory)")
    sub.add_parser("update", parents=[fab])
    sub.add_parser("prices").add_argument("--layers", default="4,6,8,10")
    p = sub.add_parser("validate")
    p.add_argument("--new", type=int, default=0, help="add this many fresh random JLC solves first (online)")
    p.add_argument("--fine", type=float, default=1.5)
    for name in ("list", "rank"):
        p = sub.add_parser(name, parents=[fab])
        p.add_argument("--layers", type=int, default=8)
        p.add_argument("--thick", default="")
        p.add_argument("--inner", default="")
        p.add_argument("--outer", type=float, default=1.0)
        p.add_argument("--free", action="store_true", help="only stacks without surcharge")
        p.add_argument("--all", action="store_true", help="include stacks the quote form does not offer")
        p.add_argument("--hdi", action="store_true")
    r = sub.choices["rank"]
    r.add_argument("--se", default="40,50")
    r.add_argument("--diff", default="80,90,100")
    r.add_argument("--outer-se", default="50")
    r.add_argument("--outer-diff", default="90,100")
    r.add_argument("--wmin", type=float, default=4.0)
    r.add_argument("--smin", type=float, default=4.0)
    r.add_argument("--wmax", type=float, default=8.0, help="widest comfortable single-ended trace")
    r.add_argument("--wmax-ok", type=float, default=10.0)
    r.add_argument("--pmax", type=float, default=14.0, help="widest comfortable diff pitch W+S")
    r.add_argument("--pmax-ok", type=float, default=18.0)
    r.add_argument("--top", type=int, default=15)
    p = sub.add_parser("show", parents=[fab])
    p.add_argument("name")
    p = sub.add_parser("widths", parents=[fab])
    p.add_argument("name")
    p.add_argument("--layers", dest="layers_sel", default="")
    p.add_argument("--se", default="40,50")
    p.add_argument("--diff", default="80,85,90,100")
    p.add_argument("--online", action="store_true")
    p = sub.add_parser("z", parents=[fab])
    p.add_argument("name")
    p.add_argument("--layer", required=True)
    p.add_argument("--w", type=float, required=True)
    p.add_argument("--s", type=float)
    p.add_argument("--refs", default="")
    p.add_argument("--online", action="store_true")
    a = ap.parse_args()
    S.use(getattr(a, "fab", "jlcpcb"))
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
