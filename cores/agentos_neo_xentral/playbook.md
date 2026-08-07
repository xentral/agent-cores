# AgentOS Neo (Xentral) — core playbook

What a back-office clerk does every day, expressed in this core's entities, actions and
statuses. Read this once before building a workflow or an agent; use
`xentral_erp_core action="describe" key="<Entity>"` only to confirm a field tree, not to
explore.

Every capability below is stated as **executable** or **not executable on this core**, and
§9 replays those claims against the core's own metadata in CI. If the playbook and a
`describe` disagree, `describe` wins and the playbook has a bug — report it.

---

## §1 What this core is

A **facade**: it owns no data. Every read and write is translated live onto the connected
Xentral instance's v3/v2/v1 APIs. So a capability exists here only where Xentral exposes a
public endpoint for it — which is why a third of this document is about what you *cannot*
do (§7). That is not a limitation of the model; it is today's upstream, stated honestly.

The document chain is the mental model:

```
 Quote ──────────> SalesOrder ──┬──> DeliveryNote ─────> Shipment
 quo_              so_          │    dn_                 ship_ (tracking, label)
 (no conversion    │            │     │
  action — §4 P1)  │            │     └──> Return ──────> CreditNote
                   │            │          ret_           cn_
                   │            └──> SalesInvoice ──────> Payment
                   │                 si_                  paym_ (read-only)
                   └── fulfillment/dispatch creates the delivery note + pick run

 Supplier ──> PurchaseOrder ──> GoodsReceipt ──> PurchaseInvoice
 sup_         po_               gr_ (read-only)  pi_ (no executable action)
```

Two rules govern everything built on top:

- **The `business-entity` node is the only way into Xentral** from a workflow. There is no
  second path; a graph carrying a raw API node is rejected at save.
- A `business-entity` node outputs **the record itself**, not a gateway envelope
  (ADR-0002). Bind `{{ node.number }}`, not `{{ node.data.number }}`.

**How to invoke anything below.** Actions and process steps both run through `run`:

```
xentral_erp_core action="run" key="SalesOrder" handle="so_123" op="release"
xentral_erp_core action="run" key="StorageLocation" handle="loc_9" op="putaway" \
                 command={"product": "prd_1", "quantity": 5, "dryRun": true}
```

`op` is an `actions[].key` **or** a `processSteps[].commands[].key` — **never** a process-step
group key. `documentStatus` and `fulfillment` are group names; `release`, `cancel`,
`dispatch` are what you pass. In a workflow the same thing is
`params.<op>.path.uuid` (the record id) + `params.<op>.body` (the command).

---

## §2 Speaking ids and the German glossary

Every record id carries its type as a prefix, so you can identify any id in a payload
without a lookup:

| Prefix | Entity | Prefix | Entity | Prefix | Entity |
|---|---|---|---|---|---|
| `quo_` | Quote | `po_` | PurchaseOrder | `prd_` | Product |
| `so_` | SalesOrder | `gr_` | GoodsReceipt | `pl_` | PriceList |
| `si_` | SalesInvoice | `pi_` | PurchaseInvoice | `loc_` | StorageLocation |
| `dn_` | DeliveryNote | `cus_` | Customer | `slv_` | StockLevel |
| `cn_` | CreditNote | `sup_` | Supplier | `stk_` | StockTake |
| `ret_` | Return | `ch_` | Channel | `pick_` | PickingRun |
| `ship_` | Shipment *and* ShippingMethod | `paym_` | Payment *and* PaymentMethod | `wh_` | Warehouse |
| `sm_` | StockMovement | `cor_` | Correspondence | `tag_` | Tag |

Note the two collisions: `ship_` and `paym_` are each shared by a document and a settings
lookup. Disambiguate by the field you read it from, never by the prefix alone.

German term → entity, for mapping what a user asks for:

| German | Entity | German | Entity |
|---|---|---|---|
| Angebot | `Quote` | Bestellung (an Lieferant) | `PurchaseOrder` |
| Auftrag | `SalesOrder` | Wareneingang | `GoodsReceipt` |
| Rechnung / Ausgangsrechnung | `SalesInvoice` | Eingangsrechnung | `PurchaseInvoice` |
| Lieferschein | `DeliveryNote` | Lieferant | `Supplier` |
| Gutschrift | `CreditNote` | Versandart | `ShippingMethod` |
| Retoure / RMA | `Return` | Zahlungsart | `PaymentMethod` |
| Sendung / Paket | `Shipment` | Zahlungsbedingung | `PaymentTermsGroup` |
| Zahlung | `Payment` | Mahnwesen / Mahnstufe | `SalesInvoice.dunning` |
| Kunde | `Customer` | Zahlungsstatus | `SalesInvoice.payment.status` |
| Artikel | `Product` | Lagerplatz | `StorageLocation` |
| Preisliste / Staffelpreis | `PriceList` | Inventur | `StockTake` |
| Einkaufspreis (EK) | `PurchasePrice` | Kommissionierung | `PickingRun` |
| Kostenstelle | `CostCenter` | Charge / Seriennummer | `Batch` / `SerialNumber` |

---

## §3 The entities you actually need

**Documents** — full CRUD unless noted.

