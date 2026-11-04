"""Offline 2D quasi-static field solver for the four JLC/Si9000 models in use:

  OffsetStripline1B1A / DiffOffsetStripline1B1A        (inner layers)
  CoatedMicrostrip1B  / DiffEdgeCoupledCoatedMicrostrip1B (outer layers, solder mask)

Parameters use Si9000 names and mils, exactly as sent to JLC's calc API:
  H1/Er1  dielectric under the trace (trace base W1 sits on it)
  H2/Er2  dielectric above the trace base, up to the far plane (includes T1)
  W1/W2   trace bottom/top width, T1 trace thickness, S1 edge gap at the base
  C1/C2/C3/CEr  mask over substrate / over trace / between traces, mask Er
  Plies1/Plies2 optional [(h, er), ...] when a dielectric is several plies of different Dk:
          Plies1 from the plane up to the trace base, Plies2 from the trace base up to the
          far plane. They replace Er1/Er2 in this solver; JLC's model has one Er per dielectric.

Method: finite volumes on a graded rectilinear grid, half cross-section by
symmetry (Neumann plane for single-ended, Dirichlet 0 plane for odd mode),
capacitance from field energy, Z = eta0 / sqrt(C * C_air). Validated against
JLC's own solver results in data/jlc_calc_cache.json (see validate.py).
"""
import math

import numpy as np

ETA0 = 376.730313668


def _plies_eps(y, plies, y0):
    """Dk at heights y for plies [(h, er), ...] stacked upwards from y0."""
    out = np.full_like(y, plies[-1][1], dtype=float)
    z = y0
    for h, er in plies[:-1]:
        out = np.where((y >= z) & (y < z + h), er, out)
        z += h
    return out


def _plies_keys(plies, y0):
    """Interface heights of the plies, as mesh keys."""
    out, z = [], y0
    for h, _ in plies[:-1]:
        z += h
        out.append(z)
    return out


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


def _block_solve(gh, gv, fixed, val):
    """Potential on the grid: direct block elimination, column by column along the longer side.
    gh[i, j] is the conductance of (i, j)-(i+1, j), gv[i, j] of (i, j)-(i, j+1); fixed nodes hold val."""
    if fixed.shape[1] > fixed.shape[0]:
        return _block_solve(gv.T, gh.T, fixed.T, val.T).T
    nx, ny = fixed.shape
    diag = np.zeros((nx, ny))
    diag[:-1, :] += gh
    diag[1:, :] += gh
    diag[:, :-1] += gv
    diag[:, 1:] += gv
    diag[fixed] = 1.0
    known = fixed * val                             # known potentials move to the right side
    y = np.zeros((nx, ny))
    y[:-1, :] += gh * known[1:, :]
    y[1:, :] += gh * known[:-1, :]
    y[:, :-1] += gv * known[:, 1:]
    y[:, 1:] += gv * known[:, :-1]
    y[fixed] = val[fixed]
    free = ~fixed
    cx = gh * (free[:-1, :] & free[1:, :])          # coupling of neighbouring columns
    cy = gv * (free[:, :-1] & free[:, 1:])          # coupling inside a column
    r, k = np.arange(ny), np.arange(ny - 1)
    inv = [None] * nx
    for i in range(nx):
        a = np.zeros((ny, ny))
        a[r, r] = diag[i]
        a[k, k + 1] = a[k + 1, k] = -cy[i]
        if i:
            c = cx[i - 1]
            a -= (c[:, None] * inv[i - 1]) * c[None, :]
            y[i] += c * (inv[i - 1] @ y[i - 1])
        inv[i] = np.linalg.inv(a)
    phi = np.zeros((nx, ny))
    phi[-1] = inv[-1] @ y[-1]
    for i in range(nx - 2, -1, -1):
        phi[i] = inv[i] @ (y[i] + cx[i] * phi[i + 1])
    return phi


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
        phi = _block_solve(gh, gv, self.fixed, self.val)
        d1, d2 = phi[1:, :] - phi[:-1, :], phi[:, 1:] - phi[:, :-1]
        return float(np.sum(gh * d1 * d1) + np.sum(gv * d2 * d2))  # = 2*energy = C*V^2 (V=1), in eps0 units

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
        pl1, pl2 = p.get("Plies1"), p.get("Plies2")
        ykeys = [0, yb, yt, top] + (_plies_keys(pl1, 0) if pl1 else []) + (_plies_keys(pl2, H1) if pl2 else [])
        xs = _mesh([0] + xedges, 0, X, hmin, max(top / 6, 0.5) / fine)
        ys = _mesh(ykeys, 0, top, hmin, max(top / 10, 0.25) / fine)
        pr = _Problem(xs, ys)
        pr.eps[:] = np.where(pr.CY < H1, _plies_eps(pr.CY, pl1, 0) if pl1 else Er1,
                             _plies_eps(pr.CY, pl2, H1) if pl2 else Er2)
        pr.fixed[:, 0] = pr.fixed[:, -1] = True
    else:
        C1, C2, CEr = p.get("C1", 0.0), p.get("C2", 0.0), p.get("CEr", 1.0)
        C3 = p.get("C3", C1)
        span = max(H1 + T, W1 + S)
        X = xc + W1 / 2 + 40 * span
        top = H1 + 40 * span
        mx = [xc - W1 / 2 - C2, xc + W1 / 2 + C2, xc - W2 / 2 - C2, xc + W2 / 2 + C2]
        pl1 = p.get("Plies1")
        ykeys = [0, yb, yt, yb + C1, yb + C3, yt + C2] + (_plies_keys(pl1, 0) if pl1 else [])
        xs = _mesh([0] + xedges + mx, 0, X, hmin, max(span / 2, 1) / fine, 1.3)
        ys = _mesh(ykeys, 0, top, hmin, max(span / 2, 1) / fine, 1.3)
        pr = _Problem(xs, ys)
        hw = _halfwidth(pr.CY, yb, T, W1, W2)
        band = np.where(pr.CX < S / 2, C3, C1) if diff else C1
        mask = (pr.CY > yb) & (pr.CY < yb + band)
        mask |= (pr.CY > yb) & (pr.CY < yt + C2) & (np.abs(pr.CX - xc) < hw + C2)
        pr.eps[:] = np.where(pr.CY < H1, _plies_eps(pr.CY, pl1, 0) if pl1 else Er1, np.where(mask, CEr, 1.0))
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
