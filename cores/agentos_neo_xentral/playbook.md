# AgentOS Neo (Xentral) — core playbook

The 80 % an e-commerce back office actually does, in this core's terms. Read once before
building; use `describe` for the full field tree of a specific entity.

This core is a **facade**: it owns no data and translates live onto the connected Xentral's
v3/v2/v1 APIs. A capability exists here only where Xentral exposes an endpoint — hence §5.
What must be executable is specified in `erp-spec.yaml` and replayed against
the core's metadata in CI. If this file and a `describe` disagree, `describe` wins.

---

## §1 How you call anything

```
records  key=SalesInvoice filters=[{"key":"status","op":"equals","value":"open"}] sort="-dates.issued"
get      key=SalesOrder handle="so_123"
create   key=SalesOrder body={...}          update  key=SalesOrder handle="so_123" body={...}
run      key=SalesOrder handle="so_123" op="release"
run      key=StorageLocation handle="loc_9" op="putaway" command={"product":"prd_1","quantity":5,"dryRun":true}
```

`op` is an `actions[].key` **or** a `processSteps[].commands[].key` — never the step *group*
name (`documentStatus`, `fulfillment` are groups; `release`, `cancel`, `dispatch` are what
you pass). In a workflow: `params.<op>.path.uuid` + `params.<op>.body`, via the
`business-entity` node — the only way into Xentral. A node outputs the record itself, not
an envelope (ADR-0002): bind `{{ node.number }}`.

**Filtering.** `op` defaults to `equals`; `contains`, `in`, `lessThan`, `greaterThan`,
`isNull` and friends exist per field. **An undeclared key is refused with 422 and the
response lists every accepted key** — the cheapest way to learn an entity's filter contract.
`describe`'s `query` block gives the same three lists (`filterable` / `sortable` /
`searchable`), which differ per entity: documents sort only by `number`, `dates.issued`,
`createdAt`, `updatedAt`, and `Shipment`, `ShippingMethod` and `PriceList` barely filter at
all.

**Search** is one consolidated key defaulting to `contains`, and it is *narrower* than the
field tree suggests — on `SalesInvoice` it accepts `number` only. Take it from
`query.searchable`, never from the fields.

**Tags** are your own memory on a record: `run op="addTag" command={"title":"express"}`,
same for `removeTag`. A tag that does not exist is created automatically; you address it by
title, never by id. `tags` is filterable on documents, customers and suppliers — mind the
draft trap in §4.

**Write protection (Schreibschutz).** `writeProtection` is a read-only, filterable boolean
on all seven documents; flip it with `run op="setWriteProtection"` /
`op="removeWriteProtection"`. A protected document answers **409 write-protected** on
update — except for two fields that still go through, the internal `note` and the `status`.
A successful note write therefore proves nothing; read the field.

---

## §2 The documents and what you call on them

```
Quote ──> SalesOrder ──┬──> DeliveryNote ──> Shipment          Supplier ──> PurchaseOrder
quo_      so_          │    dn_              ship_                          po_
                       │      └──> Return ──> CreditNote                     └──> GoodsReceipt
                       │           ret_       cn_                                 gr_ (read)
                       └──> SalesInvoice ──> Payment                              └──> PurchaseInvoice
                            si_              paym_ (read)                              pi_ (no actions)
```

| Entity | German | Executable on it (beyond `addTag`/`removeTag`, `downloadPdf`, write protection) |
|---|---|---|
| `Quote` | Angebot | `release`, `cancel`, `send` |
| `SalesOrder` | Auftrag | `release`, `close`, `cancel`, **`dispatch`**, `createSalesInvoice`, `split`, `splitOrder` |
| `SalesInvoice` | Rechnung | `release`, `cancel`, `send` |
| `DeliveryNote` | Lieferschein | `release`, `markDelivered`, `cancel`, `createReturn`, `createSalesInvoice` |
| `CreditNote` | Gutschrift | `release`, `send` — **no cancel once released** |
| `Return` | Retoure | `createFromDeliveryNote`, `createCreditNote`, **`restock`**, `release`, `settle`, `cancel` |
| `PurchaseOrder` | Bestellung | `release`, `close`, `cancel`, `send`, **`createGoodsReceipt`** |
| `GoodsReceipt` / `PurchaseInvoice` | Wareneingang / Eingangsrechnung | **nothing** — booked from the order, see §3; `PurchaseInvoice` has no action at all |
| `StorageLocation` | Lagerplatz | `putaway`, `stockRemoval`, `stockTransfer`, `inventoryCount`, `stockAdjustment` |
| `Product` | Artikel | `activate`, `deactivate` |
| `Printer` / `EmailAccount` | — | `printDocument` / `sendEmail` |

Read-only: `Shipment`, `Payment`, `StockLevel`, `StockTake`, `PickingRun`, `Tag`, `Channel`.
`StockMovement` reads the warehouse ledger and creates bookings, but never updates or deletes
one — read and write do not share a grain there (one create, two ledger rows for a transfer).
**`Batch` and `SerialNumber` have no operations at all** — Xentral exposes no such resource;
do not design around them.

