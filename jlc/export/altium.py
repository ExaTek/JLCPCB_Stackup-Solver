"""Altium Designer .stackup writer (Layer Stack Manager > File > Load Stackup).

Field layout mirrors a stackup file saved by AD itself (Layer Stack Manager > File >
Save Stackup): UTF-8 with BOM, one line of KEY=VALUE pairs joined by '|'.
All copper layers are written as signal layers (planes drawn as polygons).
Impedance profiles are named S50 / DIFF90 (the requirement name goes to the description)
and carry a width for every signal layer, TOP/BOTTOM included.
"""
import datetime
import re
import uuid

from ..core import engine as E

TYPE = {
    "overlay": "{C7EF040E-8D00-490B-B00C-A7E7823FF174}",
    "solder": "{7B384237-13D8-4318-8BCB-ACCD8D9A51E7}",
    "finish": "{B0827674-798C-4CF8-807C-8E6C2A11C145}",
    "copper": "{F4ECCD87-2CFB-4F37-BE50-4F3A272B4D01}",
    "prepreg": "{1A79611A-039D-4D40-A204-53C26C50F8B5}",
    "core": "{136C62EF-1FA6-4897-AE71-7E797B632B92}",
}
FINISH = {"ENIG": ("0.1575mil", "ENIG", "Gold", "#FFFFD700"),
          "HASL": ("0.7874mil", "HASL", "PbSn", "#FFF2F2F2"),
          "LF-HASL": ("0.7874mil", "HASL", "SnCu", "#FFF2F2F2")}
MM = 0.0254


def g():
    return "{" + str(uuid.uuid4()).upper() + "}"


def fmt(x, unit="mil"):
    return f"{x:.4f}".rstrip("0").rstrip(".") + unit


def default_names(plan):
    n = len(plan)
    out, cnt = [], {"S": 0, "G": 0, "P": 0}
    for k, r in enumerate(plan):
        if k == 0:
            out.append("TOP")
        elif k == n - 1:
            out.append("BOTTOM")
        else:
            cnt[r] += 1
            out.append({"S": "SIG", "G": "GND", "P": "PWR"}[r] + str(cnt[r]))
    return out


def profile_names(reqs):
    """S50, DIFF90, ...; a repeated name gets _2, _3."""
    out, seen = [], {}
    for r in reqs:
        base = ("DIFF" if r["kind"] == "diff" else "S") + f"{float(r['z']):g}"
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return out


def profile_desc(req, name):
    """The requirement's own name unless it says nothing new (S50, D90, Z3, ...)."""
    nm = (req.get("name") or "").strip()
    z = f"{float(req['z']):g}"
    if nm.upper() in (name, "S" + z, "D" + z, "DIFF" + z) or re.fullmatch(r"[SDZ]\d+", nm):
        return ""
    return nm


