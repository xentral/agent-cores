# Ist-Analyse der Xentral-API — Essenz + Quellcode-Anker

> Destillat der vollständigen Code-Analyse vom 18.07.2026. Die interaktive Vollversion
> (Feldmatrix aller Felder × UI/View/Create/Update/Filter/Search/Sort) liegt im Artefakt
> (Link im README). Dieses Dokument nennt die **Fakten, die Bauentscheidungen tragen**,
> und die **Dateien, die sie beweisen** — damit kein Agent sie neu ausgraben muss.

## 1 Die 9 belegbasierten V3-Endpunkte

| Endpunkt | Controller (app/Modules/…) | Legacy-UI-Maske |
|---|---|---|
| /v3/salesOrders | ERP/Sales/Http/SalesOrders/SalesOrderController.php | www/widgets/templates/auftrag.tpl |
| /v3/invoices | ERP/Sales/Http/Invoices/InvoiceController.php | rechnung.tpl |
| /v3/offers | ERP/Sales/Http/Offers/OfferController.php | angebot.tpl |
| /v3/creditNotes | ERP/Accounting/Http/CreditNote/CreditNoteController.php | gutschrift.tpl |
| /v3/proformaInvoices | ERP/Accounting/Http/ProformaInvoice/… | proformarechnung.tpl |
| /v3/deliveryNotes | ERP/Warehousing/Http/DeliveryNotes/… | lieferschein.tpl |
| /v3/returnOrders | ERP/Warehousing/Http/ReturnOrders/… | retoure.tpl |
| /v3/purchaseOrders | ERP/Purchasing/Http/PurchaseOrders/… | bestellung.tpl |
| /v3/productions | ERP/Production/Http/ProductionController.php (KEIN BusinessDocument, KEIN PATCH!) | produktion.tpl |

Gemeinsame Basis: `app/Shared/ERP/Resources/BusinessDocumentResource.php`,
`app/Shared/ERP/Data/Create|Update/…BusinessDocumentData.php`, Traits in
`app/Shared/ERP/{Resources/Concerns,Data/Concerns}/`, Filter-Collections in
`app/Shared/ERP/BusinessDocuments/Http/` + `BusinessObjects/Http/AddressFilterCollection.php`.
Routen: `routes/v3.php`. Search: `app/Core/Http/QueryBuilder.php::withBusinessDocumentSearch()`
→ **fix 5 Spalten**: id, belegnr, datum, name, kundennummer (+ je Doc wenige Extras).

## 2 Die tragenden Befunde

1. **Referenzen nur als IDs** — Belegnummern verknüpfter Belege fehlen überall; Gutschrift
   kennt Auftrag/Lieferschein gar nicht (CreditNoteResource hat keine Felder dafür,
   das PDF zieht sie per SQL aus der referenzierten Rechnung: `class.gutschrift.php:79-88`).
2. **Create/Update deutlich schmaler als View** — 279 Feld-Lücken (04-backlog-tasks.csv).
   SalesOrder ist der dünnste Contract (Termine, Versandart, Zahlungskonditionen,
   Automatisierungs-Flags, externalOrderNumber/customerOrderNumber fehlen im Write).
   Positions-Schreibseite kennt NUR Produkt-Positionen (keine heading/subtotal),
   countryOfOrigin/hsCode/customerProductNumber nur im Create, packagingUnit gar nicht.
3. **Versteckte status-Filter-Defaults** — jede Liste blendet ohne expliziten Filter
   Drafts aus (QueryFilter::string('status', …)->default([...]) in jedem Controller).
