# Architecture Decision Records — ERP Core (Xentral-V3-based)

Format je ADR: **Kontext** (Problem im Ist-System) → **Entscheidung** → **Verworfen** →
**Konsequenzen/Invarianten**. Die Invarianten sind bindend — ein Bau-Agent darf sie nicht
„pragmatisch“ aufweichen, ohne dass ein Mensch das ADR ändert.

---

## ADR-001 · Referenzen sind Objekte, nie nackte IDs

**Kontext:** Die V3-API liefert Referenzen nur als `{id}`. Folge (per Feldmatrix belegt):
PDF-Infoboxen und Listen-UIs brauchen pro referenziertem Beleg einen Nachlade-Request;
die Gutschrift kennt ihre Auftrags-/Lieferschein-Nummer gar nicht.
**Entscheidung:** Jede Referenz ist `{ "id", "number", "name", "href" }`. Beim Schreiben
genügt `{id}` ODER `{number}`.
**Verworfen:** Nur-ID (heutiges Verhalten); vollständiges Einbetten (Payload-Explosion).
**Invarianten:** Kein Feld im Modell referenziert je per nackter ID. `number` ist immer
die menschenlesbare Belegnummer/SKU/Kundennummer. `href` ist relativ zur API-Wurzel.

## ADR-002 · Sprechende ID-Präfixe

**Kontext:** Agenten und Integratoren müssen aus einer ID Typ und Endpoint ableiten können.
**Entscheidung:** Typisierte Präfixe (`so_`, `si_`, `prd_` … siehe README-Glossar).
Für Alt-Objekte deterministisch aus der numerischen Alt-ID kodiert (kein Lookup nötig).
**Verworfen:** UUIDs (opak, nicht selbstbeschreibend); numerische IDs (kollidieren über Typen).
**Invarianten:** Ein Präfix pro Typ, nie wiederverwendet. `uuid`-Felder entfallen (eine ID reicht).

## ADR-003 · Ein gemeinsames Belegskelett

**Kontext:** Im Ist-System hat jeder Belegtyp eigene Feldnamen und -strukturen
(z. B. `ihrebestellnummer` vs. `anfrage`, Financials mal Trait, mal inline).
**Entscheidung:** Alle 9 Belege teilen exakt dieselbe Struktur:
`party (customer|supplier) · channel · project/costCenter · references · dates ·
addresses · items · currency/exchangeRate · totals · payment · shipping · texts · note ·
documents · available · customFields · timestamps`. Belegspezifisches kommt als klar
benannter Zusatzblock dazu (`dunning`, `match`, `confirmation`, `customs` …).
**Invarianten:** Kein Beleg weicht in gemeinsamen Feldern ab. Neue Belegtypen erben das Skelett.

## ADR-004 · Steps vs. Actions + die fünf Agenten-Garantien

**Kontext:** Agenten sind „blinde Sachbearbeiter“ — keine Maske, keine ausgegrauten Buttons.
Im Ist-System sind Statusregeln implizit (z. B. DELETE nur bei draft, versteckte Filter-Defaults).
**Entscheidung:**
- **Step** = ändert den eigenen `status` (State-Machine): `POST …/steps/{name}`.
- **Action** = Operation mit Wirkung (Folgebeleg, Versand, Zuweisung): `POST …/actions/{name}`.
  Ändert nie den eigenen Haupt-Status.
- Garantien: (1) `available: {steps, actions}` im Payload; (2) statisches
  `GET /{collection}/$schema/steps+actions` mit Params/Preconditions/Scopes;
  (3) 409-Fehler mit maschinenlesbarer `resolution`; (4) `Idempotency-Key`-Pflicht +
  `?dryRun=true`; (5) Antwort = aktualisiertes/erzeugtes Objekt + `performed{by,at}`.
**Verworfen:** Alles unter „actions“ mischen (verwischt State-Machine); HATEOAS-Vollausbau (zu schwer).
**Invarianten:** Haupt-Status ändert NUR ein Step, nie ein PATCH. `(auto)`-Übergänge
(fulfilled, matched, paid) macht das System, nie der Client.

