"""
pdf_report.py — PDF-Aktienanalyse für ausgewählte Titel.

Erzeugt ein kompaktes, gut lesbares PDF (eine Seite je Aktie) mit den
Kernzahlen, allen 9 Bewertungsmethoden, PVGO und Signal — plus Titelkopf
und Disclaimer. Nutzt fpdf2 (reine Python-Lib, keine System-Abhängigkeiten).

Geldbeträge sind bereits in EUR (Umrechnung erfolgt in der Pipeline).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from fpdf import FPDF

# Farben (RGB) passend zur App/Excel
C_HEADER = (31, 56, 100)
C_GREEN = (198, 239, 206)
C_YELLOW = (255, 235, 156)
C_RED = (255, 199, 206)
C_GREY = (240, 242, 246)
C_TEXT = (33, 37, 41)

METHOD_LABELS = [
    ("M1 Gordon-Growth", "m1"), ("M2 2-Stufen-DDM", "m2"),
    ("M3 Fundamentales KGV", "m3"), ("M4 Comparable-KGV", "m4"),
    ("M5 P/S", "m5"), ("M6 P/B", "m6"), ("M7 EV/EBITDA", "m7"),
    ("M8 DCF/FCFF", "m8"), ("M9 Asset-based (Info)", "m9"),
]

DISCLAIMER = (
    "Keine Anlageberatung. Dieses Tool wendet Standard-Bewertungsmethoden mechanisch an. "
    "Die Signale sind regelbasiert und nur so gut wie die (kostenlosen) Daten und die "
    "gewaehlten Annahmen. Vor jeder Entscheidung eigene Pruefung und Risikostreuung."
)


# fpdf2 Standardfont (Helvetica) unterstützt nur Latin-1. Unicode-Satzzeichen
# (Gedankenstrich, Ellipse, €) auf sichere Entsprechungen abbilden.
_REPL = {"—": "-", "–": "-", "…": "...", "€": "EUR", "’": "'", "‘": "'",
         "“": '"', "”": '"', "‑": "-", " ": " "}


def _s(text: Any) -> str:
    t = str(text)
    for k, v in _REPL.items():
        t = t.replace(k, v)
    return t.encode("latin-1", "replace").decode("latin-1")


def _is_num(v: Any) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _eur(v: Any) -> str:
    return f"{float(v):,.2f} EUR" if _is_num(v) else "-"


def _pct(v: Any) -> str:
    return f"{float(v) * 100:,.1f} %" if _is_num(v) else "-"


def _mult(v: Any) -> str:
    return f"{float(v):,.1f}x" if _is_num(v) else "-"


def _signal_color(signal: str) -> tuple[int, int, int]:
    if signal in ("STRONG BUY", "BUY"):
        return C_GREEN
    if signal == "HOLD":
        return C_YELLOW
    if signal == "REDUCE":
        return C_RED
    return C_GREY


class _PDF(FPDF):
    def header(self) -> None:  # noqa: D401
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 6, "S&P-500 Valuation - Aktienanalyse", align="L")
        self.ln(8)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(150, 150, 150)
        self.cell(0, 6, f"Seite {self.page_no()} - keine Anlageberatung", align="C")


def build_analysis_pdf(
    rows: list[dict[str, Any]],
    run_date: datetime | None = None,
    fx_eurusd: float | None = None,
) -> bytes:
    """rows: Liste von Zeilen-Dicts (aus latest.parquet). Rückgabe: PDF als bytes."""
    run_date = run_date or datetime.now()
    pdf = _PDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)

    _cover(pdf, rows, run_date, fx_eurusd)
    for row in rows:
        _stock_page(pdf, row)

    out = pdf.output()
    return bytes(out)


def _cover(pdf: _PDF, rows: list[dict[str, Any]], run_date: datetime,
           fx: float | None) -> None:
    pdf.add_page()
    pdf.set_text_color(*C_HEADER)
    pdf.set_font("Helvetica", "B", 22)
    pdf.ln(20)
    pdf.cell(0, 12, "Aktienanalyse", align="L")
    pdf.ln(14)
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(*C_TEXT)
    pdf.cell(0, 8, "S&P-500 Valuation-Pipeline", align="L")
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, f"Erstellt am {run_date.strftime('%d.%m.%Y %H:%M')}", align="L")
    pdf.ln(6)
    if fx:
        pdf.cell(0, 7, f"Waehrung EUR  (1 EUR = {fx:.4f} USD)", align="L")
        pdf.ln(6)
    pdf.cell(0, 7, f"Ausgewaehlte Titel: {len(rows)}", align="L")
    pdf.ln(12)

    # Kurz-Übersicht als Liste
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(*C_HEADER)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(70, 8, " Titel", border=0, fill=True)
    pdf.cell(45, 8, "Kurs", border=0, fill=True, align="R")
    pdf.cell(35, 8, "Ø-Upside", border=0, fill=True, align="R")
    pdf.cell(40, 8, "Signal ", border=0, fill=True, align="R")
    pdf.ln(8)
    pdf.set_text_color(*C_TEXT)
    pdf.set_font("Helvetica", "", 10)
    for row in rows:
        name = _clip(f"{row.get('yahoo', '')}  {row.get('security', '')}", 34)
        pdf.cell(70, 7, _s(f" {name}"))
        pdf.cell(45, 7, _eur(row.get("price")), align="R")
        pdf.cell(35, 7, _pct(row.get("avg_upside")), align="R")
        sig = str(row.get("signal", "-"))
        pdf.set_fill_color(*_signal_color(sig))
        pdf.cell(40, 7, _s(f"{sig} "), align="R", fill=True)
        pdf.ln(7)


def _stock_page(pdf: _PDF, row: dict[str, Any]) -> None:
    pdf.add_page()
    name = _s(row.get("security", ""))
    ticker = _s(row.get("yahoo", ""))
    sector = _s(row.get("sector", ""))

    # Kopf
    pdf.set_fill_color(*C_HEADER)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 11, f"  {name}", fill=True)
    pdf.ln(11)
    pdf.set_fill_color(*C_GREY)
    pdf.set_text_color(*C_TEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, f"  {ticker}   |   {sector}", fill=True)
    pdf.ln(12)

    # Signal-Badge + Kernzahlen
    sig = str(row.get("signal", "-"))
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_fill_color(*_signal_color(sig))
    pdf.cell(0, 10, _s(f"  Signal: {sig}"), fill=True)
    pdf.ln(13)

    _kv_grid(pdf, [
        ("Kurs", _eur(row.get("price"))),
        ("Blended Fair Value", _eur(row.get("blended_fair_value"))),
        ("Ø-Upside", _pct(row.get("avg_upside"))),
        ("Blended Upside", _pct(row.get("blended_upside"))),
        ("Konsens-Upside", _pct(row.get("consensus_upside"))),
        ("Analysten-Konsens", str(row.get("rec_key") or "—")),
        ("KGV fwd", _mult(row.get("kgv_fwd"))),
        ("Div-Rendite", _pct(row.get("div_yield"))),
        ("PVGO %", _pct(row.get("pvgo_pct"))),
        ("Value Creation (ROE>r)", str(row.get("value_creation") or "—")),
        ("#Methoden", str(int(row["n_methods"])) if _is_num(row.get("n_methods")) else "—"),
        ("Divergenz", _pct(row.get("divergence"))),
    ])
    pdf.ln(4)

    # Methoden-Tabelle
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*C_HEADER)
    pdf.cell(0, 8, "Bewertungsmethoden (fairer Preis je Aktie)")
    pdf.ln(9)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*C_TEXT)
    fill = False
    for label, key in METHOD_LABELS:
        pdf.set_fill_color(*(C_GREY if fill else (255, 255, 255)))
        pdf.cell(120, 7, f"  {label}", fill=True)
        pdf.cell(70, 7, _eur(row.get(key)) + "  ", align="R", fill=True)
        pdf.ln(7)
        fill = not fill

    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 4, DISCLAIMER)


def _kv_grid(pdf: _PDF, items: list[tuple[str, str]]) -> None:
    """Zweispaltiges Label/Wert-Raster."""
    col_w = 95
    pdf.set_font("Helvetica", "", 10)
    for i in range(0, len(items), 2):
        for j in range(2):
            if i + j >= len(items):
                break
            label, value = items[i + j]
            pdf.set_text_color(110, 110, 110)
            pdf.cell(42, 7, _s(f"  {label}"))
            pdf.set_text_color(*C_TEXT)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(col_w - 42, 7, _s(value))
            pdf.set_font("Helvetica", "", 10)
        pdf.ln(7)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "..."
