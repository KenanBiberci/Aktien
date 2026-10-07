"""
backtest.py — historischer 12-Monats-Backtest je Aktie.

Idee: Wenn man am Anfang eines Jahres gekauft und 12 Monate gehalten hätte —
welche Rendite hätte das jahresweise über die letzten ~20 Jahre gebracht?
Daraus:
- annual returns je Jahr (für das Säulendiagramm),
- win_rate (Trefferquote: Anteil Jahre mit positiver 12M-Rendite) — die
  „Wahrscheinlichkeit", nach der im Screener sortiert/gefiltert wird,
- avg_return (Ø 12M-Rendite).

Renditen sind Verhältnisse -> währungsneutral (keine FX nötig). Kurse werden
gebündelt via yfinance geholt (auto_adjust=True -> Total-Return-nah).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def annual_returns_from_prices(close: pd.Series) -> tuple[list[int], list[float]]:
    """Aus einer (monatlichen) Kursreihe die jahresweisen 12-Monats-Renditen.

    Nimmt je Kalenderjahr den ersten verfügbaren Kurs und bildet die Rendite
    zum ersten Kurs des Folgejahres (nur bei aufeinanderfolgenden Jahren).
    Rückgabe: (start_jahre, renditen) — z. B. Jahr 2019 = Rendite 2019->2020.
    """
    s = close.dropna()
    if s.empty:
        return [], []
    s = s.sort_index()
    by_year = s.groupby(s.index.year).first()
    years = list(by_year.index)
    out_years: list[int] = []
    returns: list[float] = []
    for i in range(len(years) - 1):
        y0, y1 = int(years[i]), int(years[i + 1])
        p0, p1 = float(by_year.iloc[i]), float(by_year.iloc[i + 1])
        if y1 == y0 + 1 and p0 > 0 and np.isfinite(p0) and np.isfinite(p1):
            out_years.append(y0)
            returns.append(p1 / p0 - 1.0)
    return out_years, returns


def summarize(years: list[int], returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {"years": [], "returns": [], "win_rate": None, "avg_return": None}
    wins = sum(1 for r in returns if r > 0)
    return {
        "years": years,
        "returns": [round(float(r), 4) for r in returns],
        "win_rate": round(wins / len(returns), 4),
        "avg_return": round(float(np.mean(returns)), 4),
    }


def short_term_from_prices(close: pd.Series, vol_window: int = 60) -> dict[str, Any]:
    """Kurzfrist-Kennzahlen aus einer (monatlichen) Kursreihe.

    - mom_1m / mom_3m: aktueller Aufwärts-Schwung (letzter 1-/3-Monats-Verlauf)
    - mret_p90: typisches 'gutes Monatsplus' (90. Perzentil der Monatsrenditen)
    - mret_max: größtes Monatsplus der Historie
    - mret_vol: Monats-Volatilität (wie stark der Titel schwankt)
    - dist_high: Abstand zum 12-Monats-Hoch (nahe 0 = nahe Hoch = Schwung)
    """
    s = close.dropna().sort_index()
    if len(s) < 4:
        return {}
    mret = s.pct_change().dropna()
    if mret.empty:
        return {}
    recent = mret.iloc[-vol_window:] if len(mret) > vol_window else mret
    last = float(s.iloc[-1])
    hi12 = float(s.iloc[-12:].max()) if len(s) >= 12 else float(s.max())

    def _r(x: float) -> float | None:
        return round(float(x), 4) if np.isfinite(x) else None

    return {
        "mom_1m": _r(s.iloc[-1] / s.iloc[-2] - 1) if len(s) >= 2 else None,
        "mom_3m": _r(s.iloc[-1] / s.iloc[-4] - 1) if len(s) >= 4 else None,
        "mret_p90": _r(recent.quantile(0.90)),
        "mret_max": _r(mret.max()),
        "mret_vol": _r(recent.std()),
        "dist_high": _r(last / hi12 - 1) if hi12 > 0 else None,
    }


def _close_series(df: pd.DataFrame, ticker: str) -> pd.Series | None:
    try:
        if isinstance(df.columns, pd.MultiIndex):
            if ticker in df.columns.get_level_values(0):
                sub = df[ticker]
                return sub["Close"] if "Close" in sub.columns else None
            return None
        return df["Close"] if "Close" in df.columns else None
    except Exception:  # noqa: BLE001
        return None


def compute_annual_backtests(
    tickers: list[str],
    years: int = 20,
    chunk: int = 80,
    sleep: float = 1.0,
    vol_window: int = 60,
) -> dict[str, dict[str, Any]]:
    """Für alle Ticker: jahresweise 12M-Renditen + Kurzfrist-Kennzahlen (1 Download)."""
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        log.warning("yfinance für Backtest nicht verfügbar: %s", exc)
        return {}

    start = f"{datetime.now().year - years - 1}-01-01"
    out: dict[str, dict[str, Any]] = {}
    total = len(tickers)
    for i in range(0, total, chunk):
        part = tickers[i:i + chunk]
        try:
            df = yf.download(part, start=start, interval="1mo", auto_adjust=True,
                             progress=False, group_by="ticker", threads=True)
        except Exception as exc:  # noqa: BLE001 — Teil-Fehler überspringen
            log.debug("Backtest-Download-Chunk %d fehlgeschlagen: %s", i, exc)
            continue
        if df is None or df.empty:
            continue
        for t in part:
            s = _close_series(df, t)
            if s is None:
                continue
            yrs, rets = annual_returns_from_prices(s)
            yrs, rets = yrs[-years:], rets[-years:]
            entry: dict[str, Any] = summarize(yrs, rets) if rets else {}
            st = short_term_from_prices(s, vol_window=vol_window)
            if st:
                entry.update(st)
            if entry:
                out[t] = entry
        log.info("Backtest: %d/%d Ticker verarbeitet.", min(i + chunk, total), total)
        time.sleep(sleep)
    return out
