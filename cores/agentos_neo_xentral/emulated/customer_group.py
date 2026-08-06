"""Xentral V3 facade · customerGroup — Kundengruppe, Preisgruppe, Vertreter.

Reads and writes the next-generation entity API ``/api/entity/customerGroup``:
full CRUD, live-verified on mvp 2026-08-02.

ONE UPSTREAM TABLE, THREE BUSINESS OBJECTS. The upstream `type` discriminator
carries `group`, `price_group` and `representative` — a customer group, a price
group and a sales representative are all rows here. They are not variants of one
another, so the model names the discriminator `kind` and says which is which
instead of pretending the entity is only about customers.

The conditions block is NOT universal, and this is enforced upstream, not by
convention: writing `baseDiscount`, `paymentTermDays`, `cashDiscountRate`,
`cashDiscountDays`, `freeShippingThreshold` or `isFreeShippingActive` on a
`customerGroup` row is rejected with "The <field> field is not applicable for
the group type". Price groups and representatives accept all six. Existing
`group` rows still REPORT values in those columns (a leftover `paymentTermDays`
of 14, for instance) — they are read through unchanged rather than blanked,
because inventing a null would be as wrong as implying the value means anything.

Two upstream shapes are straightened out here:

* `isFreeShippingActive` is declared and stored as a DECIMAL holding 0 or 1. It
  is a flag; the model says so and converts both ways.
* `identificationNumber` is the short code a clerk types (`VETRL`, `rabatt_10`),
  so it is named `code`.

`category` is an integer that is 0 on every record on mvp with no discoverable
meaning — deliberately not modelled rather than exposed as an unexplained number.
"""

from __future__ import annotations

from typing import Any

from entity_registry.core_sdk import EmulationManifest

from .base import FacadeAdapterBase, REQUIRED, RO, prop, ref

_CU = {"creatable": True, "updatable": True}
# The model's vocabulary → the upstream discriminator.
_KIND_UP = {
    "customerGroup": "group",
    "priceGroup": "price_group",
    "salesRepresentative": "representative",
}
_KIND_DOWN = {v: k for k, v in _KIND_UP.items()}
# Conditions upstream refuses on a plain customer group (measured field by field).
_CONDITION_PATHS = (
    "conditions.baseDiscount",
    "conditions.paymentTermDays",
    "conditions.cashDiscountRate",
    "conditions.cashDiscountDays",
    "conditions.freeShippingFrom",
    "conditions.freeShippingActive",
)


