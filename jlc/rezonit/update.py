"""Rezonit's typical multilayer stackups -> rezonit_stackups.zip, in the format of
jlcpcb_stackups.zip (templates / order_options / configs / meta), so the search, the
exports and the command line work on them unchanged (`python -m jlc list --fab rezonit`).

Read from rezonit.ru (two pages, a few seconds; `python -m jlc update --fab rezonit`):
  STACKS  typical builds: 4/6/8 layers, MSO (through vias only), foil 18 or 35 um, FR-4
          TG150 or TG170, required thickness 1.0/1.5/2.0 mm: foils, prepreg styles and
          thicknesses, cores, actual thickness; and Rezonit's 50/100 Ohm design table
  TECH    production capabilities: Dk of every core (per Tg and thickness) and prepreg
          style (1 MHz values), minimum trace and gap per foil
Rezonit does not publish plated outer copper, etch and solder mask; COPPER and MASK take
JLC's model for the same base foil. With them the solver reproduces Rezonit's own
50/100 Ohm table within 1.5 % (check()).
"""
import html
import json
import os
import re
import time
import urllib.request
import zipfile

from .. import paths
from ..core import stackup as ST

STACKS = ("https://www.rezonit.ru/directory/v-pomoshch-konstruktoru/proektirovanie-elementov-konstrukcii-"
          "pechatnoj-platy-proizvodstva-v-rezonit/tipovye-sborki-i-konstruktsii-pechatnykh-plat/")
TECH = ("https://www.rezonit.ru/directory/tekhnologicheskie-osobennosti-proizvodstva/"
        "?active_tab=capabilities&type[]=urgent")
MIL = 1 / 0.0254
OZ = {18: (0.5, 1.0), 35: (1.0, 1.5)}       # base foil um -> (inner oz, plated outer oz)
# (layerType 1 outer / 2 inner, system oz, base oz, trace thickness mil, width delta mil)
COPPER = [(1, 1.0, 0.5, 1.6, 0.5), (1, 1.5, 1.0, 2.2, 1.0),
          (2, 0.5, 0.5, round(0.018 * MIL, 3), 0.5), (2, 1.0, 1.0, round(0.035 * MIL, 3), 0.8)]
MASK = (1.0, 0.6, 1.0)                      # mils over substrate / over trace / between traces
CLASSES = ["base", "advanced", "limit"]     # Rezonit: базовый / продвинутый (x1.5) / предельный (on request)


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _text(s):
    s = re.sub(r"<(style|script)\b.*?</\1>", " ", s, flags=re.S)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", s).strip()


def _num(s):
    m = re.search(r"\d+(?:[.,]\d+)?", s)
    return float(m.group(0).replace(",", ".")) if m else None


def _attr(a, name):
    m = re.search(name + r'="(\d+)"', a)
    return int(m.group(1)) if m else 1


def _rows(table):
    """HTML table -> [[cell texts]] as written (no spans: the capabilities page's rowspans
    run past their groups)."""
    return [[_text(c.group(2)) for c in re.finditer(r"<t([dh])\b[^>]*>(.*?)</t\1>", tr.group(0), re.S)]
            for tr in re.finditer(r"<tr\b.*?</tr>", table, re.S)]


def _grid(table):
    """HTML table -> [(in_thead, [cell texts])] with rowspan/colspan spread out."""
    out, spans = [], {}
    head_end = table.find("</thead>")
    for tr in re.finditer(r"<tr\b.*?</tr>", table, re.S):
        row = []

        def fill():
            while len(row) in spans:
                text, left = spans[len(row)]
                if left == 1:
                    del spans[len(row)]
                else:
                    spans[len(row)] = (text, left - 1)
                row.append(text)

        for c in re.finditer(r"<t([dh])\b([^>]*)>(.*?)</t\1>", tr.group(0), re.S):
            fill()
            text, rs = _text(c.group(3)), _attr(c.group(2), "rowspan")
            for _ in range(_attr(c.group(2), "colspan")):
                if rs > 1:
                    spans[len(row)] = (text, rs - 1)
                row.append(text)
        fill()
        out.append((tr.start() < head_end, row))
    return out


def _tgs(s):
    m = re.search(r"TG\s*(\d+)(?:\s*/\s*(\d+))?", s, re.I)
    return {int(x) for x in m.groups() if x} if m else set()


