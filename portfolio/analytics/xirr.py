"""Annualised money-weighted return (XIRR). Ported verbatim from legacy analytics."""

from __future__ import annotations

from datetime import date


def _npv(rate: float, days: list[int], amounts: list[float]) -> float:
    return sum(a / (1 + rate) ** (d / 365.0) for a, d in zip(amounts, days, strict=True))


def xirr(cash_flows: list[tuple[date, float]], tol: float = 1e-7) -> float | None:
    """XIRR in percent. amount < 0 = capital in, > 0 = capital out / terminal value.

    Newton-Raphson from several guesses, bisection in (-0.95, 10) as fallback.
    None when all flows share a sign or no root exists in range.
    """
    if not cash_flows or len(cash_flows) < 2:
        return None
    cash_flows = sorted(cash_flows, key=lambda x: x[0])
    t0 = cash_flows[0][0]
    days = [(d - t0).days for d, _ in cash_flows]
    amounts = [float(a) for _, a in cash_flows]
    if all(a >= 0 for a in amounts) or all(a <= 0 for a in amounts):
        return None

    for guess in (0.10, 0.0, -0.10, 0.30, -0.30, 0.50):
        rate = guess
        for _ in range(80):
            try:
                npv = _npv(rate, days, amounts)
                dnpv = sum(-d / 365.0 * a / (1 + rate) ** (d / 365.0 + 1)
                           for a, d in zip(amounts, days, strict=True))
            except (OverflowError, ZeroDivisionError):
                break
            if abs(dnpv) < 1e-12:
                break
            new_rate = rate - npv / dnpv
            if new_rate <= -0.999:
                new_rate = -0.99
            if abs(new_rate - rate) < tol:
                return round(new_rate * 100, 2)
            rate = new_rate

    lo, hi = -0.95, 10.0
    try:
        f_lo, f_hi = _npv(lo, days, amounts), _npv(hi, days, amounts)
    except (OverflowError, ZeroDivisionError):
        return None
    if f_lo * f_hi > 0:
        return None
    for _ in range(120):
        mid = (lo + hi) / 2
        try:
            f_mid = _npv(mid, days, amounts)
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(f_mid) < tol or abs(hi - lo) < tol:
            return round(mid * 100, 2)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return round(((lo + hi) / 2) * 100, 2)
