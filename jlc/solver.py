"""Offline 2D quasi-static field solver for the four JLC/Si9000 models in use:

  OffsetStripline1B1A / DiffOffsetStripline1B1A        (inner layers)
  CoatedMicrostrip1B  / DiffEdgeCoupledCoatedMicrostrip1B (outer layers, solder mask)

Parameters use Si9000 names and mils, exactly as sent to JLC's calc API:
  H1/Er1  dielectric under the trace (trace base W1 sits on it)
  H2/Er2  dielectric above the trace base, up to the far plane (includes T1)
  W1/W2   trace bottom/top width, T1 trace thickness, S1 edge gap at the base
  C1/C2/C3/CEr  mask over substrate / over trace / between traces, mask Er

Method: finite volumes on a graded rectilinear grid, half cross-section by
symmetry (Neumann plane for single-ended, Dirichlet 0 plane for odd mode),
capacitance from field energy, Z = eta0 / sqrt(C * C_air). Validated against
JLC's own solver results in data/jlc_calc_cache.json (see validate.py).
"""
import math

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

ETA0 = 376.730313668


def _mesh(keys, lo, hi, hmin, hmax, ratio=1.25):
    keys = sorted({min(max(k, lo), hi) for k in keys} | {lo, hi})
    pts = [keys[0]]
    for a, b in zip(keys, keys[1:]):
        if b - a < 1e-9:
            continue
        # grow from both ends towards the middle
        left, right, h = [a], [b], hmin
        while left[-1] + h < right[-1] - h:
            left.append(left[-1] + h)
            right.append(right[-1] - h)
            h = min(h * ratio, hmax)
        mid = right[::-1]
        if mid[0] - left[-1] < 0.3 * h and len(left) > 1:
            left.pop()
        pts += left[1:] + mid
    out = np.unique(np.round(pts, 9))
    return out


class _Problem:
    """One cross-section: x >= 0 half plane, trace(s) given as trapezoids."""

    def __init__(self, xs, ys):
        self.x, self.y = xs, ys
        self.nx, self.ny = len(xs), len(ys)
        cx = 0.5 * (xs[1:] + xs[:-1])
        cy = 0.5 * (ys[1:] + ys[:-1])
        self.CX, self.CY = np.meshgrid(cx, cy, indexing="ij")
        self.eps = np.ones((self.nx - 1, self.ny - 1))
        self.fixed = np.zeros((self.nx, self.ny), bool)
        self.val = np.zeros((self.nx, self.ny))
        self.X, self.Y = np.meshgrid(xs, ys, indexing="ij")

    def capacitance(self, eps):
        nx, ny = self.nx, self.ny
        dx, dy = np.diff(self.x), np.diff(self.y)
        # horizontal edges (i,j)-(i+1,j): dual face height from the cells below/above
        e = np.zeros((nx - 1, ny))
        e[:, 1:] += eps * dy[None, :] * 0.5
        e[:, :-1] += eps * dy[None, :] * 0.5
        gh = e / dx[:, None]
        f = np.zeros((nx, ny - 1))
        f[1:, :] += eps * dx[:, None] * 0.5
        f[:-1, :] += eps * dx[:, None] * 0.5
        gv = f / dy[None, :]
        idx = np.arange(nx * ny).reshape(nx, ny)
        a, b = idx[:-1, :].ravel(), idx[1:, :].ravel()
        c, d = idx[:, :-1].ravel(), idx[:, 1:].ravel()
        gi, gj = np.concatenate([a, c]), np.concatenate([b, d])
        g = np.concatenate([gh.ravel(), gv.ravel()])
        n = nx * ny
        L = sp.coo_matrix((np.concatenate([-g, -g, g, g]),
                           (np.concatenate([gi, gj, gi, gj]), np.concatenate([gj, gi, gi, gj]))),
                          shape=(n, n)).tocsr()
        fx = self.fixed.ravel()
        phi = self.val.ravel().copy()
        free = ~fx
        rhs = -L[free][:, fx] @ phi[fx]
        phi[free] = spla.spsolve(L[free][:, free].tocsc(), rhs)
        dphi = phi[gi] - phi[gj]
        return float(np.sum(g * dphi * dphi))  # = 2*energy = C*V^2 (V=1), in eps0 units

    def z(self):
        c = self.capacitance(self.eps)
        c0 = self.capacitance(np.ones_like(self.eps))
        return ETA0 / math.sqrt(c * c0), c / c0