4. **customFields überall read-only** (Include), Schreiben fehlt API-weit.
5. **ProductResource ist absichtlich „slim“** (nur id+number; Kommentar in
   app/Shared/ERP/Resources/…ProductResource) — EAN/Gewicht/Hersteller via Beleg unerreichbar.
   Die neue Product-Read-API (PR #24325 „API-710“, Branch API-710) liefert ~100 Felder +
   33 Includes: app/Modules/ERP/MasterData/Products/ (ProductController + Resources/).
6. **Quellenlage jenseits v3 (verifiziert 18.07.2026):**
   - **Vorhanden (v1/v2):** Shipments/Tracking (`POST /v1/shipments`,
     `GET /v1/deliveryNotes/{id}/shipments`); **PickLists** (`GET /pickLists`,
     `GET /pickLists/{id}`, Actions start/complete, mergedLabels + `mobilePickingTotes`
     CRUD/assign — Feature-Flag FFU_MODULE_MOBILE_PICKING_APP);
     **PaymentTransactions** (`GET /paymentTransactions/{id}`, `PATCH …/status`) +
     **PSP-Transactions** (`GET+POST /v1/paymentServiceProviders/{id}/transactions`) +
     `POST /v1/incomingPayments/{id}/matchTransactions`; **Webhooks komplett**
     (`/webhooks` CRUD + `/webhookEventTypes`); **DATEV-Export**
     (`/accounting/datev/csvExport|xmlExport/…` + Download-Status);
     eInvoice-XML-Daten (`GET /v3/einvoice/invoices/{id}/xmlData`, Experiment FAC-5934);
     Inventur (v1 inventoryRuns + Reports); StorageLocations (v1 CRUD + v2 items).
   - **Wirklich fehlend:** StockMovements lesen/generisch buchen; PaymentTransactions-
     LIST + Zahlungs↔Rechnungs-Zuordnungen; PickList-ANLEGEN per Kriterien +
     Task-Level-Bestätigung; Batches/SN/MHD als Ressourcen + Trace; GoodsReceipt list/cancel;
     Eingangsrechnung CRUD/approve/match; Nummernkreise; Festschreibung/Storno-Kette;
     globaler AuditLog; PDF-Versionsarchiv; Preislisten-Write; Letterhead-Kontext.
     → Details + Endpoint-Vorschläge: 05-fehlende-apis.csv.
7. **StorageLocations:** CRUD v1 (`/v1/warehouses/{id}/storageLocations`), Inhalte nur v2
   (`/v2/…/storageLocations/{id}/items`), Produktbestand `GET /v1/products/{id}/stocks`
   + `/storageLocations`; Inventur: v1 inventoryRuns (+ Zähllisten-Reports) — gute Basis.
8. **Actions-Konvention existiert bereits** (release/cancel/complete/send/
   set|removeWriteProtection/logActivity je Beleg + createFrom… bei Invoice/Return) —
   Production hat nur release/start.

## 2b Die zweite API-Ebene: Business-Framework Metadata/Entity-API (NEU entdeckt 18.07.2026)

Parallel zu v3 existiert eine generische, registry-getriebene API
(`vendor/xentral/business-framework/src/routes.php` + `app/Core/Metadata/Http/`):

- `GET /api/metadata` — Liste aller registrierten BusinessEntities (key, label, domain, operations)
- `GET /api/metadata/{Entity}` — komplettes Schema (rootNode-Feldbaum, actions, processSteps —
  exakt das metadata()-Format, das der agent-hub-Guide beschreibt!)
- `GET/POST/PATCH/DELETE /api/entity/{Entity}(/{uuid})` — generisches CRUD je nach `operations`
- `PATCH /api/entity/{Entity}/actions/{action}` — Actions im `{ids, command}`-Envelope
- `GET /api/preview/{Entity}(/{id})` — Vorschau-Sicht
- `POST/GET/PATCH/DELETE /api/sessionState/{Entity}/{uuid}` + `actions/promote` — Draft-Space

**Stand: 71 registrierte Entities** (`#[BusinessEntity]` in `app/Domains/…`), darunter für
unsere Lückenliste hochrelevant:

- **SupplierInvoice** (volles CRUD!) mit ProcessSteps DocumentStatus / **GoodsCheck** /
  **InvoiceCheck** / Payment → deckt Eingangsrechnung inkl. Freigabe/3-Wege-Match-Workflow
- **GoodsReceipt + ParcelReceipt** (Read) → Wareneingangs-LISTE existiert hierüber
- **LayoutTemplate, BusinessLetterTemplate, BusinessDocumentInfoBlock, TextSnippet, Company**
  → Letterhead-Rohbausteine
- Konfig-Objekte komplett: Warehouse, ShippingMethod, PaymentMethod, CostCenter, Project,
  Tag/TagGroup, SalesChannel, CustomsTariffNumber, DeliveryTerm, StockMovementType,
  MobilePickingProfile, CustomerGroup, BusinessPartner(+Type), ContactPerson, Product,
  ProductCategory, SalesOrder, PurchaseOrder, CreditNote, ReturnOrder u. a.

**Konsequenz für den Core:** Der agentos_neo-Core sollte ZWEI Upstreams kennen: v3-REST
(Belege, reichhaltig) und die BF-Entity-API (Konfig-/Stammdaten, Eingangsrechnung,
Wareneingangs-Liste, Letterhead-Bausteine). Die Feldmatrix dieser Analyse deckt die
BF-Ebene noch NICHT ab — je Entity vor Nutzung `GET /api/metadata/{Entity}` prüfen.
Offen bleiben trotzdem: StockMovements, Payments-LIST/allocations, PickList-Create,
Batch/SN-Trace, Nummernkreise, Festschreibung, AuditLog, PDF-Archiv (siehe 05, Rev. 2).

## 3 PDF-Erzeugung (für letterhead-expand & Rendering-Parität)

Basis: `www/lib/dokumente/class.briefpapier.php` (~5000 Zeilen); Subklassen
class.{angebot,auftrag,rechnung,gutschrift,lieferschein,retoure,bestellung,
proformarechnung,produktion}.php. Kernfakten:

- **Firmendaten/Layout**: `getStyleElement()`/`Firmendaten()` (globale Settings) —
  Logo/Briefpapier-PDFs auch **je Projekt** (projekt.speziallieferschein + FileManager
  OWNER_PROJECT); Footer = 24 Freitextfelder `footer_0_0…footer_3_5` (inkl. Bank).
- **Beschriftungen/Übersetzungen**: `Beschriftung()` → Tabelle `uebersetzung` je Belegsprache
  (Belegtitel inkl. `bezeichnung…ersatz`, Spaltenköpfe `dokument_*`, Einheiten via
  `artikeleinheit`+`uebersetzung`).
- **Berechnet zur Renderzeit**: Steuerbetrag je Satz, Zwischen-/Gruppensummen
  (`DrawZwischenpositionen`), Zahlungsbedingungs-Text (`Zahlungsweisetext()` mit
  {ZAHLUNGSZIELTAGE}/{ZAHLUNGBISDATUM}/…), EU-/Export-Vermerk ({USTID}/{LAND}).
- **Positions-Anhänge in description**: `CheckPosition()` (Z. ~676) hängt SN/Charge/MHD/EAN/
  Zolltarif an; weitere Toggles: herstellernummerimdokument, abmessungimdokument,
  freifelderimdokument, gewichtbezeichnung (Proforma-Zollblock).
- **Konfigurierbare Infobox**: `DocumentCustomizationService` + Tabelle
  `document_customization_infoblock` (corr-Block je Belegtyp/Projekt) — Variablen wie
  AUFTRAGSNUMMER/LIEFERSCHEINNUMMER/TRACKINGNUMMER/BEARBEITEREMAIL.
- **Mahnwesen**: `MahnwesenBody()` in class.erpapi.php (~Z. 12460) mit Gebühren/Fristen-Settings.

## 4 Schreibende Legacy-Prozesse (laufen an der API vorbei!)

- Shop-/Marktplatz-Import: `www/lib/class.remote.php` schreibt shopextid, transaktionsnummer,
  shop (salesChannel) — per V3 nirgends setzbar.
- storage_country: nur einmalig systemseitig (OSS/Lieferschwelle, class.erpapi.php:2847,
  `WHERE storage_country = ''`).
- Zahlungsabgleich, Kommissionierung, SN/Chargen-Zuordnung: nur interne Module.

→ Konsequenz für den Mapping-Layer: Delta-Sync via updatedAt-Filter (überall vorhanden),
aber Legacy-Writes sind erst nach dem nächsten Poll sichtbar (siehe 03, offene Punkte).
