"""Refresh the stack database jlcpcb_stackups.zip from JLCPCB
(Settings > Update stack database, or `python -m jlc update`).

The archive holds what the quote form offers (order options with the real stackup
surcharge), the impedance templates of those stackups and JLC's parameters, which JLC can change:
  templates.json     layers of every stack with Dk and thicknesses
  order_options.json what can be ordered, surcharges
  configs.json       copper_trace_width: T1, trace etch W1 - W2; coverlay: mask of the calculator;
                     pictures: Si9000 models and their default Er; layer_thickness; default_values;
                     published: what jlcpcb.com/impedance states (minimum W/S, mask, Dk tables)
  meta.json          date, counts, model_changes: what differs from the previous database
A refresh takes several minutes (~1500 requests). The old database stays in place unless
JLC answered (nearly) everything.
"""
import concurrent.futures as cf
import itertools
import json
import os
import time
import zipfile

from .. import paths
from ..core import stackup as ST
from . import api

LAYERS = list(range(4, 22, 2))
THICK = [0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.4, 2.5, 2.6, 3.0, 3.2]     # plus what JLC lists now
PRICED = [4, 6, 8, 10]                  # layer counts the app offers: their surcharges are priced
PRICE_REF = "5 pcs 100x100 mm"


def _retry(fn, *args):
    for i in range(3):
        try:
            return fn(*args)
        except OSError:
            time.sleep(1 + i)
    return None


def _key(o):
    return (o["templateName"], o["stencilLayer"], float(o["stencilPly"]), float(o["insideCuprumThickness"]),
            float(o["cuprumThickness"]))


def _parallel(fn, items, stage, progress):
    out = []
    with cf.ThreadPoolExecutor(8) as ex:
        for i, r in enumerate(ex.map(fn, items), 1):
            out.append(r)
            if progress:
                progress(stage, i, len(items))
    return out


def fetch_options(layers, progress=None):
    """Quote-form options for every layers x thickness x copper combination."""
    thick = set(THICK)
    try:
        offered = api.fetch_layer_thickness()
        thick.update(x for n in layers for x in offered.get(n, ()))
    except (OSError, ValueError, KeyError):
        pass
    specs = list(itertools.product(layers, sorted(thick), [0.5, 1, 2], [1, 2]))
    res = _parallel(lambda sp: _retry(api.fetch_order_options, *sp), specs, "options", progress)
    failed = sum(r is None for r in res)
    if failed > 0.05 * len(specs):
        raise RuntimeError(f"JLC answered {len(specs) - failed} of {len(specs)} order-option requests")
    return [o for lst in res for o in (lst or [])]


def add_prices(opts, old=(), progress=None):
    """Real stackup surcharge from JLC's quote form (orderCountTolls.stackupMoney); the
    fixedFee/coefficient fields of the option list do not show it. Reference board 5 pcs
    100 x 100 mm: other sizes cost differently, the ranking holds. A failed request keeps
    the previous price of that option."""
    before = {_key(o): o for o in old}

    def price(o):
        p = _retry(api.fetch_price, o["stencilLayer"], float(o["stencilPly"]), float(o["insideCuprumThickness"]),
                   float(o["cuprumThickness"]), o["impedanceTemplateCode"] if o["showName"] != "No requirement" else None)
        prev = before.get(_key(o), {})
        o["surcharge_usd"] = p["stackup"] if p else prev.get("surcharge_usd")
        o["inner_cu_usd"] = p["inner_cu"] if p else prev.get("inner_cu_usd")
        o["price_ref"] = PRICE_REF
        return o
    return _parallel(price, opts, "prices", progress)


def write_db(templates, options, configs, path=None, **meta):
    """Write the archive next to the old one and swap it in."""
    path = path or paths.data(paths.DB)
    meta = dict({"updated": time.strftime("%Y-%m-%d %H:%M")}, **meta)
    meta.update(templates=len(templates), options=len(options), price_ref=PRICE_REF, source="jlcpcb.com")
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, obj in (("templates", templates), ("order_options", options), ("configs", configs),
                          ("meta", meta)):
            z.writestr(name + ".json", json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    os.replace(tmp, path)
    ST.reload()
    return meta


def _model_rows(cfg):
    """The JLC values the calculation depends on, flat: {name: value}."""
    out = {}
    for r in cfg.get("copper_trace_width", ()):
        k = f"copper {r['systemCopperThickness']:g} oz layerType {r['layerType']} base {r['baseCopperThickness']:g}"
        out[k + " T1"], out[k + " etch"] = r["traceCopperThickness"], r["traceWidthDelta"]
    for r in cfg.get("coverlay", ()):
        k = f"coverlay {r['systemCopperThickness']:g} oz layerType {r['layerType']} base {r['baseCopperThickness']:g}"
        out[k + " C1"], out[k + " C2"], out[k + " C3"] = (r["coatingAboveSubstrate"], r["coatingAboveTrace"],
                                                          r["coatingBetweenTraces"])
    pub = cfg.get("published") or {}
    for k, v in pub.items():
        for a, b in (v.items() if isinstance(v, dict) else [("", v)]):
            out[f"published {k} {a}".strip()] = b
    return out


def model_changes(old, new):
    """['name: old -> new', ...] between two configs (values the old database had)."""
    a, b = _model_rows(old), _model_rows(new)
    return [f"{k}: {a[k]} -> {b.get(k)}" for k in sorted(a) if a[k] != b.get(k)]


def fetch_all_configs(old):
    """fetch_configs() plus the published page. A page that fails or changed its shape must not
    stop the update: its previous values stay."""
    configs = api.fetch_configs()
    try:
        configs["published"] = api.fetch_published()
    except (OSError, ValueError) as e:
        configs["published"] = (old or {}).get("published")
        configs["published_error"] = str(e)
    return configs


def refresh(progress=None):
    """Download everything; progress(stage, done, total) with stage options / templates /
    configs / prices. Returns the new meta."""
    old = ST.db()
    opts = fetch_options(LAYERS, progress)
    if progress:
        progress("templates", 0, 0)
    templates = ST.prune(api.fetch_templates(log=lambda s: None), opts)    # keep only what can be ordered
    if len(templates) < 0.5 * len(old["templates"]):
        raise RuntimeError(f"only {len(templates)} stackups from JLC (database has {len(old['templates'])})")
    if progress:
        progress("configs", 0, 0)
    configs = fetch_all_configs(old["configs"])
    priced = add_prices([o for o in opts if o["stencilLayer"] in PRICED], old["order_options"], progress)
    return write_db(templates, [o for o in opts if o["stencilLayer"] not in PRICED] + priced, configs,
                    model_changes=model_changes(old["configs"], configs),
                    published_error=configs.get("published_error"))


def reprice(layers=PRICED, progress=None):
    """Only re-price the surcharges of these layer counts."""
    db = ST.db()
    opts = fetch_options(layers, progress)
    priced = add_prices(opts, db["order_options"], progress)
    return write_db(db["templates"], [o for o in db["order_options"] if o["stencilLayer"] not in layers] + priced,
                    db["configs"])