## ADR-005 · Naming: salesInvoice/purchaseInvoice, salesOrder/purchaseOrder

**Kontext:** „Invoice“ ist mehrdeutig, sobald Eingangsrechnungen existieren.
**Entscheidung:** Verkauf/Einkauf symmetrisch: `salesOrder`/`purchaseOrder`,
`salesInvoice`/`purchaseInvoice`. Präfixe `so_/po_/si_/pi_`.
**Invarianten:** Kein unqualifiziertes „order/invoice“ im API-Vokabular.

## ADR-006 · Geld als Dezimal-String, Mengen als {value, unit}

**Kontext:** Float-Geldbeträge erzeugen Rundungsfehler; Legacy speichert Mengen ohne Einheit.
**Entscheidung:** Geld = `{"amount": "12.90", "currency": "EUR"}` (String!). Mengen =
`{"value": 10, "unit": "piece"}`. Daten ISO 8601, Länder ISO 3166.
**Invarianten:** Nie Float für Geld. `unit` ist Pflicht an jeder Menge.

## ADR-007 · Filter sind generiert, Listen zeigen alles

**Kontext:** Die V3-Filter sind handkuratierte Listen (vergessene Filter belegt, z. B.
customerOrderNumber an der Invoice) und `status`-Filter haben versteckte Defaults, die
Drafts ausblenden — Integrationen übersehen dadurch Belege.
**Entscheidung:** Jedes skalare Payload-Feld (Punktnotation) und jede Referenz-ID/-Nummer
ist filterbar — automatisch aus dem Schema generiert. Operatoren: eq, ne, in, gte, lte,
between, contains, startsWith, isNull. Listen haben KEINE versteckten Defaults.
`?search=` deckt number, references.*, Partnername, E-Mail, PLZ, Ort ab.
**Konsequenz:** Index-/Suchstrategie pro Entity ist Pflichtteil des Bau-Auftrags (Kostenpunkt!).
**Invarianten:** Kein kuratiertes Filter-Whitelisting. Kein impliziter Status-Filter.

## ADR-008 · expand statt Include-Listen, keine „slim“ Resources

**Kontext:** V3 hat pro Endpoint eigene Include-Listen; `include=lineItems.product` liefert
absichtlich nur id+number („slim“) — EAN/Gewicht/Hersteller sind API-weit unerreichbar.
**Entscheidung:** Ein Mechanismus: `?expand=a,b.c` (max. 2 Ebenen) ersetzt Referenz-Objekte
durch volle Objekte. Expandierte Objekte sind IMMER die vollen Ressourcen.
**Invarianten:** Keine Zweitrepräsentationen einer Ressource.

## ADR-009 · GoBD-Fundament: Festschreibung, Storno-Kette, Nummernkreise, Audit

**Kontext:** Das Ist-System erlaubt PATCH nach Rechnungsstellung (Schreibschutz optional);
cancel ändert nur den Status; Nummernkreise/Audit sind nur intern. Für 5–200-M€-Händler
ist das nicht abschlussfähig.
**Entscheidung:** (a) Gebuchte Belege tragen `fixedAt`; danach kein PATCH — Korrektur nur
über Storno-Beleg (`cancel` erzeugt Gegenbeleg mit Rückreferenz) bzw. neuen Beleg.
`salesInvoice.issue` = Festschreibungszeitpunkt. (b) `numberRange` mit Lückenprotokoll.
(c) Audit-Trail an jedem Objekt (Akteur inkl. `agent:…`/API-Key), global abfragbar.
(d) Jeder Beleg: `files[]` + versioniertes PDF-Archiv. (e) `stockMovement` trägt
`unitCost` (Bewertung zum Buchungszeitpunkt).
**Invarianten:** Kein Write-Pfad umgeht Festschreibung oder Audit-Trail. Keine Löschung
gebuchter Objekte.

