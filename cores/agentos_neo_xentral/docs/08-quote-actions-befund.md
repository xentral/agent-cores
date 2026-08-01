# Befund: die fünf blauen Actions und Process-Steps bei `Quote`

Stand 01.08.2026. Gemessen gegen mvp (`mvp.xentral.biz`) und belegt am Monorepo
(`GitHub/xentral`, Commit `77b4b2daf2a`).

Anlass: Im Steckbrief stehen bei `Quote` fünf Einträge blau — die Actions
`convertToSalesOrder`, `duplicate`, `downloadPdf` und die Process-Steps `accept`
und `decline`. Ein Wunsch ist eine Behauptung über den Upstream, und Behauptungen
altern; zuletzt mussten zehn davon zurückgezogen werden, weil die Fähigkeit
inzwischen existierte. Also nachgeprüft.

Randbedingung: Der Kern spricht **nur dokumentierte APIs** (v3 primär, v1/v2 als
Fallback). Legacy-`index.php`-Pfade sind ausgeschlossen — sie sind session- statt
tokenbasiert und ohne Stabilitätszusage.

## Die fünf auf einen Blick

| Wunsch | Was upstream existiert | Was der Kern tun müsste | Einschätzung |
|---|---|---|---|
| `downloadPdf` | **Vorhanden.** `GET /api/v3/{beleg}/{id}` mit `Accept: application/pdf` liefert das gerenderte PDF | Action bauen: rendern, per Fileshare ablegen, `{fileKey, filename, bytes}` antworten | **Kein Upstream-Wunsch mehr — Bau-Auftrag an uns.** Betrifft 7 Entities |
| `convertToSalesOrder` | Nur die Legacy-XML-API `GET/POST /api/v1/AngebotZuAuftrag?id=` — Digest-Auth, Recht `standard_angebotzuauftrag`, hinter Killswitch | nichts (Auth-Verfahren passt nicht zum Bearer-Token des Kerns) | bleibt Wunsch; die Fachlogik existiert und ist einmal exponiert — v3 muss sie nur umhüllen |
| `accept` | **Nichts.** Nur die Oberfläche: `index.php?module=angebot&action=beauftragt&id=` | nichts | bleibt Wunsch |
| `decline` | **Nichts.** Nur die Oberfläche: `index.php?module=angebot&action=abgelehnt&id=` | nichts | bleibt Wunsch |
| `duplicate` | **Nichts**, in keiner Generation. Nur `erpAPI::CopyAngebot()` hinter `index.php?module=angebot&action=copy` | nichts | bleibt Wunsch |

## Belege im Einzelnen

### `downloadPdf` — der Wunschtext war falsch

Bisheriger Text: *„No public PDF render endpoint; the archived files at
`/api/v2/{type}/{id}/files` are not yet composed."*

Gemessen auf mvp, `GET /api/v3/{ressource}/{id}` mit `Accept: application/pdf`:

| Ressource | Antwort |
|---|---|
| `offers`, `salesOrders`, `invoices`, `purchaseOrders` | 200, `application/pdf`, ~2,2 KB, beginnt mit `%PDF-1.3` |
| `creditNotes` | 200, ~1,8 KB |
| `deliveryNotes` | 200, ~3,3 KB |
| `returns` | auf mvp keine Belege vorhanden — ungeprüft |

Das ist **dokumentiert**, nicht zufällig: `documents.yml:4980` schreibt
*„**Content Negotiation:** This endpoint supports additional response formats:
`application/pdf`"*, mit `application/pdf: {type: string, format: binary}` als
Antwortschema und Scope `offer:read`. Umgesetzt in
`OfferController::show()` über `$request->wantsPdf()`
(`HttpServiceProvider.php:77`) → `PDFArchiveServiceInterface::getPdfDocumentResponse()`.
Dieselbe Verzweigung tragen `salesOrders`, `invoices`, `creditNotes`,
`deliveryNotes`, `proformaInvoices`, `returnOrders` und `purchaseOrders`.

Zwei Verhaltensdetails, die man kennen muss:

- **Archiv zuerst.** `Briefpapier::displayTMP(true)` sucht erst eine gespeicherte
  Fassung in `pdfarchiv` und rendert nur, wenn keine da ist. Archiviert wird beim
  Setzen des Schreibschutzes und beim Versand — **nicht** bei der Freigabe
  (`ReleaseOfferAction` archiviert nicht). Wer eine feste Fassung braucht, kann sie
  über `PATCH /api/v3/offers/{id}/actions/setWriteProtection` erzwingen.
- **Die archivierten Fassungen selbst sind über keine API erreichbar.** `pdfarchiv`
  hat in keiner Generation einen Endpunkt; die Oberfläche geht über
  `action=pdffromarchive&id={pdfarchiv.id}`. Das ist genau der Punkt aus
  `05-fehlende-apis.csv` Nr. 12.

