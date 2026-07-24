# Fehlende Spalten je Xentral-Tabelle (API-Zugriff) — Team-Übergabe

> Konkret pro Tabelle: welche Spalten sind per API **nicht lesbar (R✗)** bzw.
> **nicht schreibbar (W✗)**. Spaltennamen aus den v3-Resources/DTOs verifiziert
> (Feldmatrix-Analyse 18.07.2026). „W✗“ heißt: Spalte ist im GET sichtbar, fehlt aber
> in Create- UND Update-DTO. Freifelder = customFields-Include (R✓/W✗ überall).
> BF-Entity-API (/api/entity) ist hier NICHT eingerechnet — vor Bau je Entity
> /api/metadata/{Entity} gegenprüfen.

## 1 Belegtabellen (v3 vorhanden, Spalten-Lücken)

### `auftrag` (v3 salesOrders)
- **W✗ (lesbar, nicht schreibbar):** `internet`, `ihrebestellnummer`, `lieferdatum`,
  `lieferdatumkw`, `tatsaechlicheslieferdatum`, `reservationdate`, `einzugsdatum`,
  `versandart`, `zahlungsweise`, `zahlungszieltage`, `zahlungszielskonto`,
  `zahlungszieltageskonto`, `keinsteuersatz`, `autoversand`, `art`, `fastlane`,
  `ust_ok`, `vorabbezahltmarkieren`, `keinporto`, `lieferungtrotzsperre`,
  `keinestornomail`, `keinetrackingmail`, `zahlungsmailcounter`,
  `abweichendebezeichnung`, `kundennummer_buchhaltung`, `angebotid`,
  `lieferantenauftrag`, `adresse` (Kundenwechsel im Update), Freifelder
- **R✗+W✗ (nicht mal lesbar):** `systemfreitext`
- **Sonderfall:** `storage_country` (W✗; wird nur systemseitig einmalig gesetzt),
  `shopextid` + `transaktionsnummer` + `shop` (W✗; schreibt heute nur der Shop-Importer)

### `rechnung` (v3 invoices)
- **W✗:** `zahlungsweise`, `zahlungszieltage`, `zahlungszielskonto`,
  `zahlungszieltageskonto`, `keinsteuersatz`, `ihrebestellnummer`-Filter fehlt (Feld W✓),
  Freifelder
- **R✗+W✗:** `versandart` (+ Trackingnummer aus `versand`), `buchhaltung`
  (Ansprechpartner-Zeile), `doppel` (Kopie-Kennzeichen), `systemfreitext`

### `gutschrift` (v3 creditNotes)
- **W✗:** `ihrebestellnummer`, `kundennummer_buchhaltung`, `stornorechnung`,
  `zahlungsweise`, `zahlungsziel*` (3 Spalten), `keinsteuersatz`, Freifelder
- **R✗+W✗:** Auftrags-Bezug (via `rechnung.auftrag`), `lieferschein` (LS-Referenz!)

### `lieferschein` (v3 deliveryNotes)
- **W✗:** `ihrebestellnummer`, `versandart`, `abweichendebezeichnung`,
  `lieferantenretoure`, Freifelder
- **R✗+W✗:** `keinerechnung` (existiert nur als Filter)

### `bestellung` (v3 purchaseOrders)
- **W✗:** `bestellung_bestaetigt`, `bestellungbestaetigtabnummer`, `angebot`
  (AN-Nr. Lieferant), `bestellbestaetigung`, `zahlungsweise`, `zahlungsziel*`,
  `keinsteuersatz`, komplette `liefer*`-Adress-Spalten (abweichende Lieferadresse:
  R✓/W✗!), Freifelder
- **R✗+W✗:** `kundennummerlieferant` (unsere Kundennr. beim Lieferanten),
  `artikelnummerninfotext`

### `angebot` (v3 offers) — bester Contract, Rest:
- **W✗:** `zahlungsziel*` (3 Spalten), `keinsteuersatz`, Freifelder
- Update-Asymmetrie: `versandart`, `zahlungsweise` (nur im Create)

### `retoure` (v3 returnOrders)
- **W✗:** `standardlager`, `versandart`, Freifelder;
  Asymmetrien: `lieferantenretoure` (nur Create), `abweichendebezeichnung` (nur Update)