## ADR-010 · Bestände ändern sich nur über stockMovements

**Kontext:** Ist-System: absolute Setzung via setTotalStock, implizite Beleg-Buchungen,
kein lesbares Lagerprotokoll.
**Entscheidung:** Einziger Schreibweg für Bestand: `POST /stockMovements`
(receipt | issue | transfer | correction; correction wahlweise Delta oder `setQuantityTo`,
mit Pflicht-`reason`). Belege buchen automatisch und erscheinen als `source.document`.
`stockLevel` (Produkt × Platz × Charge) ist eine read-only Projektion.
**Invarianten:** Kein PATCH auf Bestände/contents. Jede Bewegung hat `source`
(Dokument ODER User+reason) — der Trail ist lückenlos.
**Ergänzt durch ADR-017:** Die Bewegung bleibt der Datensatz der Wahrheit, ist aber
nicht mehr die Bedienoberfläche — gebucht wird über benannte Lager-Actions.
**Nachtrag 16.08.2026 (API-805):** Der Kontext „kein lesbares Lagerprotokoll" ist
überholt — `GET /api/v3/stockMovements` liefert es und der Core liest es
(list/read). Zwei Korrekturen am Wortlaut oben: das Feld heißt nicht mehr
`source.document`, sondern `causedBy` mit eigenem Vokabular (Belege *plus*
Inventurlauf, Umbuchungsbeleg, Paketannahme, Serviceauftrag — die Beleg-Sprache
allein hätte die Mehrheit der Buchungen als ursachenlos gemeldet); und der Trail
ist nicht lückenlos: knapp ein Drittel der Buchungen nennt keine Ursache, dort
tragen `source.reason` und `systemType` die Herkunft. Lesen und Schreiben haben
außerdem nicht dieselbe Granularität — ein `transfer` ist ein Kommando und zwei
Bewegungszeilen.

## ADR-011 · Flags → Enums (Produkt und überall)

**Kontext:** Die neue v3-Product-API (PR API-710) hat >30 Booleans
(isServiceProduct/isFee/isShippingCostsProduct, isProductionProduct/isExternallyProduced/…,
isDeleted/isDisabled/disabledReason, hasBatches/serialNumbersMode/hasBestBeforeDate).
**Entscheidung:** Konsolidierung zu sprechenden Feldern: `kind`
(physical|service|digital|shippingCost|fee), `production.mode`
(none|inHouse|external|justInTime), `status`+`statusReason`, `tracking`-Block
(stock/batches/serialNumbers: none|onReceipt|onDelivery/bestBefore). Die 16-Konten-
Steuermatrix wird ein referenzierbares `taxProfile`; 80 % sehen nur `tax.rate`.
**Invarianten:** Keine neuen Boolean-Paare, die sich gegenseitig ausschließen — dann Enum.

## ADR-012 · Konditionen leben in PriceList, nicht am Produkt

**Kontext (PO-Feedback):** Rabattfähigkeit/Skonto sind Beziehungs-Eigenschaften
(Kunde/Kanal ↔ Produkt), keine Produkteigenschaften.
**Entscheidung:** `priceList` (Staffeln, Gültigkeit, je Kunde/Kanal zuweisbar) trägt die
Konditionen. Am Produkt bleibt nur `documentDefaults` (hidePrice, noticeText,
requiresCustomerApproval). Auflösung deterministisch: Item-Preis > Kundenpreisliste >
Kanalpreisliste > Produkt-Basispreis; das Ergebnis steht als `priceSource` am Item.
**Status:** Grenze wartet auf finales PO-Feedback (als offen markiert).

## ADR-013 · Der Belegfluss ist im Payload (documents-Graph)

**Kontext:** Ist-System: Referenzen nur teilweise vorhanden (Gutschrift kennt ihren
Lieferschein gar nicht), keine Rückwärts-Navigation.
**Entscheidung:** Jeder Beleg trägt `documents` mit allen vor-/nachgelagerten Belegen
als Referenz-Objekte, beidseitig gepflegt, read-only, immer aktuell.
**Invarianten:** Erzeugende Actions (`createSalesInvoice` …) verdrahten `documents`
auf beiden Seiten in derselben Transaktion.

