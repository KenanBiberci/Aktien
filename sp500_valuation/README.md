# S&P-500 Valuation-Pipeline

Zieht für **alle S&P-500-Aktien plus die großen europäischen Aktien** automatisch
Fundamentaldaten (yfinance), rechnet sie durch ein festes Bewertungsmodell (9 Methoden,
Equity-Investments-Werkzeugkasten) und gibt das Ergebnis als **Excel** mit
**Buy/Hold/Sell-Signal** aus. Alle Geldbeträge werden **währungsrichtig nach EUR** umgerechnet
(USD, GBP/Pence, CHF, SEK/DKK/NOK). Alles mit **einem Befehl**, wiederholbar, und vom
**iPhone** aus abrufbar.

> ⚠️ **Keine Anlageberatung.** Lern-/Analyse-Tool. Signale sind regelbasiert und nur so gut
> wie die kostenlosen Daten und die gewählten Annahmen. Eigene Prüfung und Risikostreuung nötig.

---

## Schnellstart

```bash
cd sp500_valuation
pip install -r requirements.txt

python main.py --limit 10        # erst an 10 Tickern testen
python main.py                   # voller Lauf (~503) -> data/latest.parquet + output/latest.xlsx
python main.py --refresh         # Cache ignorieren, neu ziehen
python main.py --details AAPL,MSFT   # zusätzliche Einzel-Detailblätter
streamlit run app.py             # lokale Web-Oberfläche (dieselbe, die aufs iPhone kommt)
```

Jeder volle Lauf schreibt **immer** `data/latest.parquet` (Ergebnis-Tabelle) und
`output/latest.xlsx` — genau diese Dateien laden die App und der iPhone-Zugriff. Zusätzlich
entsteht ein datiertes Workbook `output/sp500_valuation_YYYYMMDD.xlsx`.

---

## Projektstruktur

```
sp500_valuation/
  main.py                    # Orchestrierung / CLI (schwerer Lauf)
  fetch.py                   # Datenabruf + Caching (yfinance, optional FMP)
  valuation.py               # die 9 Methoden + Blend + Signal
  excel.py                   # Workbook-Erzeugung
  app.py                     # Streamlit-Web-App (iPhone-Oberfläche)
  config.yaml                # Annahmen (r, g, WACC, Schwellen, Multiplikatoren)
  data/cache/                # gecachte Rohdaten (json je Ticker)
  data/latest.parquet        # letztes Ergebnis (von der App geladen)
  output/latest.xlsx         # fertiges Workbook (von der App geladen)
  requirements.txt

.github/workflows/run.yml    # GitHub Action (im Repo-Wurzelverzeichnis, damit
                             # GitHub sie erkennt) — Cloud-Lauf + Ergebnis committen
```

---

## Das Bewertungsmodell (Kurz)

| Methode | Idee |
|---|---|
| **M1 Gordon-Growth** | `D1/(r-g)` — nur wenn Dividende, `r>g`, Payout ≥ Gate |
| **M2 2-Stufen-DDM** | N Jahre `g1`, danach `g` |
| **M3 Fundamentales KGV** | `justified_pe = payout/(r-g)` × EPS_fwd |
| **M4 Comparable-KGV** | Sektor-Median-PE × EPS_fwd |
| **M5 P/S** | Sektor-Median-P/S × Umsatz je Aktie |
| **M6 P/B** | Sektor-Median-P/B × Buchwert je Aktie |
| **M7 EV/EBITDA** | Sektor-Median × EBITDA → Equity je Aktie |
| **M8 DCF/FCFF** | wachsende Perpetuität, guard `wacc>g` |
| **M9 Asset-based** | Buchwert je Aktie (nur Info) |

- **Eigenkapitalkosten r (CAPM):** `r = rf + beta × ERP` (beta fehlt → 1).
- **WACC:** `(E/(E+D))·r + (D/(E+D))·r_d·(1-Steuer)`, `r_d = rf + 1,5 %`.
- **g1:** `clip(ROE·(1-payout), g, 20 %)`.
- **Sektor-Median-Multiplikatoren** sind die „fairen" Multiplikatoren der Comparable-Methoden
  (Law-of-one-price); nur wenn ein Sektor-Median fehlt → globale `fallback_multiples`.

**Blend:** `blended_fair_value = Median` der gültigen Methoden (M1–M3 nur bei Payout ≥ Gate,
M4–M8 immer). **Signal** über Ø-Upside (Mittel aus Blended- und Konsens-Upside):

| Ø-Upside | Signal |
|---|---|
| ≥ `strong_buy` und #Methoden ≥ 3 | **STRONG BUY** |
| ≥ `buy` | **BUY** |
| ≥ `hold_floor` | **HOLD** |
| darunter | **REDUCE** |
| #Methoden < 2 und kein Analystenziel | **N/A – Datenlücke** |

Alle Hebel stehen in **`config.yaml`** und sind ohne Codeänderung anpassbar.

---

## Excel-Output

Vier Blätter (Font Arial, bedingte Formatierung auf dem Signal — grün/gelb/rot):

- **Screener** — eine Zeile pro Aktie, alle Kennzahlen + M1…M8 + Blend + Signal;
  `freeze_panes`, `auto_filter`; zusätzliches Blatt sortiert nach Ø-Upside.
