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

    net_income = _num(row.get("net_income"))
    net_margin = (net_income / revenue) if (is_finite(net_income) and _pos(revenue)) else NaN
    equity = (bvps * shares) if (is_finite(bvps) and is_finite(shares)) else NaN

    row["dps"] = dps
    row["net_debt"] = net_debt
    row["sps"] = sps
    row["bvps"] = bvps
    row["kgv_ttm"] = kgv_ttm
    row["kgv_fwd"] = kgv_fwd
    row["div_yield"] = div_yield
    row["payout"] = payout
    row["net_margin"] = net_margin
    row["equity"] = equity

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
# Peer-Multiplikatoren (Sub-Industry -> Sektor -> global; Law-of-one-price)
# =============================================================================
PEER_METRICS = ("pe", "ps", "pb", "ev_ebitda", "net_margin")


def compute_peer_stats(df: pd.DataFrame) -> dict[str, dict[str, dict[str, float]]]:
    """Peer-Mediane je Gruppierungsebene ('sub_industry', 'sector').

    Rückgabe: {level: {gruppe: {'pe','ps','pb','ev_ebitda','net_margin','n'}}}.
    Mismatch-Titel (Kurs- vs. Bilanzwährung) werden aus P/S und EV/EBITDA
    herausgehalten (verzerren die Ratios).
    """
    work = df.copy()

    def _mismatch(r: pd.Series) -> bool:
        return bool(r.get("fx_mismatch")) if "fx_mismatch" in r else False

    work["_ps"] = work.apply(
        lambda r: NaN if _mismatch(r) else (
            (r["price"] / r["sps"]) if _pos(_num(r["sps"])) else NaN), axis=1)
    work["_pb"] = work.apply(
        lambda r: (r["price"] / r["bvps"]) if _pos(_num(r["bvps"])) else NaN, axis=1)
    work["_ev"] = work.apply(
        lambda r: NaN if _mismatch(r) else _ev_ebitda_row(r), axis=1)

    stats: dict[str, dict[str, dict[str, float]]] = {}
    for level in ("sub_industry", "sector"):
        if level not in work.columns:
            stats[level] = {}
            continue
        level_stats: dict[str, dict[str, float]] = {}
        for grp_name, grp in work.groupby(level):
            key = str(grp_name).strip()
            if not key or key.lower() in ("nan", "none", ""):
                continue
            level_stats[key] = {
                "pe": _median_positive(grp["kgv_fwd"]),
                "ps": _median_positive(grp["_ps"]),
                "pb": _median_positive(grp["_pb"]),
                "ev_ebitda": _median_positive(grp["_ev"]),
                "net_margin": _median_finite(grp.get("net_margin", pd.Series(dtype=float))),
                "n": float(len(grp)),
            }
        stats[level] = level_stats
    return stats


def peer_stat(metric: str, row: dict[str, Any], stats: dict[str, Any],
              cfg: dict[str, Any]) -> float:
    """Peer-Wert für `metric`: Sub-Industry (>= min_peers) -> Sektor -> global.

    Für Multiplikatoren fällt der globale Fallback auf `fallback_multiples`;
    für 'net_margin' gibt es keinen globalen Fallback (-> NaN).
    """
    min_peers = int(cfg.get("peer", {}).get("min_peers", 5))
    sub = str(row.get("sub_industry") or "").strip()
    sec = str(row.get("sector") or "").strip()

    for level, name in (("sub_industry", sub), ("sector", sec)):
        grp = stats.get(level, {}).get(name)
        if grp and grp.get("n", 0) >= min_peers:
            val = grp.get(metric, NaN)
            if metric == "net_margin":
                if is_finite(val):
                    return float(val)
            elif _pos(val):
                return float(val)
    # globaler Fallback nur für Multiplikatoren
    if metric in ("pe", "ps", "pb", "ev_ebitda"):
        return float(cfg["fallback_multiples"][metric])
    return NaN


def _ev_ebitda_row(r: Any) -> float:
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
    return float(median(vals)) if vals else NaN