def _halfwidth(y, yb, t, w1, w2):
    f = np.clip((y - yb) / t, 0.0, 1.0)
    return 0.5 * (w1 + (w2 - w1) * f)


def impedance(model, p, fine=1.0):
    """Z0 (single) or Zdiff (Diff* models) for Si9000-style parameters p (mils).
    Returns (Z, Er_eff)."""
    diff = model.startswith("Diff")
    strip = "Stripline" in model
    H1, Er1, W1, W2, T = p["H1"], p["Er1"], p["W1"], p["W2"], p["T1"]
    W2 = min(W2, W1)
    S = p.get("S1", 0.0) if diff else 0.0
    xc = S / 2 + W1 / 2 if diff else 0.0
    yb, yt = H1, H1 + T
    hmin = min(0.05, T / 6, (W1 - W2) / 4 if W1 > W2 else 1) / fine
    hmin = max(hmin, 0.01)
    xedges = [xc - W1 / 2, xc + W1 / 2, xc - W2 / 2, xc + W2 / 2]
    if diff:
        xedges.append(0.0)
    if strip:
        H2, Er2 = p["H2"], p["Er2"]
        top = H1 + H2
        X = xc + W1 / 2 + 6 * top + 10
        ykeys = [0, yb, yt, top]
        xs = _mesh([0] + xedges, 0, X, hmin, max(top / 6, 0.5) / fine)
        ys = _mesh(ykeys, 0, top, hmin, max(top / 10, 0.25) / fine)
        pr = _Problem(xs, ys)
        pr.eps[:] = np.where(pr.CY < H1, Er1, Er2)
        pr.fixed[:, 0] = pr.fixed[:, -1] = True
    else:
        C1, C2, CEr = p.get("C1", 0.0), p.get("C2", 0.0), p.get("CEr", 1.0)
        C3 = p.get("C3", C1)
        span = max(H1 + T, W1 + S)
        X = xc + W1 / 2 + 40 * span
        top = H1 + 40 * span
        mx = [xc - W1 / 2 - C2, xc + W1 / 2 + C2, xc - W2 / 2 - C2, xc + W2 / 2 + C2]
        ykeys = [0, yb, yt, yb + C1, yb + C3, yt + C2]
        xs = _mesh([0] + xedges + mx, 0, X, hmin, max(span / 2, 1) / fine, 1.3)
        ys = _mesh(ykeys, 0, top, hmin, max(span / 2, 1) / fine, 1.3)
        pr = _Problem(xs, ys)
        hw = _halfwidth(pr.CY, yb, T, W1, W2)
        band = np.where(pr.CX < S / 2, C3, C1) if diff else C1
        mask = (pr.CY > yb) & (pr.CY < yb + band)
        mask |= (pr.CY > yb) & (pr.CY < yt + C2) & (np.abs(pr.CX - xc) < hw + C2)
        pr.eps[:] = np.where(pr.CY < H1, Er1, np.where(mask, CEr, 1.0))
        pr.fixed[:, 0] = True
        pr.fixed[:, -1] = True
        pr.fixed[-1, :] = True
    # trace(s)
    inside = (pr.Y >= yb - 1e-9) & (pr.Y <= yt + 1e-9) & \
             (np.abs(pr.X - xc) <= _halfwidth(pr.Y, yb, T, W1, W2) + 1e-9)
    pr.fixed |= inside
    pr.val[inside] = 1.0
    if diff:
        pr.fixed[0, :] = True       # odd-mode symmetry plane at x = 0
    zc, ereff = pr.z()
    # single-ended: the half section holds half of C and C_air, so Z0 = zc/2;
    # odd mode: the half section is one line's odd capacitance, Zdiff = 2*Z_odd
    return (2 * zc if diff else zc / 2), ereff
