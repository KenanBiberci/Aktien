"""
app.py — mobil-taugliche Streamlit-Web-App (iPhone-Oberfläche, Abschnitt 11).

Rechnet NICHT live 500 Ticker (würde im Web-Request timen out), sondern lädt
data/latest.parquet und rendert es. Excel-Download aus output/latest.xlsx.
Button "Neu berechnen" löst optional den GitHub-Actions-Workflow per
workflow_dispatch aus (Token/Repo aus st.secrets).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
PARQUET_PATH = BASE_DIR / "data" / "latest.parquet"
CSV_FALLBACK = BASE_DIR / "data" / "latest.csv"
XLSX_PATH = BASE_DIR / "output" / "latest.xlsx"

SIGNAL_ORDER = ["STRONG BUY", "BUY", "HOLD", "REDUCE", "N/A – Datenlücke"]

st.set_page_config(page_title="S&P-500 Valuation", layout="centered", page_icon="📈")


@st.cache_data(ttl=300)
def load_data() -> pd.DataFrame | None:
    if PARQUET_PATH.exists():
        try:
            return pd.read_parquet(PARQUET_PATH)
        except Exception:  # noqa: BLE001
            pass
    if CSV_FALLBACK.exists():
        return pd.read_csv(CSV_FALLBACK)
    return None


def last_run_label() -> str:
    for path in (PARQUET_PATH, CSV_FALLBACK):
        if path.exists():
            ts = pd.Timestamp(path.stat().st_mtime, unit="s")
            return ts.strftime("%Y-%m-%d %H:%M")
    return "—"


def trigger_workflow() -> None:
    """workflow_dispatch via GitHub-API (aus st.secrets)."""
    try:
        token = st.secrets["GITHUB_TOKEN"]
        repo = st.secrets["GITHUB_REPO"]           # z. B. "user/Aktien"
        workflow = st.secrets.get("GITHUB_WORKFLOW", "run.yml")
        ref = st.secrets.get("GITHUB_REF", "main")
    except Exception:  # noqa: BLE001
        st.warning("Kein GitHub-Token in st.secrets — 'Neu berechnen' ist deaktiviert. "
                   "Hinterlege GITHUB_TOKEN und GITHUB_REPO in den App-Secrets.")
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        json={"ref": ref},
        timeout=20,
    )
    if resp.status_code in (201, 204):
        st.success("Neuberechnung gestartet — läuft in der Cloud, in ~2–3 Min. aktualisiert.")
    else:
        st.error(f"Konnte Workflow nicht auslösen ({resp.status_code}): {resp.text[:200]}")


def style_signal(df: pd.DataFrame):
    def color(val: str) -> str:
        if val in ("STRONG BUY", "BUY"):
            return "background-color: #C6EFCE"
        if val == "HOLD":
            return "background-color: #FFEB9C"
        if val == "REDUCE":
            return "background-color: #FFC7CE"
        return ""
    return df.style.map(color, subset=["Signal"])


# =============================================================================
# UI
# =============================================================================
st.title("📈 S&P-500 Valuation")
st.caption("Regelbasiertes Bewertungsmodell · **keine Anlageberatung**")

df = load_data()
if df is None:
    st.error("Noch kein Ergebnis vorhanden. Starte den GitHub-Actions-Workflow "
             "oder lokal `python main.py`, damit data/latest.parquet entsteht.")
    st.stop()

col_a, col_b = st.columns([2, 1])
with col_a:
    st.metric("Letzter Lauf", last_run_label())
with col_b:
    if st.button("🔄 Neu berechnen", use_container_width=True):
        trigger_workflow()

st.divider()

# --- Filter (mobil, untereinander) ---
sectors = sorted([s for s in df.get("sector", pd.Series(dtype=str)).dropna().unique()])
sel_sectors = st.multiselect("Sektor", options=sectors, default=[])

present_signals = [s for s in SIGNAL_ORDER if s in set(df.get("signal", []))]
sel_signals = st.multiselect("Signal", options=present_signals,
                             default=[s for s in ("STRONG BUY", "BUY") if s in present_signals])

min_upside = st.slider("Mindest-Ø-Upside", min_value=-0.50, max_value=1.00,
                       value=0.0, step=0.05, format="%.0f%%")

# --- Filtern ---
view = df.copy()
if sel_sectors:
    view = view[view["sector"].isin(sel_sectors)]
if sel_signals:
    view = view[view["signal"].isin(sel_signals)]
if "avg_upside" in view.columns:
    view = view[view["avg_upside"].fillna(-99) >= min_upside]
view = view.sort_values("avg_upside", ascending=False, na_position="last")

st.caption(f"{len(view)} Titel")

# --- Kern-Tabelle ---
core_cols = {
    "yahoo": "Ticker", "security": "Name", "price": "Kurs", "kgv_fwd": "KGV fwd",
    "blended_upside": "Blended Upside", "signal": "Signal", "rec_key": "Konsens",
}
available = {k: v for k, v in core_cols.items() if k in view.columns}
table = view[list(available.keys())].rename(columns=available)
for pct_col in ("Blended Upside",):
    if pct_col in table.columns:
        table[pct_col] = (table[pct_col] * 100).round(1)
if "Kurs" in table.columns:
    table["Kurs"] = table["Kurs"].round(2)
if "KGV fwd" in table.columns:
    table["KGV fwd"] = table["KGV fwd"].round(1)

st.dataframe(style_signal(table), use_container_width=True, hide_index=True)

# --- Excel-Download ---
if XLSX_PATH.exists():
    with XLSX_PATH.open("rb") as fh:
        st.download_button("⬇️ Excel-Workbook laden", data=fh.read(),
                           file_name="sp500_valuation_latest.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)

st.divider()

# --- Detail-Ansicht: 9 Methoden + PVGO eines Tickers ---
st.subheader("Detail")
tickers = view["yahoo"].tolist() if "yahoo" in view.columns else []
if tickers:
    sel = st.selectbox("Ticker wählen", options=tickers)
    row = df[df["yahoo"] == sel].iloc[0]
    method_labels = {
        "m1": "M1 Gordon", "m2": "M2 DDM", "m3": "M3 Justified PE", "m4": "M4 Comp-PE",
        "m5": "M5 P/S", "m6": "M6 P/B", "m7": "M7 EV/EBITDA", "m8": "M8 DCF",
        "m9": "M9 Asset-based",
    }
    detail_rows = [{"Methode": lbl, "Fairer Preis": round(float(row[k]), 2)}
                   for k, lbl in method_labels.items()
                   if k in row and pd.notna(row[k])]
    if "pvgo_pct" in row and pd.notna(row["pvgo_pct"]):
        detail_rows.append({"Methode": "PVGO %",
                            "Fairer Preis": round(float(row["pvgo_pct"]) * 100, 1)})
    st.table(pd.DataFrame(detail_rows))
    st.caption(f"Kurs {round(float(row.get('price', float('nan')) or 0), 2)} · "
               f"Signal {row.get('signal', '—')} · Konsens {row.get('rec_key', '—')}")

st.divider()
st.caption(
    "⚠️ Dieses Tool wendet Standard-Bewertungsmethoden mechanisch an. Signale sind "
    "regelbasiert und nur so gut wie die kostenlosen Daten und Annahmen. "
    "**Keine Anlageberatung.** Eigene Prüfung und Risikostreuung nötig."
)
