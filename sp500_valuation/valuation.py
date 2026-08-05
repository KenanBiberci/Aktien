"""
valuation.py — Bewertungsmethoden (Schritt 3-5).

Enthält:
- abgeleitete Größen je Zeile (net_debt, sps, kgv, ...)
- Annahmen je Aktie (CAPM r, WACC, g1)
- Sektor-Median-Multiplikatoren
- die 9 benannten Bewertungsmethoden (M1..M9)
- ergänzende Kennzahlen (PVGO, value_creation)
- Blend (Median) + Buy/Hold/Sell-Signal

Jede Methode liefert einen fairen Preis je Aktie oder NaN (fehlende/ungültige Inputs).
Guards: Division durch 0, r <= g, fehlende Felder -> NaN.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any

import numpy as np
import pandas as pd

NaN = float("nan")


# =============================================================================
# Kleine numerische Guards
# =============================================================================
def _num(value: Any) -> float:
    """value -> float; None/nicht-zahl -> NaN."""
    if value is None:
        return NaN
    try:
        out = float(value)
    except (TypeError, ValueError):
        return NaN
    return out


def is_finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _pos(x: float) -> bool:
    """endlich UND > 0."""
    return is_finite(x) and x > 0


# =============================================================================
# Abgeleitete Größen (Schritt 2 Ende)
# =============================================================================
def derive_row_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Ergänzt eine Rohdatenzeile um abgeleitete Kennzahlen."""
    price = _num(row.get("price"))
    eps_ttm = _num(row.get("eps_ttm"))
    eps_fwd = _num(row.get("eps_fwd"))
    dps = _num(row.get("dps"))
    shares = _num(row.get("shares"))
    total_debt = _num(row.get("total_debt"))
    cash = _num(row.get("cash"))
    revenue = _num(row.get("revenue"))
    bvps = _num(row.get("book_value_ps"))

    if dps is None or math.isnan(dps):
        dps = 0.0

    net_debt = (total_debt - cash) if (is_finite(total_debt) and is_finite(cash)) else NaN
    sps = (revenue / shares) if (is_finite(revenue) and _pos(shares)) else NaN
    kgv_ttm = (price / eps_ttm) if (is_finite(price) and _pos(eps_ttm)) else NaN
    kgv_fwd = (price / eps_fwd) if (is_finite(price) and _pos(eps_fwd)) else NaN
    div_yield = (dps / price) if (_pos(price)) else NaN
    payout = (dps / eps_fwd) if (_pos(eps_fwd)) else NaN

    row["dps"] = dps
    row["net_debt"] = net_debt
    row["sps"] = sps
    row["bvps"] = bvps
    row["kgv_ttm"] = kgv_ttm
    row["kgv_fwd"] = kgv_fwd
    row["div_yield"] = div_yield
    row["payout"] = payout

    # Währungs-Mismatch: Kurs (Notierungswährung) vs. Bilanzwährung. Trifft v. a.
    # ADRs (Kurs USD, Umsatz/EBITDA in JPY/CNY/TWD). Dann sind umsatz-/EBITDA-
    # basierte Kennzahlen (sps, EV/EBITDA, DCF) nicht direkt vergleichbar.
    fin = row.get("financial_currency")
    nat = row.get("currency_native")
    row["fx_mismatch"] = bool(fin and nat and str(fin) != str(nat))
    return row


# =============================================================================
# Annahmen je Aktie (Schritt 3)
# =============================================================================
def cost_of_equity(beta: float, cfg: dict[str, Any]) -> float:
    """CAPM: r = rf + beta * ERP. beta fehlt -> beta = 1."""
    rf = float(cfg["risk_free_rate"])
    erp = float(cfg["equity_risk_premium"])
    b = beta if is_finite(beta) else 1.0
    return rf + b * erp


def wacc(row: dict[str, Any], r: float, cfg: dict[str, Any]) -> float:
    """WACC für DCF. Falls D~0 -> wacc ~ r."""
    rf = float(cfg["risk_free_rate"])
    tax = float(cfg["default_tax"])
    price = _num(row.get("price"))
    shares = _num(row.get("shares"))
    total_debt = _num(row.get("total_debt"))

    E = price * shares if (is_finite(price) and is_finite(shares)) else NaN
    D = max(total_debt, 0.0) if is_finite(total_debt) else 0.0
    if not is_finite(E) or E <= 0:
        return r
    r_d = rf + 0.015
    denom = E + D
    return (E / denom) * r + (D / denom) * r_d * (1.0 - tax)