def _median_finite(series: pd.Series) -> float:
    vals = [float(x) for x in series if is_finite(x)]
    return float(median(vals)) if vals else NaN


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


def m5_price_sales(peer_ps: float, sps: float, net_margin: float,
                   peer_margin: float, cfg: dict[str, Any]) -> float:
    """M5 P/S — margen-adjustiert (fairer P/S = Peer-P/S x eigene/Peer-Marge).

    Verhindert Überschätzung margenschwacher Titel (z. B. Kliniken): eine niedrige
    Nettomarge zieht den fairen P/S nach unten. Ohne verwertbare Marge -> NaN.
    """
    if not (_pos(peer_ps) and _pos(sps)):
        return NaN
    ea = cfg.get("method_eligibility", {})
    if not ea.get("ps_margin_adjust", True):
        return peer_ps * sps
    if ea.get("ps_skip_if_margin_nonpositive", True) and not _pos(net_margin):
        return NaN                      # negative/fehlende Marge -> P/S nicht sinnvoll
    if not _pos(peer_margin):
        return NaN                      # ohne Peer-Marge nicht adjustierbar
    fair_ps = peer_ps * (net_margin / peer_margin)
    return fair_ps * sps if _pos(fair_ps) else NaN


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


def m8_dcf_two_stage(
    operating_cashflow: float,
    capex: float,
    fcff_cagr: float,
    wacc_val: float,
    net_debt: float,
    shares: float,
    cfg: dict[str, Any],
) -> float:
    """M8 DCF/FCFF — 2-Stufen: explizite Phase (gedecktes Wachstum) + Terminal Value.

    FCFF = OperatingCF - |CapEx|. Ohne FCFF-Historie (kein Wachstum schätzbar) -> NaN
    (keine Notannahmen). WACC-g-Abstand wird erzwungen; Wachstum hart gedeckelt.
    """
    if not (is_finite(operating_cashflow) and is_finite(capex)):
        return NaN
    fcff = operating_cashflow - abs(capex)
    if not (_pos(fcff) and _pos(shares) and is_finite(net_debt)):
        return NaN
    if not is_finite(fcff_cagr):
        return NaN                      # keine Historie -> Wachstum unbekannt -> NaN

    d = cfg.get("dcf", {})
    years = int(d.get("explicit_years", 10))
    g1 = min(float(fcff_cagr), float(d.get("stage1_growth_cap", 0.12)))
    g1 = max(g1, -0.05)                  # extreme negative Historien bändigen
    gt = min(float(cfg["terminal_growth"]), float(d.get("terminal_growth_max", 0.025)))
    w = max(_num(wacc_val), gt + float(d.get("min_wacc_minus_g", 0.04)))

    pv_explicit = sum(fcff * (1 + g1) ** t / (1 + w) ** t for t in range(1, years + 1))
    fcff_t = fcff * (1 + g1) ** years
    tv = fcff_t * (1 + gt) / (w - gt)
    pv_terminal = tv / (1 + w) ** years
    firm_value = pv_explicit + pv_terminal
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
# Schritt 5 — Blend (Kappung + Trimmen) + Konfidenz + Signal
# =============================================================================
METHOD_LABELS = {
    "m1": "M1 Gordon", "m2": "M2 DDM", "m3": "M3 Fund.KGV", "m4": "M4 KGV",
    "m5": "M5 P/S", "m6": "M6 P/B", "m7": "M7 EV/EBITDA", "m8": "M8 DCF",
}


