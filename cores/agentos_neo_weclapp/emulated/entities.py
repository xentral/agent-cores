"""weclapp entity roster (curated, static). Phase 1 ships one entity end to end.

The one-at-a-time rule from ``docs/guides/building-an-erp-core.md`` applies: each
weclapp entity is modelled and verified before the next. Phase 1 = **Customer**
over the polymorphic ``/party`` endpoint. Next: Article, SalesOrder, SalesInvoice,
Shipment.

weclapp property names below are provisional and MUST be reconciled against the
tenant's live OpenAPI/Swagger before the entity is "done" (see
``../docs/00-concept.md``). The engine's translation logic is what the unit tests
pin down; live verification against a weclapp tenant is a separate, required step.
"""

from __future__ import annotations

from .base import Collection, Entity, Field, Reference, WeclappAdapterBase

# --- Customer -----------------------------------------------------------------
# weclapp has no ``/customer`` route: customers, suppliers, leads and contacts are
# all the polymorphic ``/party`` object, discriminated by role flags. ``Customer``
# is ``/party`` sliced to ``customer=true`` (see ``base_params``). ``partyType``
# distinguishes an organisation from a person.

_ADDRESS_FIELDS: tuple[Field, ...] = (
    Field("company", "Company", section="addresses"),
    Field("firstName", "First name", section="addresses"),
    Field("lastName", "Last name", section="addresses"),
    Field("street1", "Street", section="addresses"),
    Field("street2", "Street 2", section="addresses"),
    Field("zipcode", "ZIP", section="addresses"),
    Field("city", "City", section="addresses"),
    Field("state", "State", section="addresses"),
    Field("countryCode", "Country", section="addresses"),
    Field("primeAddress", "Primary", type="boolean", section="addresses"),
)

CUSTOMER = Entity(
    key="Customer",
    label_en="Customer",
    category="crm",
    endpoint="party",
    label_field="company",
    sections=(
        ("general", "General"),
        ("contact", "Contact"),
        ("addresses", "Addresses"),
        ("classification", "Classification"),
        ("system", "System"),
    ),
    scalars=(
        Field(
            "customerNumber",
            "Customer no.",
            section="general",
            filterable=True,
            sortable=True,
            preview=0,
        ),
        Field("company", "Company", section="general", filterable=True, sortable=True, preview=1),
        Field("firstName", "First name", section="general", filterable=True),
        Field("lastName", "Last name", section="general", filterable=True),
        Field(
            "partyType",
            "Type",
            type="select",
            section="general",
            filterable=True,
            options=(("ORGANIZATION", "Organisation"), ("PERSON", "Person")),
        ),
        Field("email", "Email", section="contact", filterable=True, preview=2),
        Field("phone", "Phone", section="contact"),
        Field("website", "Website", section="contact"),
        Field("vatRegistrationNumber", "VAT no.", section="classification", filterable=True),
        Field("customer", "Is customer", type="boolean", section="classification", filterable=True),
        Field("supplier", "Is supplier", type="boolean", section="classification", filterable=True),
        Field(
            "createdDate", "Created", type="datetime", section="system", sortable=True, epoch=True
        ),
        Field(
            "lastModifiedDate",
            "Updated",
            type="datetime",
            section="system",
            sortable=True,
            epoch=True,
        ),
    ),
    references=(Reference("currencyId", "Currency", reference="Currency", section="general"),),
    collections=(
        Collection("addresses", "Addresses", fields=_ADDRESS_FIELDS, section="addresses"),
    ),
    operations=("list", "read"),
    # Slice the polymorphic /party endpoint to customers only.
    base_params=(("customer-eq", "true"),),
    # weclapp returns the address list only when explicitly requested.
    additional_properties=("addresses",),
)


_ENTITIES: tuple[Entity, ...] = (CUSTOMER,)


def build_adapters() -> tuple[WeclappAdapterBase, ...]:
    """The weclapp entity roster. Static (weclapp has no live schema discovery),
    so it never touches the tenant and never raises — an unconfigured connection
    simply yields empty data on the request path, not a broken catalogue."""
    return tuple(WeclappAdapterBase(e) for e in _ENTITIES)
