"""
fetch.py — Tickerliste (Schritt 1) und Datenabruf mit Caching/Retry/Rate-Limit (Schritt 2).

Primäre Datenquelle: yfinance (kein API-Key nötig).
Optional: Financial Modeling Prep (FMP) über Umgebungsvariable FMP_API_KEY
(bessere Forward-EPS/Analystenziele). Ohne Key -> nur yfinance.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

log = logging.getLogger(__name__)

# --- Pfade -------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "data" / "cache"

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
FALLBACK_CSV = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)

# Felder, die wir je Ticker aus yfinance info holen (interner Name -> info-Key).
# price/eps_fwd werden gesondert (mit fast_info-Fallback bzw. FMP) behandelt.
INFO_FIELDS: dict[str, str] = {
    "price": "currentPrice",
    "eps_ttm": "trailingEps",
    "eps_fwd": "forwardEps",
    "dps": "dividendRate",
    "shares": "sharesOutstanding",
    "ebitda": "ebitda",
    "total_debt": "totalDebt",
    "cash": "totalCash",
    "book_value_ps": "bookValue",
    "revenue": "totalRevenue",
    "roe": "returnOnEquity",
    "beta": "beta",
    "target": "targetMeanPrice",
    "rec_key": "recommendationKey",
    "n_analysts": "numberOfAnalystOpinions",
}


# =============================================================================
# Schritt 1 — Tickerliste
# =============================================================================
def get_sp500_constituents() -> pd.DataFrame:
    """Aktuelle S&P-500-Mitglieder: Symbol, Security, GICS Sector.

    Zwei Quellen mit Fallback (Wikipedia -> datasets-CSV). Yahoo-Ticker:
    '.' wird durch '-' ersetzt (BRK.B -> BRK-B).
    """
    df = _constituents_from_wikipedia()
    if df is None:
        log.warning("Wikipedia-Quelle fehlgeschlagen, nutze Fallback-CSV.")
        df = _constituents_from_csv()
    if df is None or df.empty:
        raise RuntimeError("Konnte S&P-500-Liste aus keiner Quelle laden.")

    df["yahoo"] = df["symbol"].str.replace(".", "-", regex=False)
    df = df.drop_duplicates(subset="yahoo").reset_index(drop=True)
    log.info("S&P-500-Liste geladen: %d Titel.", len(df))
    return df


def _constituents_from_wikipedia() -> pd.DataFrame | None:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (sp500-valuation-pipeline)"}
        resp = requests.get(WIKI_URL, headers=headers, timeout=30)
        resp.raise_for_status()
        tables = pd.read_html(resp.text)
        table = tables[0]
        out = pd.DataFrame(
            {
                "symbol": table["Symbol"].astype(str).str.strip(),
                "security": table["Security"].astype(str).str.strip(),
                "sector": table["GICS Sector"].astype(str).str.strip(),
            }
        )
        return out
    except Exception as exc:  # noqa: BLE001 — Fallback ist gewollt
        log.debug("Wikipedia-Abruf fehlgeschlagen: %s", exc)
        return None


def _constituents_from_csv() -> pd.DataFrame | None:
    try:
        # Über requests laden (respektiert Proxy zuverlässig), dann parsen.
        headers = {"User-Agent": "Mozilla/5.0 (sp500-valuation-pipeline)"}
        resp = requests.get(FALLBACK_CSV, headers=headers, timeout=30)
        resp.raise_for_status()
        import io

        table = pd.read_csv(io.StringIO(resp.text))
        # Spaltennamen variieren je Quelle: 'Name'/'Sector' (alt) vs.
        # 'Security'/'GICS Sector' (aktuell). Beide Schemata unterstützen.
        name_col = _first_present(table, ["Name", "Security"])
        sector_col = _first_present(table, ["Sector", "GICS Sector"])
        out = pd.DataFrame(
            {
                "symbol": table["Symbol"].astype(str).str.strip(),
                "security": table[name_col].astype(str).str.strip(),
                "sector": table[sector_col].astype(str).str.strip(),
            }
        )
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("Fallback-CSV-Abruf fehlgeschlagen: %s", exc)
        return None


def _first_present(df: pd.DataFrame, candidates: list[str]) -> str:
    for col in candidates:
        if col in df.columns:
            return col
    raise KeyError(f"Keine der Spalten {candidates} in CSV gefunden.")


# =============================================================================
# Wechselkurs USD -> EUR
# =============================================================================
def get_eurusd_rate(cfg: dict[str, Any]) -> float:
    """USD je 1 EUR (yfinance EURUSD=X). Fehlschlag -> Fallback aus config."""
    fallback = float(cfg.get("currency", {}).get("fallback_eurusd", 1.08))
    try:
        import yfinance as yf

        tk = yf.Ticker("EURUSD=X")
        rate = None
        try:
            rate = _clean_number(tk.fast_info.get("last_price"))
        except Exception:  # noqa: BLE001
            rate = None
        if rate is None:
            rate = _clean_number((tk.info or {}).get("regularMarketPrice"))
        if rate is not None and 0.5 < rate < 2.0:
            log.info("EUR/USD-Kurs: %.4f (1 EUR = %.4f USD).", rate, rate)
            return float(rate)
    except Exception as exc:  # noqa: BLE001
        log.debug("EUR/USD-Abruf fehlgeschlagen: %s", exc)
    log.warning("EUR/USD-Live-Kurs nicht verfügbar, nutze Fallback %.4f.", fallback)
    return fallback


# =============================================================================
# Schritt 2 — Datenabruf je Ticker (mit Caching, Retry, Rate-Limit)
# =============================================================================
def fetch_ticker_raw(
    yahoo_symbol: str,
    cfg: dict[str, Any],
    refresh: bool = False,
) -> dict[str, Any] | None:
    """Rohdaten eines Tickers als dict holen (mit Cache).

    Bei erneutem Lauf aus data/cache/ lesen, außer refresh=True.
    Rückgabe None nur bei hartem Fehler (Ticker landet in der Fehlerliste).
    """
    cache_file = CACHE_DIR / f"{yahoo_symbol}.json"
    if not refresh and cache_file.exists():
        try:
            with cache_file.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001 — kaputter Cache -> neu ziehen
            log.debug("Cache %s defekt (%s), ziehe neu.", cache_file, exc)

    raw = _fetch_from_yfinance(yahoo_symbol, cfg)
    if raw is None:
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump(raw, fh)
    except Exception as exc:  # noqa: BLE001 — Cache-Schreiben ist best effort
        log.debug("Cache-Schreiben fehlgeschlagen (%s): %s", cache_file, exc)
    return raw


def _fetch_from_yfinance(yahoo_symbol: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    import yfinance as yf  # lokaler Import: yfinance nur laden, wenn wirklich gezogen wird

    fetch_cfg = cfg.get("fetch", {})
    max_retries = int(fetch_cfg.get("max_retries", 3))
    backoff_base = float(fetch_cfg.get("backoff_base", 1.5))
    sleep_between = float(fetch_cfg.get("sleep_between_calls", 0.4))

    info: dict[str, Any] = {}
    fast: dict[str, Any] = {}
    cashflow_vals: dict[str, Any] = {}

    for attempt in range(max_retries):
        try:
            tk = yf.Ticker(yahoo_symbol)
            # info kann teuer sein und gelegentlich leer/fehlerhaft zurückkommen.
            try:
                info = dict(tk.info) if tk.info else {}
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: info-Abruf-Problem: %s", yahoo_symbol, exc)
                info = {}
            try:
                fi = tk.fast_info
                fast = {"last_price": _safe_get(fi, "last_price")}
            except Exception:  # noqa: BLE001
                fast = {}
            cashflow_vals = _extract_cashflow(tk, yahoo_symbol)
            break
        except Exception as exc:  # noqa: BLE001 — Retry mit Backoff
            wait = backoff_base * (2 ** attempt)
            log.debug("%s: Versuch %d fehlgeschlagen (%s), warte %.1fs.",
                      yahoo_symbol, attempt + 1, exc, wait)
            time.sleep(wait)
    else:
        log.warning("%s: alle %d Versuche fehlgeschlagen.", yahoo_symbol, max_retries)
        return None

    if not info and not fast:
        log.warning("%s: keine Daten (leeres info).", yahoo_symbol)
        return None

    row: dict[str, Any] = {"yahoo": yahoo_symbol}
    # Jeder Feldzugriff einzeln abgesichert -> fehlend = None.
    for internal, key in INFO_FIELDS.items():
        row[internal] = _clean_number(info.get(key)) if key != "rec_key" else info.get(key)

    # price-Fallback über fast_info.last_price
    if row.get("price") is None:
        row["price"] = _clean_number(fast.get("last_price"))

    row.update(cashflow_vals)

    # Optional: FMP-Anreicherung (bessere Forward-EPS / Analystenziel)
    _enrich_with_fmp(row, yahoo_symbol)

    time.sleep(sleep_between)
    return row


def _extract_cashflow(tk: Any, yahoo_symbol: str) -> dict[str, Any]:
    """operating_cashflow und capex für M8 (DCF) — best effort."""
    result: dict[str, Any] = {"operating_cashflow": None, "capex": None}
    try:
        cf = tk.cashflow
        if cf is None or cf.empty:
            return result
        latest = cf.columns[0]

        def pick(*names: str) -> float | None:
            for name in names:
                if name in cf.index:
                    return _clean_number(cf.loc[name, latest])
            return None

        result["operating_cashflow"] = pick(
            "Operating Cash Flow", "Total Cash From Operating Activities"
        )
        result["capex"] = pick("Capital Expenditure", "Capital Expenditures")
    except Exception as exc:  # noqa: BLE001
        log.debug("%s: Cashflow-Abruf-Problem: %s", yahoo_symbol, exc)
    return result


def _enrich_with_fmp(row: dict[str, Any], symbol: str) -> None:
    """Optionale Anreicherung über Financial Modeling Prep, falls FMP_API_KEY gesetzt."""
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        return
    # FMP nutzt US-Symbole ohne '-'-Ersetzung teils anders; wir versuchen das Yahoo-Symbol.
    fmp_symbol = symbol.replace("-", ".")
    try:
        url = (
            f"https://financialmodelingprep.com/api/v3/analyst-estimates/"
            f"{fmp_symbol}?limit=1&apikey={api_key}"
        )
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            est_eps = _clean_number(data[0].get("estimatedEpsAvg"))
            if est_eps is not None and (row.get("eps_fwd") is None):
                row["eps_fwd"] = est_eps
    except Exception as exc:  # noqa: BLE001 — FMP ist rein optional
        log.debug("%s: FMP-Anreicherung übersprungen: %s", symbol, exc)


# =============================================================================
# Hilfsfunktionen
# =============================================================================
def _safe_get(obj: Any, attr: str) -> Any:
    try:
        return getattr(obj, attr)
    except Exception:  # noqa: BLE001
        try:
            return obj[attr]
        except Exception:  # noqa: BLE001
            return None


def _clean_number(value: Any) -> float | None:
    """In float wandeln; None/NaN/inf -> None (JSON-serialisierbar)."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num
