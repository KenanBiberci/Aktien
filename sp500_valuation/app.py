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
import yaml

import pdf_report

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
PARQUET_PATH = BASE_DIR / "data" / "latest.parquet"
CSV_FALLBACK = BASE_DIR / "data" / "latest.csv"
XLSX_PATH = BASE_DIR / "output" / "latest.xlsx"

# Mindestanzahl getesteter Jahre, damit Trefferquote/Ø-Rendite als aussagekräftig
# gelten. Neuemissionen/Spinoffs (z. B. SanDisk, nur 1 Jahr) liefern sonst
# irreführende Werte (+1130 %, 100 % Trefferquote).
MIN_BT_YEARS = 5

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
    df = None
    if PARQUET_PATH.exists():
        try:
            df = pd.read_parquet(PARQUET_PATH)
        except Exception:  # noqa: BLE001
            df = None
    if df is None and CSV_FALLBACK.exists():
        df = pd.read_csv(CSV_FALLBACK)
    if df is None:
        return None

    # Backtest-Aussagekraft: Anzahl getesteter Jahre aus den Jahresrenditen ableiten.
    # Titel mit zu kurzer Historie (Neuemissionen/Spinoffs) bekommen KEINE
    # Trefferquote/Ø-Rendite, damit sie nicht mit Fantasiewerten oben auftauchen.
    if "annual_returns_json" in df.columns:
        df["n_years_1y"] = df["annual_returns_json"].apply(_n_years)
        thin = df["n_years_1y"] < MIN_BT_YEARS
        for col in ("win_rate_1y", "avg_return_1y"):
            if col in df.columns:
                df.loc[thin, col] = float("nan")
    return df


def _n_years(raw) -> int:
    try:
        return len(json.loads(raw).get("returns", [])) if isinstance(raw, str) else 0
    except Exception:  # noqa: BLE001
        return 0


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


def _pct0(v) -> str:
    """Prozent ohne Vorzeichen (für Quoten wie die Trefferquote)."""
    return f"{float(v) * 100:.0f} %" if _is_num(v) else "—"


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


@st.cache_data(ttl=600)
def load_config() -> dict:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def _resignal(avg_upside, n_methods, divergence, confidence, cfg) -> str:
    """Signal aus Ø-Upside neu ableiten (gleiche Logik/Gate wie in valuation.py)."""
    th = cfg.get("signal_thresholds", {})
    sb, bu, hf = (float(th.get("strong_buy", 0.30)), float(th.get("buy", 0.10)),
                  float(th.get("hold_floor", -0.10)))
    block = float(cfg.get("confidence", {}).get("block_strong_buy_above", 0.60))
    if not _is_num(avg_upside):
        return "N/A – Datenlücke"
    n = int(n_methods) if _is_num(n_methods) else 0
    if avg_upside >= sb and n >= 3:
        sig = "STRONG BUY"
    elif avg_upside >= bu:
        sig = "BUY"
    elif avg_upside >= hf:
        sig = "HOLD"
    else:
        sig = "REDUCE"
    if sig == "STRONG BUY" and _is_num(divergence) and float(divergence) > block:
        sig = "BUY"
    if str(confidence) == "Niedrig" and sig != "N/A – Datenlücke":
        if sig == "STRONG BUY":
            sig = "BUY"
        sig = f"{sig} (niedrige Konfidenz)"
    return sig


def _live_prices(tickers: list[str], chunk: int = 60) -> dict[str, float]:
    """Aktuelle Kurse (Notierungswährung) je Ticker, in Häppchen gegen Rate-Limits."""
    import time

    import yfinance as yf

    import backtest

    prices: dict[str, float] = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            data = yf.download(part, period="5d", interval="1d", progress=False,
                               group_by="ticker", threads=True)
        except Exception:  # noqa: BLE001 — Teil-Fehler überspringen
            continue
        if data is None or getattr(data, "empty", True):
            continue
        for t in part:
            s = backtest._close_series(data, t)
            if s is None:
                continue
            s = s.dropna()
            if not s.empty and float(s.iloc[-1]) > 0:
                prices[t] = float(s.iloc[-1])
        time.sleep(0.4)
    return prices