def _styles(s):
    """'1 x 2116' / '2 x 2116' / '2116 + 7628' -> glass styles, one per ply."""
    out = []
    for part in s.split("+"):
        m = re.fullmatch(r"\s*(?:(\d)\s*[xх×]\s*)?(\d{3,4})\s*", part)
        if not m:
            raise ValueError(f"prepreg {s!r}")
        out += [m.group(2)] * int(m.group(1) or 1)
    return out


# ------------------------------------------------------------------ pages
HEAD = re.compile(r"(\d+)-\S*\s+слойн\S*\s+(?:печатн\S*\s+)?плата\s+МСО\s+фольга\s+(\d+)\s*мкм(?:\s*TG\s*(\d+))?",
                  re.I)


def parse_stacks(page):
    """Typical builds page -> ([stack], [reference row]); a stack is
    {n, foil, tg, thickness, actual, seq: [("Cu", mm) | ("PP", styles, [mm]) | ("Core", mm)]}."""
    stacks, refs, last = [], [], 0
    for m in re.finditer(r'<table class="table table-prototype.*?</table>', page, re.S):
        before, last = _text(page[last:m.start()]), m.end()
        grid = _grid(m.group(0))
        heads = HEAD.findall(before)
        if "Полосок" in before[-200:]:
            refs += _parse_refs(grid)
            continue
        if not heads:
            continue
        n, foil, tg = int(heads[-1][0]), int(heads[-1][1]), heads[-1][2]
        top = [r for h, r in grid if h][-1]
        cols = [i for i, c in enumerate(top) if re.fullmatch(r"\d+(?:[.,]\d+)?", c)]
        thick = [_num(top[i]) for i in cols]
        seq, actual, tgs = [], {}, {int(tg)} if tg else set()
        for _, row in ((h, r) for h, r in grid if not h):
            label, sub = row[0], (row[1] if cols[0] > 1 else "")
            vals = [row[i] if i < len(row) else "" for i in cols]
            own = _tgs(label + " " + sub)
            word = label.split()[0].lower() if label else ""
            if word.startswith("фольга"):
                seq.append({"k": "Cu", "t": [_num(v) for v in vals]})
            elif word.startswith("препрег"):
                if not seq or seq[-1].get("key") != label:
                    seq.append({"k": "PP", "key": label, "styles": None, "h": {}})
                if re.search(r"\d[.,]\d", " ".join(vals)):
                    for t in own or [None]:
                        seq[-1]["h"][t] = vals
                        tgs.add(t) if t else None
                else:
                    seq[-1]["styles"] = vals
            elif word.startswith("ядро"):
                seq.append({"k": "Core", "h": [_num(v) for v in vals]})
            elif word.startswith("фактическая"):
                for t in own or [None]:
                    actual[t] = [_num(v) for v in vals]
        for t in sorted(tgs):
            for j, T in enumerate(thick):
                out, ok = [], True
                for e in seq:
                    if e["k"] == "Cu":
                        out.append(("Cu", e["t"][j]))
                    elif e["k"] == "Core":
                        out.append(("Core", e["h"][j]))
                    else:
                        hs = (e["h"].get(t) or e["h"].get(None) or [""] * len(thick))[j]
                        hs = [float(x.replace(",", ".")) for x in re.findall(r"\d+[.,]\d+", hs)]
                        if not hs or not e["styles"][j]:
                            ok = False
                            break
                        st = _styles(e["styles"][j])
                        if len(st) != len(hs):
                            raise ValueError(f"{n} layers {foil} um TG{t} {T} mm: {e['styles'][j]} vs {hs}")
                        out.append(("PP", st, hs))
                act = (actual.get(t) or actual.get(None) or [None] * len(thick))[j]
                if ok and act and None not in [x[1] for x in out if x[0] != "PP"]:
                    stacks.append({"n": n, "foil": foil, "tg": t, "thickness": T, "actual": act, "seq": out})
    return stacks, refs


