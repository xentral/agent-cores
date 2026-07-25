"""OpenAPI → Entity declarations for the native weclapp mirror.

Pure, tenant-independent mapping: it turns a weclapp OpenAPI/Swagger spec into the
``Entity`` declarations the shared engine consumes. No network, no state — unit
tested against ``openapi.sample.json``.

weclapp property names and shapes are taken verbatim; the only interpretation is
translating the spec's types into our render vocabulary and recognising weclapp's
conventions (epoch-ms dates, ``<name>Id`` foreign keys, ``*Items`` collections).
Tuning notes for the real spec live in ``docs/00-concept.md``.
"""

from __future__ import annotations

from typing import Any

# The declaration types + engine live in the curated core; reused, not duplicated.
# (A later refactor can extract a shared ``weclapp/engine`` module.)
from xentral_entity_cores.agentos_neo_weclapp.emulated.base import (
    Collection,
    Embed,
    Entity,
    Field,
    Reference,
)

# openapi scalar type -> our render type. ``integer`` date-like fields become
# epoch datetimes (handled in _scalar_field); ``number`` is money/decimal.
_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "decimal",
    "boolean": "boolean",
}

# ``<name>Id`` foreign keys whose target entity is not the literal name. weclapp's
# polymorphic party is referenced under many role names. Extend against the spec.
_REF_TARGET_ALIASES: dict[str, str] = {
    "customer": "party",
    "supplier": "party",
    "recipient": "party",
    "invoiceRecipient": "party",
    "contact": "party",
    "lead": "party",
}

# Schemas that only ever appear nested (line items, addresses, custom attributes),
# never as a top-level entity. Matched by suffix.
_NESTED_SUFFIXES = ("Item", "Address", "CustomAttribute", "Reference")

# Scalar types we optimistically mark filter/sortable on a native mirror (weclapp
# filters on most properties). Reconcile against 400s from the live API.
_QUERYABLE = frozenset({"string", "integer", "decimal", "boolean", "date", "datetime", "select"})


def _schemas(spec: dict[str, Any]) -> dict[str, Any]:
    """The component schema map, tolerating OpenAPI 3 (components.schemas) and
    Swagger 2 (definitions)."""
    comps = (spec.get("components") or {}).get("schemas")
    return comps or spec.get("definitions") or {}


def _resolve(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Resolve a ``$ref`` (one hop) to its schema, else return the node as-is."""
    ref = node.get("$ref")
    if not ref:
        return node
    name = ref.rsplit("/", 1)[-1]
    return _schemas(spec).get(name) or {}


def _is_epoch(name: str, otype: str) -> bool:
    return otype == "integer" and ("date" in name.lower() or name.lower().endswith("time"))


def _ref_target(name: str) -> str:
    base = name[:-2]  # strip trailing "Id"
    return _REF_TARGET_ALIASES.get(base, base[:1].upper() + base[1:])


def _scalar_field(name: str, prop: dict[str, Any], *, section: str, nested: bool) -> Field:
    otype = prop.get("type", "string")
    if _is_epoch(name, otype):
        ptype = "datetime"  # weclapp epoch-ms; date-vs-datetime is tuned live
        epoch = True
    elif prop.get("enum"):
        ptype = "select"
        epoch = False
    else:
        ptype = _TYPE_MAP.get(otype, "string")
        epoch = False
    options = tuple((v, v) for v in (prop.get("enum") or []))
    queryable = (not nested) and ptype in _QUERYABLE
    return Field(
        name,
        label=name,
        type=ptype,
        section=section,
        epoch=epoch,
        options=options,
        filterable=queryable,
        sortable=queryable,
    )


def _sub_fields(spec: dict[str, Any], schema: dict[str, Any]) -> tuple[Any, ...]:
    """One level of a nested schema's fields (scalars + id references)."""
    out: list[Any] = []
    for name, raw in (schema.get("properties") or {}).items():
        if name == "id":
            continue  # the engine adds the item id itself
        prop = _resolve(spec, raw)
        if name.endswith("Id") and prop.get("type", "string") == "string":
            out.append(Reference(name, label=name, reference=_ref_target(name), section="items"))
        elif prop.get("type") in _TYPE_MAP or prop.get("enum"):
            out.append(_scalar_field(name, prop, section="items", nested=True))
    return tuple(out)


def _label_field(key: str, props: dict[str, Any]) -> str:
    endpoint = key[:1].lower() + key[1:]
    for cand in ("name", "company", f"{endpoint}Number", "number", "id"):
        if cand in props:
            return cand
    return "id"


def build_entity(spec: dict[str, Any], key: str, schema: dict[str, Any]) -> Entity:
    props = schema.get("properties") or {}
    scalars: list[Field] = []
    references: list[Reference] = []
    embeds: list[Embed] = []
    collections: list[Collection] = []
    additional: list[str] = []
    for name, raw in props.items():
        prop = _resolve(spec, raw)
        otype = prop.get("type")
        if name == "id":
            continue
        if name.endswith("Id") and otype == "string":
            references.append(
                Reference(name, label=name, reference=_ref_target(name), section="references")
            )
        elif otype == "array":
            item = _resolve(spec, prop.get("items") or {})
            if item.get("properties"):
                collections.append(
                    Collection(name, label=name, fields=_sub_fields(spec, item), section="items")
                )
                additional.append(name)
        elif otype == "object" or "$ref" in raw:
            sub = _resolve(spec, raw)
            if sub.get("properties"):
                embeds.append(
                    Embed(name, label=name, fields=_sub_fields(spec, sub), section="general")
                )
        elif otype in _TYPE_MAP or prop.get("enum"):
            scalars.append(_scalar_field(name, prop, section="general", nested=False))

    endpoint = key[:1].lower() + key[1:]
    return Entity(
        key=key,
        label_en=key,
        category="weclapp",
        endpoint=endpoint,
        label_field=_label_field(key, props),
        sections=(
            ("general", "General"),
            ("references", "References"),
            ("items", "Items"),
        ),
        scalars=tuple(scalars),
        references=tuple(references),
        embeds=tuple(embeds),
        collections=tuple(collections),
        operations=("list", "read"),  # read-only v1
        additional_properties=tuple(additional),
    )


def _is_entity(name: str, schema: dict[str, Any]) -> bool:
    """A top-level entity has an ``id`` and is not a nested-only schema."""
    if name.endswith(_NESTED_SUFFIXES):
        return False
    return "id" in (schema.get("properties") or {})


def build_entities_from_openapi(spec: dict[str, Any]) -> tuple[Entity, ...]:
    """Every top-level schema in the spec, as an Entity. Empty spec → empty."""
    schemas = _schemas(spec)
    return tuple(
        build_entity(spec, name, schema)
        for name, schema in sorted(schemas.items())
        if _is_entity(name, schema)
    )
