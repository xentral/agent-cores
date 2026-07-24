"""Xentral V3 facade · payment — Zahlung (docs/01-model.md §7.2).

Honest resource gap (docs/05 #2): upstream has only ``GET /paymentTransactions/
{id}`` (view + status per id), PSP transactions and ``matchTransactions`` — but NO
payment LIST/filter and no readable/writable allocations (payment ↔ invoice). This
adapter declares the target model (immutable payment record with allocations) so
the entity is visible; list stays grey/blue until ``GET /v3/payments`` +
``/allocations`` ship upstream.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import RO, FacadeAdapterBase, prop

_KIND_OPTIONS = [
    {"value": v, "label": v.capitalize()} for v in ("incoming", "outgoing", "refund", "chargeback")
]


class PaymentAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="Payment",
        label_en="Payment",
        category="documents",
        rollout_batch="agentos_neo",
        adapter="agentos_neo.payment",
        source_apis=("agentos_neo",),
        operations=("list", "read"),  # no list upstream (docs/05 #2)
    )
    v3_path = "/api/v3/payments"  # proposed endpoint — 404 until built
    include = ""
    preview_template = "{{number}}"
    sections = {"general": {"label": "General"}, "allocations": {"label": "Allocations"}}

    def actions(self):
        return [
            self.action_def(
                "allocate",
                "Allocate",
                wish="The payments API itself is missing upstream (the entity is blocked) — allocation comes with it.",
            ),
            self.action_def(
                "unallocate",
                "Unallocate",
                wish="The payments API itself is missing upstream — allocation comes with it.",
            ),
            self.action_def(
                "refund",
                "Refund",
                wish="The payments API itself is missing upstream — refunds come with it.",
            ),
        ]

    def fields(self) -> dict[str, dict[str, Any]]:
        return {
            "object": prop("string", "Object", **RO, section="general"),
            "id": prop("string", "ID", **RO, section="general"),
            "direction": prop(
                "select",
                "Direction",
                **RO,
                section="general",
                options=[
                    {"value": "incoming", "label": "Incoming"},
                    {"value": "outgoing", "label": "Outgoing"},
                ],
            ),
            "date": prop("date", "Date", **RO, section="general"),
            "fees": prop(
                "embedded",
                "Fees",
                **RO,
                section="general",
                properties={
                    "amount": prop("string", "Amount", **RO),
                    "currency": prop("string", "Currency", **RO),
                },
            ),
            "unallocated": prop("string", "Unallocated", **RO, section="general"),
            "number": prop("string", "Number", **RO, section="general", previewable=True),
            "kind": prop(
                "select",
                "Kind",
                **RO,
                section="general",
                options=_KIND_OPTIONS,
                filterable=True,
                previewable=True,
            ),
            "amount": prop(
                "embedded",
                "Amount",
                **RO,
                section="general",
                properties={
                    "amount": prop("string", "Amount", **RO),
                    "currency": prop("string", "Currency", **RO),
                },
            ),
            "method": prop(
                "reference",
                "Payment method",
                reference="PaymentMethod",
                renderProperty="name",
                **RO,
                section="general",
            ),
            "reference": prop(
                "string", "Bank/PSP reference", **RO, section="general", searchable=True
            ),
            "bookedAt": prop(
                "datetime", "Booked at", **RO, section="general", filterable=True, sortable=True
            ),
            "allocations": prop(
                "collection",
                "Allocations",
                **RO,
                section="allocations",
                node={
                    "properties": {
                        "invoice": prop(
                            "reference",
                            "Invoice",
                            reference="SalesInvoice",
                            renderProperty="number",
                            **RO,
                        ),
                        "amount": prop(
                            "embedded",
                            "Amount",
                            **RO,
                            properties={
                                "amount": prop("string", "Amount", **RO),
                                "currency": prop("string", "Currency", **RO),
                            },
                        ),
                    }
                },
            ),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        # No upstream list payload exists yet — passthrough placeholder.
        return {
            "object": "payment",
            "direction": None,
            "date": None,
            "fees": None,
            "unallocated": None,
            "id": (f"pay_{r.get('id')}" if r.get("id") is not None else None),
            **r,
        }

    def map_write(
        self, model: dict[str, Any], *, creating: bool
    ) -> tuple[dict[str, Any], set[str]]:
        return {}, {k for k in model if k != "object"}