## ADR-014 · Kein Overlay-Store — reiner 1:1-Durchgriff (revidiert 18.07.2026)

**Kontext:** Bis die Alt-API-Task-Liste umgesetzt ist, sind viele Felder alt nicht schreibbar.
Ursprünglicher Entwurf: ein Overlay-Store im Layer. **Verworfen per PO-Entscheid** — der
Kern hat KEINE eigene Persistenz.
**Entscheidung:** Der Kern reicht 1:1 an die Xentral-APIs durch. Nicht schreibbare Felder
sind `creatable/updatable = false` und werden als blaue Wünsche geführt
(`priorities.json`, siehe 03-mapping-layer.md Kap. 5); Requests mit solchen Feldern
antworten 409 mit Feldliste. Entities ohne jede Alt-Quelle erscheinen ohne Operationen,
bis die API nachgereicht ist.
**Konsequenzen:** Kein Datenmigrations-Thema, keine Overlay-Legacy-Divergenz; dafür keine
echte Idempotenz-Deduplizierung im Layer (nur dryRun + Client-Disziplin) und kein
Vorgriff auf fehlende Felder.
**Invarianten:** Der Kern persistiert nichts außer Konfiguration. Jede Lücke ist im
Steckbrief sichtbar (blau), nie stillschweigend weggelassen.

## ADR-015 · Kleine Status-Ketten + orthogonale Neben-Status

**Kontext:** Ist-System mischt Zustand (status) mit Deriviertem (Ampeln, Zahlungsstatus)
und hat deprecated Status-Werte.
**Entscheidung:** Haupt-Status = 4–6 Werte je Typ, immer inkl. draft und cancelled, keine
deprecated-Werte. Orthogonale, berechnete Neben-Status: `payment.status`,
`shipping.status`, `match.status`, `fulfillment`-Zähler, `holds[]` (mit Typ+Grund statt
anonymer Ampeln).
**Invarianten:** Neben-Status sind nie direkt schreibbar.

## ADR-016 · Multi-Company ab Tag 1 im Schema, Feature in Phase 2

**Kontext:** Ab ~50 M€ fast immer mehrere Legal Entities; Nachrüsten eines
Mandanten-Scopes ist die teuerste Migration überhaupt.
**Entscheidung:** `company`-Scope (`com_`) ist in Schema/IDs/Nummernkreisen/TaxProfiles
vorgesehen; V1 läuft mit genau einer Company.
**Invarianten:** Keine Tabelle/kein Objekt ohne company-Spalte (Default: die eine Company).

## ADR-017 · Lagerarbeit sind benannte Actions, keine Payload-Kombinatorik

**Kontext:** Bestand buchen war eine einzige `stockMovement`-Payload, deren Bedeutung
sich aus `type` **plus** Feldkombination ergab: acht Vorgänge in einer Form. Die Regeln
dazu (`quantity` immer positiv, `correction` genau **ein** Lagerplatz, `quantity` XOR
`setQuantityTo`, `reason` nur bei `correction` Pflicht) standen im Docstring — also
genau dort, wo ein Agent nie hinsieht. Er plant aus `describe`, und `describe` zeigte
einen Discriminator ohne die Regeln. Der Lagerbereich war damit der einzige Teil des
Modells, der so funktioniert: überall sonst hat ein Beleg `release`, `cancel`, `ship`.

**Entscheidung:** Fünf benannte Actions am **Lagerplatz** — dort, wo die physische
Arbeit stattfindet — jede mit eigenem `command`-Schema:

| key | Label | Vorgang |
|---|---|---|
| `putaway` | Einlagern | Zugang auf diesen Platz |
| `stockRemoval` | Auslagern | Abgang von diesem Platz |
| `stockTransfer` | Umlagern | auf einen anderen Platz (`target` Pflicht) |
| `inventoryCount` | Inventur zählen | gezählte Menge, **absolut** |
| `stockAdjustment` | Bestandskorrektur | **vorzeichenbehaftete** Differenz, `reason` Pflicht |

