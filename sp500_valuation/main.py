"""
main.py — Orchestrierung / CLI der S&P-500-Valuation-Pipeline (schwerer Lauf).

Ablauf:
  1. Tickerliste holen (Wikipedia -> Fallback-CSV)
  2. Rohdaten je Ticker ziehen (Cache/Retry/Rate-Limit)
  3. Abgeleitete Größen + Sektor-Median-Multiplikatoren
  4. 9 Bewertungsmethoden + Blend + Signal je Aktie
  5. Ergebnis -> data/latest.parquet und output/latest.xlsx (+ datiertes Workbook)

CLI:
  python main.py --limit 10      # an 10 Tickern testen
  python main.py                 # voller Lauf (~503)
  python main.py --refresh       # Cache ignorieren, neu ziehen
  python main.py --details AAPL,MSFT
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from tqdm import tqdm

import fetch
import valuation
import excel

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

log = logging.getLogger("sp500")

# Geldbeträge (je Aktie) die von USD nach EUR umgerechnet werden. Alle
# Verhältnis-/Prozent-/Multiplikator-Spalten bleiben währungsneutral.
MONETARY_COLS = [
    "price", "target", "dps", "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9",
    "blended_fair_value", "no_growth_value", "pvgo",
]


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run(limit: int | None, refresh: bool, details: list[str] | None,
        cfg: dict[str, Any]) -> pd.DataFrame:
    # --- Schritt 1: Tickerliste (S&P 500 + große europäische Aktien) ---
    sp = fetch.get_sp500_constituents()
    try:
        eu = fetch.get_european_constituents()
        constituents = pd.concat([sp, eu], ignore_index=True)
    except Exception as exc:  # noqa: BLE001 — Europa-Liste ist statisch, sollte nicht failen
        log.warning("Europäische Liste nicht geladen (%s), nur S&P 500.", exc)
        constituents = sp
    constituents = constituents.drop_duplicates(subset="yahoo").reset_index(drop=True)
    log.info("Universum gesamt: %d Titel (S&P 500 + Europa).", len(constituents))
    if limit:
        constituents = constituents.head(limit)
        log.info("Limit aktiv: nur %d Ticker.", len(constituents))

    # --- Schritt 2: Rohdaten ziehen ---
    rows: list[dict[str, Any]] = []
    failed: list[str] = []
    sector_by_yahoo = dict(zip(constituents["yahoo"], constituents["sector"]))
    security_by_yahoo = dict(zip(constituents["yahoo"], constituents["security"]))

    for yahoo in tqdm(constituents["yahoo"], desc="Datenabruf", unit="ticker"):
        raw = fetch.fetch_ticker_raw(yahoo, cfg, refresh=refresh)
        if raw is None:
            failed.append(yahoo)
            continue
        # Sektor/Name robust aus Konstituenten-CSV (nicht aus yfinance).
        raw["sector"] = sector_by_yahoo.get(yahoo, "Unknown")
        raw["security"] = security_by_yahoo.get(yahoo, yahoo)
        rows.append(valuation.derive_row_fields(raw))

    if not rows:
        raise RuntimeError("Keine Daten abgerufen — Abbruch.")

    df = pd.DataFrame(rows)

    # --- Schritt 3: Sektor-Median-Multiplikatoren ---
    medians = valuation.compute_sector_medians(df)

    # --- Schritt 4: Bewertung je Zeile (in USD) ---
    valued = [valuation.value_row(row.to_dict(), medians, cfg)
              for _, row in df.iterrows()]
    result = pd.DataFrame(valued)

    # --- Währungsumrechnung -> EUR (je Aktie nach Notierungswährung) ---
    rates = fetch.get_fx_rates(cfg)              # Fremdwährung je 1 EUR
    result = _convert_to_eur(result, rates)
    result["currency"] = cfg.get("currency", {}).get("target", "EUR")
    result["fx_eurusd"] = rates.get("USD")

    # --- Report ---
    n_total = len(result)
    n_with_signal = int(result["blended_fair_value"].apply(valuation.is_finite).sum())
    n_gaps = n_total - n_with_signal
    log.info("Erfolgreich bewertet: %d/%d (Blended Fair Value vorhanden).",
             n_with_signal, n_total)
    log.info("Zeilen mit Datenlücke (kein Blended Fair Value): %d.", n_gaps)
    if failed:
        log.warning("Fehlgeschlagene Ticker (%d): %s", len(failed), ", ".join(failed))

    _write_outputs(result, cfg, medians, details)
    return result


def _write_outputs(result: pd.DataFrame, cfg: dict[str, Any],
                   medians: dict[str, dict[str, float]],
                   details: list[str] | None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now()

    # data/latest.parquet — von der App geladen
    parquet_path = DATA_DIR / "latest.parquet"
    _write_parquet(result, parquet_path)
    log.info("Ergebnis-Tabelle geschrieben: %s", parquet_path)

    # Excel: datiert + latest.xlsx
    wb = excel.build_workbook(result, cfg, medians, run_date, details=details)
    dated_path = OUTPUT_DIR / f"sp500_valuation_{run_date.strftime('%Y%m%d')}.xlsx"
    latest_path = OUTPUT_DIR / "latest.xlsx"
    wb.save(dated_path)
    wb.save(latest_path)
    log.info("Workbook geschrieben: %s und %s", dated_path, latest_path)


def _convert_to_eur(df: pd.DataFrame, rates: dict[str, float]) -> pd.DataFrame:
    """Rechnet Geldbeträge je Aktie von ihrer Notierungswährung nach EUR um.

    EUR = betrag / rate[währung]. Notierungswährung aus 'currency_native'
    (bzw. aus dem Börsen-Suffix abgeleitet). Unbekannte Währung -> EUR (Faktor 1).
    """
    out = df.copy()
    if "currency_native" in out.columns:
        curs = out.apply(
            lambda r: r.get("currency_native") or fetch.infer_currency(str(r["yahoo"])),
            axis=1)
    else:
        curs = out["yahoo"].map(lambda y: fetch.infer_currency(str(y)))
    divisor = curs.map(lambda c: rates.get(c, 1.0) or 1.0)
    for col in MONETARY_COLS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce") / divisor
    out["currency_native"] = curs
    return out


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Parquet-Schreiben; objekt-gemischte Spalten vorher säubern."""
    safe = df.copy()
    # rec_key/value_creation/signal etc. sind Strings; Zahlen bleiben Zahlen.
    for col in safe.columns:
        if safe[col].dtype == object:
            safe[col] = safe[col].apply(
                lambda v: v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
            )
    try:
        safe.to_parquet(path, index=False)
    except Exception as exc:  # noqa: BLE001 — Fallback, damit App-Datei immer entsteht
        log.warning("Parquet-Schreiben fehlgeschlagen (%s), nutze CSV-Fallback.", exc)
        safe.to_csv(path.with_suffix(".csv"), index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="S&P-500 Valuation-Pipeline")
    parser.add_argument("--limit", type=int, default=None,
                        help="Nur die ersten N Ticker (schneller Test).")
    parser.add_argument("--refresh", action="store_true",
                        help="Cache ignorieren, Rohdaten neu ziehen.")
    parser.add_argument("--details", type=str, default=None,
                        help="Komma-Liste von Tickern für Detailblätter, z. B. AAPL,MSFT.")
    parser.add_argument("--log-level", default="INFO",
                        help="Logging-Level (DEBUG/INFO/WARNING).")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    details = [t.strip().upper().replace(".", "-")
               for t in args.details.split(",")] if args.details else None

    cfg = load_config()
    run(limit=args.limit, refresh=args.refresh, details=details, cfg=cfg)
    log.info("Fertig.")


if __name__ == "__main__":
    main()
