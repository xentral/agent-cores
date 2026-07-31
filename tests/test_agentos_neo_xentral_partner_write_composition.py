"""A partner write must answer with the WHOLE record, or its answer destroys data.

``contacts`` and the shipping ``addresses`` are sub-resources, not fields of the v3
partner record: ``map_read`` reports ``contacts: None`` and builds ``addresses``
from primaryAddress + the billing singleton only. The rest is filled in by
``_compose`` — and that used to run on writes ONLY when the request body itself
carried a collection.

So a tags-only write (``addTag``) or a plain field update answered 200 with
``contacts: null`` and the shipping rows missing, while the record upstream was
intact. That is not cosmetic: both collections are documented as a FULL DESIRED
SET — what the list omits gets deleted. A caller that reads the write response,
edits one address and sends it back therefore deletes every shipping address the
response failed to mention. Measured on mvp (supplier 20454): update
``{"website": …}`` → response listed only ``adr_main``; sending that list back
with one changed street deleted ``adr_s47`` upstream. 200, no error.

Every write answer now composes. Where no collection was written the record
itself is already fresh, so it composes in place instead of paying for a re-read.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from xentral_entity_cores.agentos_neo_xentral.emulated.supplier import SupplierAdapter

_SUP = "/api/v3/suppliers/20454"


class _Upstream:
    """Stateful fake Xentral: one supplier, one contact person, one shipping
    address — each in its own store, exactly like upstream."""

    def __init__(self) -> None:
        self.record: dict[str, Any] = {
            "id": 20454,
            "number": "70057",
            "primaryAddress": {
                "name": "Testlieferant Nordwind GmbH",
                "street": "Hafenstrasse 12",
                "zipCode": "20457",
                "city": "Hamburg",
                "country": "DE",
                "email": "einkauf@nordwind-test.example",
            },
            "communication": {"website": "https://nordwind-test.example", "language": "DE"},
            "financials": {},
            "tags": [{"title": "Testdaten"}],
            "createdAt": "2026-07-31T16:37:51+02:00",
            "updatedAt": "2026-07-31T16:37:51+02:00",
        }
        self.contacts: dict[str, dict[str, Any]] = {
            "73": {"id": 73, "type": "mrs", "name": "Anna Beispiel", "department": "Vertrieb"}
        }
        self.shipping: dict[str, dict[str, Any]] = {
            "47": {
                "id": 47,
                "name": "Nordwind Lager Nord",
                "street": "Speicherweg 3",
                "zipCode": "24103",
                "city": "Kiel",
                "country": "DE",
            }
        }
        self.record_gets = 0
        self.next_id = 90

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: C901
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}

        if path == _SUP and method == "GET":
            self.record_gets += 1
            return httpx.Response(200, json={"data": self.record})
        if path == _SUP and method == "PATCH":
            for key, value in body.items():
                if key in ("primaryAddress", "communication", "financials") and isinstance(
                    value, dict
                ):
                    self.record.setdefault(key, {}).update(value)
                else:
                    self.record[key] = value
            return httpx.Response(200, json={"data": self.record})

        for sub, store in (("contactPersons", self.contacts), ("deliveryAddresses", self.shipping)):
            if path == f"{_SUP}/{sub}" and method == "GET":
                return httpx.Response(200, json={"data": list(store.values())})
            if path == f"{_SUP}/{sub}" and method == "POST":
                self.next_id += 1
                new = {**body, "id": self.next_id}
                store[str(self.next_id)] = new
                return httpx.Response(201, json={"data": new})
            if path.startswith(f"{_SUP}/{sub}/"):
                ident = path.rsplit("/", 1)[1]
                if method == "PATCH":
                    store[ident].update(body)
                    return httpx.Response(200, json={"data": store[ident]})
                if method == "DELETE":
                    store.pop(ident, None)
                    return httpx.Response(204)

        raise AssertionError(f"unexpected call: {method} {path}")


def _call(up: _Upstream, coro_factory) -> Any:
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(up.handler)) as client:
            return await coro_factory(SupplierAdapter(), client)

    return asyncio.run(go())


def _update(up: _Upstream, model: dict[str, Any]) -> dict[str, Any]:
    resp = _call(
        up,
        lambda adapter, client: adapter.request(
            method="PATCH",
            handle="sup_20454",
            query=[],
            body=json.dumps(model).encode(),
            base_url="https://unit.test",
            token="t",  # noqa: S106
            client=client,
        ),
    )
    assert resp.status_code == 200, resp.content
    return json.loads(resp.content)["data"]


def _add_tag(up: _Upstream, title: str) -> dict[str, Any]:
    resp = _call(
        up,
        lambda adapter, client: adapter.action(
            action_key="addTag",
            handle="sup_20454",
            body=json.dumps({"ids": ["sup_20454"], "command": {"title": title}}).encode(),
            base_url="https://unit.test",
            token="t",  # noqa: S106
            client=client,
        ),
    )
    assert resp.status_code == 200, resp.content
    return json.loads(resp.content)["data"]


# ---- the write answer is complete ---------------------------------------


def test_field_only_update_answers_with_contacts_and_shipping_rows():
    up = _Upstream()
    data = _update(up, {"website": "https://nordwind-test.example/neu"})
    assert [c["id"] for c in data["contacts"]] == ["con_73"]
    assert [a["id"] for a in data["addresses"]] == ["adr_main", "adr_s47"]


def test_tag_action_answers_with_contacts_and_shipping_rows():
    up = _Upstream()
    data = _add_tag(up, "Neu")
    assert data["tags"] == ["Testdaten", "Neu"]
    assert [c["id"] for c in data["contacts"]] == ["con_73"]
    assert [a["id"] for a in data["addresses"]] == ["adr_main", "adr_s47"]


def test_collection_write_still_answers_composed():
    """The path that always worked must keep working: a write that DOES carry a
    collection re-reads (the sync mutates the billing singleton on the record)."""
    up = _Upstream()
    data = _update(
        up,
        {
            "contacts": [
                {"id": "con_73", "type": "mrs", "name": "Anna Beispiel"},
                {"type": "mr", "name": "Bert Zweitkontakt"},
            ]
        },
    )
    assert [c["name"] for c in data["contacts"]] == ["Anna Beispiel", "Bert Zweitkontakt"]
    assert [a["id"] for a in data["addresses"]] == ["adr_main", "adr_s47"]


# ---- the reason it matters: round-tripping the answer -------------------


def test_write_answer_is_safe_to_send_back():
    """THE regression. Read a write response, change one address, send it back —
    the shipping row must survive. Before the fix the response omitted it and the
    full-desired-set sync deleted it upstream."""
    up = _Upstream()
    answer = _update(up, {"website": "https://nordwind-test.example/neu"})

    addresses = [dict(a) for a in answer["addresses"]]
    addresses[0]["street"] = "Hafenstrasse 14"  # the caller's one edit
    after = _update(up, {"addresses": addresses})

    assert "47" in up.shipping, "round-tripping a write answer deleted a shipping address"
    assert up.shipping["47"]["street"] == "Speicherweg 3"
    assert up.record["primaryAddress"]["street"] == "Hafenstrasse 14"
    assert [a["id"] for a in after["addresses"]] == ["adr_main", "adr_s47"]


def test_omitting_a_row_from_an_explicit_collection_write_still_deletes_it():
    """The counterpart: full-desired-set semantics are deliberate. A caller that
    really sends a shorter list must still get the delete — the fix makes the
    answer honest, it does not soften the write contract."""
    up = _Upstream()
    _update(
        up,
        {
            "addresses": [
                {"id": "adr_main", "type": "both", "isDefault": True, "street": "Hafenstrasse 12"}
            ]
        },
    )
    assert up.shipping == {}


# ---- cost ---------------------------------------------------------------


def test_collection_less_write_composes_onto_the_read_the_base_already_did():
    """Completeness costs the two sub-resource GETs, nothing more. The base write
    already re-reads the record (base.py ``_write_document``), and with no sync to
    invalidate it that payload is fresh — so compose onto it rather than read the
    record a second time, the way the collection path has to."""
    up = _Upstream()
    _update(up, {"website": "https://nordwind-test.example/neu"})
    assert up.record_gets == 1

    up = _Upstream()
    _update(up, {"contacts": []})
    assert up.record_gets == 2  # base's read + the post-sync re-read