| Entity | German | Required to create | Notable |
|---|---|---|---|
| `Quote` | Angebot | `customer`, `items.product`, `items.quantity` | `release`, `cancel`, `send`, `downloadPdf` |
| `SalesOrder` | Auftrag | `customer`, `items.product`, `items.quantity` | the hub: `release`/`close`/`cancel`, `dispatch`, `createSalesInvoice`, `split`/`splitOrder` |
| `SalesInvoice` | Rechnung | `customer`, `items.product`, `items.quantity` | `release`, `cancel`, `send`; carries `payment.*` and `dunning.*` |
| `DeliveryNote` | Lieferschein | `customer`, `items.product`, `items.quantity` | `release`, `markDelivered`, `cancel`, `createReturn`, `createSalesInvoice` |
| `CreditNote` | Gutschrift | `customer`, `items.product`, `items.quantity` | `release`, `send`; **no cancel** once released |
| `Return` | Retoure | `customer`, `items.product`, `items.quantity`, `items.reason` | `createFromDeliveryNote`, `createCreditNote`, `release`, `settle`, `cancel` |
| `PurchaseOrder` | Bestellung | `supplier`, `items.product`, `items.quantity` | `release`/`close`/`cancel`, `send` |
| `GoodsReceipt` | Wareneingang | — | **read-only**, no executable action |
| `PurchaseInvoice` | Eingangsrechnung | `supplier` | CRUD, but **zero executable actions or steps** |
| `Shipment` | Sendung | — | **read-only, and not filterable at all** (§6) |
| `Payment` | Zahlung | — | **read-only**, immutable booking record |
| `StockMovement` | Buchung | `type`, `product` | **create-only**, append-only primitive — prefer the `StorageLocation` actions |
| `StockTake` | Inventur | — | read-only; every step is a wish |
| `PickingRun` | Kommissionierung | — | read-only; every step is a wish |

**Master data**

| Entity | Required to create | Notable |
|---|---|---|
| `Customer` / `Supplier` | `name` (+ `contacts.name`) | shared `addresses[]` / `contacts[]` shape; `addTag`/`removeTag` are the only executable actions |
| `Product` | `name` | `activate` / `deactivate` executable; `prices.sale` is the **standard** price only |
| `PriceList` | `product` | customer-, group- and scale (Staffel) **sales** prices |
| `PurchasePrice` | `product`, `supplier` | the EK per supplier |
| `StorageLocation` | `name`, `warehouse` | the richest executable surface in the core (§4 P8) |
| `StockLevel` | — | read-only stock anchors |
| `CustomerGroup`, `Task`, `Correspondence`, `CostCenter` | `name` / `title` / `customer` / `name` | plain CRUD, no actions |
| `Tag`, `Channel` | — | read-only |

**Every line item needs a product.** `items.product` is mandatory on all seven document
types — a live probe refuted the assumption that a free-text position (description +
price, no article) is possible. If you need one, create a service/fee `Product` for it
(`kind: "service"` or `"fee"`) and reference that.

**Dead ends — do not design around these.** `Batch` and `SerialNumber` declare fields but
have **no operations at all**: Xentral exposes no batch or serial resource, they exist only
as read-only includes on a product. You cannot list, read, or trace them.

**Settings catalogues** (`category: settings`) list which values *exist* so you can pick a
valid one; the value a record carries lives on that record. `list`-only unless noted:
`PaymentMethod`, `ShippingMethod` (+`read`), `ReturnReason`, `DeliveryTerm`,
`PaymentTermsGroup` (+`read`), `TaxRate`, `Warehouse` (**list/create/update/delete — no
`read`**), `Project`, `User`, `Employee`, `ProductCategory` (full CRUD),
`MerchandiseGroup` (+`read`), `ProductProperty`, `ProductTag`, `ProductFreeField` (+`read`),
`AddressCustomField`, `Webhook` (+`read`), `WebhookEventType`, `TextTemplate` (+`read`),
`CostCenter` (full CRUD), plus two device surfaces with real actions: `Printer`
(`printDocument`) and `EmailAccount` (`sendEmail`).

`ProductFreeField` and `AddressCustomField` are **definitions only** — they tell you which
slots exist, never what a record has in them.

---

## §4 Process playbooks

### P1 — Angebot → Auftrag (quote to order)

**Chain.** `Quote.create` → `release` → `send` → *(customer accepts)* → `SalesOrder.create`.

**The trap that defines this recipe:** `Quote.convertToSalesOrder` is **not executable**, and
neither is `accept` nor `decline`. Xentral has no reachable conversion endpoint (the logic
exists only behind the UI and a killswitched legacy XML route), and the quote statuses
`accepted`/`declined` are written by no API path. So:

- Build the `SalesOrder` yourself from the quote's `items`, and carry
  `references.customerInquiryNumber` (Quote) into `references.customerOrderNumber`
  (SalesOrder) so the two documents stay linkable.
- Do **not** branch on `status == "accepted"` — it will never be reached through the API.
  Branch on your own signal (an inbound mail, a form, a tag) and use `addTag` on the quote
  to record the outcome.

**Executable:** `release`, `cancel`, `send`, `downloadPdf`, `addTag`, `removeTag`.
**Fields you need:** `customer`, `items[]`, `dates.validUntil`, `dates.expectedOrderDate`,
`references.customerInquiryNumber`, `billingAddress.*`, `totals.*`.

### P2 — Auftrag → Lieferschein → Rechnung

**Chain.** `SalesOrder.create` → `release` → `dispatch` → `DeliveryNote` → `SalesInvoice`.

```
SalesOrder.release                    # draft → confirmed
SalesOrder.dispatch                   # hands to logistics: creates pick run + delivery note
   command: {"printPickList": true}   # optional
DeliveryNote.markDelivered            # shipped → delivered
SalesOrder.createSalesInvoice         # or DeliveryNote.createSalesInvoice
SalesInvoice.release                  # draft → open
SalesInvoice.send
```

