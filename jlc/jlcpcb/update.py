"""Refresh the stack database jlcpcb_stackups.zip from JLCPCB
(Settings > Update stack database, or `python -m jlc update`).

The archive holds what the quote form offers (order options with the real stackup
surcharge), the impedance templates of those stackups and JLC's copper/mask model:
  templates.json  order_options.json  configs.json  meta.json
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
THICK = [0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.4, 2.5, 2.6, 3.0, 3.2]
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
    specs = list(itertools.product(layers, THICK, [0.5, 1, 2], [1, 2]))
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
    configs = api.fetch_configs()
    priced = add_prices([o for o in opts if o["stencilLayer"] in PRICED], old["order_options"], progress)
    return write_db(templates, [o for o in opts if o["stencilLayer"] not in PRICED] + priced, configs)


def reprice(layers=PRICED, progress=None):
    """Only re-price the surcharges of these layer counts."""
    db = ST.db()
    opts = fetch_options(layers, progress)
    priced = add_prices(opts, db["order_options"], progress)
    return write_db(db["templates"], [o for o in db["order_options"] if o["stencilLayer"] not in layers] + priced,
                    db["configs"])
