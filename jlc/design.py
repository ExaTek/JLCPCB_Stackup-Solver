"""Width / gap synthesis on top of the solver (offline) or JLC (online)."""
import numpy as np
from scipy.optimize import brentq

from .solver import impedance

FINE = 1.0


def _p(geo, W1, S=None):
    p = {k: v for k, v in geo.items() if k != "d"}
    p["W1"], p["W2"] = W1, max(W1 - geo["d"], 0.05)
    if S is not None:
        p["S1"] = S
    return p


def z_of(model, geo, W1, S=None, fine=FINE):
    return impedance(model, _p(geo, W1, S), fine)[0]


def se_width(model, geo, Z, lo=None, hi=40.0):
    lo = lo or geo["d"] + 0.3
    f = lambda w: z_of(model, geo, w) - Z
    if f(lo) < 0 or f(hi) > 0:
        return None
    return brentq(f, lo, hi, xtol=0.005)


def diff_gap(model, geo, Z, W1, smax=30.0):
    f = lambda s: z_of(model, geo, W1, s) - Z
    if f(0.5) > 0 or f(smax) < 0:
        return None
    return brentq(f, 0.5, smax, xtol=0.005)


# ------------------------------------------------------------- grid method
SE_W = np.array([2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6, 7, 8, 9, 10, 12, 14])
DF_W = np.array([3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7])
DF_S = np.array([3, 3.5, 4, 5, 6, 7, 8, 10, 12, 15, 20])


def tables(se_model, df_model, geo):
    """Z on fixed W (SE) and W x S (diff) grids, for fast interpolation."""
    zse = np.array([z_of(se_model, geo, w) for w in SE_W])
    zdf = np.array([[z_of(df_model, geo, w, s) for s in DF_S] for w in DF_W])
    return zse, zdf


def interp_w(zse, Z):
    for i in range(len(SE_W) - 1):
        z1, z2 = zse[i], zse[i + 1]
        if (z1 - Z) * (z2 - Z) <= 0:
            a, b = np.log(SE_W[i]), np.log(SE_W[i + 1])
            return float(np.exp(a + (b - a) * (z1 - Z) / (z1 - z2)))
    return None


def interp_s(zrow, Z):
    for i in range(len(DF_S) - 1):
        z1, z2 = zrow[i], zrow[i + 1]
        if (z1 - Z) * (z2 - Z) <= 0:
            return float(DF_S[i] + (DF_S[i + 1] - DF_S[i]) * (Z - z1) / (z2 - z1))
    return None


def diff_options(zdf, Z):
    """[(W, S)] for every grid width where the target is reachable."""
    out = []
    for w, row in zip(DF_W, zdf):
        s = interp_s(row, Z)
        if s is not None:
            out.append((float(w), s))
    return out


def pick_pair(opts, wmin, smin, pmax):
    """Tightest pair (min pitch) with W >= wmin, S >= smin, pitch <= pmax."""
    ok = [(w, s) for w, s in opts if w >= wmin - 1e-6 and s >= smin - 1e-6 and w + s <= pmax]
    return min(ok, key=lambda ws: ws[0] + ws[1]) if ok else None