Was fehlt, liegt also auf **unserer** Seite: die MCP-Antwort ist JSON
(`mcp_server/tools/erp_core.py`, `return json.dumps(result, …)`), durch die kein
Binärdokument passt. `AdapterResponse` selbst erlaubt Bytes samt eigener Header —
der Kern `xentral_api` macht das bereits vor (`_forward_headers` in
`delivery_note.py`/`business_document.py`, inklusive Nicht-JSON-Fallback). Für die
Agentenschnittstelle wäre die Fileshare der saubere Weg
(`mcp_server/tools/fileshare.py`, `action='upload'` → `file_key`), Base64 im
Antworttext die einfache Notlösung — Base64 **hinein** ist im Kern schon üblich
(`Printer.printDocument`, `EmailAccount.sendEmail`), heraus noch nie.

Derselbe Wunsch steht in **sieben** Adaptern: `quote`, `sales_order`,
`sales_invoice`, `credit_note`, `delivery_note`, `purchase_order`, `return_order`.

Nebenbefund zum alten Wunschtext, der `/api/v2/{type}/{id}/files` erwähnt: Dieser
Endpunkt **existiert** (`routes/api.php:2016-2030`, Scope `files:read`, Spec in
`schemas/openapi/resource/file.json`), als GET-Liste und GET-Einzeldatei mit
`Accept: application/vnd.xentral.file` für die Rohbytes. Er liefert aber
ausschließlich **Anhänge** aus `datei`/`datei_stichwoerter`, nicht das erzeugte
Beleg-PDF. Der alte Text hat also den richtigen Endpunkt genannt und die falsche
Erwartung an ihn gehabt. In v3 gibt es unter `offers/{id}/files` nur POST
(anhängen) und DELETE.

### `accept` und `decline` — nur Oberfläche, und der Statusname stimmt nicht

v3 `offers` kennt genau sechs Aktionen (`routes/v3.php:167-238`,
`OfferActionsController.php`): `release`, `cancel`, `send`, `logActivity`,
`setWriteProtection`, `removeWriteProtection`. Kein accept, kein decline.

Auch nicht als gewöhnliches Update: `UpdateOfferData` (`schemas/openapi/documents.yml:23776`)
enthält kein `status`-Feld. `offers` existiert in v1/v2 überhaupt nicht. Die
Legacy-XML-API hat `AngebotEdit`, aber deren Statusliste ist auf
`angelegt|freigegeben|abgeschlossen|storniert|versendet` beschränkt
(`www/pages/api.php:11287`) — `abgelehnt` und `beauftragt` sind dort nicht setzbar.

Ausgeführt wird der Übergang ausschließlich in der Oberfläche, als zwei nackte
SQL-Updates in `www/pages/angebot.php` (`AngebotAbgelehnt()` Z. 902,
`AngebotBeauftragt()` Z. 920). Es gibt keine Domänenklasse, keinen Handler, kein
Event (`offer.accepted` existiert nicht; das Ereignis-Verzeichnis
`schemas/event/` kennt nur created/updated/sent/released/canceled/archived/
deleted/protocolCreated).

**Wichtiger Nebenbefund:** Der Status `angenommen` wird nirgends im Repository
geschrieben. Der Produktionsdaten-Audit
(`schemas/legacy-mappings/enums/audits/OfferStatus/report.md`) zählt über alle
Mandanten: `beauftragt` 605.663 · `versendet` 412.098 · `freigegeben` 241.066 ·
`abgelehnt` 117.289 · `angelegt` 100.989 · `storniert` 41.620 · `abgeschlossen`
23.060 — **`angenommen` kommt nicht vor**. Der betrieblich wirksame
„angenommen"-Zustand heißt `beauftragt` (v3: `commissioned`) und entsteht in aller
Regel als Nebenwirkung der Wandlung in einen Auftrag.

Für den Kern heißt das: Der Step sollte perspektivisch nicht „accept" heißen,
sondern den Zustand treffen, den das ERP wirklich führt.

### `convertToSalesOrder` — Fachlogik da, aber nicht über die getokente API

v3 kennt Wandlungen nur in diese Richtungen (`routes/v3.php`):
`invoices/actions/createFromSalesOrder`, `invoices/actions/createFromDeliveryNote`,
`returnOrders/actions/createFromDeliveryNote`. Für salesOrders gibt es kein
`createFromOffer`; `POST /v3/salesOrders` kann kein Angebot referenzieren
(`CreateSalesOrderData` hat kein Angebotsfeld, die Beziehung ist read-only:
`HasOfferReference.php`).

Es gibt genau einen nicht-UI-Pfad: die Legacy-XML-API
`GET/POST /api/AngebotZuAuftrag?id=` (mit `/api/v1/`-Alias). Sie ist
**Digest-authentifiziert**, hängt am Altrecht `standard_angebotzuauftrag` und
steht bereits hinter einem Killswitch
(`killswitch-block-legacy-api-angebot-zu-auftrag` → HTTP 400). Damit ist sie für
den Kern nicht benutzbar: er authentifiziert mit Bearer-Token.