**Traps.**
- `SalesOrder.createDeliveryNote` is **not executable** — a clean "delivery note only" call
  has no endpoint. `dispatch` (group `fulfillment`) is the way, and it is **destructive**:
  it also creates a pick run and starts shipping. It needs a released order with positions.
- To find the delivery notes of an order, read `SalesOrder.documents.deliveryNotes`, or —
  more reliably — list `DeliveryNote` filtered by `documents.salesOrder` (it *is* filterable).
  Same for invoices via `SalesInvoice.documents.salesOrder`.
- `items.fulfillment.shipped` / `.invoiced` / `.returned` per line tell you how far a
  position got; `items.availability.deliverable` says whether it can ship at all.
- `SalesOrder.addHold` / `releaseHold` are **not executable** (holds map to `trafficLights`,
  readable only). A blocking workflow must model the block outside the ERP.

### P3 — Versandarten, Sendungen und Labels

**Pick a shipping method:** `ShippingMethod` (`list` + `read`) carries `name`, `module`,
`type`, `project`, `supportsDeliveries`, `supportsReturns`, `shippingEmailBehaviour`. Write
the chosen one to `SalesOrder.shipping.method`; `shipping.status` and `shipping.cost` sit
next to it.

**Follow a parcel:** `Shipment` (read-only) has `trackingNumber`, `trackingUrl`, `labelUrl`,
`carrier`, `status`, `weight`, `packages[].trackingNumber`, `events[]` (`at`, `status`,
`location`) and `documents.deliveryNote` / `documents.salesOrder`.

**The trap:** `Shipment` has **no filterable and no searchable field whatsoever**. You cannot
query "the shipment for order X". Go the other way: read the `DeliveryNote` (filterable by
`documents.salesOrder`) and follow `DeliveryNote.shipments[]` → `{id, number, name}`, then
`read` each shipment by id.

**Labels.** Every label action on this core is **not executable** — `Shipment.createLabel`,
`cancelLabel`, `downloadLabel`, `refreshTracking`, `DeliveryNote.createShipment`,
`printLabels`, `Return.sendReturnLabel`. Xentral's carrier label API is beta and not public.
For a carrier label in a workflow use the dedicated **`shiplabel` node** (DHL, DPD, UPS,
GLS, …) — that is the one supported way. To print an existing PDF, use `Printer.printDocument`
(executable): `fileshare read` → `Printer.list` filtered by name → `printDocument` with
`fileContent`, `fileName`, `quantity`.

### P4 — Zahlungsstatus und Mahnwesen

**Read the status:** `SalesInvoice.payment.status` ∈ `unpaid | partiallyPaid | paid`, and it
**is filterable** — this is the one payment query the core supports well.

```
xentral_erp_core action="records" key="SalesInvoice" \
  filters=[{"key": "status", "op": "equals", "value": "open"},
           {"key": "payment.status", "op": "equals", "value": "unpaid"}]
```

Then compute overdue **in the workflow** — `payment.dueDate` is readable but not filterable,
so fetch the open/unpaid set and do the date comparison in an `expression` or `code` node.

**Do not rely on `payment.dueDate` being populated.** On the reference instance it is
`null` on *every* invoice, paid and unpaid alike — as are `payment.discountDate`,
`fixedAt`, and `payment.payments[]` (empty even on an invoice showing `totals.paid`). The
robust rule, which works either way:

> due date = `payment.dueDate` if set, else `dates.issued` + `payment.terms.dueDays`.

Check the same on the tenant you are building for before shipping a dunning run — one
sample record answers it.

**Fields that do carry the money:** `totals.paid`, `totals.outstanding`,
`payment.terms.dueDays`, `payment.terms.discountPercent`, `payment.terms.discountDays`,
and the dunning block `dunning.level`, `dunning.blocked`, `dunning.lastReminderAt`,
`dunning.note`.

**Payments themselves:** `Payment` is a read-only, immutable booking record —
`direction`, `kind`, `amount`, `method`, `reference`, `bookedAt`, `fees`, `unallocated`,
`allocations[].invoice`. It filters only by `kind` and `bookedAt`; there is no filter by
customer or invoice, so reconcile from the invoice side.

**The trap that shapes every dunning workflow:** `registerPayment`, `remind`, `writeOff` and
`CreditNote.registerRefund` are **not executable** — Xentral's payments and dunning APIs are
not public. A dunning automation therefore **reads** status and acts *outside* the core:
send mail (`EmailAccount.sendEmail`), log a `Correspondence` record, raise a `Task`, or route
a `human-task`. It must not pretend to book anything.

### P5 — Retoure → Gutschrift, und Storno

**Chain.**

```
DeliveryNote.createReturn        command: {"lineItems": [...]}          # from the delivery note
   — or —
Return.createFromDeliveryNote    command: {"deliveryNote": "dn_1", "lineItems": [...]}
Return.release
Return.settle
Return.createCreditNote          command: {"isApproved": true, "isPaid": false}   # both optional
```

`Return` statuses run `requested → received → checked → settled → cancelled`, but
**`receive` and `check` are not executable** (v3 `returnOrders` offers only complete/cancel).
Records reach `received`/`checked` through the UI; your workflow can observe those statuses,
it cannot set them. `restock` and `createReplacementOrder` are also not executable.
Return line items carry `reason` (from the `ReturnReason` catalogue — **required on create**),
`condition`, `action`, `receivedQuantity`, `creditedQuantity`; `resolution.creditNote` points
at the result.

**The storno rule — two different models, and mixing them is the classic bug:**