Lookups you resolve references against (`list`, some `+read`): `ShippingMethod` (Versandart),
`PaymentMethod`, `PaymentTermsGroup`, `ReturnReason`, `TaxRate`, `Warehouse`, `Project`,
`ProductCategory`, `CostCenter`, `User`, `Employee`, `TextTemplate`, `Webhook`.
References take **ids** (`cus_…`, `prd_…`, `ship_…`), never names.

**Creating a document** needs `customer` (or `supplier`) + `items.product` + `items.quantity`;
`Return` also needs `items.reason`. There are **no free-text line items** — every position
needs a product, so model a service/fee article for one.

---

## §3 The everyday jobs

### Sales & customer service (Vertrieb, Kundenservice)

**Find the customer, then the order.** `Customer` filters on `number`, `name`, `email` and
`addresses.*`; query `email` first for a duplicate check. Orders are found by four
*different* numbers: `number` (Belegnummer), `references.customerOrderNumber` (the
customer's PO), `references.externalNumber` (shop/marketplace) and `references.externalId`
(the shop's technical id). All four are writable.

**Create an order from a customer number alone.** Pass `customer` + `items` and stop:
Xentral fills billing address, payment method, payment terms, shipping method, currency,
price, tax and totals from the master data. Only `shippingAddress` stays empty — set it
when the parcel goes elsewhere. You *can* override `payment.method` / `payment.terms.*` and
`shipping.method` explicitly; you cannot read those defaults off the customer beforehand
(§5).

**Order → delivery note → invoice.** `release` → `dispatch` (hands it to logistics: creates
pick run *and* delivery note, starts shipping — **destructive**, needs a released order) →
`DeliveryNote.markDelivered` → `createSalesInvoice` → `SalesInvoice.release` → `send`.
Find an order's follow-up documents by filtering `DeliveryNote` / `SalesInvoice` on
`documents.salesOrder`.

**Ship only what is available (Teillieferung).** `items[].availability.deliverable` per line
says what can go now; move those into a new partial order with
`run op="splitOrder" command={"items":[{"lineItem":"150999","quantity":3}]}` — it reduces
the original to the remainder. (`split` makes an *empty* partial; prefer `splitOrder`.)

**Why is the order stuck?** `trafficLights` is populated from creation: `stock`, `payment`,
`creditLimit`, `deliveryBlock`, `addressValidation`, `vat`, … Read them to diagnose and
route a human task — they and `holds` are read-only, and there is no way to clear a block.

### Returns, credit notes and storno (Retoure, Gutschrift)

**Return → credit note.** `DeliveryNote.createReturn command={"lineItems":[…]}` (or
`Return.createFromDeliveryNote` with `deliveryNote` + `lineItems`) → `release` → `settle` →
`createCreditNote command={"isApproved":true,"isPaid":false}`. Statuses `received`/`checked`
are UI-only — observe them, you cannot set them.

**Wieder einlagern** is `run key=Return op="restock"` — same shape as the purchase-order
receipt (§3 Purchasing), differing only in `returnItem` instead of `orderItem`. It books
the goods back and counts up `items[].receivedQuantity`; `date` required, no `dryRun`.
Measured: 3 restocked → the location goes 5 → 8.

**Storno — two different models.** `Quote`, `SalesOrder`, `DeliveryNote`, `Return`,
`PurchaseOrder` cancel via `cancel`. A **`SalesInvoice`** cancels too, but the financial
storno is a **`CreditNote` with `kind: "cancellation"`**. A released credit note cannot be
cancelled at all; only a draft can be `delete`d.

### Finance (Zahlungsstatus, Mahnwesen)

**Read the status.** Filter `SalesInvoice` on `status` + `payment.status`
(`unpaid | partiallyPaid | paid`) — the one payment query the core does well. The money is
in `totals.paid` / `totals.outstanding`; dunning in `dunning.level` / `blocked` /
`lastReminderAt`. **Do not rely on `payment.dueDate`** — measured null on every invoice;
derive it from `dates.issued` + `payment.terms.dueDays`. Booking is impossible here
(`registerPayment`, `remind`, `writeOff` are all gaps), so a dunning workflow *reads* and
then acts outside the ERP: `EmailAccount.sendEmail`, a `Task`, a `Correspondence` entry.

### Purchasing (Einkauf)

`PurchaseOrder` create → `release` → `send`; write `confirmation.*` with a normal `update`.

**Wareneingang buchen** happens on the order, not on the receipt — there is no standalone
create and no posting step, because writing the document *is* the booking:

```
run key=PurchaseOrder handle="po_185" op="createGoodsReceipt" command={
  "date": "2026-08-07",
  "items": [{"product": "prd_1", "quantity": 5, "orderItem": "150",
             "putaways": [{"quantity": 5, "warehouse": "wh_20", "storageLocation": "loc_163",
                           "batch": "L-42", "bestBefore": "2027-01-31"}]}]}
```

`date` is required (upstream rejects a receipt without one, whatever the spec says), and
there is **no `dryRun`** — unlike the `StorageLocation` actions, the first real call moves
stock, and stock movements cannot be undone, only counter-booked. `orderItem` links the
line so `items[].fulfillment.received` counts up; partial receipts are normal. `putaways`
is the only place in this core where **batch, best-before and serial numbers can be
captured** at all. Measured: 5 of 7 booked → `StockLevel` shows 5 on the location and the
order's `received` goes 0 → 5.

`PurchaseInvoice` still has no executable action, so there is no invoice approval to build
on today.

### Warehouse & shipping (Lager, Versand)

**Stock bookings.** All five `StorageLocation` actions take `product` + `quantity`
(transfer also `target`) and accept **`dryRun: true`** — use it while building.
`putaway` = Einlagern,
`stockRemoval` = Auslagern (destructive), `stockTransfer` = Umlagern (not atomic upstream),
plus `inventoryCount` and `stockAdjustment`. Read stock from `StockLevel` (filter `product`,
`warehouse`, `storageLocation`): `quantity`, `reserved`, `available` — take `available` as
given, it is **not** `quantity − reserved`.

**Shipping.** Pick from `ShippingMethod` into `SalesOrder.shipping.method`. Track via
`DeliveryNote.shipments[]` → `Shipment` (`trackingNumber`, `trackingUrl`, `status`,
`events[]`) — `Shipment` has **no filter at all**, so always come from the delivery note.
Carrier labels are not in this core: use the workflow's **`shiplabel` node**; to print an
existing PDF use `Printer.printDocument`.

### Triggering a workflow from the ERP

`trigger-erp-event` fires only after activation — saving a graph subscribes nothing. Get
real ids from `action="events"`; never hardcode one. One business action emits several
events, and the subscription is scoped by core *and* connection.

---

## §4 Traps that cost hours

- **Drafts are invisible.** The v3 lists apply a hidden default status filter: without an
  explicit `status` you get `released`/`completed`/`cancelled`/`sent` — **drafts are
  excluded**. Measured: 10 rows for a customer, 4 more only with `status=draft`. A fresh
  order is a draft *and* has `number: null`, so it is missing from lists and unfindable by
  document number — keep the id from `create`. Any "my filter returns nothing" starts here.
- **Statuses are enums.** A guessed value matches silently nothing. Take them from
  `describe`'s `options`; the chains are draft→confirmed→fulfilled→closed (order),
  draft→open→paid (invoice), draft→picking→shipped→delivered (delivery note),
  requested→received→checked→settled (return).
- **`detailOnly` fields are null in a list** — `get` one record before concluding a field is
  empty.
- **Dates:** partner endpoints want `Y-m-d`, documents a timestamp, and neither accepts what
  it emits. Never pipe one entity's output straight into another's filter.
- **`list` is live on every call** — once per planning pass, never in a loop.
- **Write protection lets `note` and `status` through** (§1).

---

## §5 What this core cannot do

Roughly 80 of ~110 declared actions are **wishes**: declared by the model, no public
endpoint upstream, refused at runtime with the reason. The ones that change a design:

| Wanted | Instead |
|---|---|
| `Quote.convertToSalesOrder`, `accept`, `decline` | build the order from the quote's items; record the outcome with a tag — those statuses are written by no API path |
| `SalesOrder.createDeliveryNote`, `addHold`/`releaseHold` | `dispatch`; model blocks outside the ERP |
| `SalesInvoice.registerPayment`, `remind`, `writeOff` | read status, act by mail/task |
| every carrier-label action | the `shiplabel` node |
| `GoodsReceipt.post`, `cancel` | posting is not a transition — `PurchaseOrder.createGoodsReceipt` books it (§3). No storno |
| `PurchaseInvoice.approve`/`reject` | not possible today |
| `Customer.archive`, `setHold`, `mergeInto`, `runCreditCheck` | tags for state you control |
| `Product.adjustStock` | the `StorageLocation` actions |
| `PriceList.activate`, `bulkAdjust`, `duplicate` | create a new entry with its own `validFrom` |
| `Payment.allocate`/`refund`, `StockTake.*`, `PickingRun.*`, `Channel.*` | read-only surfaces |

Also read-only on the customer, though the order inherits them: `defaults.paymentTerms`,
`taxation`, `shippingMethod` and `creditLimit` are readable. The price list, the
partial-shipping preference and the aggregated open amount are not there at all any
more — the domain review removed them, because a field that is always null reads like
one that merely happens to be empty on this record.

Which price lives where: `Product.prices.sale` = the standard price, `PriceList` =
customer/group/scale sale prices, `PurchasePrice` = the EK per supplier.

If you need something on this list, say so and stop rather than routing around the core —
the gap is reported so the core can gain the capability.

---

## §6 Going deeper

`describe key=<Entity>` — full field tree, writability, `options`, the `query` contract and
every action's `command` schema. `list` — the tenant's live catalogue. `events` — trigger
ids. `api_search` / `api_endpoint` — the raw Xentral OpenAPI, to check whether a wish has
become reachable. `bulk_template` / `bulk_validate` / `bulk_run` — mass import.

Capability specification: [`erp-spec.yaml`](erp-spec.yaml) · file
map for reviewers: [`README.md`](README.md).