Die eigentliche Arbeit erledigt `Erpapi::WeiterfuehrenAngebotZuAuftrag()`
(`www/lib/class.erpapi.php:31689`) — kopiert Kopf und Positionen, setzt das
Angebot auf `beauftragt` mit Schreibschutz, gibt den neuen Auftrag frei und feuert
`salesOrder.created`. Aufgerufen an genau zwei Stellen: der Oberfläche und der
Legacy-API.

Der bisherige Wunschtext („v1 salesOrders/import ist ein Rohimport, keine
Angebotswandlung") bleibt richtig, ist aber unvollständig: Es gibt sehr wohl einen
programmatischen Pfad, nur nicht in einem Auth-Verfahren, das wir sprechen.

### `duplicate` — nichts, in keiner Generation

`grep` über `routes/v3.php`, `routes/api.php`, `routes/web.php` nach
`duplicat|copy|clone`: kein Treffer. Die vollständige Liste der v3-Aktionen über
alle Belegtypen lautet `cancel`, `complete`, `logActivity`, `migrateToConnect`,
`release`, `removeWriteProtection`, `send`, `sendEmail`, `setWriteProtection`,
`start` — kein Kopieren für irgendeinen Belegtyp. Auch die Legacy-XML-API kennt
keins (`ApiAngebotCreate|Edit|Freigabe|Versenden|Archivieren|ZuAuftrag`, sonst
nichts).

Ausgeführt wird es von `erpAPI::CopyAngebot()` (`www/lib/class.erpapi.php:30227`),
erreichbar nur über `index.php?module=angebot&action=copy`. Die Funktion kopiert
Kopf und Positionen, setzt den neuen Beleg auf `angelegt` mit frischem
`gueltigbis`, lässt die Belegnummer bewusst leer und feuert `offer.created`.
Schwestern für Auftrag, Rechnung und Lieferschein existieren nach demselben
Muster — ein v3-Endpunkt würde also sieben Belegtypen auf einmal bedienen.

## Was wir vom API-Team brauchen

1. **`PATCH /api/v3/offers/{id}/actions/decline`** — setzt `angebot.status =
   'abgelehnt'`, schließt offene Wiedervorlagen, schreibt ins Angebotsprotokoll.
   Fachlogik existiert (`www/pages/angebot.php:902`), sie braucht nur einen
   Endpunkt. Scope analog `offer:cancel`.
2. **`POST /api/v3/salesOrders/actions/createFromOffer`** — umhüllt
   `Erpapi::WeiterfuehrenAngebotZuAuftrag()` und antwortet mit dem neuen Auftrag.
   Das ist die einzige Wandlung, die in v3 fehlt, während drei andere existieren.
   Ersetzt zugleich die Legacy-XML-API, die ohnehin abgeschaltet werden soll.
3. **Klärung `accept`**: Braucht es einen eigenen Endpunkt für „angenommen, aber
   noch nicht beauftragt"? Der Status `angenommen` ist im Enum vorhanden, wird
   aber von keinem Code geschrieben und kommt in keinem Mandanten vor. Entweder
   der Zustand wird betrieblich gebraucht — dann fehlt der Weg dorthin —, oder er
   sollte aus dem Enum verschwinden, damit niemand ihn abbildet.
4. **`POST /api/v3/offers/{id}/actions/duplicate`** — umhüllt
   `erpAPI::CopyAngebot()`. Dieselbe Fachlogik liegt für Auftrag, Rechnung und
   Lieferschein bereit; ein einheitliches `actions/duplicate` je Belegtyp bedient
   sieben Entities auf einmal.
5. **PDF-Versionsarchiv** — siehe `05-fehlende-apis.csv` Nr. 12. Der aktuelle
   Render ist erreichbar, die archivierten Fassungen (`pdfarchiv`) sind es in
   keiner Generation. Gebraucht: eine Liste je Beleg plus Abruf einer Fassung.

## Nebenbefund außerhalb der Fragestellung: Statuswerte gehen verloren

Beim Nachschlagen der Angebotsstatus fiel auf, dass die Statuskarten des Kerns
Werte nicht abbilden, die der Upstream tatsächlich liefert. `status_map()` fällt
in dem Fall auf den Standardwert zurück — und der ist `draft`.

Gemessen auf mvp, dieselbe Liste roh gegen den Kern gelesen:

```
deliveryNotes   upstream {'released': 52, 'cancelled': 4, 'sent': 44}
                Kern     {'picking': 52, 'cancelled': 4, 'draft': 44}
```

**44 versendete Lieferscheine meldet der Kern als Entwurf.** Ebenso fehlen bei
`Quote` die Werte `commissioned` und `ordered` — `commissioned` ist upstream der
mit Abstand häufigste Angebotsstatus (605.663 Zeilen). Auf mvp gibt es davon
gerade keinen, weshalb der Prüflauf blind dafür war; das ist dasselbe Muster wie
beim Seriennummern-Modus.

Betroffen: `delivery_note.py` (`sent` fehlt) und `quote.py` (`commissioned`,
`ordered` fehlen). Die übrigen fünf Belegtypen sind vollständig. Der Prüfer sollte
zusätzlich lernen, gelieferte Statuswerte gegen die Karte zu halten, statt sie
still auf den Standard fallen zu lassen.