def _parse_refs(grid):
    """Rezonit's 50 Ohm / 100 Ohm table (outer layer of the 4-layer builds)."""
    rows = [r for _, r in grid]
    foil = {i: int(_num(c)) for i, c in enumerate(rows[0]) if re.fullmatch(r"\d+\s*мкм", c)}
    thick = next(({i: _num(c) for i, c in enumerate(r) if re.fullmatch(r"\d+(?:[.,]\d+)?", c)}
                  for r in rows if any(re.fullmatch(r"\d+(?:[.,]\d+)?", c) for c in r)), {})
    out = []
    for row in rows:
        tg = _tgs(" ".join(row[:3]))
        for i, c in enumerate(row):
            m = re.fullmatch(r"(\d+)\s*Ом\s*\((\d+)(?:\s*/\s*(\d+))?\s*мкм\)", c)
            if not m or not tg or i not in thick:
                continue
            out.append({"foil": foil.get(i), "thickness": thick[i], "tg": min(tg), "z": int(m.group(1)),
                        "w_um": int(m.group(2)), "s_um": int(m.group(3)) if m.group(3) else None})
    return out


def parse_tech(page):
    """Capabilities page -> {"core": {tg: [(mm, dk)]}, "pp": {(style, tg): (mm, dk)},
    "min_mm": {"trace"|"gap": {foil: [base, advanced, limit]}}, "mask_dk": float}."""
    out = {"core": {}, "pp": {}, "min_mm": {"trace": {}, "gap": {}}, "mask_dk": None}
    for m in re.finditer(r"<table\b.*?</table>", page, re.S):
        rows = [r for r in _rows(m.group(0)) if r]
        head = rows[0] if rows else []
        if head[:1] == ["Цвет"] and head[-1:] == ["Dk"]:        # solder mask, Dk in the last column
            out["mask_dk"] = next((_num(r[-1]) for r in rows[1:] if "зелен" in r[0] and _num(r[-1])), None)
        elif "Толщина диэлектрика, мм" in head:
            group = None
            for r in rows:
                label = next((c for c in r if re.match(r"(FR4|RO\d|Углеводород|тип\s)", c)), "")
                if label and not label.startswith("тип"):
                    group = re.match(r"FR4\s*\(Tg\s*(\d+)\)", label)
                idk = next((i for i, c in enumerate(r) if re.search(r"\(1\s*MHz\)", c)), None)
                if idk is None or idk < 4 or _num(r[idk - 4]) is None:
                    continue
                h, dk = _num(r[idk - 4]), _num(r[idk])      # thickness, foil, total, tolerance, Dk
                pp = re.match(r"тип\s+(\d+)\s*\(Tg\s*(\d+)\)", label)
                if pp:
                    out["pp"][(pp.group(1), int(pp.group(2)))] = (h, dk)
                elif group and not label.startswith("тип"):
                    out["core"].setdefault(int(group.group(1)), []).append((h, dk))
        elif head[:1] == ["Минимальные значения"]:
            what = None
            for r in rows[1:]:
                if r[0]:
                    what = {"Проводник": "trace", "Зазор между проводниками": "gap"}.get(r[0])
                if what and len(r) >= 5 and r[1].isdigit():
                    out["min_mm"][what][r[1]] = [_num(x) for x in r[2:5]]
    if not out["core"] or not out["pp"] or not out["min_mm"]["trace"] or not out["mask_dk"]:
        raise RuntimeError("rezonit.ru: materials, solder mask or minimum trace table not found")
    return out


# ------------------------------------------------------------------ database
def _layer(material, name=None, h=0.0, dk=0.0, top=0.0, bot=0.0, mat="", style=""):
    return {"layerName": name, "material": material, "materialName": mat, "dielectricConstant": dk,
            "dielectricThick": h, "topConductorThick": top, "botConductorThick": bot, "specificationsModel": style}


def template(b, tech):
    """One typical build -> a template record like JLC's (mm; the copper on a core is part of
    the core record; a prepreg of several plies is one layer with the thickness-weighted Dk)."""
    n, foil, tg, T = b["n"], b["foil"], b["tg"], b["thickness"]
    seq, layers, k, i = b["seq"], [], 0, 0
    while i < len(seq):
        e = seq[i]
        if e[0] == "Cu" and i + 2 < len(seq) and seq[i + 1][0] == "Core":
            h = seq[i + 1][1]
            dk = min(tech["core"][tg], key=lambda x: abs(x[0] - h))[1]
            layers.append(_layer("Core", f"L{k + 1}/L{k + 2}", h, dk, e[1], seq[i + 2][1], f"FR-4 TG{tg} {h:g}mm"))
            k, i = k + 2, i + 3
            continue
        if e[0] == "Cu":
            k += 1
            layers.append(_layer("Copper", f"L{k}", top=e[1], mat=f"{e[1] * 1000:g}um"))
        else:
            styles, hs = e[1], e[2]
            dks = [tech["pp"][(st, tg)][1] for st in styles]
            h = sum(hs)
            layers.append(_layer("Prepreg", None, round(h, 4), round(sum(a * b for a, b in zip(hs, dks)) / h, 3),
                                 mat="+".join(styles), style=" + ".join(f"{st} {x:g}mm" for st, x in zip(styles, hs))))
        i += 1
    inner, outer = OZ[foil]
    return {"appointName": f"RZ{n}-{T:g}mm-{foil}um-TG{tg}",
            "receptionDisplayName": f"Rezonit typical {n}L, foil {foil} um, FR-4 TG{tg}, {T:g} mm "
                                    f"(actual {b['actual']:g} mm w/o plating, finish, mask)",
            "plateLayerNumber": n, "plateThickness": T, "innerCopperThickness": inner, "cuprumThickness": outer,
            "actualThickness": b["actual"], "chargeFlag": False, "laminationType": 1, "foil_um": foil, "tg": tg,
            "layers": layers}