def stage1_growth(row: dict[str, Any], cfg: dict[str, Any]) -> float:
    """g1 = clip(ROE * (1 - payout), terminal_growth, 0.20)."""
    g = float(cfg["terminal_growth"])
    roe = _num(row.get("roe"))
    payout = _num(row.get("payout"))
    if not is_finite(roe) or not is_finite(payout):
        return g
    g_sustainable = roe * (1.0 - payout)
    return float(np.clip(g_sustainable, g, 0.20))


# =============================================================================
# Sektor-Median-Multiplikatoren (Schritt 3 Ende, Law-of-one-price)
# =============================================================================
def compute_sector_medians(df: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Je GICS-Sektor Median von kgv_fwd, P/S, P/B, EV/EBITDA.

    Erwartet Spalten: sector, kgv_fwd, price, sps, bvps, shares, net_debt, ebitda.
    """
    work = df.copy()

    def _mismatch(r: pd.Series) -> bool:
        return bool(r.get("fx_mismatch")) if "fx_mismatch" in r else False

    # Mismatch-Titel (Kurs- vs. Bilanzwährung) verzerren umsatz-/EBITDA-Ratios ->
    # aus den Sektor-Medianen für P/S und EV/EBITDA heraushalten.
    work["ps"] = work.apply(
        lambda r: NaN if _mismatch(r) else (
            (r["price"] / r["sps"]) if _pos(_num(r["sps"])) else NaN), axis=1
    )
    work["pb"] = work.apply(
        lambda r: (r["price"] / r["bvps"]) if _pos(_num(r["bvps"])) else NaN, axis=1
    )
    work["ev_ebitda"] = work.apply(
        lambda r: NaN if _mismatch(r) else _ev_ebitda_row(r), axis=1
    )

    medians: dict[str, dict[str, float]] = {}
    for sector, grp in work.groupby("sector"):
        medians[sector] = {
            "pe": _median_positive(grp["kgv_fwd"]),
            "ps": _median_positive(grp["ps"]),
            "pb": _median_positive(grp["pb"]),
            "ev_ebitda": _median_positive(grp["ev_ebitda"]),
        }
    return medians


def _ev_ebitda_row(r: pd.Series) -> float:
    price = _num(r["price"])
    shares = _num(r["shares"])
    net_debt = _num(r["net_debt"])
    ebitda = _num(r["ebitda"])
    if not (is_finite(price) and is_finite(shares) and is_finite(net_debt)):
        return NaN
    if not _pos(ebitda):
        return NaN
    ev = price * shares + net_debt
    return ev / ebitda


def _median_positive(series: pd.Series) -> float:
    vals = [float(x) for x in series if _pos(x)]
    if not vals:
        return NaN
    return float(median(vals))


def sector_multiple(
    sector: str,
    key: str,
    medians: dict[str, dict[str, float]],
    cfg: dict[str, Any],
) -> float:
    """Sektor-Median bevorzugt; nur falls fehlend -> globaler Fallback."""
    m = medians.get(sector, {}).get(key, NaN)
    if _pos(m):
        return m
    return float(cfg["fallback_multiples"][key])


# =============================================================================
# Schritt 4 — die 9 Bewertungsmethoden
# =============================================================================
def m1_gordon_growth(dps: float, r: float, g: float, payout: float, gate: float) -> float:
    """M1 Gordon-Growth (nur wenn dps>0, r>g, payout>=gate)."""
    if not (_pos(dps) and is_finite(r) and r > g and is_finite(payout) and payout >= gate):
        return NaN
    d1 = dps * (1 + g)
    return d1 / (r - g)


def m2_two_stage_ddm(
    dps: float, r: float, g1: float, g: float, n: int, payout: float, gate: float
) -> float:
    """M2 2-Stufen-DDM (nur wenn dps>0, r>g1, r>g, payout>=gate)."""
    if not (_pos(dps) and is_finite(r) and r > g1 and r > g
            and is_finite(payout) and payout >= gate):
        return NaN
    stage1 = dps * ((1 + g1) / (r - g1)) * (1 - ((1 + g1) / (1 + r)) ** n)
    terminal = (dps * (1 + g1) ** n * (1 + g) / (r - g)) / ((1 + r) ** n)
    return stage1 + terminal


def m3_justified_pe(eps_fwd: float, r: float, g: float, payout: float, gate: float) -> float:
    """M3 Fundamentales KGV (nur wenn payout>0, r>g, payout>=gate)."""
    if not (_pos(payout) and is_finite(r) and r > g and payout >= gate and is_finite(eps_fwd)):
        return NaN
    justified_pe = payout / (r - g)
    return justified_pe * eps_fwd


def m4_comparable_pe(sector_pe: float, eps_fwd: float) -> float:
    """M4 Comparable-KGV."""
    if not (_pos(sector_pe) and _pos(eps_fwd)):
        return NaN
    return sector_pe * eps_fwd


def m5_price_sales(sector_ps: float, sps: float) -> float:
    """M5 P/S."""
    if not (_pos(sector_ps) and _pos(sps)):
        return NaN
    return sector_ps * sps


def m6_price_book(sector_pb: float, bvps: float) -> float:
    """M6 P/B."""
    if not (_pos(sector_pb) and _pos(bvps)):
        return NaN
    return sector_pb * bvps


def m7_ev_ebitda(sector_ev: float, ebitda: float, net_debt: float, shares: float) -> float:
    """M7 EV/EBITDA -> Equity je Aktie."""
    if not (_pos(sector_ev) and _pos(ebitda) and is_finite(net_debt) and _pos(shares)):
        return NaN
    ev = sector_ev * ebitda
    return (ev - net_debt) / shares


def m8_dcf_fcff(
    operating_cashflow: float,
    capex: float,
    wacc_val: float,
    g: float,
    net_debt: float,
    shares: float,
) -> float:
    """M8 DCF/FCFF (wachsende Perpetuität; guard wacc>g)."""
    if not (is_finite(operating_cashflow) and is_finite(capex)):
        return NaN
    fcff = operating_cashflow - capex  # capex ist bei yfinance i.d.R. negativ
    if not (_pos(fcff) and is_finite(wacc_val) and wacc_val > g and _pos(shares)
            and is_finite(net_debt)):
        return NaN
    firm_value = fcff * (1 + g) / (wacc_val - g)
    return (firm_value - net_debt) / shares


def m9_asset_based(bvps: float) -> float:
    """M9 Asset-based (grobe Näherung, nur Info-Spalte)."""
    return bvps if is_finite(bvps) else NaN


# =============================================================================
# Ergänzende Kennzahlen
# =============================================================================
def supplementary_metrics(row: dict[str, Any], r: float) -> dict[str, Any]:
    eps_ttm = _num(row.get("eps_ttm"))
    price = _num(row.get("price"))
    roe = _num(row.get("roe"))

    no_growth_value = (eps_ttm / r) if (is_finite(eps_ttm) and _pos(r)) else NaN
    pvgo = (price - no_growth_value) if (is_finite(price) and is_finite(no_growth_value)) else NaN
    pvgo_pct = (pvgo / price) if (is_finite(pvgo) and _pos(price)) else NaN
    value_creation = "Ja" if (is_finite(roe) and is_finite(r) and roe > r) else "Nein"

    return {
        "no_growth_value": no_growth_value,
        "pvgo": pvgo,
        "pvgo_pct": pvgo_pct,
        "value_creation": value_creation,
    }


# =============================================================================
# Schritt 5 — Blend + Signal
# =============================================================================
def blend_and_signal(
    methods_map: dict[str, float],
    price: float,
    target: float,
    payout: float,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Cross-Check-Blend (Median) + Buy/Hold/Sell-Signal.

    methods_map: {'m1':..,'m2':..,'m3':..,'m4':..,'m5':..,'m6':..,'m7':..,'m8':..}
    M1-M3 nur, wenn payout >= gate; M4-M8 immer (falls nicht NaN).
    """
    gate = float(cfg["ddm_payout_gate"])
    th = cfg["signal_thresholds"]
    strong_buy = float(th["strong_buy"])
    buy = float(th["buy"])
    hold_floor = float(th["hold_floor"])

    use_ddm = is_finite(payout) and payout >= gate
    selected: list[float] = []
    if use_ddm:
        selected += [methods_map.get("m1", NaN), methods_map.get("m2", NaN),
                     methods_map.get("m3", NaN)]
    selected += [methods_map.get(k, NaN) for k in ("m4", "m5", "m6", "m7", "m8")]
    selected = [x for x in selected if is_finite(x) and x > 0]

    n_methods = len(selected)
    blended_fair_value = float(median(selected)) if selected else NaN
    divergence = (max(selected) / min(selected) - 1) if len(selected) >= 2 else NaN

    blended_upside = (blended_fair_value / price - 1) if (
        is_finite(blended_fair_value) and _pos(price)) else NaN
    consensus_upside = (target / price - 1) if (is_finite(target) and _pos(price)) else NaN

    upsides = [u for u in (blended_upside, consensus_upside) if is_finite(u)]
    avg_upside = float(np.mean(upsides)) if upsides else NaN

    # Signal
    if not is_finite(avg_upside) or (n_methods < 2 and not is_finite(consensus_upside)):
        signal = "N/A – Datenlücke"
    elif avg_upside >= strong_buy and n_methods >= 3:
        signal = "STRONG BUY"
    elif avg_upside >= buy:
        signal = "BUY"
    elif avg_upside >= hold_floor:
        signal = "HOLD"
    else:
        signal = "REDUCE"

    return {
        "n_methods": n_methods,
        "blended_fair_value": blended_fair_value,
        "blended_upside": blended_upside,
        "consensus_upside": consensus_upside,
        "avg_upside": avg_upside,
        "divergence": divergence,
        "signal": signal,
    }


# =============================================================================
# Orchestrierung je Zeile
# =============================================================================
def value_row(
    row: dict[str, Any],
    medians: dict[str, dict[str, float]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Berechnet alle Methoden + Blend + Signal für eine (bereits abgeleitete) Zeile."""
    g = float(cfg["terminal_growth"])
    n = int(cfg["stage1_years"])
    gate = float(cfg["ddm_payout_gate"])

    beta = _num(row.get("beta"))
    r = cost_of_equity(beta, cfg)
    wacc_val = wacc(row, r, cfg)
    g1 = stage1_growth(row, cfg)

    sector = row.get("sector", "")
    dps = _num(row.get("dps"))
    eps_fwd = _num(row.get("eps_fwd"))
    payout = _num(row.get("payout"))
    sps = _num(row.get("sps"))
    bvps = _num(row.get("bvps"))
    ebitda = _num(row.get("ebitda"))
    net_debt = _num(row.get("net_debt"))
    shares = _num(row.get("shares"))
    price = _num(row.get("price"))
    target = _num(row.get("target"))

    sector_pe = sector_multiple(sector, "pe", medians, cfg)
    sector_ps = sector_multiple(sector, "ps", medians, cfg)
    sector_pb = sector_multiple(sector, "pb", medians, cfg)
    sector_ev = sector_multiple(sector, "ev_ebitda", medians, cfg)

    methods_map = {
        "m1": m1_gordon_growth(dps, r, g, payout, gate),
        "m2": m2_two_stage_ddm(dps, r, g1, g, n, payout, gate),
        "m3": m3_justified_pe(eps_fwd, r, g, payout, gate),
        "m4": m4_comparable_pe(sector_pe, eps_fwd),
        "m5": m5_price_sales(sector_ps, sps),
        "m6": m6_price_book(sector_pb, bvps),
        "m7": m7_ev_ebitda(sector_ev, ebitda, net_debt, shares),
        "m8": m8_dcf_fcff(
            _num(row.get("operating_cashflow")), _num(row.get("capex")),
            wacc_val, g, net_debt, shares,
        ),
        "m9": m9_asset_based(bvps),
    }

    # Bei Währungs-Mismatch (Kurs vs. Bilanzwährung, z. B. ADRs) sind die
    # umsatz-/EBITDA-/Cashflow-basierten Methoden ungültig -> NaN. Die eps-/
    # dividendenbasierten (M1-M4) und M6/M9 bleiben (in Notierungswährung).
    if row.get("fx_mismatch"):
        methods_map["m5"] = NaN
        methods_map["m7"] = NaN
        methods_map["m8"] = NaN

    result = dict(row)
    result["r"] = r
    result["wacc"] = wacc_val
    result["g1"] = g1
    for key, val in methods_map.items():
        result[key] = val

    result.update(supplementary_metrics(row, r))
    result.update(blend_and_signal(methods_map, price, target, payout, cfg))
    return result
