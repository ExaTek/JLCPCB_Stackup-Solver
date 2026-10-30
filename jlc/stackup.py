"""JLC stackup templates -> Si9000 geometry, reproducing the rules of JLC's
impedance calculator page (computedBaseMaterialThickness & co., checked
against the page's own function on 2026-10-30):

* outer layer: H1 = everything between the layer and its reference,
  T1/etch/mask from the outer-copper config (base 0.5 oz entry preferred).
* inner layer on a core: H1 = dielectric through its own core to the reference
  on that side, H2 = dielectric to the other reference + the layer's copper.
* Er = plain average of the dielectric layers in each span.
* W2 = W1 - traceWidthDelta, T1 = traceCopperThickness (config tables).
"""
import gzip
import json
import os
import re

from .paths import DATA

MIL = 1 / 0.0254

CU, PP, CORE = "Cu", "PP", "Core"
_MAT = {"铜箔": CU, "PP": PP, "芯板": CORE, "Copper": CU, "Prepreg": PP, "Core": CORE}
_NOTES = {"通用免费": "standard, free", "通用": "standard", "特殊结构": "special structure",
          "特殊": "special", "普通": "regular", "成品板厚": "finished", "板厚": "thickness",
          "成品厚度": "finished", "无要求": "no requirement (default build)",
          "内层铜厚不一致": "mixed inner copper"}


def translate(s):
    s = s or ""
    for k in sorted(_NOTES, key=len, reverse=True):
        s = s.replace(k, _NOTES[k])
    return s.replace("（", "(").replace("）", ")")


_TEMPLATES, _CONFIGS = None, None


def load_templates():
    global _TEMPLATES
    if _TEMPLATES is None:
        with gzip.open(os.path.join(DATA, "templates.json.gz"), "rt", encoding="utf-8") as f:
            _TEMPLATES = json.load(f)
    return _TEMPLATES


def load_configs():
    global _CONFIGS
    if _CONFIGS is None:
        _CONFIGS = json.load(open(os.path.join(DATA, "configs.json"), encoding="utf-8"))
    return _CONFIGS


_ORDER = None


def order_options():
    """{(name, layers, thickness, inner_oz, outer_oz): option} from the quote form
    (data/order_options.json, see api.fetch_order_options)."""
    global _ORDER
    if _ORDER is None:
        path = os.path.join(DATA, "order_options.json")
        opts = json.load(open(path, encoding="utf-8"))["options"] if os.path.exists(path) else []
        _ORDER = {(o["templateName"], o["stencilLayer"], float(o["stencilPly"]), float(o["insideCuprumThickness"]),
                   float(o["cuprumThickness"])): o for o in opts if o["showName"] != "No requirement"}
    return _ORDER


def prune(templates, options):
    """Keep only templates the quote form offers (one per name/specs)."""
    keys = {(o["templateName"], o["stencilLayer"], float(o["stencilPly"]), float(o["insideCuprumThickness"]),
             float(o["cuprumThickness"])) for o in options if o["showName"] != "No requirement"}
    out, seen = [], set()
    for t in templates:
        k = (t["appointName"], t["plateLayerNumber"], float(t["plateThickness"]), float(t["innerCopperThickness"]),
             float(t.get("cuprumThickness") or 1))
        if k in keys and k not in seen and not (t.get("receptionDisplayName") or "").startswith("无要求"):
            seen.add(k)
            out.append(t)
    return out


def _pick(rows, oz, layer_type):
    rows = [r for r in rows if abs(r["systemCopperThickness"] - oz) < 0.01 and r["layerType"] == layer_type]
    if not rows:
        return None
    return next((r for r in rows if abs(r["baseCopperThickness"] - 0.5) < 0.01), rows[0])