class CustomerGroupAdapter(FacadeAdapterBase):
    manifest = EmulationManifest(
        key="CustomerGroup",
        label_en="Customer group",
        category="masterdata",
        rollout_batch="agentos_neo_xentral",
        adapter="agentos_neo_xentral.customerGroup",
        source_apis=("agentos_neo_xentral",),
        operations=("list", "read", "create", "update", "delete"),
    )
    v3_path = "/api/entity/customerGroup"
    include = ""
    preview_template = "{{name}}"
    bf_sort = True
    query_aliases = {"kind": "type", "code": "identificationNumber"}
    filter_value_maps = {"kind": _KIND_UP}
    sections = {"general": {"label": "General"}, "conditions": {"label": "Conditions"}}

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
            ),
            "kind": prop(
                "select",
                "Kind",
                **_CU,
                section="general",
                filterable=True,
                previewable=True,
                options=[
                    {"value": "customerGroup", "label": "Customer group"},
                    {"value": "priceGroup", "label": "Price group"},
                    {"value": "salesRepresentative", "label": "Sales representative"},
                ],
                description=(
                    "Which of the three objects this row is. Upstream keeps all "
                    "three in one table under `type`."
                ),
            ),
            "code": prop(
                "string",
                "Code",
                **_CU,
                section="general",
                filterable=True,
                searchable=True,
                description="Short code a clerk types (upstream `identificationNumber`).",
            ),
            "isActive": prop("boolean", "Active", **_CU, section="general", filterable=True),
            "internalNote": prop("string", "Internal note", **_CU, section="general"),
            "project": prop("reference", "Project", **_CU, reference="Project", section="general"),
            "conditions": prop(
                "embedded",
                "Conditions",
                section="conditions",
                description=(
                    "Only applicable to `priceGroup` and `salesRepresentative`. "
                    "Upstream REJECTS every one of these on a `customerGroup` row "
                    '("not applicable for the group type").'
                ),
                properties={
                    "baseDiscount": prop(
                        "decimal", "Base discount %", **_CU, description="Grundrabatt in percent."
                    ),
                    "paymentTermDays": prop("integer", "Payment term (days)", **_CU),
                    "cashDiscountRate": prop(
                        "decimal", "Cash discount %", **_CU, description="Skonto in percent."
                    ),
                    "cashDiscountDays": prop("integer", "Cash discount (days)", **_CU),
                    "freeShippingFrom": prop(
                        "decimal",
                        "Free shipping from",
                        **_CU,
                        description="Order value above which shipping is free.",
                    ),
                    "freeShippingActive": prop(
                        "boolean",
                        "Free shipping active",
                        **_CU,
                        description=(
                            "A flag. Upstream declares and stores it as a decimal "
                            "holding 0 or 1; this model converts both ways."
                        ),
                    ),
                },
            ),
            "createdAt": prop("datetime", "Created at", **RO, sortable=True),
            "updatedAt": prop("datetime", "Updated at", **RO, sortable=True),
        }

    def map_read(self, r: dict[str, Any]) -> dict[str, Any]:
        prj = r.get("project")
        flag = r.get("isFreeShippingActive")
        return {
            "object": "customerGroup",
            "id": (
                f"cgr_{r['uuid']}"
                if r.get("uuid")
                else (f"cgr_{r.get('id')}" if r.get("id") is not None else None)
            ),
            "name": r.get("name"),
            "kind": _KIND_DOWN.get(r.get("type") or "", r.get("type")),
            "code": r.get("identificationNumber") or None,
            "isActive": r.get("isActive"),
            "internalNote": r.get("internalNote") or None,
            "project": ref(
                "prj_",
                prj.get("id") if isinstance(prj, dict) else prj,
                None,
                None,
                "projects",
            ),
            "conditions": {
                "baseDiscount": r.get("baseDiscount"),
                "paymentTermDays": r.get("paymentTermDays"),
                "cashDiscountRate": r.get("cashDiscountRate"),
                "cashDiscountDays": r.get("cashDiscountDays"),
                "freeShippingFrom": r.get("freeShippingThreshold"),
                # stored as "0.00"/"1.00" — a flag wearing a decimal's clothes
                "freeShippingActive": (None if flag is None else bool(float(flag or 0))),
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
        if "kind" in model:
            wire["type"] = _KIND_UP.get(model["kind"] or "", model["kind"])
        if "code" in model:
            wire["identificationNumber"] = model["code"]
        if "isActive" in model:
            wire["isActive"] = bool(model["isActive"])
        if "internalNote" in model:
            wire["internalNote"] = model["internalNote"]
        if "project" in model:
            prj = model["project"]
            pid = prj.get("id") if isinstance(prj, dict) else prj
            wire["project"] = {"id": str(pid).split("_", 1)[-1]} if pid else None

        cond = model.get("conditions") or {}
        for mine, theirs in (
            ("baseDiscount", "baseDiscount"),
            ("paymentTermDays", "paymentTermDays"),
            ("cashDiscountRate", "cashDiscountRate"),
            ("cashDiscountDays", "cashDiscountDays"),
            ("freeShippingFrom", "freeShippingThreshold"),
        ):
            if mine in cond:
                wire[theirs] = cond[mine]
        if "freeShippingActive" in cond:
            value = cond["freeShippingActive"]
            wire["isFreeShippingActive"] = None if value is None else int(bool(value))

        # A plain customer group cannot carry conditions — upstream rejects each
        # field individually with a message about "the group type". Saying so here
        # names the whole set at once instead of failing on the first one.
        if model.get("kind") == "customerGroup" and any(
            p.split(".", 1)[1] in cond for p in _CONDITION_PATHS
        ):
            rejected.update(p for p in _CONDITION_PATHS if p.split(".", 1)[1] in cond)
        return wire, rejected