- **Status flip:** `Quote`, `SalesOrder`, `DeliveryNote`, `Return`, `PurchaseOrder` all
  cancel via the `cancel` command.
- **Counter-document:** `SalesInvoice` cancels (`cancel` is executable, giving
  `cancelled` / `partiallyCancelled`), but a real financial storno is a **credit note** —
  create a `CreditNote` with `kind: "cancellation"`. A released `CreditNote` itself
  **cannot be cancelled at all** (UI-only upstream); only a draft can be removed with
  `delete`.

### P6 — Einkauf: Bestellung → Wareneingang → Eingangsrechnung

**Executable on `PurchaseOrder`:** `release`, `close`, `cancel`, `send`, `downloadPdf`,
`addTag`, `removeTag`. Statuses `draft → sent → confirmed → received → closed | cancelled`.
Fields: `supplier`, `items[]`, `deliveryAddress.*`, `warehouse`, `dates.requestedDelivery`,
`confirmation.*` (`status`, `supplierOrderNumber`, `confirmedAt`, `via`),
`references.supplierOfferNumber`.

**Say this out loud before designing:** the rest of the purchasing chain is read-only.

- `GoodsReceipt` — `list`/`read` only. `post`, `cancel`, `proposeStorageLocations` and both
  label prints are **not executable**; the upstream entity is read-only.
- `PurchaseInvoice` — CRUD works, but **no action or step is executable**: `approve`,
  `reject`, `rematch`, `registerPayment`, `schedulePayment`, `attachFile` are all wishes.
  There is no approval workflow to build on this entity today. It also cannot be filtered by
  `status` (only `number`, `references.supplierInvoiceNumber`, `dates.invoiceDate`,
  `dates.received`, `tags`).
- `PurchaseOrder.recordConfirmation` / `requestConfirmation` / `createGoodsReceipt` /
  `createPurchaseInvoice` / `updateDeliveryDates` are **not executable**. Write
  `confirmation.*` with a normal `update` instead of looking for an action.

### P7 — Stammdaten und Preise

**Customer / Supplier** share one shape: `addresses[]` (`type` ∈ `billing|shipping|both`,
`label`, `isDefault`, `name`, `contactPerson`, `street`, `zip`, `city`, `state`, `country`,
`email`, `phone`, `gln`) and `contacts[]` (`type` ∈ `mr|mrs|company|other`, `name`,
`salutation`, `title`, `position`, `department`, `email`, `phone`, `mobile`, `language`).
Only `addTag`/`removeTag` are executable — `archive`, `reactivate`, `setHold`,
`releaseHold`, `mergeInto`, `runCreditCheck` and `statement` are all wishes. Status is
`active | archived` and **not writable through the API**.

**Which entity writes which price** — the single most common mistake:

| Price | Entity | Field |
|---|---|---|
| Standard sale price (Listenpreis) | `Product` | `prices.sale` |
| Customer-specific / group / scale (Staffel) sale price | `PriceList` | `unitPrice` + `scope`, `minQuantity`, `validFrom/Until` |
| Purchase price per supplier (EK) | `PurchasePrice` | `unitPrice` + `supplier`, `minQuantity` |

`Product.activate` / `deactivate` are executable; `archive` is not (v2 rejects `isDeleted`).
`adjustStock` is **not executable** — book stock through `StorageLocation` (P8).

### P7b — Labels/Tags setzen und entfernen

Two different things are called "Label" in German. This recipe is about **tags**
(Schlagwörter on a record); for a **Paketmarke / carrier label** see P3 — those are all
wishes and go through the `shiplabel` node.

Tags are the workhorse of any automation that needs state the ERP cannot store: a quote
that was accepted (P1), a customer on hold (P7), an invoice already chased. `addTag` and
`removeTag` are executable on nine entities — `Quote`, `SalesOrder`, `SalesInvoice`,
`DeliveryNote`, `CreditNote`, `Return`, `PurchaseOrder`, `Customer`, `Supplier`.

```
xentral_erp_core action="run" key="SalesOrder" handle="so_123" op="addTag" \
                 command={"title": "express"}
xentral_erp_core action="run" key="SalesOrder" handle="so_123" op="removeTag" \
                 command={"title": "express"}
```

Both take exactly `{"title": "<tag>"}` (required). **A tag that does not exist yet is
created automatically** — you do not pre-create it, and there is no create path on the
`Tag` entity anyway (it is `list`/`read` only, exposing `label`, `slug`, `color`, `group`).

Three things worth knowing:

- **Address by title, not by id.** `removeTag` also takes the title, so a workflow never
  has to resolve a `tag_…` id.
- **Do NOT plan on finding records by tag.** `tags` is *declared* filterable on the
  documents, and the filter is accepted (200) — but measured on a live order carrying the
  tag, it returns **zero rows**, by title and by tag id alike. Only `Product` really
  filters by tag (the facade emulates it by scanning pages). So a tag is a reliable
  *marker on a record you already have*, not a reliable *index to find records by*. If a
  workflow must re-find what it marked, keep the ids itself, or filter on something that
  demonstrably works (`status`, `customer`, `documents.salesOrder`) and check the tag per
  record.
- **The `tags` field is writable on create and update too.** `addTag`/`removeTag` change
  one entry; writing the `tags` field on an `update` replaces the set. Prefer the actions
  in an automation — a blind `update` silently drops tags someone else added.

In a workflow this is a `business-entity` node with `params.addTag.path.uuid` (the record
id) and `params.addTag.body = {"title": "…"}`.

### P8 — Lager

