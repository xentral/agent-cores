"""OpenAPI → Entity declarations for the native weclapp mirror.

Pure, tenant-independent mapping: turns a weclapp Swagger/OpenAPI spec into the
``Entity`` declarations the shared engine consumes. No network, no state — unit
tested against ``openapi.sample.json``.

Everything is taken from authoritative signals in weclapp's own spec, so nothing
is guessed:

- **entities** = definitions that have a top-level ``GET /{name}`` list path
  (this excludes item/nested schemas like ``salesOrderItem`` and ``recordAddress``)
- **dates**: ``format: timestamp`` → epoch-ms ``datetime``
- **money/decimals**: ``format: number`` (weclapp serialises these as strings)
- **references**: ``x-relatedEntityName`` gives the exact target entity — this is
  how weclapp resolves the polymorphic party (``customerId`` → ``party``)
- **collections**: array-of-``$ref`` (e.g. ``orderItems`` → ``salesOrderItem``),
  writable on entities that expose write verbs (weclapp accepts nested items on
  the same POST/PUT; PUT replaces the whole list — see ``Collection``)
- **tag lists**: array-of-string (e.g. ``tags``) → a writable ``tag`` field
- **embeds**: a ``$ref`` to a non-listed schema (e.g. ``deliveryAddress`` →
  ``recordAddress``); read-only

Operations come from the paths' verbs (``POST /{name}`` → create, …).
"""

from __future__ import annotations

import re
from typing import Any

# The declaration types + engine live in the curated core; reused, not duplicated.
from xentral_entity_cores.agentos_neo_weclapp.emulated.base import (
    Collection,
    Embed,
    Entity,
    Field,
    Reference,
)

# openapi scalar type -> our render type (format is handled first, see _ptype).
_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "decimal",
    "boolean": "boolean",
}

_LIST_PATH = re.compile(r"^/([a-zA-Z][a-zA-Z0-9]*)$")
_QUERYABLE = frozenset({"string", "integer", "decimal", "boolean", "date", "datetime", "select"})


def _schemas(spec: dict[str, Any]) -> dict[str, Any]:
    """Component schemas, tolerating OpenAPI 3 (components.schemas) and Swagger 2
    (definitions)."""
    return (spec.get("components") or {}).get("schemas") or spec.get("definitions") or {}


def _resolve(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Resolve a ``$ref`` (one hop) to its schema, else return the node as-is."""
    ref = node.get("$ref")
    if not ref:
        return node
    return _schemas(spec).get(ref.rsplit("/", 1)[-1]) or {}


def _properties(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """A schema's properties, flattening ``allOf`` inheritance (weclapp uses it for
    the shared ``abstractEntity`` base)."""
    props: dict[str, Any] = dict(schema.get("properties") or {})
    for member in schema.get("allOf") or []:
        props.update(_properties(spec, _resolve(spec, member)))
    return props


def _entity_names(spec: dict[str, Any]) -> set[str]:
    """Definitions reachable via a top-level ``GET /{name}`` list path — the real
    entity set (nested item/address schemas have no such path)."""
    out: set[str] = set()
    for path, ops in (spec.get("paths") or {}).items():
        m = _LIST_PATH.match(path)
        if m and any(k.lower() == "get" for k in ops):
            out.add(m.group(1))
    return out


# System-managed fields weclapp assigns/computes — never writable. The spec has no
# ``readOnly`` markers, so this is a heuristic: everything else is writable and
# weclapp stays the final authority (it ignores/rejects what it won't accept, and
# updates are read-modify-write so the full object round-trips safely).
_READONLY_FIELDS = frozenset({"id", "version", "createdDate", "lastModifiedDate"})


def _writable(name: str) -> bool:
    return name not in _READONLY_FIELDS and not name.endswith("InCompanyCurrency")


def _operations(spec: dict[str, Any], name: str) -> tuple[str, ...]:
    """list/read plus the writes weclapp exposes for the entity (from the path
    verbs): ``POST /{name}`` → create, ``PUT /{name}/id/{id}`` → update,
    ``DELETE /{name}/id/{id}`` → delete."""
    paths = spec.get("paths") or {}
    coll = {k.lower() for k in paths.get(f"/{name}", {})}
    item = {k.lower() for k in paths.get(f"/{name}/id/{{id}}", {})}
    ops = ["list", "read"]
    if "post" in coll:
        ops.append("create")
    if "put" in item:
        ops.append("update")
    if "delete" in item:
        ops.append("delete")
    return tuple(ops)


def _ptype(prop: dict[str, Any]) -> tuple[str, bool]:
    """(render type, is_epoch) for a scalar property."""
    fmt = prop.get("format")
    if fmt == "timestamp":
        return "datetime", True  # weclapp epoch-ms
    if prop.get("enum"):
        return "select", False
    if fmt == "number":
        return "decimal", False  # weclapp sends decimals as type:string, format:number
    return _TYPE_MAP.get(prop.get("type"), "string"), False


def _scalar_field(name: str, prop: dict[str, Any], *, section: str, nested: bool) -> Field:
    ptype, epoch = _ptype(prop)
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
        writable=_writable(name),
    )


def _ref_target(name: str, prop: dict[str, Any]) -> str:
    """The target entity of a reference. weclapp states it explicitly via
    ``x-relatedEntityName`` (authoritative, handles the polymorphic party); the
    ``<name>Id`` fallback strips the suffix."""
    return prop.get("x-relatedEntityName") or (name[:-2] if name.endswith("Id") else name)


def _is_reference(name: str, prop: dict[str, Any]) -> bool:
    if prop.get("x-relatedEntityName"):
        return True
    return name.endswith("Id") and prop.get("type") == "string"


def _sub_fields(spec: dict[str, Any], schema: dict[str, Any]) -> tuple[Any, ...]:
    """One level of a nested schema's fields (scalars + references)."""
    out: list[Any] = []
    for name, raw in _properties(spec, schema).items():
        if name == "id":
            continue  # the engine adds the item id itself
        prop = _resolve(spec, raw)
        if _is_reference(name, prop):
            out.append(
                Reference(
                    name,
                    label=name,
                    reference=_ref_target(name, prop),
                    section="items",
                    writable=_writable(name),
                )
            )
        elif prop.get("type") in _TYPE_MAP or prop.get("enum"):
            out.append(_scalar_field(name, prop, section="items", nested=True))
    return tuple(out)


