"""
app.py — mobil-taugliche, moderne Streamlit-Web-App (iPhone-Oberfläche).

Rechnet NICHT live 500 Ticker, sondern lädt das fertige Ergebnis
(data/latest.parquet) und rendert es. Funktionen:
- Übersicht mit Signal-Kacheln
- Screener-Tabelle (Ticker + Name, Kurse in EUR, farbige Signale)
- Detail-Ansicht je Aktie (9 Methoden + PVGO)
- PDF-Aktienanalyse für ausgewählte Titel (Download)
- Excel-Download, "Neu berechnen" (löst GitHub-Actions-Workflow aus)

Geldbeträge sind bereits in EUR (Umrechnung erfolgt in der Pipeline).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

import pdf_report

BASE_DIR = Path(__file__).resolve().parent
PARQUET_PATH = BASE_DIR / "data" / "latest.parquet"
CSV_FALLBACK = BASE_DIR / "data" / "latest.csv"
XLSX_PATH = BASE_DIR / "output" / "latest.xlsx"

SIGNAL_ORDER = ["STRONG BUY", "BUY", "HOLD", "REDUCE", "N/A – Datenlücke"]
SIGNAL_COLORS = {
    "STRONG BUY": "#1a7f37", "BUY": "#2da44e",
    "HOLD": "#bf8700", "REDUCE": "#cf222e", "N/A – Datenlücke": "#8c959f",
}
SIGNAL_BG = {
    "STRONG BUY": "#c6efce", "BUY": "#d7f7df",
    "HOLD": "#ffeb9c", "REDUCE": "#ffc7ce", "N/A – Datenlücke": "#eaeef2",
}

st.set_page_config(page_title="S&P-500 Valuation", layout="centered",
                   page_icon="📈", initial_sidebar_state="collapsed")

# --- modernes, mobiles Styling -----------------------------------------------
st.markdown(
    """
    <style>
      .block-container {padding-top: 1.2rem; padding-bottom: 3rem; max-width: 780px;}
      h1, h2, h3 {letter-spacing: -0.02em;}
      /* Übersichts-Kacheln */
      .kpi {border-radius: 14px; padding: 14px 12px; text-align: center;
            color: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.12);}
      .kpi .num {font-size: 1.6rem; font-weight: 700; line-height: 1;}
      .kpi .lbl {font-size: .72rem; text-transform: uppercase; opacity:.95;
                 letter-spacing:.04em; margin-top:4px;}
      /* Signal-Badge */
      .badge {display:inline-block; padding:3px 12px; border-radius:999px;
              font-weight:700; font-size:.85rem; color:#0b1f12;}
      /* Buttons größer für Touch */
      .stButton>button, .stDownloadButton>button {border-radius:10px; padding:.55rem 1rem;
              font-weight:600; width:100%;}
      div[data-testid="stMetricValue"] {font-size:1.3rem;}
      /* Tabs breiter/lesbarer */
      button[data-baseweb="tab"] {font-size:0.95rem; font-weight:600;}
    </style>
    """,
    unsafe_allow_html=True,
)


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


def last_run() -> tuple[str, datetime | None]:
    for path in (PARQUET_PATH, CSV_FALLBACK):
        if path.exists():
            ts = datetime.fromtimestamp(path.stat().st_mtime)
            return ts.strftime("%d.%m.%Y %H:%M"), ts
    return "—", None


def trigger_workflow() -> None:
    try:
        token = st.secrets["GITHUB_TOKEN"]
        repo = st.secrets["GITHUB_REPO"]
        workflow = st.secrets.get("GITHUB_WORKFLOW", "run.yml")
        ref = st.secrets.get("GITHUB_REF", "main")
    except Exception:  # noqa: BLE001
        st.warning("Kein GitHub-Token in den App-Secrets — 'Neu berechnen' ist deaktiviert. "
                   "Hinterlege GITHUB_TOKEN und GITHUB_REPO in den Streamlit-Secrets.")
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    resp = requests.post(
        url, headers={"Authorization": f"Bearer {token}",
                      "Accept": "application/vnd.github+json"},
        json={"ref": ref}, timeout=20)
    if resp.status_code in (201, 204):
        st.success("Neuberechnung gestartet — läuft in der Cloud, in ~2–3 Min. aktualisiert.")
    else:
        st.error(f"Konnte Workflow nicht auslösen ({resp.status_code}).")


def _is_num(v) -> bool:
    try:
        return v is not None and pd.notna(v) and float(v) == float(v)
    except (TypeError, ValueError):
        return False


def _eur(v) -> str:
    return f"{float(v):,.2f} €" if _is_num(v) else "—"


def _pct(v) -> str:
    return f"{float(v) * 100:+.1f} %" if _is_num(v) else "—"


def _mult(v) -> str:
    return f"{float(v):,.1f}x" if _is_num(v) else "—"


def signal_label(ticker: str, name: str) -> str:
    return f"{ticker} — {name}" if name else ticker


def fx_rate(df: pd.DataFrame) -> float | None:
    if "fx_eurusd" in df.columns and len(df):
        try:
            return float(df["fx_eurusd"].dropna().iloc[0])
        except Exception:  # noqa: BLE001
            return None
    return None


# =============================================================================
# Laden
# =============================================================================
df = load_data()

st.title("📈 S&P-500 Valuation")
st.caption("Regelbasiertes Bewertungsmodell · Beträge in **Euro** · **keine Anlageberatung**")

if df is None:
    st.error("Noch kein Ergebnis vorhanden. Starte den GitHub-Actions-Workflow "
             "oder lokal `python main.py`, damit data/latest.parquet entsteht.")
    st.stop()

label, _ = last_run()
col_a, col_b = st.columns([2, 1])
with col_a:
    st.metric("Letzter Lauf", label)
with col_b:
    st.write("")
    if st.button("🔄 Neu berechnen"):
        trigger_workflow()

# --- Übersichts-Kacheln je Signal --------------------------------------------
counts = df["signal"].value_counts().to_dict() if "signal" in df.columns else {}
kpi_cols = st.columns(4)
for c, sig in zip(kpi_cols, ["STRONG BUY", "BUY", "HOLD", "REDUCE"]):
    with c:
        st.markdown(
            f"<div class='kpi' style='background:{SIGNAL_COLORS[sig]}'>"
            f"<div class='num'>{int(counts.get(sig, 0))}</div>"
            f"<div class='lbl'>{sig}</div></div>",
            unsafe_allow_html=True,
        )

st.divider()

# =============================================================================
# Filter
# =============================================================================
query = st.text_input("🔍 Suche (Ticker oder Name)", value="",
                      placeholder="z. B. SAP, Apple, Nestlé …")

has_prob = "win_rate_1y" in df.columns and df["win_rate_1y"].notna().any()

with st.expander("🔎 Filter & Sortierung", expanded=not query):
    sectors = sorted([s for s in df.get("sector", pd.Series(dtype=str)).dropna().unique()])
    sel_sectors = st.multiselect("Sektor", options=sectors, default=[])
    present = [s for s in SIGNAL_ORDER if s in set(df.get("signal", []))]
    sel_signals = st.multiselect(
        "Signal", options=present,
        default=[s for s in ("STRONG BUY", "BUY") if s in present])
    # Regler in ganzen Prozent (−50 % … +200 % bzw. 0 % … 100 %); intern /100.
    min_upside = st.slider("Mindest-Ø-Upside", min_value=-50, max_value=200,
                           value=0, step=5, format="%d %%",
                           help="Nur Aktien mit mindestens diesem durchschnittlichen "
                                "Kurspotenzial (fairer Wert vs. aktueller Kurs).")
    min_prob = st.slider("Mindest-Trefferquote (12M, ~20 J.)", min_value=0,
                         max_value=100, value=0, step=5, format="%d %%",
                         disabled=not has_prob,
                         help="Anteil der letzten ~20 Jahre, in denen ein 12-Monats-"
                              "Halten Gewinn gebracht hätte.")
    sort_options = {"Ø-Upside": "avg_upside", "Blended Upside": "blended_upside",
                    "Kurs": "price"}
    if has_prob:
        sort_options = {"Trefferquote (Wahrscheinlichkeit)": "win_rate_1y", **sort_options}
    sort_label = st.selectbox("Sortieren nach", list(sort_options.keys()))
    sort_col = sort_options[sort_label]

view = df.copy()
# Suche hat Vorrang: greift sie, werden die Signal-/Upside-Filter gelockert,
# damit man jede Aktie unabhängig vom aktuellen Filter findet.
if query.strip():
    q = query.strip().lower()
    mask = (view["yahoo"].astype(str).str.lower().str.contains(q, na=False)
            | view.get("security", pd.Series("", index=view.index))
                  .astype(str).str.lower().str.contains(q, na=False))
    view = view[mask]
    if sel_sectors:
        view = view[view["sector"].isin(sel_sectors)]
else:
    if sel_sectors:
        view = view[view["sector"].isin(sel_sectors)]
    if sel_signals:
        view = view[view["signal"].isin(sel_signals)]
    if "avg_upside" in view.columns:
        view = view[view["avg_upside"].fillna(-99) >= min_upside / 100]
    if has_prob and min_prob > 0:
        view = view[view["win_rate_1y"].fillna(-1) >= min_prob / 100]
if sort_col not in view.columns:
    sort_col = "avg_upside"
view = view.sort_values(sort_col, ascending=False, na_position="last")

st.caption(f"**{len(view)}** von {len(df)} Titeln")

# Auswahllisten für Detail/Backtest/PDF: IMMER das ganze Universum (gefilterte
# Titel zuerst), damit man jede Aktie wählen kann — auch wenn sie der Filter
# gerade ausblendet (z. B. Spotify bei aktivem STRONG-BUY-Filter).
ALL_NAMES = dict(zip(df["yahoo"].astype(str),
                     df.get("security", df["yahoo"]).astype(str)))
_view_ids = view["yahoo"].astype(str).tolist()
ORDERED_IDS = _view_ids + [t for t in df["yahoo"].astype(str).tolist()
                           if t not in set(_view_ids)]

# =============================================================================
# Tabs
# =============================================================================
tab_screener, tab_detail, tab_backtest, tab_pdf = st.tabs(
    ["📊 Screener", "🔍 Detail", "📉 Backtest", "📄 PDF-Report"])

# --- Screener ----------------------------------------------------------------
with tab_screener:
    core = {
        "yahoo": "Ticker", "security": "Name", "price": "Kurs",
        "kgv_fwd": "KGV fwd", "avg_upside": "Ø-Upside", "win_rate_1y": "Trefferquote",
        "signal": "Signal", "rec_key": "Konsens",
    }
    avail = {k: v for k, v in core.items() if k in view.columns}
    table = view[list(avail.keys())].rename(columns=avail)

    def _color_sig(val: str) -> str:
        bg = SIGNAL_BG.get(val, "")
        return f"background-color:{bg}; font-weight:600" if bg else ""

    fmt = {}
    if "Kurs" in table:
        fmt["Kurs"] = "{:,.2f} €"
    if "KGV fwd" in table:
        fmt["KGV fwd"] = "{:,.1f}x"
    if "Ø-Upside" in table:
        fmt["Ø-Upside"] = "{:+.1%}"
    if "Trefferquote" in table:
        fmt["Trefferquote"] = "{:.0%}"
    styled = table.style.format(fmt, na_rep="—")
    if "Signal" in table:
        styled = styled.map(_color_sig, subset=["Signal"])

    st.dataframe(styled, use_container_width=True, hide_index=True,
                 height=min(560, 44 + 35 * len(table)))

    if XLSX_PATH.exists():
        with XLSX_PATH.open("rb") as fh:
            st.download_button(
                "⬇️ Excel-Workbook laden", data=fh.read(),
                file_name="sp500_valuation_latest.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# --- Detail ------------------------------------------------------------------
with tab_detail:
    if ORDERED_IDS:
        sel = st.selectbox("Aktie wählen", options=ORDERED_IDS,
                           format_func=lambda t: signal_label(t, str(ALL_NAMES.get(t, ""))))
        row = df[df["yahoo"] == sel].iloc[0]

        st.subheader(f"{row.get('security', sel)}  ·  {sel}")
        sig = str(row.get("signal", "—"))
        st.markdown(
            f"<span class='badge' style='background:{SIGNAL_BG.get(sig, '#eaeef2')}'>"
            f"{sig}</span>  <span style='color:#6a737d'>{row.get('sector','')}</span>",
            unsafe_allow_html=True)

        m1, m2, m3 = st.columns(3)
        m1.metric("Kurs", _eur(row.get("price")))
        m2.metric("Blended Fair Value", _eur(row.get("blended_fair_value")))
        m3.metric("Ø-Upside", _pct(row.get("avg_upside")))
        m4, m5, m6 = st.columns(3)
        m4.metric("KGV fwd", _mult(row.get("kgv_fwd")))
        m5.metric("Div-Rendite", _pct(row.get("div_yield")))
        m6.metric("Konsens", str(row.get("rec_key") or "—"))

        st.markdown("**Bewertungsmethoden (fairer Preis je Aktie)**")
        labels = {"m1": "M1 Gordon", "m2": "M2 DDM", "m3": "M3 Justified PE",
                  "m4": "M4 Comp-PE", "m5": "M5 P/S", "m6": "M6 P/B",
                  "m7": "M7 EV/EBITDA", "m8": "M8 DCF", "m9": "M9 Asset-based"}
        rows = [{"Methode": lbl, "Fairer Preis (€)": _eur(row.get(k))}
                for k, lbl in labels.items()]
        if pd.notna(row.get("pvgo_pct")):
            rows.append({"Methode": "PVGO %", "Fairer Preis (€)": _pct(row.get("pvgo_pct"))})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("Keine Titel im aktuellen Filter.")

# --- Backtest ----------------------------------------------------------------
with tab_backtest:
    st.markdown("**Backtest — 12-Monats-Halten, jahresweise über ~20 Jahre**")
    st.caption("Wenn man Anfang eines Jahres gekauft und **12 Monate gehalten** hätte — "
               "wie wäre es ausgegangen? Grün = Gewinn, Rot = Verlust.")
    if "annual_returns_json" not in df.columns:
        st.info("Backtest-Daten sind erst nach dem nächsten Cloud-Lauf verfügbar.")
    else:
        sel_bt = st.selectbox("Aktie für den Backtest", options=ORDERED_IDS,
                              format_func=lambda t: signal_label(t, str(ALL_NAMES.get(t, ""))),
                              key="bt_select")
        brow = df[df["yahoo"] == sel_bt].iloc[0]
        raw = brow.get("annual_returns_json")
        if not raw or (isinstance(raw, float) and pd.isna(raw)):
            st.info("Für diesen Titel liegen keine ausreichenden Kurshistorien vor.")
        else:
            data = json.loads(raw)
            years, rets = data.get("years", []), data.get("returns", [])
            if not rets:
                st.info("Keine Backtest-Daten für diesen Titel.")
            else:
                c1, c2, c3 = st.columns(3)
                c1.metric("Trefferquote", _pct(brow.get("win_rate_1y")),
                          help="Anteil der Jahre mit positivem 12-Monats-Ergebnis.")
                c2.metric("Ø 12M-Rendite", _pct(brow.get("avg_return_1y")))
                c3.metric("Jahre getestet", str(len(rets)))

                cdf = pd.DataFrame({
                    "Zeitraum": [f"{y}→{y + 1}" for y in years],
                    "Rendite": rets,
                    "Ergebnis": ["Gewinn" if r >= 0 else "Verlust" for r in rets],
                })
                chart = (
                    alt.Chart(cdf).mark_bar().encode(
                        x=alt.X("Zeitraum:N", sort=None, title=None,
                                axis=alt.Axis(labelAngle=-55)),
                        y=alt.Y("Rendite:Q", axis=alt.Axis(format="%"), title="12M-Rendite"),
                        color=alt.Color("Ergebnis:N",
                                        scale=alt.Scale(domain=["Gewinn", "Verlust"],
                                                        range=["#2da44e", "#cf222e"]),
                                        legend=None),
                        tooltip=[alt.Tooltip("Zeitraum:N"),
                                 alt.Tooltip("Rendite:Q", format="+.1%")],
                    ).properties(height=320)
                )
                st.altair_chart(chart, use_container_width=True)

                best_i = int(pd.Series(rets).idxmax())
                worst_i = int(pd.Series(rets).idxmin())
                st.caption(
                    f"Bestes Jahr: **{years[best_i]}→{years[best_i] + 1}** "
                    f"({rets[best_i] * 100:+.1f} %) · "
                    f"schlechtestes: **{years[worst_i]}→{years[worst_i] + 1}** "
                    f"({rets[worst_i] * 100:+.1f} %). "
                    "Vergangene Wertentwicklung ist keine Garantie für die Zukunft."
                )

# --- PDF-Report --------------------------------------------------------------
with tab_pdf:
    st.markdown("**PDF-Aktienanalyse erstellen**")
    st.caption("Wähle eine oder mehrere Aktien — je Titel eine Seite mit allen "
               "Kennzahlen, den 9 Methoden und dem Signal.")
    default_sel = ORDERED_IDS[:5]
    sel_tickers = st.multiselect(
        "Aktien für den Report (alle Titel wählbar — auch per Tippen suchen)",
        options=ORDERED_IDS, default=default_sel,
        format_func=lambda t: signal_label(t, str(ALL_NAMES.get(t, ""))))

    if not sel_tickers:
        st.info("Bitte mindestens eine Aktie auswählen.")
    else:
        if st.button(f"📄 PDF erstellen ({len(sel_tickers)} Titel)"):
            with st.spinner("Erzeuge PDF …"):
                subset = df[df["yahoo"].isin(sel_tickers)]
                # Reihenfolge der Auswahl beibehalten
                subset = subset.set_index("yahoo").loc[sel_tickers].reset_index()
                _, ts = last_run()
                pdf_bytes = pdf_report.build_analysis_pdf(
                    subset.to_dict("records"), run_date=ts or datetime.now(),
                    fx_eurusd=fx_rate(df))
            st.success("PDF fertig.")
            st.download_button(
                "⬇️ PDF herunterladen", data=pdf_bytes,
                file_name=f"aktienanalyse_{datetime.now():%Y%m%d}.pdf",
                mime="application/pdf")

st.divider()
st.caption(
    "⚠️ Dieses Tool wendet Standard-Bewertungsmethoden mechanisch an. Signale sind "
    "regelbasiert und nur so gut wie die kostenlosen Daten und Annahmen. "
    "**Keine Anlageberatung.** Eigene Prüfung und Risikostreuung nötig."
)