class Stack:
    def __init__(self, t, cfg):
        self.t = t
        self.name = t["appointName"]
        self.display = t.get("receptionDisplayName") or self.name
        self.n = t["plateLayerNumber"]
        self.thickness = float(t["plateThickness"])
        self.inner_oz = float(t["innerCopperThickness"])
        self.outer_oz = float(t.get("cuprumThickness") or 1)
        self.paid = bool(t.get("chargeFlag"))
        self.hdi = self.name.startswith("JLCH")
        self.custom = "自定义" in self.name
        self.default_build = (t.get("receptionDisplayName") or "").startswith("无要求")
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*±", self.display)
        self.finished = float(m.group(1)) if m else None
        self.cfg = cfg
        self.items = []
        for y in t["layers"]:
            kind = _MAT.get(y["material"], y["material"])
            if kind == CU:
                self.items.append({"k": CU, "name": y["layerName"] or f"L{self.n}",
                                   "t": float(y["topConductorThick"]) * MIL, "core": None})
            elif kind == PP:
                self.items.append({"k": PP, "h": float(y["dielectricThick"]) * MIL,
                                   "er": float(y["dielectricConstant"]), "mat": (y["materialName"] or "").split()[0]})
            elif kind == CORE:
                a, b = (y["layerName"] or "/").split("/")
                self.items.append({"k": CU, "name": a, "t": float(y["topConductorThick"]) * MIL, "core": "upper"})
                self.items.append({"k": CORE, "h": float(y["dielectricThick"]) * MIL,
                                   "er": float(y["dielectricConstant"]), "mat": y["materialName"]})
                self.items.append({"k": CU, "name": b, "t": float(y["botConductorThick"]) * MIL, "core": "lower"})
        # top foil has no name in some templates
        cus = [i for i in self.items if i["k"] == CU]
        if cus and not cus[0]["name"]:
            cus[0]["name"] = "L1"

    # ------------------------------------------------------------ helpers
    @property
    def order(self):
        """Quote-form entry (fixedFee, coefficient, expeditedFlag) or None if not orderable."""
        return order_options().get((self.name, self.n, self.thickness, self.inner_oz, self.outer_oz))

    @property
    def surcharge(self):
        """Real stackup surcharge in USD from JLC's price API (5 pcs 100x100 mm, see
        update_prices.py). fixedFee/coefficient of the template list do NOT reflect it."""
        o = self.order
        if not o:
            return None
        return o.get("surcharge_usd") if o.get("surcharge_usd") is not None else None

    @property
    def inner_cu_fee(self):
        o = self.order
        return (o or {}).get("inner_cu_usd") or 0.0

    @property
    def free(self):
        return self.surcharge == 0

    def cost(self):
        o = self.order
        if not o:
            return "not orderable"
        sc = self.surcharge
        fee = "price unknown (run update_prices.py)" if sc is None else (
            "no stackup surcharge" if sc == 0 else f"stackup +${sc:.2f}")
        if self.inner_cu_fee:
            fee += f", inner Cu +${self.inner_cu_fee:.2f}"
        return fee + (", expedited ok" if o["expeditedFlag"] else "")

    def note(self):
        return translate(self.display)

    def _idx(self, layer):
        for i, it in enumerate(self.items):
            if it["k"] == CU and it["name"] == layer:
                return i
        raise KeyError(f"{self.name}: no layer {layer}")

    def between(self, a, b):
        i, j = sorted((self._idx(a), self._idx(b)))
        seg = self.items[i + 1:j]
        diel = [x for x in seg if x["k"] != CU]
        h = sum(x["h"] for x in diel) + sum(x["t"] for x in seg if x["k"] == CU)
        return h, sum(x["er"] for x in diel) / len(diel)

    def copper(self, layer):
        return self.items[self._idx(layer)]

    def layer_names(self):
        return [i["name"] for i in self.items if i["k"] == CU]

    def summary(self):
        out = []
        for it in self.items:
            if it["k"] == PP:
                out.append(f"{it['mat']}:{it['h']:.2f}")
            elif it["k"] == CORE:
                out.append(f"[core {it['h']:.2f}]")
        return " ".join(out)

    # ------------------------------------------------------------ geometry
    def mask(self):
        """JLC solder mask model for the outer copper: (C1 over substrate, C2 over trace, Er)."""
        cv = _pick(self.cfg["coverlay"], self.outer_oz, 1)
        return cv["coatingAboveSubstrate"], cv["coatingAboveTrace"], 3.8

    def geometry(self, layer, up=None, down=None, diff=False, mask=None):
        """(model, params) for a trace on `layer` between reference planes
        `up`/`down` (defaults: adjacent layers; outer layers use one).
        mask=(C1, C2, Er) overrides JLC's solder mask model."""
        names = self.layer_names()
        k = names.index(layer)
        outer = k in (0, len(names) - 1)
        if outer:
            ref = (down or names[1]) if k == 0 else (up or names[-2])
            h, er = self.between(layer, ref)
            ct = _pick(self.cfg["copper_trace_width"], self.outer_oz, 1)
            cv = _pick(self.cfg["coverlay"], self.outer_oz, 1)
            p = {"H1": h, "Er1": er, "T1": ct["traceCopperThickness"], "d": ct["traceWidthDelta"],
                 "C1": cv["coatingAboveSubstrate"], "C2": cv["coatingAboveTrace"], "CEr": 3.8}
            if diff:
                p["C3"] = cv["coatingBetweenTraces"]
            if mask:
                p.update(C1=float(mask[0]), C2=float(mask[1]), CEr=float(mask[2]))
                if diff:
                    p["C3"] = float(mask[0])
            model = "DiffEdgeCoupledCoatedMicrostrip1B" if diff else "CoatedMicrostrip1B"
        else:
            up = up or names[k - 1]
            down = down or names[k + 1]
            ct = _pick(self.cfg["copper_trace_width"], self.inner_oz, 2)
            own = self.copper(layer)["t"]
            hu, eu = self.between(up, layer)
            hd, ed = self.between(layer, down)
            if self.copper(layer)["core"] == "upper":     # core below the layer
                p = {"H1": hd, "Er1": ed, "H2": hu + own, "Er2": eu}
            else:                                         # core above (or foil)
                p = {"H1": hu, "Er1": eu, "H2": hd + own, "Er2": ed}
            p.update(T1=ct["traceCopperThickness"], d=ct["traceWidthDelta"])
            model = "DiffOffsetStripline1B1A" if diff else "OffsetStripline1B1A"
        return model, {k2: round(v, 4) if isinstance(v, float) else v for k2, v in p.items()}


def stacks(templates=None, cfg=None, layers=None, include_hdi=False, include_custom=False):
    templates = templates or load_templates()["templates"]
    cfg = cfg or load_configs()
    out, seen = [], set()
    for t in templates:
        if layers and t["plateLayerNumber"] not in layers:
            continue
        try:
            s = Stack(t, cfg)
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            continue
        if (s.hdi and not include_hdi) or (s.custom and not include_custom):
            continue
        sig = (s.name, s.display, s.summary(), s.thickness)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(s)
    return out


def find(name, **kw):
    hits = [s for s in stacks(**kw, include_hdi=True, include_custom=True) if s.name == name and not s.default_build]
    hits = hits or [s for s in stacks(**kw, include_hdi=True, include_custom=True) if s.name == name]
    if not hits:
        raise KeyError(name)
    return hits[0]
