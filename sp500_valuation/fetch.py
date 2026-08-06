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
    "net_income": "netIncomeToCommon",   # für Nettomarge (P/S-Adjustierung)
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
        sub = (table["GICS Sub-Industry"].astype(str).str.strip()
               if "GICS Sub-Industry" in table.columns else "")
        out = pd.DataFrame(
            {
                "symbol": table["Symbol"].astype(str).str.strip(),
                "security": table["Security"].astype(str).str.strip(),
                "sector": table["GICS Sector"].astype(str).str.strip(),
                "sub_industry": sub,
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
        sub_col = next((c for c in ["GICS Sub-Industry", "Sub-Industry", "Sub Industry"]
                        if c in table.columns), None)
        out = pd.DataFrame(
            {
                "symbol": table["Symbol"].astype(str).str.strip(),
                "security": table[name_col].astype(str).str.strip(),
                "sector": table[sector_col].astype(str).str.strip(),
                "sub_industry": (table[sub_col].astype(str).str.strip()
                                 if sub_col else ""),
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
# Wechselkurse -> EUR (Multi-Währung)
# =============================================================================
# Börsen-Suffix -> Notierungswährung (GBp = London in Pence).
SUFFIX_CCY = {
    ".L": "GBp", ".SW": "CHF", ".CO": "DKK", ".ST": "SEK", ".OL": "NOK",
    ".DE": "EUR", ".PA": "EUR", ".AS": "EUR", ".MI": "EUR", ".MC": "EUR",
    ".BR": "EUR", ".HE": "EUR", ".LS": "EUR", ".VI": "EUR", ".IR": "EUR",
    ".F": "EUR",
}
_FX_PAIRS = {"USD": "EURUSD=X", "GBP": "EURGBP=X", "CHF": "EURCHF=X",
             "DKK": "EURDKK=X", "SEK": "EURSEK=X", "NOK": "EURNOK=X"}


def infer_currency(yahoo_symbol: str) -> str:
    """Notierungswährung aus dem Börsen-Suffix ableiten (US ohne Suffix -> USD)."""
    for suf, ccy in SUFFIX_CCY.items():
        if yahoo_symbol.endswith(suf):
            return ccy
    return "USD"


def get_eurusd_rate(cfg: dict[str, Any]) -> float:
    """Rückwärtskompatibel: nur USD je 1 EUR."""
    return get_fx_rates(cfg).get("USD", 1.08)


def get_fx_rates(cfg: dict[str, Any]) -> dict[str, float]:
    """Fremdwährung je 1 EUR für alle relevanten Währungen.

    z. B. {'EUR':1, 'USD':1.08, 'GBP':0.84, 'GBp':84.0, 'CHF':0.94, ...}.
    Umrechnung Fremdwährung->EUR: betrag_eur = betrag / rate[währung].
    """
    fb = cfg.get("currency", {}).get("fallback_rates", {})
    rates: dict[str, float] = {"EUR": 1.0}
    try:
        import yfinance as yf
    except Exception:  # noqa: BLE001
        yf = None

    for cur, sym in _FX_PAIRS.items():
        rate = None
        if yf is not None:
            try:
                rate = _clean_number(yf.Ticker(sym).fast_info.get("last_price"))
            except Exception:  # noqa: BLE001
                rate = None
            if rate is None:
                try:
                    rate = _clean_number((yf.Ticker(sym).info or {}).get("regularMarketPrice"))
                except Exception:  # noqa: BLE001
                    rate = None
        if not rate or rate <= 0:
            rate = float(fb.get(cur, 1.0))
            log.warning("FX %s nicht live verfügbar, Fallback %.4f.", cur, rate)
        rates[cur] = float(rate)

    rates["GBp"] = rates["GBP"] * 100.0     # London notiert in Pence
    log.info("Wechselkurse (je 1 EUR): USD=%.4f GBP=%.4f CHF=%.4f",
             rates["USD"], rates["GBP"], rates["CHF"])
    return rates


# =============================================================================
# Große europäische Aktien (kuratierte Liste, Yahoo-Ticker + GICS-Sektor)
# =============================================================================
def get_european_constituents() -> pd.DataFrame:
    """Kuratierte Liste großer europäischer Aktien (Yahoo-Ticker bereits korrekt)."""
    df = pd.DataFrame(EUROPEAN_STOCKS, columns=["yahoo", "security", "sector"])
    df["symbol"] = df["yahoo"]
    df = df.drop_duplicates(subset="yahoo").reset_index(drop=True)
    log.info("Europäische Liste: %d Titel.", len(df))
    return df[["symbol", "security", "sector", "yahoo"]]


def get_additional_constituents() -> pd.DataFrame:
    """Weitere große Titel außerhalb des S&P 500 (US-notiert + große ADRs, in USD)."""
    df = pd.DataFrame(ADDITIONAL_STOCKS, columns=["yahoo", "security", "sector"])
    df["symbol"] = df["yahoo"]
    df = df.drop_duplicates(subset="yahoo").reset_index(drop=True)
    log.info("Zusätzliche Liste: %d Titel.", len(df))
    return df[["symbol", "security", "sector", "yahoo"]]


# (yahoo, Name, GICS-Sektor) — große Werte quer durch Europa
EUROPEAN_STOCKS: list[tuple[str, str, str]] = [
    # Deutschland (.DE)
    ("SAP.DE", "SAP", "Information Technology"),
    ("SIE.DE", "Siemens", "Industrials"),
    ("ALV.DE", "Allianz", "Financials"),
    ("DTE.DE", "Deutsche Telekom", "Communication Services"),
    ("MBG.DE", "Mercedes-Benz Group", "Consumer Discretionary"),
    ("BMW.DE", "BMW", "Consumer Discretionary"),
    ("VOW3.DE", "Volkswagen", "Consumer Discretionary"),
    ("BAS.DE", "BASF", "Materials"),
    ("BAYN.DE", "Bayer", "Health Care"),
    ("ADS.DE", "Adidas", "Consumer Discretionary"),
    ("DBK.DE", "Deutsche Bank", "Financials"),
    ("MUV2.DE", "Munich Re", "Financials"),
    ("IFX.DE", "Infineon", "Information Technology"),
    ("DHL.DE", "DHL Group", "Industrials"),
    ("MRK.DE", "Merck KGaA", "Health Care"),
    ("SHL.DE", "Siemens Healthineers", "Health Care"),
    ("ENR.DE", "Siemens Energy", "Industrials"),
    ("EOAN.DE", "E.ON", "Utilities"),
    ("RWE.DE", "RWE", "Utilities"),
    ("HEN3.DE", "Henkel", "Consumer Staples"),
    ("BEI.DE", "Beiersdorf", "Consumer Staples"),
    ("VNA.DE", "Vonovia", "Real Estate"),
    ("P911.DE", "Porsche AG", "Consumer Discretionary"),
    ("DTG.DE", "Daimler Truck", "Industrials"),
    # Frankreich (.PA)
    ("MC.PA", "LVMH", "Consumer Discretionary"),
    ("OR.PA", "L'Oréal", "Consumer Staples"),
    ("RMS.PA", "Hermès", "Consumer Discretionary"),
    ("TTE.PA", "TotalEnergies", "Energy"),
    ("SAN.PA", "Sanofi", "Health Care"),
    ("AIR.PA", "Airbus", "Industrials"),
    ("SU.PA", "Schneider Electric", "Industrials"),
    ("AI.PA", "Air Liquide", "Materials"),
    ("EL.PA", "EssilorLuxottica", "Health Care"),
    ("BNP.PA", "BNP Paribas", "Financials"),
    ("KER.PA", "Kering", "Consumer Discretionary"),
    ("BN.PA", "Danone", "Consumer Staples"),
    ("CS.PA", "AXA", "Financials"),
    ("DG.PA", "Vinci", "Industrials"),
    ("SAF.PA", "Safran", "Industrials"),
    ("RI.PA", "Pernod Ricard", "Consumer Staples"),
    ("CAP.PA", "Capgemini", "Information Technology"),
    ("SGO.PA", "Saint-Gobain", "Industrials"),
    ("DSY.PA", "Dassault Systèmes", "Information Technology"),
    ("ORA.PA", "Orange", "Communication Services"),
    ("ENGI.PA", "Engie", "Utilities"),
    ("LR.PA", "Legrand", "Industrials"),
    ("EN.PA", "Bouygues", "Industrials"),
    # Niederlande (.AS)
    ("ASML.AS", "ASML", "Information Technology"),
    ("PRX.AS", "Prosus", "Consumer Discretionary"),
    ("ADYEN.AS", "Adyen", "Financials"),
    ("HEIA.AS", "Heineken", "Consumer Staples"),
    ("WKL.AS", "Wolters Kluwer", "Industrials"),
    ("INGA.AS", "ING Group", "Financials"),
    ("AD.AS", "Ahold Delhaize", "Consumer Staples"),
    ("PHIA.AS", "Philips", "Health Care"),
    ("ASM.AS", "ASM International", "Information Technology"),
    ("FER.AS", "Ferrovial", "Industrials"),
    # Schweiz (.SW)
    ("NESN.SW", "Nestlé", "Consumer Staples"),
    ("ROG.SW", "Roche", "Health Care"),
    ("NOVN.SW", "Novartis", "Health Care"),
    ("UBSG.SW", "UBS Group", "Financials"),
    ("ZURN.SW", "Zurich Insurance", "Financials"),
    ("ABBN.SW", "ABB", "Industrials"),
    ("CFR.SW", "Richemont", "Consumer Discretionary"),
    ("SIKA.SW", "Sika", "Materials"),
    ("GIVN.SW", "Givaudan", "Materials"),
    ("HOLN.SW", "Holcim", "Materials"),
    ("LONN.SW", "Lonza", "Health Care"),
    ("ALC.SW", "Alcon", "Health Care"),
    ("SREN.SW", "Swiss Re", "Financials"),
    # UK (.L) — notiert in Pence (GBp)
    ("AZN.L", "AstraZeneca", "Health Care"),
    ("SHEL.L", "Shell", "Energy"),
    ("HSBA.L", "HSBC", "Financials"),
    ("ULVR.L", "Unilever", "Consumer Staples"),
    ("BP.L", "BP", "Energy"),
    ("GSK.L", "GSK", "Health Care"),
    ("DGE.L", "Diageo", "Consumer Staples"),
    ("RIO.L", "Rio Tinto", "Materials"),
    ("BATS.L", "British American Tobacco", "Consumer Staples"),
    ("GLEN.L", "Glencore", "Materials"),
    ("REL.L", "RELX", "Industrials"),
    ("LSEG.L", "London Stock Exchange", "Financials"),
    ("NG.L", "National Grid", "Utilities"),
    ("RR.L", "Rolls-Royce", "Industrials"),
    ("BA.L", "BAE Systems", "Industrials"),
    ("BARC.L", "Barclays", "Financials"),
    ("LLOY.L", "Lloyds Banking Group", "Financials"),
    ("TSCO.L", "Tesco", "Consumer Staples"),
    ("VOD.L", "Vodafone", "Communication Services"),
    ("RKT.L", "Reckitt Benckiser", "Consumer Staples"),
    ("PRU.L", "Prudential", "Financials"),
    ("CPG.L", "Compass Group", "Consumer Discretionary"),
    # Spanien (.MC)
    ("ITX.MC", "Inditex", "Consumer Discretionary"),
    ("IBE.MC", "Iberdrola", "Utilities"),
    ("SAN.MC", "Banco Santander", "Financials"),
    ("BBVA.MC", "BBVA", "Financials"),
    ("CABK.MC", "CaixaBank", "Financials"),
    ("TEF.MC", "Telefónica", "Communication Services"),
    ("AMS.MC", "Amadeus IT", "Information Technology"),
    # Italien (.MI)
    ("RACE.MI", "Ferrari", "Consumer Discretionary"),
    ("ENEL.MI", "Enel", "Utilities"),
    ("ISP.MI", "Intesa Sanpaolo", "Financials"),
    ("UCG.MI", "UniCredit", "Financials"),
    ("ENI.MI", "Eni", "Energy"),
    ("G.MI", "Generali", "Financials"),
    ("STLAM.MI", "Stellantis", "Consumer Discretionary"),
    ("PRY.MI", "Prysmian", "Industrials"),
    ("MONC.MI", "Moncler", "Consumer Discretionary"),
    # Dänemark (.CO)
    ("NOVO-B.CO", "Novo Nordisk", "Health Care"),
    ("DSV.CO", "DSV", "Industrials"),
    ("MAERSK-B.CO", "A.P. Møller-Mærsk", "Industrials"),
    ("CARL-B.CO", "Carlsberg", "Consumer Staples"),
    ("ORSTED.CO", "Ørsted", "Utilities"),
    ("VWS.CO", "Vestas Wind Systems", "Industrials"),
    ("COLO-B.CO", "Coloplast", "Health Care"),
    ("GMAB.CO", "Genmab", "Health Care"),
    # Schweden (.ST)
    ("ATCO-A.ST", "Atlas Copco", "Industrials"),
    ("INVE-B.ST", "Investor AB", "Financials"),
    ("VOLV-B.ST", "Volvo", "Industrials"),
    ("EQT.ST", "EQT", "Financials"),
    ("ERIC-B.ST", "Ericsson", "Information Technology"),
    ("SAND.ST", "Sandvik", "Industrials"),
    ("HEXA-B.ST", "Hexagon", "Information Technology"),
    ("ASSA-B.ST", "Assa Abloy", "Industrials"),
    ("HM-B.ST", "H&M", "Consumer Discretionary"),
    # Norwegen (.OL)
    ("EQNR.OL", "Equinor", "Energy"),
    ("DNB.OL", "DNB Bank", "Financials"),
    ("TEL.OL", "Telenor", "Communication Services"),
    # Finnland (.HE)
    ("NOKIA.HE", "Nokia", "Information Technology"),
    ("KNEBV.HE", "Kone", "Industrials"),
    ("NESTE.HE", "Neste", "Energy"),
    ("SAMPO.HE", "Sampo", "Financials"),
    # Belgien (.BR)
    ("ABI.BR", "Anheuser-Busch InBev", "Consumer Staples"),
    ("KBC.BR", "KBC Group", "Financials"),
    ("UCB.BR", "UCB", "Health Care"),
    # Portugal (.LS) / Österreich (.VI)
    ("EDP.LS", "EDP", "Utilities"),
    ("GALP.LS", "Galp Energia", "Energy"),
    ("OMV.VI", "OMV", "Energy"),
    ("EBS.VI", "Erste Group Bank", "Financials"),
    ("VER.VI", "Verbund", "Utilities"),
]

# Weitere große Titel außerhalb des S&P 500 — US-notiert bzw. große ADRs (alle USD).
# Doppelte zum S&P 500 werden beim Zusammenführen automatisch entfernt.
ADDITIONAL_STOCKS: list[tuple[str, str, str]] = [
    # US-notiert, (noch) nicht/erst spät im S&P 500 oder ausländischer Sitz
    ("SPOT", "Spotify", "Communication Services"),
    ("ARM", "Arm Holdings", "Information Technology"),
    ("SHOP", "Shopify", "Information Technology"),
    ("SNOW", "Snowflake", "Information Technology"),
    ("COIN", "Coinbase", "Financials"),
    ("RBLX", "Roblox", "Communication Services"),
    ("RIVN", "Rivian", "Consumer Discretionary"),
    ("LCID", "Lucid Group", "Consumer Discretionary"),
    ("U", "Unity Software", "Information Technology"),
    ("HOOD", "Robinhood", "Financials"),
    ("SOFI", "SoFi Technologies", "Financials"),
    ("AFRM", "Affirm", "Financials"),
    ("DKNG", "DraftKings", "Consumer Discretionary"),
    ("CVNA", "Carvana", "Consumer Discretionary"),
    ("NET", "Cloudflare", "Information Technology"),
    ("MDB", "MongoDB", "Information Technology"),
    ("DASH", "DoorDash", "Consumer Discretionary"),
    ("ABNB", "Airbnb", "Consumer Discretionary"),
    ("PLTR", "Palantir", "Information Technology"),
    ("SNAP", "Snap", "Communication Services"),
    ("PINS", "Pinterest", "Communication Services"),
    ("RKLB", "Rocket Lab", "Industrials"),
    ("DDOG", "Datadog", "Information Technology"),
    ("ZS", "Zscaler", "Information Technology"),
    ("TTD", "The Trade Desk", "Communication Services"),
    ("MELI", "MercadoLibre", "Consumer Discretionary"),
    # Große internationale ADRs (USD)
    ("TSM", "Taiwan Semiconductor", "Information Technology"),
    ("BABA", "Alibaba", "Consumer Discretionary"),
    ("PDD", "PDD Holdings", "Consumer Discretionary"),
    ("JD", "JD.com", "Consumer Discretionary"),
    ("BIDU", "Baidu", "Communication Services"),
    ("NIO", "NIO", "Consumer Discretionary"),
    ("LI", "Li Auto", "Consumer Discretionary"),
    ("XPEV", "XPeng", "Consumer Discretionary"),
    ("SE", "Sea Limited", "Communication Services"),
    ("NU", "Nu Holdings", "Financials"),
    ("GRAB", "Grab Holdings", "Industrials"),
    ("SONY", "Sony Group", "Consumer Discretionary"),
    ("TM", "Toyota Motor", "Consumer Discretionary"),
]


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

    # Notierungswährung (für die EUR-Umrechnung). Fehlt sie -> aus Suffix ableiten.
    ccy = (info.get("currency") or "").strip()
    row["currency_native"] = ccy or infer_currency(yahoo_symbol)
    # Bilanzwährung (Umsatz/EBITDA/Cashflow). Weicht sie vom Kurs ab (typisch bei
    # ADRs, z. B. Sony in JPY), sind umsatzbasierte Methoden nicht direkt gültig.
    row["financial_currency"] = (info.get("financialCurrency") or "").strip() or None

    row.update(cashflow_vals)

    # Optional: FMP-Anreicherung (bessere Forward-EPS / Analystenziel)
    _enrich_with_fmp(row, yahoo_symbol)

    time.sleep(sleep_between)
    return row


def _extract_cashflow(tk: Any, yahoo_symbol: str) -> dict[str, Any]:
    """operating_cashflow, capex (aktuell) und fcff_cagr (Historie) für M8 (DCF)."""
    result: dict[str, Any] = {"operating_cashflow": None, "capex": None,
                              "fcff_cagr": None}
    try:
        cf = tk.cashflow
        if cf is None or cf.empty:
            return result
        latest = cf.columns[0]

        def pick(*names: str, col: Any = latest) -> float | None:
            for name in names:
                if name in cf.index:
                    return _clean_number(cf.loc[name, col])
            return None

        result["operating_cashflow"] = pick(
            "Operating Cash Flow", "Total Cash From Operating Activities")
        result["capex"] = pick("Capital Expenditure", "Capital Expenditures")

        # FCFF-Historie über alle verfügbaren Jahre -> CAGR (ältestes -> neuestes).
        fcffs: list[float] = []
        for col in cf.columns:
            ocf = pick("Operating Cash Flow", "Total Cash From Operating Activities", col=col)
            cpx = pick("Capital Expenditure", "Capital Expenditures", col=col)
            if ocf is not None and cpx is not None:
                fcffs.append(ocf - abs(cpx))   # CapEx ist i. d. R. negativ
        # cf.columns sind absteigend (neuestes zuerst) -> für CAGR umdrehen
        fcffs = list(reversed(fcffs))
        if len(fcffs) >= 2 and fcffs[0] > 0 and fcffs[-1] > 0:
            n = len(fcffs) - 1
            result["fcff_cagr"] = (fcffs[-1] / fcffs[0]) ** (1.0 / n) - 1.0
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
