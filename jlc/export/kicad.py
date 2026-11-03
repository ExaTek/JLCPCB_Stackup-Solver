"""KiCad project with the JLC stackup (KiCad 7 or newer; the files are in KiCad 7 format).

save() writes three files side by side:
  <name>.kicad_pcb   empty board: named copper layers, dielectrics with Dk, solder mask, finish
  <name>.kicad_pro   net classes S50, DIFF90, ... with the width (and pair gap) of the top layer
  <name>.kicad_dru   custom rules: width and pair gap of every net class on every signal layer
Open the project, or bring all of it into an existing board with Board Setup > Import Settings
(board layers and physical stackup, net classes, custom rules).
"""
import json
import os

from ..core import engine as E
from .altium import default_names, profile_desc, profile_names

MM = 0.0254
FINISH = {"ENIG": "ENIG", "HASL": "HAL SnPb", "LF-HASL": "HAL lead-free"}
USER_LAYERS = ['(32 "B.Adhes" user "B.Adhesive")', '(33 "F.Adhes" user "F.Adhesive")', '(34 "B.Paste" user)',
               '(35 "F.Paste" user)', '(36 "B.SilkS" user "B.Silkscreen")', '(37 "F.SilkS" user "F.Silkscreen")',
               '(38 "B.Mask" user)', '(39 "F.Mask" user)', '(40 "Dwgs.User" user "User.Drawings")',
               '(41 "Cmts.User" user "User.Comments")', '(42 "Eco1.User" user "User.Eco1")',
               '(43 "Eco2.User" user "User.Eco2")', '(44 "Edge.Cuts" user)', '(45 "Margin" user)',
               '(46 "B.CrtYd" user "B.Courtyard")', '(47 "F.CrtYd" user "F.Courtyard")', '(48 "B.Fab" user)',
               '(49 "F.Fab" user)']
# KiCad's own defaults for everything that is not about impedance
NETCLASS = {"bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
            "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
            "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": 0.25,
            "via_diameter": 0.8, "via_drill": 0.4, "wire_width": 6}


def mm(mil):
    return round(mil * MM, 5)


def q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def cu_layer(k, n):
    """JLC copper index (0 = L1) -> KiCad layer name."""
    return "F.Cu" if k == 0 else ("B.Cu" if k == n - 1 else f"In{k}.Cu")