def refresh_prices_live(df: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Holt aktuelle Kurse (yfinance, in Häppchen), rechnet nach EUR um und
    aktualisiert Kurs + Upsides + Signal. Fundamentaldaten/faire Werte bleiben
    vom letzten Cloud-Lauf. Rückgabe: (df, Anzahl aktualisiert, Anzahl gesamt)."""
    import fetch

    cfg = load_config()
    tickers = df["yahoo"].astype(str).tolist()
    rates = fetch.get_fx_rates(cfg)
    prices = _live_prices(tickers)

    out = df.copy()
    updated = 0
    for i in out.index:
        t = str(out.at[i, "yahoo"])
        p_nat = prices.get(t)
        if p_nat is None:
            continue
        ccy = str(out.at[i, "currency_native"]) if "currency_native" in out.columns else "USD"
        divisor = rates.get(ccy, 1.0) or 1.0
        p_eur = p_nat / divisor
        if not (p_eur > 0):
            continue
        out.at[i, "price"] = p_eur
        bfv = out.at[i, "blended_fair_value"] if "blended_fair_value" in out.columns else float("nan")
        tgt = out.at[i, "target"] if "target" in out.columns else float("nan")
        b_up = (float(bfv) / p_eur - 1) if _is_num(bfv) else float("nan")
        c_up = (float(tgt) / p_eur - 1) if _is_num(tgt) else float("nan")
        ups = [u for u in (b_up, c_up) if _is_num(u)]
        a_up = sum(ups) / len(ups) if ups else float("nan")
        out.at[i, "blended_upside"] = b_up
        out.at[i, "consensus_upside"] = c_up
        out.at[i, "avg_upside"] = a_up
        if "signal" in out.columns:
            out.at[i, "signal"] = _resignal(
                a_up, out.at[i, "n_methods"] if "n_methods" in out.columns else 0,
                out.at[i, "divergence"] if "divergence" in out.columns else float("nan"),
                out.at[i, "confidence"] if "confidence" in out.columns else "",
                cfg)
        updated += 1
    return out, updated, len(out)


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

# Falls die Kurse zuvor live aktualisiert wurden: diese Version verwenden.
if "live_df" in st.session_state:
    df = st.session_state["live_df"]

label, _ = last_run()
col_a, col_b, col_c = st.columns([2, 1, 1])
with col_a:
    stamp = st.session_state.get("live_stamp")
    st.metric("Letzter Lauf", label,
              delta=(f"Kurse live: {stamp}" if stamp else None), delta_color="off")
with col_b:
    st.write("")
    if st.button("💹 Kurse aktualisieren", use_container_width=True,
                 help="Holt die aktuellen Börsenkurse live und rechnet Upside & Signal "
                      "sofort neu (faire Werte bleiben vom letzten Lauf)."):
        with st.spinner("Hole aktuelle Kurse (in Häppchen) …"):
            try:
                updated_df, n_upd, n_tot = refresh_prices_live(load_data())
                st.session_state["live_df"] = updated_df
                st.session_state["live_stamp"] = datetime.now().strftime("%H:%M")
                st.session_state["live_msg"] = (n_upd, n_tot)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Kurse konnten nicht geladen werden: {str(exc)[:150]}")
with col_c:
    st.write("")
    if st.button("🔄 Neu berechnen", use_container_width=True,
                 help="Kompletter Neulauf in der Cloud (alle Daten, ~2–3 Min). "
                      "Benötigt GITHUB_TOKEN/GITHUB_REPO in den App-Secrets."):
        trigger_workflow()

# Rückmeldung des letzten Live-Kurs-Updates (übersteht das st.rerun)
if st.session_state.get("live_msg"):
    n_upd, n_tot = st.session_state["live_msg"]
    if n_upd >= max(1, int(0.6 * n_tot)):
        st.success(f"✅ {n_upd} von {n_tot} Kursen live aktualisiert.")
    elif n_upd > 0:
        st.warning(f"⚠️ Nur {n_upd} von {n_tot} Kursen aktualisiert — Yahoo hat den Rest "
                   "gerade gedrosselt. Gleich nochmal probieren.")
    else:
        st.error("Keine Live-Kurse erhalten (Yahoo drosselt gerade). "
                 "Bitte in ein paar Minuten erneut versuchen.")
    del st.session_state["live_msg"]

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
    has_conf = "confidence" in df.columns
    only_high_conf = st.checkbox("Nur hohe Konfidenz", value=False, disabled=not has_conf,
                                 help="Nur Titel, bei denen die Methoden gut übereinstimmen "
                                      "(geringe Divergenz, genug Methoden).")
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
if has_conf and only_high_conf:
    view = view[view["confidence"] == "Hoch"]
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
        "signal": "Signal", "confidence": "Konfidenz", "rec_key": "Konsens",
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
    conf_bg = {"Hoch": "#d7f7df", "Mittel": "#ffeb9c", "Niedrig": "#ffc7ce"}

    def _color_conf(val: str) -> str:
        bg = conf_bg.get(str(val), "")
        return f"background-color:{bg}" if bg else ""

    styled = table.style.format(fmt, na_rep="—")
    if "Signal" in table:
        styled = styled.map(_color_sig, subset=["Signal"])
    if "Konfidenz" in table:
        styled = styled.map(_color_conf, subset=["Konfidenz"])

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
        m5.metric("Konfidenz", str(row.get("confidence") or "—"),
                  help="Wie gut die Methoden übereinstimmen (Divergenz + Anzahl).")
        m6.metric("Divergenz", _pct0(row.get("divergence")))

        st.markdown("**Bewertungsmethoden (fairer Preis je Aktie)**")
        labels = {"m1": "M1 Gordon", "m2": "M2 DDM", "m3": "M3 Justified PE",
                  "m4": "M4 Comp-PE", "m5": "M5 P/S", "m6": "M6 P/B",
                  "m7": "M7 EV/EBITDA", "m8": "M8 DCF", "m9": "M9 Asset-based"}
        rows = [{"Methode": lbl, "Fairer Preis (€)": _eur(row.get(k))}
                for k, lbl in labels.items()]
        if pd.notna(row.get("pvgo_pct")):
            rows.append({"Methode": "PVGO %", "Fairer Preis (€)": _pct(row.get("pvgo_pct"))})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        used = str(row.get("used_methods") or "")
        dropped = str(row.get("dropped_methods") or "")
        if used:
            st.caption(f"**Genutzt im Blend:** {used}")
        if dropped and dropped.lower() not in ("nan", "none"):
            st.caption(f"**Ausgeschlossen:** {dropped}")
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
                thin = len(rets) < MIN_BT_YEARS
                if thin:
                    st.warning(
                        f"⚠️ Nur **{len(rets)} Jahr(e)** Historie — Trefferquote und "
                        f"Ø-Rendite sind **nicht aussagekräftig** (z. B. Neuemission/"
                        f"Spinoff). Ein einzelnes Jahr kann durch den Börsenstart stark "
                        f"verzerrt sein. Das Diagramm zeigt die vorhandenen Jahre nur "
                        f"zur Orientierung.")
                c1, c2, c3 = st.columns(3)
                # win_rate_1y/avg_return_1y sind für zu kurze Historie bereits NaN.
                c1.metric("Trefferquote", _pct0(brow.get("win_rate_1y")),
                          help="Anteil der Jahre mit positivem 12-Monats-Ergebnis "
                               f"(erst ab {MIN_BT_YEARS} Jahren ausgewiesen).")
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