- **Annahmen** — globale Hebel (gelb als Eingaben markiert) + berechnete Sektor-Median-Tabelle.
- **Methodik** — Beschreibung der 9 Methoden, Signal-Schwellen, Datenquellen, Laufdatum, Disclaimer.
- **Top-Ideen** — gefiltert auf STRONG BUY / BUY, sortiert, mit leerer Notiz-Spalte.

Optional per `--details TICKER1,TICKER2`: Einzelblätter mit allen 9 Methoden + PVGO +
`r/g`-Sensitivitätsraster (`P0 = D1/(r-g)`).

---

## Konfiguration (`config.yaml`)

```yaml
risk_free_rate: 0.043
equity_risk_premium: 0.05
terminal_growth: 0.03
stage1_years: 5
ddm_payout_gate: 0.25
default_tax: 0.21
signal_thresholds: { strong_buy: 0.30, buy: 0.10, hold_floor: -0.10 }
fallback_multiples: { pe: 18, ps: 3, pb: 3, ev_ebitda: 12 }
```

**Optional FMP:** Setze `FMP_API_KEY` als Umgebungsvariable (bzw. GitHub-Secret) für bessere
Forward-EPS/Analystenziele. Ohne Key → nur yfinance.

---

## 📱 iPhone — Schritt für Schritt

Ziel: Ergebnis vom iPhone abrufen und aktualisieren, **ohne** dass das Handy die 500 Titel
selbst rechnet. Die schwere Berechnung läuft in der **Cloud** (GitHub Actions), das iPhone
zeigt nur die fertige Tabelle über die **Streamlit-App**.

**App-Funktionen** (mobil optimiert, Tabs):
- **Suche** nach Ticker oder Firmenname (z. B. „SAP", „Nestlé", „Apple").
- **Übersicht** mit Signal-Kacheln (Anzahl STRONG BUY / BUY / HOLD / REDUCE).
- **Screener** — Tabelle mit Ticker **und Firmennamen**, Kursen in **EUR**, farbigen Signalen; Excel-Download.
- **Detail** — je Aktie alle 9 Methoden + PVGO + Kennzahlen.
- **PDF-Report** — für ausgewählte Aktien eine PDF-Analyse erzeugen und herunterladen (eine Seite je Titel).
- **„Neu berechnen"** — stößt den Cloud-Lauf per `workflow_dispatch` an.

1. **Repo forken/pushen** zu GitHub.
2. **(optional)** `FMP_API_KEY` unter *Settings → Secrets and variables → Actions* hinterlegen.
3. **Action einmal manuell starten:** *Actions → „S&P-500 Valuation-Pipeline" → Run workflow*.
   Sie zieht die Daten, rechnet und committet `data/latest.parquet` + `output/latest.xlsx`.
   (Danach läuft sie werktags automatisch.)
4. **App auf Streamlit Cloud deployen:** [share.streamlit.io](https://share.streamlit.io) →
   Repo verbinden → `sp500_valuation/app.py` als Einstieg → öffentliche URL.
   Für den „Neu berechnen"-Button unter *App → Settings → Secrets*:
   ```toml
   GITHUB_TOKEN = "ghp_dein_pat_mit_actions_scope"
   GITHUB_REPO  = "deinuser/Aktien"
   GITHUB_WORKFLOW = "run.yml"
   GITHUB_REF = "main"
   ```
5. **URL am iPhone zum Home-Bildschirm hinzufügen:** in **Safari** öffnen → Teilen-Symbol →
   **„Zum Home-Bildschirm"** → liegt als Icon wie eine native App.
6. **Fertig** — Tabelle ansehen, filtern (Sektor/Signal/Ø-Upside), Excel laden,
   „Neu berechnen" antippen (aktualisiert in ~2–3 Min. in der Cloud).

**Bonus — iOS-Kurzbefehl:** Lege optional einen Apple-Shortcut an, der entweder die
Streamlit-URL öffnet oder direkt den Workflow triggert (`POST` auf
`https://api.github.com/repos/<user>/<repo>/actions/workflows/run.yml/dispatches` mit
`{"ref":"main"}` und PAT). Als Home-Screen-Button = „ein Tipp → frische Analyse".

**Ganz ohne Cloud:** Notebook-Variante in Google Colab (`Runtime → Run all`, am Ende
`files.download('output/latest.xlsx')`) — für iPhone der einfachste Sofort-Weg. On-device
(a-Shell/Pyto) ist möglich, aber langsam/zickig — nur Notlösung.

---

## Robustheit

- Fehlende Felder brechen den Lauf **nie** ab — betroffene Methode = NaN, Zeile bleibt.
- Am Ende: Anzahl erfolgreich, Anzahl mit Datenlücken, Liste fehlgeschlagener Ticker.
- Logging statt `print`; `--limit N` für schnelle Tests; Cache → erneuter Lauf in Sekunden.
- Alle Multiplikatoren/Annahmen zentral in `config.yaml` — keine Magic Numbers im Code.

---

## Disclaimer

> Dieses Tool wendet Standard-Bewertungsmethoden mechanisch an. Die Signale sind regelbasiert
> und nur so gut wie die (kostenlosen) Daten und die gewählten Annahmen. Uniforme/Sektor-Median-
> Multiplikatoren sind Näherungen. **Keine Anlageberatung.** Vor jeder Entscheidung eigene
> Prüfung und Risikostreuung. „Enormes Potenzial" = hohes Risiko; bei Hebelprodukten
> (z. B. Knock-outs) droht Totalverlust.