def _label_field(key: str, props: dict[str, Any]) -> str:
    for cand in ("name", "company", f"{key}Number", "orderNumber", "number", "id"):
        if cand in props:
            return cand
    return "id"


def build_entity(spec: dict[str, Any], key: str, schema: dict[str, Any]) -> Entity:
    props = _properties(spec, schema)
    scalars: list[Field] = []
    references: list[Reference] = []
    embeds: list[Embed] = []
    collections: list[Collection] = []
    additional: list[str] = []
    for name, raw in props.items():
        if name == "id":
            continue
        prop = _resolve(spec, raw)
        otype = prop.get("type")
        if otype == "array":
            item = _resolve(spec, prop.get("items") or {})
            if _properties(spec, item):
                collections.append(
                    Collection(
                        name,
                        label=name,
                        fields=_sub_fields(spec, item),
                        section="items",
                        writable=_writable(name),
                    )
                )
                additional.append(name)
            elif item.get("type") == "string" and not item.get("enum"):
                # array of bare strings (weclapp's ``tags``) — a writable tag field;
                # the list round-trips verbatim through write_payload/transform.
                scalars.append(
                    Field(
                        name,
                        label=name,
                        type="tag",
                        section="general",
                        writable=_writable(name),
                    )
                )
        elif _is_reference(name, prop):
            references.append(
                Reference(
                    name,
                    label=name,
                    reference=_ref_target(name, prop),
                    section="references",
                    writable=_writable(name),
                )
            )
        elif "$ref" in raw or otype == "object":
            sub = _resolve(spec, raw)
            if _properties(spec, sub):
                embeds.append(
                    Embed(name, label=name, fields=_sub_fields(spec, sub), section="general")
                )
        elif otype in _TYPE_MAP or prop.get("enum"):
            scalars.append(_scalar_field(name, prop, section="general", nested=False))

    return Entity(
        key=key,
        label_en=key,
        category="weclapp",
        endpoint=key,  # the definition name IS the weclapp resource path
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
        operations=_operations(spec, key),  # list/read + create/update/delete per paths
        additional_properties=tuple(additional),
    )


def build_entities_from_openapi(spec: dict[str, Any]) -> tuple[Entity, ...]:
    """Every listable weclapp entity, as an Entity. Empty spec → empty."""
    schemas = _schemas(spec)
    names = _entity_names(spec) & set(schemas)
    return tuple(build_entity(spec, name, schemas[name]) for name in sorted(names))