`StorageLocation` carries the core's richest executable surface. All five take
`product` + `quantity` and accept **`dryRun: true`**, which validates and reports what
*would* be booked without booking it — use it in every workflow you are still testing.

| Action | German | Required command | Note |
|---|---|---|---|
| `putaway` | Einlagern | `product`, `quantity` | irreversible; correct by counter-booking |
| `stockRemoval` | Auslagern | `product`, `quantity` | destructive |
| `stockTransfer` | Umlagern | `product`, `quantity`, `target` | not atomic upstream; a partial failure is compensated and reported |
| `inventoryCount` | Inventur zählen | `product`, `quantity` | |
| `stockAdjustment` | Korrektur | `product`, `quantity` | |

All five also accept `batch` and `reason`. `StorageLocation.block`/`release`/`printLabel`
are **not executable**.

Read stock from `StockLevel` (filter by `product`, `warehouse`, `storageLocation`):
`quantity`, `reserved`, `available`, each `{value, unit}`. Treat `available` as the
upstream's own figure — it is **not** simply `quantity − reserved`, so do not recompute it.

`StockMovement` is the low-level primitive: **create-only**, append-only, `type` ∈
`receipt | issue | transfer | correction`. Prefer the named `StorageLocation` actions —
same orchestration, but each with its own validated command schema.

`StockTake` (Inventur) and `PickingRun` are **read-only**: every one of their steps
(`startCounting`, `submit`, `post`, `cancel`; `release`, `start`, `pause`, `complete`) and
actions is a wish.

### P9 — Triggering a workflow from the ERP

`trigger-erp-event` fires only after the subscription is **activated** — saving the graph
subscribes nothing (`xentral_workflows action='erp_event' erp_event_op='activate'`).
Enumerate the real ids with `xentral_erp_core action="events"`; they are per-installation
and per-version (~162 on a current Xentral), dot-segmented like
`com.xentral.salesOrder.created.v1`. Never hardcode one you have not seen in that response.

Two things bite here. **One business action emits several events** — creating one sales
order fires `salesOrder.created`, `salesOrderPosition.created` and a protocol event — so
subscribe to the one you mean. And the subscription has **two scoping axes**, core *and*
connection; the connection lives in a different connector namespace than the entity
gateway's, and a mismatch falls back to the tenant default silently rather than failing.

Event data arrives wrapped: `{{ trigger.body.type }}` and `{{ trigger.body.body.<field> }}`.

---

## §5 Status vocabularies

Guessing a status value produces a filter that silently matches nothing.

| Entity | Field | Values |
|---|---|---|
| `Quote` | `status` | `draft`, `sent`, `accepted`*, `declined`*, `expired`, `cancelled` |
| `SalesOrder` | `status` | `draft`, `confirmed`, `fulfilled`, `closed`, `cancelled` |
| `SalesInvoice` | `status` | `draft`, `open`, `paid`, `partiallyCancelled`, `cancelled` |
| `SalesInvoice` | `payment.status` | `unpaid`, `partiallyPaid`, `paid` |
| `DeliveryNote` | `status` | `draft`, `picking`, `shipped`, `delivered`, `cancelled` |
| `CreditNote` | `status` | `draft`, `open`, `settled`, `cancelled` |
| `CreditNote` | `kind` | `correction`, `cancellation` |
| `CreditNote` | `settlement.mode` | `refund`, `offset` |
| `Return` | `status` | `requested`, `received`*, `checked`*, `settled`, `cancelled` |
| `PurchaseOrder` | `status` | `draft`, `sent`, `confirmed`, `received`, `closed`, `cancelled` |
| `GoodsReceipt` | `status` | `open`, `posted`, `closed`, `cancelled` |
| `PurchaseInvoice` | `status` | `received`, `matched`, `approved`, `paid`, `rejected` |
| `PurchaseInvoice` | `match.status` | `pending`, `matched`, `mismatch` |
| `Shipment` | `status` | `label`, `handedOver`, `inTransit`, `delivered`, `exception`, `returned` |
| `Payment` | `direction` / `kind` | `incoming`, `outgoing` / `incoming`, `outgoing`, `refund`, `chargeback` |
| `Customer` / `Supplier` | `status` | `active`, `archived` (not writable) |
| `Product` | `status` | `active`, `inactive`, `archived` |
| `StorageLocation` | `status` | `active`, `blocked` |
| `StockTake` | `status` | `draft`, `counting`, `review`, `posted`, `cancelled` |
| `PickingRun` | `status` | `draft`, `released`, `inProgress`, `picked`, `completed`, `cancelled` |
| `StockMovement` | `type` | `receipt`, `issue`, `transfer`, `correction` |
| `Task` | `status` / `priority` | `open`, `inProgress`, `completed` / `low`, `normal`, `high` |
| `Correspondence` | `kind` | `email`, `letter`, `fax`, `phone`, `note` |

\* declared, but **no API path writes it** — observable only if a human sets it in the UI.

---

## §6 Querying: what you can filter, and the traps

`records` takes `filters=[{key, op, value}]`; `op` defaults to `equals`. Only the keys below
are accepted — anything else is refused by the core.