Aus Prosa-Regeln werden damit Schemata: `putaway` hat kein `target`, `stockTransfer`
verlangt eines; `inventoryCount` nimmt eine absolute Menge, `stockAdjustment` ein
Delta mit Vorzeichen. Jede Action antwortet mit dem resultierenden `stockLevel`
(ADR-004 Garantie 5, hier erstmals auch für einen Schreibvorgang eingelöst) und
akzeptiert `dryRun` im Command.

**Vokabular:** Lagerwirtschaft, nicht API-Slang — SAP WM (Ein-/Auslagerung,
Umlagerung) und MM (Inventur, Differenzbuchung). Bewusst **nicht**
`goodsReceipt`/`goodsIssue`: das ist die MM-Belegebene, und `GoodsReceipt` ist hier
bereits eine Entity. `key` ist agentenseitig (englisch, wie im ganzen Modell), das
`label` ist die Textzeile für den Lagerarbeiter.

**Verworfen:** (a) Nur die Payload dokumentieren — verlagert die Regeln erneut dorthin,
wo der Aufrufer sie nicht liest. (b) Actions an `StockMovement` — Actions brauchen einen
Zieldatensatz, und Bewegungen sind nicht lesbar. (c) Actions an `StockLevel` — es gibt
noch keinen Level, wenn ein Artikel erstmals auf einen Platz kommt. (d) Zweite
Implementierung neben `stockMovement.create` — die Actions delegieren an dieselbe
Orchestrierung, `create` bleibt als Primitive und verweist auf sie.

**Invarianten:** Eine Lager-Action validiert im **eigenen** Vokabular (nie mit
`to`/`from`/`setQuantityTo`, die der Aufrufer nicht gesendet hat). Der Anlass wird
später zum Action-Namen (`scrapping`, `sampling`, `returnToSupplier`), nicht zu einem
Freitextfeld. `inventoryCount` ist der einzige wiederholbare Schreibweg — gleiche
Zählung zweimal bucht beim zweiten Mal nichts.

**Offen:** Die deutschen Labels sind noch nicht ausspielbar — `EmulationManifest.label()`
verwirft `accept_language`, und `action_def` nimmt einen festen String. Für Agenten
irrelevant (die wählen über `key` + `description`), für die UI Voraussetzung.

---

## Offene Klärungen vor Bau-Auftrag (Stand 18.07.2026)

1. **PO-Einzelfeedback je Entity** steht aus (insb. ADR-012-Grenze).
2. **Rundungsregeln** als Spec mit Testvektoren (Parität zum Legacy-PDF, `projekt.preisberechnung`).
3. **Status-Mapping-Tabellen Alt→Neu** verbindlich je Belegtyp (für Layer UND Migration).
4. **Steuersatz-Quelle** mit Gültigkeitszeiträumen (OSS-Zielländer): eigene Tabelle vs. Dienst.
5. **customFields-Typisierung** (string|number|date|enum + Validierung).
6. **Konsistenzmodell:** Beleg + Movements + Audit atomar? Event-Sourcing vs. CRUD+Audit.
7. **Index-/Suchstrategie** für generierte Filter (ADR-007-Kostenpunkt).
8. **Rollen-/Scope-Modell** je Step/Action (wer darf releaseHold bei Kreditlimit?).
9. **Altdaten:** read-only via Layer anbinden (Empfehlung) vs. migrieren.
10. **Strangler-Reihenfolge:** erste „echte“ Entity im neuen Kern (Empfehlung: Product oder salesInvoice).
11. **Abnahmekriterien:** 10 Berater-Sichten (01-model, Kap. Filter) + PDF-/Summen-Parität
    (Golden Files, Shadow-Traffic) + GoBD-Checkliste als Muss.
