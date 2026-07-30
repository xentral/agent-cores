"""createCreditNote must never settle an unreleased (draft) return.

Upstream accepts createCreditNote on a draft return: it mints a real, numbered
credit note while the return keeps `documentNumber: null` and can no longer be
released ("Only a draft ReturnOrder can be released") — the source document
strands in draft forever. `status` cannot distinguish the states (it reads
`requested` before and after a release), so the document number is the marker.

The facade releases the draft first, and refuses the credit note if that fails.
"""

from __future__ import annotations

import asyncio
import json

from xentral_entity_cores.agentos_neo_xentral.emulated.return_order import ReturnAdapter


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class _Client:
    """Records every call; serves the return record from `record`."""

    def __init__(self, record, release_status=200):
        self.record = record
        self.release_status = release_status
        self.calls: list[tuple[str, str]] = []

    async def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url))
        return _Resp(200, {"data": self.record})

    async def request(self, method, url, json=None, headers=None):  # noqa: A002
        self.calls.append((method, url))
        if url.endswith("/actions/release"):
            if self.release_status >= 400:
                return _Resp(self.release_status, {"title": "not a draft"})
            self.record = {**self.record, "documentNumber": "500074"}
            return _Resp(200, {"data": self.record})
        return _Resp(200, {"data": {"id": 123}})

    async def post(self, url, json=None, headers=None):  # noqa: A002
        self.calls.append(("POST", url))
        return _Resp(200, {"data": {"id": 123}})


def _credit_note(record, release_status=200):
    client = _Client(record, release_status=release_status)
    resp = asyncio.run(
        ReturnAdapter().action(
            action_key="createCreditNote",
            handle="ret_121",
            body=json.dumps({"ids": ["ret_121"], "command": {}}).encode(),
            base_url="https://x",
            token="t",
            client=client,
        )
    )
    return resp, client


def _paths(client):
    return [url for _, url in client.calls]


def test_draft_return_is_released_before_the_credit_note():
    resp, client = _credit_note({"id": 121, "documentNumber": None, "lineItems": []})
    assert resp.status_code == 200
    paths = _paths(client)
    release = next(i for i, u in enumerate(paths) if u.endswith("/actions/release"))
    credit = next(i for i, u in enumerate(paths) if u.endswith("/actions/createCreditNote"))
    assert release < credit  # released first, then settled


def test_released_return_is_not_released_again():
    _, client = _credit_note({"id": 122, "documentNumber": "500074", "lineItems": []})
    assert not any(u.endswith("/actions/release") for u in _paths(client))


def test_credit_note_refused_when_the_draft_cannot_be_released():
    resp, client = _credit_note(
        {"id": 121, "documentNumber": None, "lineItems": []}, release_status=409
    )
    assert resp.status_code == 409
    # the money-moving call must not have happened
    assert not any(u.endswith("/actions/createCreditNote") for u in _paths(client))


def test_empty_document_number_counts_as_draft():
    _, client = _credit_note({"id": 121, "documentNumber": "", "lineItems": []})
    assert any(u.endswith("/actions/release") for u in _paths(client))


def test_other_actions_are_not_gated():
    client = _Client({"id": 121, "documentNumber": None, "lineItems": []})
    asyncio.run(
        ReturnAdapter().action(
            action_key="cancel",
            handle="ret_121",
            body=json.dumps({"ids": ["ret_121"], "command": {}}).encode(),
            base_url="https://x",
            token="t",
            client=client,
        )
    )
    assert not any(u.endswith("/actions/release") for u in _paths(client))


def test_description_states_the_precondition():
    by_key = {a["key"]: a for a in ReturnAdapter().actions()}
    desc = by_key["createCreditNote"]["description"]
    assert "draft" in desc and "released" in desc


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