| Entity | Filterable |
|---|---|
| `Quote` | `number`, `status`, `customer`, `references.customerInquiryNumber`, `dates.issued`, `dates.validUntil`, `dates.expectedOrderDate`, `dates.requestedDelivery`, `items.product`, `tags`, `createdAt`, `updatedAt` |
| `SalesOrder` | `number`, `status`, `customer`, `references.customerOrderNumber`, `references.externalId`, `references.externalNumber`, `dates.issued`, `dates.requestedDelivery`, `items.product`, `tags`, `createdAt`, `updatedAt` |
| `SalesInvoice` | `number`, `status`, `customer`, `references.customerOrderNumber`, `dates.issued`, `items.product`, **`payment.status`**, **`documents.salesOrder`**, `tags`, `createdAt`, `updatedAt` |
| `DeliveryNote` | `number`, `status`, `customer`, `references.customerOrderNumber`, `dates.issued`, `items.product`, **`documents.salesOrder`**, `tags`, `createdAt`, `updatedAt` |
| `Return` | `number`, `status`, `customer`, `references.customerOrderNumber`, **`dates.requested`** (not `dates.issued`), `items.product`, `documents.salesOrder`, `tags`, … |
| `PurchaseOrder` | `number`, `status`, `supplier`, `references.supplierOfferNumber`, `dates.issued`, `dates.requestedDelivery`, `items.product`, `tags`, … |
| `Customer` / `Supplier` | `number`, `name`, `email`, `addresses.street/zip/city/state/country`, `tags`, `createdAt`, `updatedAt` — **no `status`** |
| `Product` | `number`, `status`, `name`, `project`, `identifiers.ean`, `identifiers.manufacturerNumber`, `variant.isMatrix`, `updatedAt` |
| `StockLevel` | `product`, `warehouse`, `storageLocation` |
| `PriceList` | `product` — that is all |
| `Payment` | `kind`, `bookedAt` |
| `GoodsReceipt` | `number`, `status` |
| `PurchaseInvoice` | `number`, `references.supplierInvoiceNumber`, `dates.invoiceDate`, `dates.received`, `tags` — **no `status`** |
| `Shipment`, `ShippingMethod` | **nothing** — list everything and filter in the workflow |

**Traps.**

- **The datetime asymmetry.** Partner endpoints (`Customer`, `Supplier`) expect `Y-m-d`;
  document endpoints expect a full timestamp. Neither accepts the format it emits. The core
  bridges this per entity, so pass what the *entity* wants and never feed a value straight
  from one entity's output into another's filter without reformatting.
- **References take ids, not names.** `customer`, `supplier`, `items.product`,
  `shipping.method` all want `cus_…` / `prd_…` / `ship_…`. Resolve through the lookup entity
  first.
- **Filterable ≠ sortable ≠ searchable.** Documents sort only by `number`, `dates.issued`
  (`dates.requested` on `Return`), `createdAt`, `updatedAt`. `Product`, `PriceList`,
  `StockLevel`, `Shipment` have no sort keys at all.
- **`search` is a consolidated key** defaulting to `op: "contains"`, but the keys it accepts
  are narrower than the entity's declared `searchFields` — on `SalesInvoice` the live
  contract accepts `number` only. Take the list from `describe`'s `query.searchable`, not
  from the field tree.
- **An undeclared filter key is refused (422), not ignored** — deliberately, because some
  upstream list endpoints answer 200 with the *unfiltered* collection, which reads like a
  filtered result. The refusal names every accepted key, so a 422 here is the cheapest way
  to get the exact contract.
- **`detailOnly` fields are null in a list.** Read one record with `get` before concluding a
  field is empty.
- **`list` is live on every call** — once per planning pass, never inside a loop.

---

## §7 What this core cannot do

Roughly 80 of the ~110 declared actions and steps are **wishes**: the model declares them,
the connected Xentral has no public endpoint, and calling one returns a refusal with the
reason. Design around them; do not discover them at runtime.

| Entity | Not executable | Do this instead |
|---|---|---|
| `Quote` | `convertToSalesOrder`, `duplicate`, `accept`, `decline` | build the `SalesOrder` from the quote's items; record the outcome with a tag |
| `SalesOrder` | `createDeliveryNote`, `createPickingRun`, `addHold`, `releaseHold`, `allocateStock`, `duplicate` | `dispatch` for logistics; model holds outside the ERP; allocation is automatic |
| `SalesInvoice` | `registerPayment`, `remind`, `writeOff`, `downloadEInvoice` | read `payment.status`; act by mail / task / correspondence |
| `DeliveryNote` | `createShipment`, `printLabels`, `startPicking` | `shiplabel` node for carrier labels; `Printer.printDocument` for a PDF |
| `CreditNote` | `registerRefund`, `offsetAgainstInvoice`, `cancel` | `delete` a draft; a released credit note is UI-only |
| `Return` | `sendReturnLabel`, `createReplacementOrder`, `restock`, `receive`, `check` | observe those statuses; create a replacement order directly |
| `PurchaseOrder` | `recordConfirmation`, `requestConfirmation`, `createGoodsReceipt`, `createPurchaseInvoice`, `updateDeliveryDates` | write `confirmation.*` with a normal `update` |
| `GoodsReceipt` | `post`, `cancel`, `proposeStorageLocations`, both label prints | read-only upstream; book stock via `StorageLocation` |
| `PurchaseInvoice` | `approve`, `reject`, `rematch`, `registerPayment`, `schedulePayment`, `attachFile` | no approval flow is possible on this entity today |
| `Customer` / `Supplier` | `archive`, `reactivate`, `setHold`, `releaseHold`, `mergeInto`, `runCreditCheck`, `statement` | tags for state you control |
| `Product` | `adjustStock`, `duplicate`, `recalculatePurchasePrice`, `syncToChannel`, `mergeInto`, `archive` | `StorageLocation` actions for stock; `deactivate` instead of archive |
| `PriceList` | `duplicate`, `bulkAdjust`, `activate`, `deactivate` | create a new entry with its own `validFrom` |
| `Shipment` | `createLabel`, `cancelLabel`, `downloadLabel`, `refreshTracking` | `shiplabel` node |
| `Payment` | `allocate`, `unallocate`, `refund` | the payments API is missing upstream entirely |
| `StorageLocation` | `block`, `release`, `printLabel` | — |
| `StockTake` | `startCounting`, `submit`, `post`, `cancel`, `exportCountingList`, `recount`, `addPosition` | read-only |
| `PickingRun` | `release`, `start`, `pause`, `complete`, `cancel`, `assign`, `reprioritize`, `printPickList` | read-only; `dispatch` creates the run |
| `Channel` | `syncOrders`, `syncStock`, `syncProducts`, `testConnection`, `pause`, `resume` | read-only |
| `Batch`, `SerialNumber` | everything, including reading | not queryable at all |

