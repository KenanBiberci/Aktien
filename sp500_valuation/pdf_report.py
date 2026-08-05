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
_REPL = {"—": "-", "–": "-", "−": "-", "…": "...", "€": "EUR", "’": "'", "‘": "'",
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


def _signal_explanation(row: dict[str, Any]) -> str:
    """Klartext-Begründung, warum dieses Signal zustande kommt (für Einsteiger)."""
    sig = str(row.get("signal", "-"))
    au = _pct(row.get("avg_upside"))
    parts: list[str] = []
    if sig == "STRONG BUY":
        parts.append(f"Das Modell hält die Aktie für deutlich unterbewertet: Der geschätzte "
                     f"faire Wert liegt im Schnitt {au} über dem aktuellen Kurs — mehr als die "
                     f"Schwelle von +30 % für 'STRONG BUY', und mindestens drei Methoden stützen das.")
    elif sig == "BUY":
        parts.append(f"Günstig bewertet: Der faire Wert liegt im Schnitt {au} über dem Kurs "
                     f"(Schwelle für 'BUY': ab +10 %).")
    elif sig == "HOLD":
        parts.append(f"Fair bewertet: Der geschätzte faire Wert liegt nahe am Kurs ({au}) — "
                     f"weder klar günstig noch klar teuer (HOLD-Bereich −10 % bis +10 %).")
    elif sig == "REDUCE":
        parts.append(f"Nach dem Modell eher zu teuer: Der faire Wert liegt bei {au} zum Kurs, "
                     f"also unter der HOLD-Schwelle von −10 %.")
    else:
        parts.append("Zu wenige belastbare Daten für ein klares Signal (Datenlücke).")

    n = row.get("n_methods")
    if _is_num(n):
        parts.append(f"Grundlage sind {int(n)} von 8 Bewertungsmethoden; ihr Median ergibt den "
                     f"'fairen Wert' (Blended Fair Value).")
    div = row.get("divergence")
    if _is_num(div) and float(div) >= 1.0:
        parts.append(f"Achtung: Die Methoden weichen stark voneinander ab (Divergenz {_pct(div)}), "
                     f"die Schätzung ist also unsicher.")
    vc = str(row.get("value_creation") or "")
    if vc == "Ja":
        parts.append("Pluspunkt: Das Unternehmen verdient mehr als seine Eigenkapitalkosten "
                     "(es schafft Wert).")
    elif vc == "Nein":
        parts.append("Hinweis: Die Eigenkapitalrendite liegt unter den Eigenkapitalkosten "
                     "(im Modell keine Wertschaffung).")
    rec = row.get("rec_key")
    if rec and str(rec).lower() not in ("nan", "none", ""):
        parts.append(f"Zum Vergleich: Analysten-Konsens laut Datenquelle ist '{rec}'.")
    return " ".join(parts)


# Einsteiger-Glossar: Fachbegriffe einfach erklärt.
GLOSSARY: list[tuple[str, str]] = [
    ("Signal & Ø-Upside",
     "Ø-Upside = wie viel Prozent über (oder unter) dem aktuellen Kurs der geschätzte faire "
     "Wert liegt. Daraus das Signal: STRONG BUY (ab +30 % und mind. 3 Methoden), "
     "BUY (ab +10 %), HOLD (−10 % bis +10 %), REDUCE (unter −10 %)."),
    ("Fairer Wert / Blended Fair Value",
     "Jede Methode schätzt einen 'fairen' Aktienpreis. Der Blended Fair Value ist der Median "
     "(die robuste Mitte) aller gültigen Methoden — unempfindlich gegen einzelne Ausreißer."),
    ("KGV (Kurs-Gewinn-Verhältnis)",
     "Kurs geteilt durch Gewinn je Aktie. Zeigt, wie viele Jahresgewinne man für die Aktie "
     "zahlt. Niedrig = tendenziell günstig, hoch = teuer (oder hohe Wachstumserwartung)."),
    ("Gordon-Growth-Modell",
     "Bewertet eine Aktie über ihre Dividende, die konstant mit einer festen Rate wächst: "
     "fairer Wert = nächste Dividende / (Renditeanspruch − Wachstum). Sinnvoll nur bei "
     "stabilen Dividendenzahlern."),
    ("Dividenden-Diskont-Modell (DDM, 2-stufig)",
     "Wie Gordon-Growth, aber mit zwei Phasen: erst einige Jahre höheres Wachstum, danach "
     "dauerhaft niedriges. Summiert die auf heute abgezinsten künftigen Dividenden."),
    ("Fundamentales (gerechtfertigtes) KGV",
     "Leitet aus Ausschüttungsquote, Renditeanspruch und Wachstum ab, welches KGV 'fair' wäre, "
     "und multipliziert es mit dem erwarteten Gewinn je Aktie."),
    ("P/S und P/B",
     "P/S = Kurs/Umsatz je Aktie, P/B = Kurs/Buchwert je Aktie. Nützlich, wenn Gewinne "
     "schwanken oder negativ sind. Verglichen wird mit dem Median der Branche."),
    ("EV/EBITDA",
     "Unternehmenswert inkl. Schulden im Verhältnis zum operativen Ergebnis (EBITDA). Macht "
     "Firmen mit unterschiedlicher Verschuldung vergleichbar."),
    ("DCF / FCFF",
     "Discounted Cash Flow: schätzt den Wert aus den künftigen freien Cashflows, abgezinst auf "
     "heute. Die 'ehrlichste', aber am stärksten von Annahmen abhängige Methode."),
    ("PVGO",
     "Barwert der Wachstumschancen: welcher Teil des Kurses steckt in erwartetem Wachstum "
     "(statt im heutigen Geschäft). Hoch = viel Fantasie im Kurs."),
    ("Trefferquote (Backtest)",
     "Anteil der letzten ~20 Jahre, in denen ein 12-Monats-Halten Gewinn gebracht hätte. "
     "Reine Vergangenheit — keine Garantie für die Zukunft."),
    ("Analysten-Konsens",
     "Durchschnittliche Empfehlung professioneller Analysten (buy/hold/sell) — zum Vergleich "
     "mit dem Modell-Signal."),
    ("Divergenz",
     "Wie weit die einzelnen Methoden auseinanderliegen. Hoch = die Schätzung ist unsicher, "
     "Annahmen kritisch prüfen."),
]


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
    _glossary_pages(pdf)

    out = pdf.output()
    return bytes(out)


def _glossary_pages(pdf: "_PDF") -> None:
    """Einsteiger-Glossar am Ende (erklärt KGV, Gordon-Growth, DDM, …)."""
    pdf.add_page()
    pdf.set_text_color(*C_HEADER)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Glossar für Einsteiger")
    pdf.ln(14)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, 4.5,
                   _s("Kurz erklärt, was hinter den Kennzahlen und Methoden steckt. "
                      "Alle Werte sind regelbasierte Schätzungen — keine Anlageberatung."))
    pdf.ln(3)
    for term, text in GLOSSARY:
        # grober Umbruch-Schutz: neue Seite, wenn unten kein Platz mehr ist
        if pdf.get_y() > 258:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*C_HEADER)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(pdf.epw, 6, _s(term))
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*C_TEXT)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(pdf.epw, 4.6, _s(text))
        pdf.ln(2.5)


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
    pdf.ln(7)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 7, _s("Je Aktie: Kennzahlen, alle 9 Methoden und 'Warum dieses Signal?'. "
                      "Am Ende: Glossar fuer Einsteiger."), align="L")
    pdf.set_text_color(*C_TEXT)
    pdf.ln(11)

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

    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*C_HEADER)
    pdf.cell(0, 8, "Warum dieses Signal?")
    pdf.ln(9)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*C_TEXT)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, 4.6, _s(_signal_explanation(row)))


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
