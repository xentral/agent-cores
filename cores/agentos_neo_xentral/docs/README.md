# ERP Core (Xentral-V3-based) — Konzept-Bundle

Zielbild und Bauplan für einen neuen, agentenfreundlichen ERP-Kern, der zunächst als
**virtueller Kern** (Mapping-Layer) auf der bestehenden Xentral-API läuft und schrittweise
durch echte Implementierungen ersetzt wird (Strangler-Pattern).

Erarbeitet am 18.07.2026 auf Basis einer vollständigen Code-Analyse der V3-Dokument-APIs,
der Legacy-PDF-Erzeugung und der Legacy-UI-Masken. Zielgruppe des Produkts: Händler mit
physischen Gütern, 5–200 Mio. € Umsatz, E-Commerce + B2B gemischt (Shopify-Multi-Channel
bis klassischer Großhandel).

## Lesereihenfolge

| Datei | Inhalt | Für wen |
|---|---|---|
| `00-decisions.md` | Architecture Decision Records — das **Warum** hinter jeder Modell-Entscheidung. Zuerst lesen! | Bau-Agenten, Architekten |
| `01-model.md` | Das komplette Zielmodell: Prinzipien, alle Entities mit Payloads, Steps & Actions, Filter-Modell | Bau-Agenten, API-Designer |
| `02-ist-analyse.md` | Essenz der Ist-Analyse + **Quellcode-Anker** (welche Datei beweist was) | Bau-Agenten, Mapping-Entwickler |
| `03-mapping-layer.md` | Architektur des virtuellen Kerns auf der Alt-API + ehrliche Offen-Liste | Mapping-Entwickler |
| `04-backlog-tasks.csv` | 279 Feld-Aufgaben an der bestehenden V3-API (aus der Feldmatrix generiert) | API-Team |
| `05-fehlende-apis.csv` | 18 komplett fehlende Alt-APIs mit Endpoint-Vorschlägen | API-Team |

## Lebender Backlog (nach dem Bau — PR #1506)

Der Core ist gebaut; der offene Rest lebt **maschinenlesbar neben dem Code** und
wird im Steckbrief (ERP Core → Entity) als Capability-Grid gerendert:

- `../priorities.json` — blaue **Feld-Wünsche** je Entity (`field` × `ops` +
  Begründung): im Modell versprochen, Upstream fehlt.
- `../verified.json` — Live-Testergebnisse je Feld × Facette (read/create/update/
  filter/sort/search); `fail` + `<facet>Note` = ehrlicher Upstream-Befund.
  Regenerieren: `python -m entity_registry.cores.agentos_neo_xentral.checks.verify`
  (destruktiv-sicher: Marker-Roundtrips + create/DELETE, net-zero).
- **Action-/Step-Wünsche** stehen direkt in den Adapter-Katalogen
  (`emulated/*.py`, `wish="…"`); Ausführung antwortet 409 + Grund (ADR-014).

`04-backlog-tasks.csv` und `05-fehlende-apis.csv` bleiben die ursprüngliche
Analyse; die drei Quellen oben sind der aktuelle Stand — bei Abweichung gewinnen sie.

## Zugehörige (menschenfreundliche) Artefakte

- Offene Punkte im Capability-Grid (Snapshot 19.07.2026, nach Merge #1506 —
  187 Feld-Wünsche · 110 Action-Wünsche · 11 Fails · 3 blockierte Entities):
  https://claude.ai/code/artifact/c2c95298-f1b4-4a48-8333-63dcf692d1f3
- Feldmatrix aller 9 V3-Dokument-Endpunkte (interaktiv, mit Excel-Export):
  https://claude.ai/code/artifact/dd15e5c9-656c-4c2d-bf89-5e5bc0257ddc
- Zielmodell (interaktiv, mit Beispiel-Payloads und Mermaid-Diagrammen):
  https://claude.ai/code/artifact/a9665439-e437-4385-a7f7-5a160c9b4ce3

**Quelltexte der Artefakte** liegen versioniert unter `artifacts/`
(`nextgen-erp-api.html`, `feldmatrix-v3-dokument-apis.html`) — selbsttragende
HTML-Dateien (Payloads/Matrix als Daten im Markup bzw. JS). Zum Weiterarbeiten:
Datei editieren und über eine Claude-Session als Artefakt (re-)publizieren; die
Links oben bleiben nur bei Republish aus der Ursprungs-Session stabil, sonst
entsteht eine neue URL.

Die Markdown-Dateien hier sind die **verbindliche Quelle** für Agenten; die Artefakte sind
die Ansicht für Menschen. Bei Abweichungen gewinnt dieses Verzeichnis.

## ID-Präfix-Glossar

```
quo_ Quote            so_  SalesOrder       dn_  DeliveryNote    si_  SalesInvoice
cn_  CreditNote       ret_ Return           po_  PurchaseOrder   gr_  GoodsReceipt
pi_  PurchaseInvoice  cus_ Customer         sup_ Supplier        prd_ Product
ch_  Channel          prl_ PriceList        wh_  Warehouse       loc_ StorageLocation
shp_ Shipment         pay_ Payment          bat_ Batch           sn_  SerialNumber
stm_ StockMovement    stk_ StockTake        pkr_ PickingRun      pkt_ PickTask
tote_ Container       prj_ Project          usr_ User            cat_ Category
paym_ PaymentMethod   ship_ ShippingMethod  taxp_ TaxProfile     nr_  NumberRange
pot_ Payout           evt_ Event            whk_ WebhookSubscription  smr_ StockMovementReason
rsn_ ReturnReason     com_ Company (Phase 2)
```

## Status (19.07.2026)

- **Gebaut und gemerged (PR #1506)**: 22 Adapter als Mode-C-Fassade auf den
  Live-APIs (v3 + v1/v2 + BF entity API), voller Steps-&-Actions-Katalog (§8),
  Tags, Partner-Subresources (contacts + billing/shipping addresses),
  Full-Facet-Verify-Suite (1300+ Zellen grün gegen die mvp-Instanz).
- Offener Rest: siehe „Lebender Backlog“ oben (Snapshot-Artefakt verlinkt).
- Historisch — Zielmodell v0.9 (18.07.2026): Naming- und Grenzfragen in
  `00-decisions.md` als „offen“ markiert; „Offene Klärungen“ dort beachten.