If a needed capability is on this list, say so and stop rather than routing around the core
with a raw API call — the gap is reported automatically so the core can gain the capability,
which is the only fix that also helps the next tenant.

---

## §8 Going deeper

- `xentral_erp_core action="describe" key="<Entity>"` — the full field tree with per-field
  `access`, `creatable`, `updatable`, `options`, plus the exact `command` schema of every
  action and step. The authority whenever this playbook is unclear.
- `action="list"` — the tenant's full live catalogue, including native entities this
  playbook does not cover.
- `action="events"` — the real event ids for `trigger-erp-event`.
- `action="api_search"` / `api_endpoint` — the raw Xentral OpenAPI, for judging whether a
  wish has become reachable upstream.
- `action="bulk_template"` / `bulk_validate` / `bulk_run` — mass import.

---

## §9 Claims (machine-checked)

`tests/test_core_playbooks.py` (agent-cores CI) verifies every entry below against the
core's own metadata: each entity exists, each `executable` entry is a real action or step
command **and carries no wish**, each `wishes` entry still *is* a wish, `executable ∪
wishes` covers **every** capability of the entities named, each status value matches the
field's `options`, and each field path resolves. Update this block whenever the prose above
changes — the build fails otherwise.

It doubles as the compact index: if you only need "what can I call on X", read this block.