def blend_and_signal(
    methods_map: dict[str, float],
    reasons: dict[str, str],
    price: float,
    target: float,
    payout: float,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Blend über getrimmtes Methodenset + Konfidenz + gedeckeltes Signal.

    Ablauf: Eignung (M1-M3 nur bei payout>=gate) -> Hard-Drop von Ausreißern
    [clamp.lower_x, clamp.upper_x]*Kurs -> Trim (±pct_band um Median, min_keep) ->
    Median = Blended Fair Value; Divergenz auf getrimmtem Set; Konfidenz + Signal-Gate.
    """
    gate = float(cfg["ddm_payout_gate"])
    th = cfg["signal_thresholds"]
    strong_buy, buy, hold_floor = (float(th["strong_buy"]), float(th["buy"]),
                                   float(th["hold_floor"]))
    clamp = cfg.get("clamp", {})
    lo, hi = float(clamp.get("lower_x", 0.3)), float(clamp.get("upper_x", 3.0))
    tcfg = cfg.get("trim", {})
    pct_band = float(tcfg.get("pct_band", 0.5))
    min_keep = int(tcfg.get("min_keep", 2))
    conf = cfg.get("confidence", {})

    use_ddm = is_finite(payout) and payout >= gate
    blend_keys = (["m1", "m2", "m3"] if use_ddm else []) + ["m4", "m5", "m6", "m7", "m8"]
    if not use_ddm:
        for k in ("m1", "m2", "m3"):
            reasons.setdefault(k, "Dividende zu gering (DDM-Gate)")

    # gültige Kandidaten
    candidates = {k: methods_map[k] for k in blend_keys
                  if is_finite(methods_map.get(k, NaN)) and methods_map[k] > 0}

    # 1) Hard-Drop von Ausreißern relativ zum Kurs
    if is_finite(price) and price > 0:
        for k in list(candidates):
            if not (lo * price <= candidates[k] <= hi * price):
                reasons[k] = f"Ausreißer (>{hi:g}x / <{lo:g}x Kurs)"
                del candidates[k]

    # 2) Trim: nur Werte nahe am Median behalten (mind. min_keep)
    kept = dict(candidates)
    if len(candidates) > min_keep:
        med0 = median(candidates.values())
        near = {k: v for k, v in candidates.items()
                if med0 <= 0 or abs(v - med0) <= pct_band * med0}
        if len(near) < min_keep:
            near = dict(sorted(candidates.items(), key=lambda kv: abs(kv[1] - med0))[:min_keep])
        for k in candidates:
            if k not in near:
                reasons[k] = "getrimmt (weit vom Median)"
        kept = near

    final = list(kept.values())
    n_final = len(final)
    blended_fair_value = float(median(final)) if final else NaN
    divergence = (max(final) / min(final) - 1) if n_final >= 2 else NaN

    blended_upside = (blended_fair_value / price - 1) if (
        is_finite(blended_fair_value) and _pos(price)) else NaN
    consensus_upside = (target / price - 1) if (is_finite(target) and _pos(price)) else NaN
    upsides = [u for u in (blended_upside, consensus_upside) if is_finite(u)]
    avg_upside = float(np.mean(upsides)) if upsides else NaN

    # Konfidenz
    high_div = float(conf.get("divergence_high_conf", 0.35))
    low_div = float(conf.get("divergence_low_conf", 0.75))
    min_high = int(conf.get("min_methods_high_conf", 4))
    if is_finite(divergence) and divergence <= high_div and n_final >= min_high:
        confidence = "Hoch"
    elif (is_finite(divergence) and divergence >= low_div) or n_final < 2:
        confidence = "Niedrig"
    else:
        confidence = "Mittel"

    # Basis-Signal aus Ø-Upside
    if not is_finite(avg_upside) or (n_final < 2 and not is_finite(consensus_upside)):
        signal = "N/A – Datenlücke"
    elif avg_upside >= strong_buy and n_final >= 3:
        signal = "STRONG BUY"
    elif avg_upside >= buy:
        signal = "BUY"
    elif avg_upside >= hold_floor:
        signal = "HOLD"
    else:
        signal = "REDUCE"

    # Signal-Gate: hohe Divergenz oder niedrige Konfidenz -> nie STRONG BUY
    block = float(conf.get("block_strong_buy_above", 0.60))
    if signal == "STRONG BUY" and is_finite(divergence) and divergence > block:
        signal = "BUY"
    if confidence == "Niedrig" and signal not in ("N/A – Datenlücke",):
        if signal == "STRONG BUY":
            signal = "BUY"
        signal = f"{signal} (niedrige Konfidenz)"

    used_methods = ", ".join(METHOD_LABELS.get(k, k) for k in kept)
    dropped_methods = "; ".join(
        f"{METHOD_LABELS.get(k, k)}: {reasons[k]}"
        for k in ("m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8")
        if k in reasons and k not in kept)

    return {
        "n_methods": n_final,
        "blended_fair_value": blended_fair_value,
        "blended_upside": blended_upside,
        "consensus_upside": consensus_upside,
        "avg_upside": avg_upside,
        "divergence": divergence,
        "confidence": confidence,
        "used_methods": used_methods,
        "dropped_methods": dropped_methods,
        "signal": signal,
    }


# =============================================================================
# Orchestrierung je Zeile
# =============================================================================
def value_row(
    row: dict[str, Any],
    stats: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Berechnet alle Methoden + Blend + Signal für eine (bereits abgeleitete) Zeile.

    `stats` sind die Peer-Statistiken aus compute_peer_stats (Sub-Industry/Sektor).
    """
    g = float(cfg["terminal_growth"])
    n = int(cfg["stage1_years"])
    gate = float(cfg["ddm_payout_gate"])
    req_eq = set(cfg.get("method_eligibility", {}).get("require_positive_equity", []))

    beta = _num(row.get("beta"))
    r = cost_of_equity(beta, cfg)
    wacc_val = wacc(row, r, cfg)
    g1 = stage1_growth(row, cfg)

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
    net_margin = _num(row.get("net_margin"))
    equity = _num(row.get("equity"))
    mismatch = bool(row.get("fx_mismatch"))
    reasons: dict[str, str] = {}

    peer_pe = peer_stat("pe", row, stats, cfg)
    peer_ps = peer_stat("ps", row, stats, cfg)
    peer_pb = peer_stat("pb", row, stats, cfg)
    peer_ev = peer_stat("ev_ebitda", row, stats, cfg)
    peer_margin = peer_stat("net_margin", row, stats, cfg)

    equity_ok = _pos(equity)

    methods_map = {
        "m1": m1_gordon_growth(dps, r, g, payout, gate),
        "m2": m2_two_stage_ddm(dps, r, g1, g, n, payout, gate),
        "m3": m3_justified_pe(eps_fwd, r, g, payout, gate),
        "m4": m4_comparable_pe(peer_pe, eps_fwd),
        "m5": NaN if mismatch else m5_price_sales(peer_ps, sps, net_margin, peer_margin, cfg),
        "m6": (m6_price_book(peer_pb, bvps)
               if (equity_ok or "pb" not in req_eq) else NaN),
        "m7": NaN if mismatch else m7_ev_ebitda(peer_ev, ebitda, net_debt, shares),
        "m8": NaN if mismatch else m8_dcf_two_stage(
            _num(row.get("operating_cashflow")), _num(row.get("capex")),
            _num(row.get("fcff_cagr")), wacc_val, net_debt, shares, cfg),
        "m9": (m9_asset_based(bvps) if (equity_ok or "asset" not in req_eq) else NaN),
    }

    # Gründe für nicht anwendbare Methoden (für Transparenz in App/PDF/Excel)
    if mismatch:
        for k in ("m5", "m7", "m8"):
            reasons[k] = "Währungs-Mismatch (Kurs vs. Bilanz)"
    if not equity_ok and "pb" in req_eq and not is_finite(methods_map["m6"]):
        reasons["m6"] = "negatives/fehlendes Eigenkapital"
    if not is_finite(methods_map["m5"]) and "m5" not in reasons:
        reasons["m5"] = ("Marge <= 0 / P/S nicht adjustierbar"
                         if not _pos(net_margin) else "P/S nicht berechenbar")
    if not is_finite(methods_map["m8"]) and "m8" not in reasons:
        reasons["m8"] = "kein FCFF / keine Historie"

    result = dict(row)
    result["r"] = r
    result["wacc"] = wacc_val
    result["g1"] = g1
    for key, val in methods_map.items():
        result[key] = val

    result.update(supplementary_metrics(row, r))
    result.update(blend_and_signal(methods_map, reasons, price, target, payout, cfg))
    return result
