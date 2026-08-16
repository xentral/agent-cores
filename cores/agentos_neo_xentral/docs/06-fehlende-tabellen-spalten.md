# Welche Tabellen & Spalten sind per API NICHT erreichbar? (Team-Übergabe)

> Übersetzung der API-Lücken (05-fehlende-apis.csv, 04-backlog-tasks.csv) in
> Legacy-Tabellen-/Spalten-Sprache. Stand 18.07.2026, verifiziert gegen
> routes/api.php + routes/v3.php + Resources/Data-Klassen.

## A · Tabellen KOMPLETT ohne API-Zugriff (weder lesen noch schreiben)

| Tabelle / Datenbereich | Was gebraucht wird | Ziel-API (Vorschlag) |
|---|---|---|
| **Lagerprotokoll / Lagerbewegungen** — LESEN geliefert (API-805, 16.08.2026) | Artikel, Menge (vorzeichenbehaftet), Lagerplatz, Grund, User, Zeitpunkt und Ursache kommen aus `GET /api/v3/stockMovements`. Offen bleiben Charge/SN je Buchung und der EK-Wert, sowie generisches Schreiben | POST /v3/stockMovements + Charge/SN und unitCost in der Payload |
| **beleg_chargesnmhd** | tatsächlich gebuchte Seriennummern/Chargen/MHD je Belegposition (LS, Rechnung …) | Teil der DN-/WE-Item-Payloads + /v3/batches\|serialNumbers |
| **seriennummern / chargen / lager_mindesthaltbarkeitsdatum** (als Ressourcen) | Charge/SN als Objekt mit Status, Bestand, Trace (WE↔LS), SN→Kunde (Garantie) | /v3/batches, /v3/serialNumbers (+/trace) |
| **Nummernkreise** (firmendaten: next_*-Zähler) | Formate, Zählerstände, Lückenprotokoll je Belegtyp | GET /v3/numberRanges (+/gaps) |
| **protokoll / globales Audit** | wer/wann/was über alle Objekte (inkl. API-Key/Agent als Akteur) | GET /v3/auditLog |
| **PDF-/Belegarchiv-Versionen** | jede gesendete/gedruckte PDF-Version unveränderlich abrufbar | GET /v3/{beleg}/{id}/pdfVersions |
| **firmendaten (Briefpapier-Block)**: footer_0_0…footer_3_5, absender, footer_reihenfolge_*, Logo-/Briefpapier-Dateien (auch je Projekt) | kompletter Briefkopf-/Footer-Kontext fürs eigene PDF-Rendering | ?expand=letterhead auf Beleg-GETs |
| **uebersetzung** (Beschriftungen: dokument_*, bezeichnung*ersatz, artikeleinheit_*) | Belegtitel, Spaltenköpfe, Einheiten-Übersetzungen je Sprache | Teil von letterhead.labels/unitTranslations |
| **zahlungsweisen.freitext** + Zahlungsart-Plugin-Texte, Mahnwesen-Konfig (mahnwesen_m1-3/ik_gebuehr, *_tage, Geschäftsbrief-Vorlagen) | aufgelöste Zahlungsbedingungs-/Mahntexte | letterhead.texts + salesInvoice-Endpoints |
| **document_customization_infoblock** | konfigurierter corr-Infoblock je Belegtyp/Projekt | Teil von letterhead |
| **Zahlungs-Zuordnungen** (Zahlung ↔ Rechnung) | allocations lesen/schreiben; PaymentTransactions-LISTE | GET /v3/payments?filter + /allocations |
| **verkaufspreise** (Schreiben) | Staffel-/Kundenpreise anlegen/ändern (lesen: nur v1-List) | POST/PATCH /v3/priceLists |
| **adresse.kreditlimit / Liefersperre (Schreiben) + Offene-Posten-Aggregat** | Kunden-Finance von außen steuerbar/lesbar | PATCH customer.finance + /openItems |
| **Festschreibungs-/Storno-Mechanik** | fixedAt, PATCH-Sperre, Storno-Gegenbeleg | GoBD-Paket (siehe 05, Nr. 5) |

## B · Tabellen mit TEIL-Zugriff — diese Spalten fehlen

**auftrag** (lesen ✓ per v3 — schreiben fehlt für):
`internet` (externalOrderNumber), `ihrebestellnummer`, `transaktionsnummer`, `shopextid`,
`shop` (Kanal), `lieferdatum`+`lieferdatumkw`, `tatsaechlicheslieferdatum`, `reservationdate`,
`einzugsdatum`, `versandart`, `zahlungsweise`, `zahlungszieltage/-skonto/-tageskonto`,
`keinsteuersatz`, `autoversand`, `art` (createDocuments), `fastlane`, `ust_ok`,
`vorabbezahltmarkieren`, `keinporto`, `lieferungtrotzsperre`,
`keinestornomail/keinetrackingmail/zahlungsmailcounter`, `abweichendebezeichnung`,
`kundennummer_buchhaltung`, `storage_country` (auch UI kann nicht!), `angebotid`,
`lieferantenauftrag`, `schreibschutz` — plus **Freifelder aller Belege** (lesen ✓, schreiben ✗).

**Alle *_position-Tabellen**: `vpe` (schreiben), `zolltarifnummer`/`herkunftsland`
(Update fehlt), `artikelnummerkunde` (Update fehlt), `grundrabatt`/`rabatt1–5`
(gar nicht — nur kombinierter Rabatt), Zwischenpositionen (heading/subtotal) per API
nicht anlegbar.

**lieferschein**: `ihrebestellnummer`, `versandart`, `abweichendebezeichnung`,
`lieferantenretoure` (schreiben); `keinerechnung` (nicht mal lesen — nur Filter).

**bestellung**: `bestellung_bestaetigt`, `bestellungbestaetigtabnummer`, `angebot`
(AN-Nr. Lieferant), `bestellbestaetigung` (schreiben); `kundennummerlieferant`
(**nicht mal lesen**); abweichende Lieferadresse (lesen ✓, schreiben ✗).

**gutschrift**: Spalten `auftrag`-Bezug + `lieferschein` (**nicht mal lesen** — View hat
keine Referenzen); `ihrebestellnummer`, `kundennummer_buchhaltung`, `stornorechnung`
(schreiben).

**produktion**: ALLE Spalten nach Create unveränderbar (kein PATCH); Charge/MHD-Spalten
(**nicht mal lesen**); `produktion_position` (Stückliste) per API unantastbar.

**Kommissionierung (pickLists)**: Ausführen ✓ (start/complete/Totes) — **Anlegen nach
Kriterien fehlt** (Welle), Task-Level-Bestätigung prüfen.

**eingangsrechnung/Verbindlichkeiten**: nur Liability-Liste — Positionen, Freigabe-Status,
Match-Spalten fehlen.

## C · Die Kurzfassung für dein Team

1. **9 neue APIs bauen** (Block A oben; Details/Endpoints: 05-fehlende-apis.csv Nr. 1–9).
2. **7 bestehende APIs ergänzen** (05-fehlende-apis.csv Nr. 10–16).
3. **279 Feld-Freischaltungen** auf bestehenden v3-Endpunkten (04-backlog-tasks.csv) —
   überwiegend: vorhandene Spalten in Create/Update-DTOs aufnehmen (Block B).

Priorität aus Core-Sicht: StockMovements → Payments-LIST/allocations → SN/Chargen →
PickList-Create → Festschreibung → Eingangsrechnung → Rest.
