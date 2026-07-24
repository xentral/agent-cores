"""Xentral V3 facade · priceList — Konditionen (docs/01-model.md §6.5, ADR-012).

Honest partial: the upstream has no named price-list containers — only flat sales-
price rows (``GET /v1/salesPrices``: product × customer/customerGroup × minQuantity
→ price, with validity). This facade exposes each row as a price ENTRY; the target
model's named lists with tier arrays (prl_b2b …) and any write path are blue
wishes (docs/05 #13: salesPrice write not found).

The v1 endpoint REQUIRES both ``page[number]`` and ``page[size]`` with size 10..50
— ``_get`` below guarantees them (clamping the gateway's requested size).
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, money, prop, ref


class PriceListAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="PriceList",
        label_en="Price list",
        category="masterdata",
        rollout_batch="agentos_neo",
        adapter="agentos_neo.priceList",
        source_apis=("agentos_neo",),
        operations=("list", "read"),  # write not found upstream (docs/05 #13)
    )
    v3_path = "/api/v1/salesPrices"
    include = ""
    preview_template = "{{product.name}}"
    query_aliases = {"product": "productId"}
    v1_paging = True
    sections = {
        "general": {"label": "General"},
        "scope": {"label": "Scope"},
        "price": {"label": "Price"},
    }

    def steps(self):
        return [
            {
                "key": "documentStatus",
                "label": "Status",
                "commands": [
                    self.step_cmd(
                        "deactivate",
                        "Deactivate",
                        wish="v1 salesPrices carries no activation state write.",
                    ),
                    self.step_cmd(
                        "activate",
                        "Activate",
                        wish="v1 salesPrices carries no activation state write.",
                    ),
                ],
            }
        ]

    def actions(self):
        return [
            self.action_def(
                "duplicate",
                "Duplicate (validFrom)",
                wish="v1 salesPrices create exists — a duplicate-with-validFrom composer is not built.",
            ),
            self.action_def(
                "bulkAdjust",
                "Bulk adjust (percent)",
                wish="A percentage bulk adjustment has no endpoint and is not composed.",
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "currency": prop("string", "Currency", **RO, section="general"),
            "entries": prop(
                "collection",
                "Entries",
                **RO,
                section="general",
                node={
                    "properties": {
                        "product": prop("reference", "Product", **RO, reference="Product"),
                        "unitPrice": prop("string", "Unit price", **RO),
                        "minQuantity": prop("number", "Min quantity", **RO),
                    }
                },
            ),
            "product": prop(
                "reference",
                "Product",
                reference="Product",
                renderProperty="name",
                section="general",
                filterable=True,
                previewable=True,
            ),
            "scope": prop(
                "embedded",
                "Scope",
                **RO,
                section="scope",
                properties={
                    "customer": prop(
                        "reference", "Customer", reference="Customer", renderProperty="name", **RO
                    ),
                    "customerGroup": prop("string", "Customer group", **RO),
                },
            ),
            "minQuantity": prop("decimal", "Min quantity", **RO, section="price", previewable=True),
            "unitPrice": prop(
                "embedded",
                "Unit price",
                **RO,
                section="price",
                properties={
                    "amount": prop("string", "Amount", **RO),
                    "currency": prop("string", "Currency", **RO),
                    "amountGross": prop("string", "Gross amount", **RO),
                    "taxRate": prop("string", "Tax rate", **RO),
                },
            ),
            "validFrom": prop("date", "Valid from", **RO, section="price"),
            "validUntil": prop("date", "Valid until", **RO, section="price"),
            "remark": prop("string", "Remark", **RO, section="general"),
            "createdAt": prop("datetime", "Created at", **RO),
            "updatedAt": prop("datetime", "Updated at", **RO),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        p = r.get("product") or {}
        cust = r.get("customer")
        price = r.get("price") or {}
        tax = price.get("taxRate") or {}
        m = money(price.get("amount"), price.get("currency") or "EUR") or {}
        mg = money(price.get("amountGross"), price.get("currency") or "EUR") or {}
        return {
            "object": "priceListEntry",
            "currency": r.get("currency"),
            "entries": None,
            "id": (f"ple_{r.get('id')}" if r.get("id") is not None else None),
            "product": ref(
                "prd_", p.get("id") if isinstance(p, dict) else p, None, None, "products"
            ),
            "scope": {
                "customer": ref(
                    "cus_",
                    cust.get("id") if isinstance(cust, dict) else cust,
                    None,
                    None,
                    "customers",
                ),
                "customerGroup": r.get("customerGroup"),
            },
            "minQuantity": r.get("amount"),
            "unitPrice": {
                "amount": m.get("amount"),
                "currency": m.get("currency"),
                "amountGross": mg.get("amount"),
                "taxRate": tax.get("type"),
            },
            "validFrom": r.get("validFrom"),
            "validUntil": r.get("expiresAt"),
            "remark": r.get("remark") or None,
            "createdAt": None,
            "updatedAt": None,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        # No write path found upstream (docs/05 #13) — everything is a blue wish.
        return {}, {k for k in model if k not in {"object", "id", "createdAt", "updatedAt"}}
