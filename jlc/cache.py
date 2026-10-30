"""Persistent cache of JLC solver answers: jlc_calc_cache.json.

Key = model + rounded parameters, value = impedance from JLC's Si9000 (or None
when JLC rejected the parameters). Every online solve lands here, so it doubles
as reference data for validating the offline solver. The copy shipped in data/
seeds it; new answers are saved in the user folder."""
import json
import os

from . import paths

NAME = "jlc_calc_cache.json"
_PARAMS = ("H1", "Er1", "H2", "Er2", "W1", "W2", "T1", "S1", "C1", "C2", "C3", "CEr")


def key(model, p):
    return model + "|" + ",".join(f"{k}={round(float(p[k]), 4):g}" for k in _PARAMS if p.get(k) is not None)


def parse_key(k):
    model, rest = k.split("|", 1)
    return model, {a: float(b) for a, b in (kv.split("=") for kv in rest.split(","))}


class Cache:
    def __init__(self, path=None):
        self.path = os.path.abspath(path or os.path.join(paths.USER, NAME))
        self.d = {}
        for src in ([] if path else [paths.data(NAME)]) + [self.path]:
            if os.path.exists(src):
                self.d.update(json.load(open(src, encoding="utf-8")))

    def get(self, model, p):
        return self.d.get(key(model, p), "miss")

    def put(self, model, p, z):
        self.d[key(model, p)] = z

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        json.dump(self.d, open(tmp, "w", encoding="utf-8"), indent=0, sort_keys=True)
        os.replace(tmp, self.path)


def jlc_args(p):
    """Parameter dict -> impedance_calc_arg for JLC's calc endpoint."""
    arg = {"dCalculateMode": 3, "isLinkComputingMode": False, "W2LinkW1Incr": round(p["W1"] - p["W2"], 4)}
    arg.update({k: p[k] for k in _PARAMS if p.get(k) is not None})
    return arg


def online(models_params, cache=None, solver=None):
    """[(model, params)] -> [Z] using the cache first, JLC for the misses."""
    from . import api
    cache = cache or Cache()
    out = [cache.get(m, p) for m, p in models_params]
    miss = [i for i, z in enumerate(out) if z == "miss"]
    if miss:
        own = solver is None
        solver = solver or api.Solver()
        try:
            for i0 in range(0, len(miss), 200):
                chunk = miss[i0:i0 + 200]
                zs = solver.calc_many([(models_params[i][0], jlc_args(models_params[i][1])) for i in chunk])
                for i, z in zip(chunk, zs):
                    out[i] = z
                    cache.put(*models_params[i], z)
                cache.save()
        finally:
            if own:
                solver.close()
    return out
