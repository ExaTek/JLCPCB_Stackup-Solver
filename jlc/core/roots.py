"""Brent's root finder: the algorithm of scipy.optimize.brentq, step for step, so the program
does not carry scipy (about 67 MB) for this one function."""
import math

RTOL = 8.881784197001252e-16


def brentq(f, a, b, xtol=2e-12, rtol=RTOL, maxiter=100):
    """Root of f in [a, b]. ValueError if f has the same sign at both ends or gives NaN."""

    def call(x):
        y = f(x)
        if math.isnan(y):
            raise ValueError(f"the function value at x={x} is NaN")
        return y

    xpre, xcur = a, b
    fpre, fcur = call(xpre), call(xcur)
    if fpre == 0:
        return xpre
    if fcur == 0:
        return xcur
    if math.copysign(1, fpre) == math.copysign(1, fcur):
        raise ValueError("f(a) and f(b) must have different signs")
    xblk, fblk, spre, scur = 0.0, 0.0, 0.0, 0.0
    for _ in range(maxiter):
        if fpre != 0 and fcur != 0 and math.copysign(1, fpre) != math.copysign(1, fcur):
            xblk, fblk = xpre, fpre
            spre = scur = xcur - xpre
        if abs(fblk) < abs(fcur):
            xpre, xcur, xblk = xcur, xblk, xcur
            fpre, fcur, fblk = fcur, fblk, fcur
        delta = (xtol + rtol * abs(xcur)) / 2
        sbis = (xblk - xcur) / 2
        if fcur == 0 or abs(sbis) < delta:
            return xcur
        if abs(spre) > delta and abs(fcur) < abs(fpre):
            if xpre == xblk:                    # interpolate
                stry = -fcur * (xcur - xpre) / (fcur - fpre)
            else:                               # extrapolate
                dpre = (fpre - fcur) / (xpre - xcur)
                dblk = (fblk - fcur) / (xblk - xcur)
                stry = -fcur * (fblk * dblk - fpre * dpre) / (dblk * dpre * (fblk - fpre))
            if 2 * abs(stry) < min(abs(spre), 3 * abs(sbis) - delta):
                spre, scur = scur, stry         # good short step
            else:
                spre = scur = sbis              # bisect
        else:
            spre = scur = sbis                  # bisect
        xpre, fpre = xcur, fcur
        xcur += scur if abs(scur) > delta else (delta if sbis > 0 else -delta)
        fcur = call(xcur)
    raise RuntimeError(f"no convergence in {maxiter} iterations")