```yaml
executable:
  Quote: [send, downloadPdf, addTag, removeTag, release, cancel]
  SalesOrder: [createSalesInvoice, split, splitOrder, downloadPdf, sendConfirmation, addTag, removeTag, release, close, cancel, dispatch]
  SalesInvoice: [send, downloadPdf, addTag, removeTag, release, cancel]
  DeliveryNote: [createReturn, createSalesInvoice, downloadPdf, addTag, removeTag, release, markDelivered, cancel]
  CreditNote: [send, downloadPdf, addTag, removeTag, release]
  Return: [createFromDeliveryNote, createCreditNote, downloadPdf, addTag, removeTag, release, settle, cancel]
  PurchaseOrder: [send, downloadPdf, addTag, removeTag, release, close, cancel]
  Customer: [addTag, removeTag]
  Supplier: [addTag, removeTag]
  Product: [activate, deactivate]
  StorageLocation: [putaway, stockRemoval, stockTransfer, inventoryCount, stockAdjustment]
  Printer: [printDocument]
  EmailAccount: [sendEmail]
wishes:
  Quote: [convertToSalesOrder, duplicate, accept, decline]
  SalesOrder: [createDeliveryNote, createPickingRun, addHold, releaseHold, allocateStock, duplicate]
  SalesInvoice: [registerPayment, remind, writeOff, downloadEInvoice]
  DeliveryNote: [createShipment, printLabels, startPicking]
  CreditNote: [registerRefund, offsetAgainstInvoice, cancel]
  Return: [sendReturnLabel, createReplacementOrder, restock, receive, check]
  PurchaseOrder: [recordConfirmation, requestConfirmation, createGoodsReceipt, createPurchaseInvoice, updateDeliveryDates]
  GoodsReceipt: [post, cancel, proposeStorageLocations, printProductLabels, printBatchLabels]
  PurchaseInvoice: [approve, reject, rematch, registerPayment, schedulePayment, attachFile]
  Customer: [archive, reactivate, setHold, releaseHold, mergeInto, runCreditCheck, statement]
  Supplier: [archive, reactivate, setHold, releaseHold, mergeInto, runCreditCheck, statement]
  Product: [adjustStock, duplicate, recalculatePurchasePrice, syncToChannel, mergeInto, archive]
  PriceList: [duplicate, bulkAdjust, activate, deactivate]
  Shipment: [createLabel, cancelLabel, downloadLabel, refreshTracking]
  Payment: [allocate, unallocate, refund]
  StorageLocation: [block, release, printLabel]
  StockTake: [startCounting, submit, post, cancel, exportCountingList, recount, addPosition]
  PickingRun: [release, start, pause, complete, cancel, assign, reprioritize, printPickList]
  Channel: [syncOrders, syncStock, syncProducts, testConnection, pause, resume]
  Batch: [traceReport, block, release]
  SerialNumber: [traceReport, block, release]
operations:
  GoodsReceipt: [list, read]
  Shipment: [list, read]
  Payment: [list, read]
  StockLevel: [list, read]
  StockTake: [list, read]
  PickingRun: [list, read]
  Tag: [list, read]
  Channel: [list, read]
  StockMovement: [create]
  Batch: []
  SerialNumber: []
  Warehouse: [list, create, update, delete]
  ShippingMethod: [list, read]
  PaymentMethod: [list]
statuses:
  Quote.status: [draft, sent, accepted, declined, expired, cancelled]
  SalesOrder.status: [draft, confirmed, fulfilled, closed, cancelled]
  SalesInvoice.status: [draft, open, paid, partiallyCancelled, cancelled]
  SalesInvoice.payment.status: [unpaid, partiallyPaid, paid]
  DeliveryNote.status: [draft, picking, shipped, delivered, cancelled]
  CreditNote.status: [draft, open, settled, cancelled]
  CreditNote.kind: [correction, cancellation]
  CreditNote.settlement.mode: [refund, offset]
  Return.status: [requested, received, checked, settled, cancelled]
  PurchaseOrder.status: [draft, sent, confirmed, received, closed, cancelled]
  GoodsReceipt.status: [open, posted, closed, cancelled]
  PurchaseInvoice.status: [received, matched, approved, paid, rejected]
  PurchaseInvoice.match.status: [pending, matched, mismatch]
  Shipment.status: [label, handedOver, inTransit, delivered, exception, returned]
  Payment.direction: [incoming, outgoing]
  Payment.kind: [incoming, outgoing, refund, chargeback]
  Customer.status: [active, archived]
  Supplier.status: [active, archived]
  Product.status: [active, inactive, archived]
  StorageLocation.status: [active, blocked]
  StockTake.status: [draft, counting, review, posted, cancelled]
  PickingRun.status: [draft, released, inProgress, picked, completed, cancelled]
  StockMovement.type: [receipt, issue, transfer, correction]
  Task.status: [open, inProgress, completed]
  Task.priority: [low, normal, high]
  Correspondence.kind: [email, letter, fax, phone, note]
  Customer.addresses.type: [billing, shipping, both]
  Customer.contacts.type: [mr, mrs, company, other]
requiredForCreate:
  Quote: [customer, items.product, items.quantity]
  SalesOrder: [customer, items.product, items.quantity]
  SalesInvoice: [customer, items.product, items.quantity]
  DeliveryNote: [customer, items.product, items.quantity]
  CreditNote: [customer, items.product, items.quantity]
  Return: [customer, items.product, items.quantity, items.reason]
  PurchaseOrder: [supplier, items.product, items.quantity]
  PurchaseInvoice: [supplier]
  Customer: [name, contacts.name]
  Supplier: [name, contacts.name]
  Product: [name]
  PriceList: [product]
  PurchasePrice: [product, supplier]
  StockMovement: [type, product]
  StorageLocation: [name, warehouse]
  Task: [title]
  Correspondence: [customer]
  CostCenter: [name]
  Warehouse: [name]
  CustomerGroup: [name]
  ProductCategory: [name]
filterable:
  # NOTE: `tags` is declared filterable on every document but measurably returns
  # nothing (P7b) — deliberately NOT claimed here, because this block is what a
  # builder trusts. Only Product's tag filter is emulated and real.
  SalesInvoice: [status, customer, payment.status, documents.salesOrder]
  DeliveryNote: [status, customer, documents.salesOrder]
  SalesOrder: [status, customer, references.customerOrderNumber]
  Return: [status, customer, dates.requested, documents.salesOrder]
  PurchaseOrder: [status, supplier]
  Product: [number, status, name, identifiers.ean]
  StockLevel: [product, warehouse, storageLocation]
  PriceList: [product]
  Payment: [kind, bookedAt]
  GoodsReceipt: [number, status]
notFilterable:
  Shipment: [status, documents.salesOrder]
  ShippingMethod: [name]
  PurchaseInvoice: [status]
  SalesInvoice: [payment.dueDate]
  Customer: [status]
fields:
  - SalesOrder.shipping.method
  - SalesOrder.shipping.status
  - SalesOrder.items.fulfillment.shipped
  - SalesOrder.items.availability.deliverable
  - SalesOrder.documents.deliveryNotes
  - SalesOrder.documents.salesInvoices
  - SalesOrder.trafficLights
  - SalesInvoice.payment.dueDate
  - SalesInvoice.payment.discountDate
  - SalesInvoice.payment.terms.dueDays
  - SalesInvoice.payment.payments
  - SalesInvoice.totals.paid
  - SalesInvoice.totals.outstanding
  - SalesInvoice.dunning.level
  - SalesInvoice.dunning.blocked
  - SalesInvoice.dunning.lastReminderAt
  - SalesInvoice.dunning.note
  - DeliveryNote.shipments
  - Shipment.trackingNumber
  - Shipment.trackingUrl
  - Shipment.labelUrl
  - Shipment.packages.trackingNumber
  - Shipment.events.location
  - Shipment.documents.deliveryNote
  - ShippingMethod.supportsDeliveries
  - ShippingMethod.supportsReturns
  - ShippingMethod.shippingEmailBehaviour
  - Payment.allocations.invoice
  - Payment.unallocated
  - Return.items.reason
  - Return.items.receivedQuantity
  - Return.items.creditedQuantity
  - Return.resolution.creditNote
  - PurchaseOrder.confirmation.supplierOrderNumber
  - Product.prices.sale
  - StockLevel.available
  - StockLevel.reserved
  - Customer.addresses.isDefault
  - Customer.contacts.salutation
commands:
  SalesOrder.addTag: [title]
  SalesOrder.removeTag: [title]
  Customer.addTag: [title]
  Customer.removeTag: [title]
  StorageLocation.putaway: [product, quantity]
  StorageLocation.stockRemoval: [product, quantity]
  StorageLocation.stockTransfer: [product, quantity, target]
  Return.createFromDeliveryNote: [deliveryNote, lineItems]
  DeliveryNote.createReturn: [lineItems]
  Quote.addTag: [title]
```
