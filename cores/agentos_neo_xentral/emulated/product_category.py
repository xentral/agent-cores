"""Xentral V3 facade · productCategory — Warengruppe.

Reads and writes the next-generation entity API ``/api/entity/productCategory``:
full CRUD, live-verified on mvp 2026-08-02. This used to be a read-only lookup on
``/api/v1/productsCategories`` carrying four fields (id, name, parent) — enough to
name a group, not enough to say what it does.

What a Warengruppe actually carries, and why it matters:

* **the posting accounts.** Sixteen of them: revenue and expense, each split by
  the tax situation of the transaction (domestic standard/reduced/exempt/
  non-taxable, intra-community, EU OSS standard/reduced, plus export on the
  revenue side and import on the expense side). This is the bridge between a
  product and the general ledger, and it was invisible here.
* **the number range.** A group either draws product numbers from the main range
  or keeps its own, with ``nextNumber`` as the next value to hand out.
* **the hierarchy.** Upstream stores it as a bare integer ``parentId``; the model
  keeps the reference the old lookup already exposed.

The asymmetry in the accounts block is upstream's and is kept: revenue has an
``export`` account and no ``import``, expense has ``import`` and no ``export``.
Inventing the missing halves would imply postings that cannot exist.

THE SPEAKING ID CHANGED SHAPE. The old lookup emitted ``pcat_<numeric>``; the
entity API addresses records by uuid and does not allow filtering on ``id``, so
this emits ``pcat_<uuid>`` and a stored numeric handle cannot be resolved back.
Nothing in this core points here — ``Product.category`` is the merchandise group
(``mg_``), and ``parent`` below is the only inbound reference — so the change is
contained, but a caller that cached an id from before will not find its record.

All sixteen accounts, both tax texts, the number range and the hierarchy were
written, read back and deleted again on mvp — every one persisted. They are
empty on every record on that instance, which says nothing about writability and
everything about that instance never having configured them.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import FacadeAdapterBase, REQUIRED, RO, prop, ref

_CU = {"creatable": True, "updatable": True}

# model leaf → upstream field, per side of the ledger. The tax situations are the
# same on both sides except for the last entry, which upstream only has on one.
_REVENUE = (
    ("standard", "revenueAccountDomesticStandardTax"),
    ("reduced", "revenueAccountDomesticReducedTax"),
    ("taxExempt", "revenueAccountDomesticTaxExempt"),
    ("nonTaxable", "revenueAccountDomesticNonTaxable"),
    ("intraCommunity", "revenueAccountDomesticIntraCommunity"),
    ("euStandard", "revenueAccountDomesticEuStandard"),
    ("euReduced", "revenueAccountDomesticEuReduced"),
    ("export", "revenueAccountDomesticExport"),
)
_EXPENSE = (
    ("standard", "expenseAccountDomesticStandardTax"),
    ("reduced", "expenseAccountDomesticReducedTax"),
    ("taxExempt", "expenseAccountDomesticTaxExempt"),
    ("nonTaxable", "expenseAccountDomesticNonTaxable"),
    ("intraCommunity", "expenseAccountDomesticIntraCommunity"),
    ("euStandard", "expenseAccountDomesticEuStandard"),
    ("euReduced", "expenseAccountDomesticEuReduced"),
    ("import", "expenseAccountDomesticImport"),
)
_LABELS = {
    "standard": "Domestic, standard rate",
    "reduced": "Domestic, reduced rate",
    "taxExempt": "Domestic, tax exempt",
    "nonTaxable": "Domestic, not taxable",
    "intraCommunity": "Intra-community",
    "euStandard": "EU (OSS), standard rate",
    "euReduced": "EU (OSS), reduced rate",
    "export": "Export (third country)",
    "import": "Import (third country)",
}


def _accounts_node(pairs: tuple[tuple[str, str], ...], label: str) -> dict[str, Any]:
    return prop(
        "embedded",
        label,
        section="accounts",
        properties={mine: prop("string", _LABELS[mine], **_CU) for mine, _theirs in pairs},
    )


class ProductCategoryAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="ProductCategory",
        label_en="Product category",
        category="settings",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.productCategory",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/entity/productCategory"
    include = ""
    preview_template = "{{name}}"
    bf_sort = True
    sections = {
        "general": {"label": "General"},
        "numbering": {"label": "Number range"},
        "accounts": {"label": "Posting accounts"},
    }

    def _created_handle(self, resp: Any) -> Any:
        """Entity-API records are addressed by ``uuid``; ``id`` is not filterable."""
        rec = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(rec, dict):
            rec = resp if isinstance(resp, dict) else {}
        return rec.get("uuid") or rec.get("id")

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "name": prop(
                "string",
                "Name",
                **REQUIRED,
                **_CU,
                section="general",
                filterable=True,
                searchable=True,
                previewable=True,
                description="Required on create — the only field upstream insists on.",
            ),
            "parent": prop(
                "reference",
                "Parent category",
                **_CU,
                reference="ProductCategory",
                renderProperty="name",
                section="general",
                description=(
                    "Upstream stores this as a bare integer `parentId`, and does "
                    "NOT allow filtering on it — `filter[parentId]` answers 422 "
                    '"Property \'parentId\' is not filterable". Listing one '
                    "group's children therefore means paging the whole tree."
                ),
            ),
            "project": prop(
                "reference",
                "Project",
                **_CU,
                reference="Project",
                renderProperty="name",
                section="general",
                filterable=True,
            ),
            "numberRange": prop(
                "embedded",
                "Number range",
                section="numbering",
                properties={
                    "usesMainRange": prop(
                        "boolean",
                        "Uses the main product number range",
                        **_CU,
                        description=(
                            "When true the group draws from the instance-wide range "
                            "and `nextNumber` is not consulted."
                        ),
                    ),
                    "nextNumber": prop(
                        "string",
                        "Next number",
                        **_CU,
                        description="The next product number this group will hand out.",
                    ),
                },
            ),
            "accounts": prop(
                "embedded",
                "Posting accounts",
                section="accounts",
                description=(
                    "The bridge from a product to the general ledger, split by the "
                    "tax situation of the transaction. Revenue carries an `export` "
                    "account and expense an `import` one — that asymmetry is "
                    "upstream's, not an omission here."
                ),
                properties={
                    "revenue": _accounts_node(_REVENUE, "Revenue accounts"),
                    "expense": _accounts_node(_EXPENSE, "Expense accounts"),
                },
            ),
            "taxTexts": prop(
                "embedded",
                "Tax notices",
                section="accounts",
                description="The sentence printed on a document for these tax cases.",
                properties={
                    "export": prop("string", "Export", **_CU),
                    "intraCommunity": prop("string", "Intra-community", **_CU),
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        prj = r.get("project")
        return {
            "object": "productCategory",
            "id": (
                f"pcat_{r['uuid']}"
                if r.get("uuid")
                else (f"pcat_{r.get('id')}" if r.get("id") is not None else None)
            ),
            "name": r.get("name"),
            "parent": ref("pcat_", r.get("parentId"), None, None, "productCategories"),
            "project": ref(
                "prj_",
                prj.get("id") if isinstance(prj, dict) else prj,
                None,
                None,
                "projects",
            ),
            "numberRange": {
                "usesMainRange": r.get("isUsingMainProductNumberRange"),
                "nextNumber": r.get("nextNumber") or None,
            },
            "accounts": {
                "revenue": {mine: r.get(theirs) or None for mine, theirs in _REVENUE},
                "expense": {mine: r.get(theirs) or None for mine, theirs in _EXPENSE},
            },
            "taxTexts": {
                "export": r.get("taxTextExport") or None,
                "intraCommunity": r.get("taxTextIntraCommunity") or None,
            },
            "createdAt": r.get("createdAt"),
            "updatedAt": r.get("updatedAt"),
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        wire: dict[str, Any] = {}
        rejected: set[str] = set()

        if "name" in model:
            wire["name"] = model["name"]
        if "parent" in model:
            parent = model["parent"]
            pid = parent.get("id") if isinstance(parent, dict) else parent
            # upstream wants a bare integer here, not a reference object
            wire["parentId"] = int(str(pid).split("_", 1)[-1]) if pid else None
        if "project" in model:
            prj = model["project"]
            pid = prj.get("id") if isinstance(prj, dict) else prj
            wire["project"] = {"id": str(pid).split("_", 1)[-1]} if pid else None

        rng = model.get("numberRange") or {}
        if "usesMainRange" in rng:
            wire["isUsingMainProductNumberRange"] = bool(rng["usesMainRange"])
        if "nextNumber" in rng:
            wire["nextNumber"] = rng["nextNumber"]

        accounts = model.get("accounts") or {}
        for side, pairs in (("revenue", _REVENUE), ("expense", _EXPENSE)):
            block = accounts.get(side) or {}
            for mine, theirs in pairs:
                if mine in block:
                    wire[theirs] = block[mine]
            # A caller who put `import` under revenue (or `export` under expense)
            # meant something upstream cannot post. Say so rather than drop it.
            known = {m for m, _ in pairs}
            rejected.update(f"accounts.{side}.{k}" for k in block if k not in known)

        texts = model.get("taxTexts") or {}
        if "export" in texts:
            wire["taxTextExport"] = texts["export"]
        if "intraCommunity" in texts:
            wire["taxTextIntraCommunity"] = texts["intraCommunity"]

        return wire, rejected