def option(t):
    """The order option of a typical build: always orderable, no stackup surcharge."""
    return {"templateName": t["appointName"], "showName": t["appointName"], "stencilLayer": t["plateLayerNumber"],
            "stencilPly": t["plateThickness"], "cuprumThickness": t["cuprumThickness"],
            "insideCuprumThickness": t["innerCopperThickness"], "expeditedFlag": False, "enableFlag": True,
            "fixedFee": 0.0, "coefficient": 0.0, "surcharge_usd": 0.0, "inner_cu_usd": 0.0}


def configs(mask_dk):
    """Copper/etch/mask model in the shape of JLC's config tables."""
    rows = [{"layerType": lt, "systemCopperThickness": so, "baseCopperThickness": bo} for lt, so, bo, _, _ in COPPER]
    return {"copper_trace_width": [dict(r, traceCopperThickness=t, traceWidthDelta=d)
                                   for r, (_, _, _, t, d) in zip(rows, COPPER)],
            "coverlay": [dict(r, coatingAboveSubstrate=MASK[0], coatingAboveTrace=MASK[1],
                              coatingBetweenTraces=MASK[2], coatingDielectricConstant=mask_dk) for r in rows]}


def check(db):
    """Rezonit's own 50/100 Ohm table (outer layer of the 4-layer builds) through the offline
    solver: [(reference row, stack name, Z)]."""
    from ..core import engine as E
    stacks = ST.stacks(db["templates"], db["configs"], layers=[4])
    out = []
    for r in db["meta"]["reference"]:
        s = next((x for x in stacks if x.t["foil_um"] == r["foil"] and x.t["tg"] == r["tg"]
                  and x.thickness == r["thickness"]), None)
        if s:
            diff = r["s_um"] is not None
            model, geo = s.geometry("L1", diff=diff)
            out.append((r, s.name, E.zval(model, geo, r["w_um"] / 25.4, r["s_um"] / 25.4 if diff else None)))
    return out


def refresh(progress=None, path=None):
    """Read both pages and write rezonit_stackups.zip (the old one stays if anything is
    missing); returns the new meta."""
    if progress:
        progress("rezonit.ru", 0, 0)
    tech = parse_tech(_get(TECH))
    builds, refs = parse_stacks(_get(STACKS))
    if len(builds) < 10 or not refs:
        raise RuntimeError(f"rezonit.ru: only {len(builds)} typical builds, {len(refs)} reference widths found")
    templates = [template(b, tech) for b in builds]
    db = {"templates": templates, "order_options": [option(t) for t in templates], "configs": configs(tech["mask_dk"]),
          "meta": {"updated": time.strftime("%Y-%m-%d %H:%M"), "templates": len(templates),
                   "options": len(templates), "source": "rezonit.ru", "pages": [STACKS, TECH],
                   "dk": "1 MHz values of Rezonit's materials table", "mask_dk": tech["mask_dk"],
                   "min_mm": tech["min_mm"], "min_classes": CLASSES, "reference": refs}}
    db["meta"]["check_max_dev"] = round(max(abs(z / r["z"] - 1) for r, _, z in check(db)), 4)
    path = path or paths.data(paths.FABS["rezonit"])
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, obj in db.items():
            z.writestr(name + ".json", json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    os.replace(tmp, path)
    if ST.fab() == "rezonit":
        ST.reload()
    return db["meta"]