def build(stack, plan, result, reqs, names=None, finish="ENIG", mask=None, outer_t=None, df=0.02,
          lim=None):
    """Same inputs as altium.build(); returns {".kicad_pcb": text, ".kicad_pro": text, ".kicad_dru": text}."""
    mask, outer_t = mask or stack.mask(), outer_t or stack.outer_t()
    names = names or default_names(plan)
    cu_names = stack.layer_names()
    n = len(cu_names)

    # stackup: copper, and per gap between two coppers one dielectric (JLC: Er = plain average)
    rows, gap, total, nd = [], [], 2 * mm(mask[0]), 0

    def flush():
        nonlocal total, nd
        if not gap:
            return
        core = gap[0]["k"] == "Core"
        h = mm(sum(x["h"] for x in gap))
        er = sum(x["er"] for x in gap) / len(gap)
        mat = f"Core {gap[0]['mat'].split()[0]}" if core else "+".join(x["mat"] for x in gap)
        nd += 1
        kind = "core" if core else "prepreg"
        rows.append(f'(layer "dielectric {nd}" (type "{kind}") (thickness {h:g}) (material {q(mat)}) '
                    f'(epsilon_r {er:.4g}) (loss_tangent {df:g}))')
        total += h
        gap.clear()

    for it in stack.items:
        if it["k"] == "Cu":
            flush()
            k = cu_names.index(it["name"])
            t = mm(outer_t if k in (0, n - 1) else it["t"])
            rows.append(f'(layer "{cu_layer(k, n)}" (type "copper") (thickness {t:g}))')
            total += t
        else:
            gap.append(it)
    sm = (f'(color "Green") (thickness {mm(mask[0]):g}) (material "Solder Resist") (epsilon_r {mask[2]:g}) '
          f'(loss_tangent 0.025)')
    stackup = ([f'(layer "F.SilkS" (type "Top Silk Screen"))', f'(layer "F.Paste" (type "Top Solder Paste"))',
                f'(layer "F.Mask" (type "Top Solder Mask") {sm})'] + rows +
               [f'(layer "B.Mask" (type "Bottom Solder Mask") {sm})', f'(layer "B.Paste" (type "Bottom Solder Paste"))',
                f'(layer "B.SilkS" (type "Bottom Silk Screen"))',
                f'(copper_finish {q(FINISH.get(finish, "None"))})', "(dielectric_constraints no)"])
    layers = [f'({0 if k == 0 else (31 if k == n - 1 else k)} "{cu_layer(k, n)}" '
              f'{"signal" if plan[k] == "S" else "power"} {q(names[k])})' for k in range(n)]
    pcb = "\n".join(["(kicad_pcb (version 20221018) (generator pcbnew)", "",
                     f"  (general\n    (thickness {total:.4f})\n  )", "", '  (paper "A4")',
                     "  (layers\n" + "\n".join("    " + x for x in layers + USER_LAYERS) + "\n  )", "",
                     "  (setup\n    (stackup\n" + "\n".join("      " + x for x in stackup) + "\n    )\n"
                     "    (pad_to_mask_clearance 0)\n  )", "", '  (net 0 "")', ")", ""])

    # widths: the search cell where a requirement was checked, else synthesized for that layer
    cells = E.all_layers(stack, result, reqs, lim, mask)
    pnames = profile_names(reqs)
    classes = [dict(NETCLASS, name="Default")]
    dru = ["(version 1)",
           f"# JLCPCB Stackup Solver: {stack.name}, layers {''.join(plan)}.",
           "# Put nets into these net classes (Board Setup > Net Classes); the router then takes",
           "# the width and pair gap of each layer from the rules below."]
    for req, pname, rl in zip(reqs, pnames, cells):
        diff = req["kind"] == "diff"
        desc = profile_desc(req, pname)
        geo = {L: (f"{v['W']:g}/{v['S']:g}" if diff else f"{v['W']:g}") for L, v in rl.items()}
        dru.append("")
        dru.append(f"# {pname}: {float(req['z']):g} Ohm {'differential' if diff else 'single-ended'}, "
                   f"+-{100 * float(req.get('tol', 0.05)):g}%" + (f" ({desc})" if desc else ""))
        dru.append("#   " + ", ".join(f"{cu_layer(cu_names.index(L), n)} {g} mil ({rl[L]['Z']:.1f} Ohm)"
                                       for L, g in geo.items()))
        first = next(iter(rl.values()), None)
        nc = dict(NETCLASS, name=pname)
        if first:
            nc["track_width"] = mm(first["W"])
            if diff:
                nc.update(diff_pair_width=mm(first["W"]), diff_pair_gap=mm(first["S"]))
        classes.append(nc)
        for L, v in rl.items():
            kl = cu_layer(cu_names.index(L), n)
            cond = f"A.NetClass == '{pname}'"
            rule = [f'(rule "{pname} {kl}"', f'\t(layer "{kl}")', f'\t(condition "{cond}")',
                    f"\t(constraint track_width (opt {mm(v['W']):g}mm))"]
            if diff:
                rule.append(f"\t(constraint diff_pair_gap (opt {mm(v['S']):g}mm))")
            dru.append("\n".join(rule) + ")")
    pro = {"board": {"design_settings": {"defaults": {}, "rules": {"min_track_width": 0.0889,
                                                                   "min_clearance": 0.0889}}},
           "meta": {"filename": "", "version": 1},
           "net_settings": {"classes": classes, "meta": {"version": 3}, "net_colors": None,
                            "netclass_assignments": None, "netclass_patterns": []}}
    return {".kicad_pcb": pcb, ".kicad_pro": pro, ".kicad_dru": "\n".join(dru) + "\n"}


def save(path, files):
    """path: the .kicad_pcb to write; the .kicad_pro and .kicad_dru go next to it."""
    base = path[:-len(".kicad_pcb")] if path.endswith(".kicad_pcb") else path
    for ext, content in files.items():
        if ext == ".kicad_pro":
            content = json.dumps(dict(content, meta=dict(content["meta"], filename=os.path.basename(base) + ext)),
                                 indent=2) + "\n"
        with open(base + ext, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    return [base + ext for ext in files]