def build(stack, plan, result, reqs, names=None, finish="ENIG", mask=None, outer_t=None,
          df=0.02, lim=None):
    """stack: jlc.stackup.Stack; plan: ['S','G',...]; result: engine.search() entry
    for this stack; reqs: requirement dicts; mask: (C1 over substrate, C2 over
    trace, Er) in mils, outer_t: plated outer copper in mils (both default to the
    stack's model); lim: the search's W/S limits (for widths synthesized on
    layers a requirement did not cover). Returns the file text (without BOM)."""
    mask, outer_t = mask or stack.mask(), outer_t or stack.outer_t()
    names = names or default_names(plan)
    sub = g()
    master = g()
    kv = [("STACKUPVERSION", "1"), ("STACKUPDOCUMENTID", master), ("STACKUPDOCUMENTREVISIONID", g()),
          ("STACKUPDOCUMENTREVISIONDATE", datetime.datetime.now().strftime("%m/%d/%Y %H:%M:%S")),
          ("FEATURE_0ID", "{C8939E8A-FD0E-4D52-8860-B7A98F452016}"), ("FEATURE_0NAME", "Standard Stackup"),
          ("FEATURE_1ID", "{E3DF2B86-5F1B-49CA-B266-D1AE57F0BA6F}"), ("FEATURE_1NAME", "Impedance Calculator"),
          ("LAYERMASTERSTACK_V8ID", master), ("LAYERMASTERSTACK_V8TYPE", "0"),
          ("LAYERMASTERSTACK_V8NAME", "Master layer stack"), ("LAYERMASTERSTACK_V8ISFLEX", "False"),
          ("LAYERMASTERSTACK_V8SHOWTOPDIELECTRIC", "False"), ("LAYERMASTERSTACK_V8SHOWBOTTOMDIELECTRIC", "False"),
          ("LAYERMASTERSTACK_V8STYLE", "3"), ("DISPLAYUNIT", "1"), ("LAYERMASTERSTACK_V8SYMMETRIC", "True"),
          ("LAYERMASTERSTACK_V8LIBRARYCOMPLIED", "False"), ("LAYERMASTERSTACK_V8ROUGHNESSTYPE", "NoRoughness"),
          ("LAYERMASTERSTACK_V8ROUGHNESSFACTORSR", "0um"), ("LAYERMASTERSTACK_V8ROUGHNESSFACTORRF", "1%"),
          ("LAYERSUBSTACK_V8_0ID", sub), ("LAYERSUBSTACK_V8_0NAME", f"{stack.name} (JLCPCB)"),
          ("LAYERSUBSTACK_V8_0SYMMETRIC", "True"), ("LAYERSUBSTACK_V8_0ISFLEX", "False"),
          ("LAYERSUBSTACK_V8_0SHOWTOPDIELECTRIC", "False"), ("LAYERSUBSTACK_V8_0SHOWBOTTOMDIELECTRIC", "False"),
          ("LAYERSUBSTACK_V8_0TYPE", "0"), ("LAYERSUBSTACK_V8_0USEDBYPRIMS", "False"),
          ("LAYERSUBSTACK_V8_0SERVICE", "False")]
    layers = []          # (fields dict, name)
    ids = {}             # JLC layer name (L1..) -> GUID

    def add(fields, name):
        layers.append((fields, name))

    def solder(top):
        return {"LAYERID": "16973834" if top else "16973835", "ID": g(), "TYPEID": TYPE["solder"],
                "NAME": "Top Solder" if top else "Bottom Solder", "ISHIDDEN": "False", "USEDBYPRIMS": "False",
                "DIELTYPE": "3", "DIELHEIGHT": fmt(mask[0]), "DIELCONST": f"{mask[2]:g}", "DIELMATERIAL": "Solder Resist",
                "$LSM$Thickness": fmt(mask[0]), "$LSM$DielectricConstant": f"{mask[2]:g}",
                "$LSM$Material": "Solder Resist", "$LSM$Material.Color": "#FF008000",
                "$LSM$Material.Frequency": "1GHz", "$LSM$LossTangent": "0.025",
                "$LSM$Material.Manufacturer": "JLCPCB", "$LSM$MaterialDescription": "Solder Mask",
                "$LSM$Material.Description": "Solder Mask"}

    def fin(top):
        if finish not in FINISH:
            return None
        t, proc, mat, col = FINISH[finish]
        return {"LAYERID": "16973824", "ID": g(), "TYPEID": TYPE["finish"], "ISSURFACEFINISH": "True",
                "NAME": ("Top" if top else "Bottom") + " Surface Finish", "ISHIDDEN": "False",
                "USEDBYPRIMS": "False", "ISADVANCED": "True", "$LSM$Thickness": t, "$LSM$Process": proc,
                "$LSM$Material": mat, "$LSM$Material.Color": col}

    add({"LAYERID": "16973830", "ID": g(), "TYPEID": TYPE["overlay"], "NAME": "Top Overlay",
         "ISHIDDEN": "False", "USEDBYPRIMS": "False"}, "Top Overlay")
    add(solder(True), "Top Solder")
    f = fin(True)
    if f:
        add(f, f["NAME"])
    cu_names = stack.layer_names()
    n = len(cu_names)
    k_cu, k_d = 0, 0
    for it in stack.items:
        if it["k"] == "Cu":
            k = cu_names.index(it["name"])
            top, bot = k == 0, k == n - 1
            lid = "16777217" if top else ("16842751" if bot else str(16777217 + k))
            t = outer_t if top or bot else it["t"]
            oz = stack.outer_oz if top or bot else stack.inner_oz
            guid = g()
            ids[it["name"]] = guid
            place = "1" if top else ("2" if bot else "0")
            # the foil is bonded to the dielectric it is etched on: the core for an inner layer,
            # the prepreg for an outer one. 0 = Above (dielectric below the copper), 1 = Below.
            orient = "0" if top or it["core"] == "upper" else "1"
            nm = names[k]
            add({"LAYERID": lid, "ID": guid, "TYPEID": TYPE["copper"], "NAME": nm, "ISHIDDEN": "False",
                 "USEDBYPRIMS": "False", "COPTHICK": fmt(t), "COMPONENTPLACEMENT": place,
                 "COPPERORIENTATION": orient, "$LSM$Thickness": fmt(t), "$LSM$ComponentPlacement": place,
                 "$LSM$CopperOrientation": orient, "$LSM$Weight": f"{oz:g}oz", "$LSM$Material": "Copper",
                 "$LSM$Process": "ED", "$LSM$Material.Manufacturer": "JLCPCB",
                 "$LSM$MaterialDescription": "Copper Foil"}, nm)
            k_cu += 1
        else:
            k_d += 1
            core = it["k"] == "Core"
            mat = it["mat"] if not core else f"Core {it['mat'].split()[0]}"
            nm = f"Dielectric {k_d}"
            add({"LAYERID": str(17039360 + k_d), "ID": g(), "TYPEID": TYPE["core" if core else "prepreg"],
                 "NAME": nm, "ISHIDDEN": "False", "USEDBYPRIMS": "False", "DIELTYPE": "1" if core else "2",
                 "DIELHEIGHT": fmt(it["h"]), "DIELCONST": f"{it['er']:g}", "DIELMATERIAL": mat,
                 "$LSM$Thickness": fmt(it["h"]), "$LSM$DielectricConstant": f"{it['er']:g}",
                 "$LSM$Material": mat, "$LSM$Material.Constructions": "" if core else it["mat"],
                 "$LSM$Material.Frequency": "1GHz", "$LSM$LossTangent": f"{df:g}",
                 "$LSM$Material.GlassTransTemp": "155C", "$LSM$Material.Manufacturer": "Nan Ya NP-155F (JLCPCB)",
                 "$LSM$MaterialDescription": "Core" if core else "Prepreg"}, nm)
    f = fin(False)
    if f:
        add(f, f["NAME"])
    add(solder(False), "Bottom Solder")
    add({"LAYERID": "16973831", "ID": g(), "TYPEID": TYPE["overlay"], "NAME": "Bottom Overlay",
         "ISHIDDEN": "False", "USEDBYPRIMS": "False"}, "Bottom Overlay")

    for i, (fields, nm) in enumerate(layers):
        for k2, v in fields.items():
            kv.append((f"LAYER_V8_{i}{k2}", v))
        kv += [(f"LAYER_V8_{i}_{sub}CONTEXT", "0"), (f"LAYER_V8_{i}_{sub}USEDBYPRIMS", "False"),
               (f"LAYER_V8_{i}_{sub}NAME", nm)]
    kv += [(f"VIASPAN_V8_{sub}_0ID", g()), (f"VIASPAN_V8_{sub}_0NAME", f"Thru 1:{n}"),
           (f"VIASPAN_V8_{sub}_0CUSTOMNAME", "Via1"), (f"VIASPAN_V8_{sub}_0TYPE", "0"),
           (f"VIASPAN_V8_{sub}_0FIRSTLAYERID", ids[cu_names[0]]),
           (f"VIASPAN_V8_{sub}_0LASTLAYERID", ids[cu_names[-1]]), (f"VIASPAN_V8_{sub}_0SYMMETRIC", "False")]

    # impedance profiles + per-layer widths (every signal layer)
    layer_rows = {x["layer"]: x for x in result["layers"]}
    cells = E.all_layers(stack, result, reqs, lim, mask)
    ti = 0
    for pi, (req, pname, rl) in enumerate(zip(reqs, profile_names(reqs), cells)):
        pid = g()
        diff = req["kind"] == "diff"
        kv += [(f"IMPEDANCEPROFILE_V8_{pi}ID", pid), (f"IMPEDANCEPROFILE_V8_{pi}NAME", pname),
               (f"IMPEDANCEPROFILE_V8_{pi}DESCRIPTION", profile_desc(req, pname)),
               (f"IMPEDANCEPROFILE_V8_{pi}TYPE", "1" if diff else "0"),
               (f"IMPEDANCEPROFILE_V8_{pi}IMPEDANCE", f"{float(req['z']):g}ohm"),
               (f"IMPEDANCEPROFILE_V8_{pi}TOLERANCE", f"{100 * float(req.get('tol', 0.05)):g}%")]
        for L, v in rl.items():
            row = layer_rows[L]
            outer = row["kind"] == "outer"
            p = f"TRACEIMPEDANCE_V8_{ti}"
            kv += [(p + "LAYERID", ids[L]), (p + "SUBSTACKID", sub), (p + "PROFILEID", pid),
                   (p + "DIFFERENTIAL", str(diff))]
            if row.get("up"):
                kv.append((p + "TOPREFID", ids[row["up"]]))
            if row.get("down"):
                kv.append((p + "BOTREFID", ids[row["down"]]))
            tl = ("Edge-Coupled " if diff else "") + ("Coated Microstrip" if outer else "Offset Stripline")
            z = v["Z"]
            kv += [(p + "WIDTH", f"{v['W'] * MM:.6f}mm"), (p + "GAP", fmt(v["S"] if diff else 5)),
                   (p + "ETCH", f"{stack.etch_factor(L, outer_t if outer else None):.4g}"),
                   (p + "CALCMODE", "1"),
                   (p + "IMPEDANCEERROR", f"{abs(z - float(req['z'])) / float(req['z']) * 100:.4f}%"),
                   (p + "TLTYPENAME", tl), (p + "HASPLATING", "False"), (p + "USESOLDERMASK", str(outer))]
            if outer:
                kv += [(p + "COATEDHEIGHT1", fmt(mask[0])), (p + "COATEDHEIGHT2", fmt(mask[1]))]
            kv.append((p + "CLEARANCETOPLANE", "5mil"))
            if diff:
                kv += [(p + "_ELECTRICPARAMS_Zdif", f"{z:.4f}ohm"), (p + "_ELECTRICPARAMS_Zo", f"{z / 2:.4f}ohm")]
            else:
                kv.append((p + "_ELECTRICPARAMS_Z", f"{z:.4f}ohm"))
            ti += 1
    return "|" + "|".join(f"{k}={v}" for k, v in kv)


def save(path, text):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(text)