### `proformarechnung` (v3 proformaInvoices)
- **W✗:** `liefer*`-Adresse, `verzollung*`-Adresse, `verzollinformationen`,
  `zollinformation`, `zahlungsweise`, `zahlungsziel*`, `keinsteuersatz`, Freifelder
- **R✗+W✗:** `lieferscheinid` (LS-Bezug!), `buchhaltung`

### `produktion` (v3 productions)
- **W✗ für ALLE Spalten** (kein PATCH-Endpoint): u. a. `datumproduktion`,
  `datumproduktionende`, `datumbereitstellung`, `datumauslieferung`, `reservierart`,
  `auslagerart`, `unterlistenexplodieren`, `funktionstest`, `seriennummer_erstellen`,
  `unterseriennummern_erfassen`, `internebezeichnung`, `freitext`, `internebemerkung`
- **R✗+W✗:** Chargen-/MHD-Spalten der Maske; `produktion_position` (Stückliste) komplett

## 2 Positionstabellen (`auftrag_position`, `rechnung_position`, `angebot_position`, `gutschrift_position`, `lieferschein_position`, `retoure_position`, `bestellung_position`, `proformarechnung_position`)

- **W✗ (Update fehlt, Create ✓):** `zolltarifnummer`, `herkunftsland`, `artikelnummerkunde`
- **W✗ (gar nicht schreibbar):** `vpe`
- **R✗+W✗:** `grundrabatt`, `rabatt1`–`rabatt5` (nur kombinierter Rabatt exponiert)
- **W✗:** Zwischenpositionen (Überschrift/Zwischensumme/Seitenumbruch —
  per API nicht anlegbar), Stücklisten-Anzeige-Steuerung (`keineeinzelartikelanzeigen`)
- `freifeld1`–`freifeld40`: R✓ (Include) / W✗
- Nur `bestellung_position`: `waehrung` je Position R✗

## 3 Tabellen KOMPLETT ohne API

| Tabelle | Spalten (alle gebraucht) |
|---|---|
| `lager_bewegung` | Bewegungshistorie komplett: Artikel, Menge, Platz von/nach, Grund, User, Zeit, Wert |
| `kontoauszuege` + `zahlungseingang` | Banktransaktionen als LISTE + Zuordnung Zahlung↔Rechnung |
| `kommissionierlauf` (Create) | Anlegen nach Kriterien; Ausführung per API ✓ |
| `chargen`, `seriennummern`, `lager_charge`, `lager_seriennummern`, `lager_mindesthaltbarkeitsdatum` | als Ressourcen mit Status/Bestand |
| `beleg_chargesnmhd` | gebuchte SN/Charge/MHD je Belegposition (Trace!) |
| `firmendaten` (Teilbereiche) | Nummernkreis-Zähler; `footer_0_0…footer_3_5`, `absender`, `footer_reihenfolge_*`; `mahnwesen_m1–3/ik_gebuehr`, `mahnwesen_*_tage` |
| `uebersetzung` | Beschriftungen `dokument_*`, `bezeichnung*ersatz`, `artikeleinheit_*` |
| `zahlungsweisen.freitext` | Zahlungsbedingungs-Textbausteine |
| `verkaufspreise` (Write) | Preise anlegen/ändern (v1-List ✓) |
| `adresse` (Teilspalten) | `kreditlimit`, Liefersperre (Write) + Offene-Posten-Aggregat |
| PDF-/Dokument-Archiv | gesendete Belegversionen |

## 4 Seit /api/metadata-Entdeckung NICHT mehr auf der Liste

`eingangsrechnung`/Verbindlichkeiten (→ BF-Entity `SupplierInvoice`, volles CRUD +
GoodsCheck/InvoiceCheck-Steps), Wareneingangs-LISTE (→ BF `GoodsReceipt`/`ParcelReceipt`
Read), Letterhead-Rohbausteine (→ BF `LayoutTemplate`, `BusinessLetterTemplate`,
`BusinessDocumentInfoBlock`, `TextSnippet`, `Company`) — jeweils Live-Verifikation
via `GET /api/metadata/{Entity}` vor Nutzung.
