"""xentral_api · Delivery Note — document read/write verification checks."""

from ._builders import STR, build_document_checks

CONFIG = {
    "slug": "delivery_note",
    "entity": "DeliveryNote",
    "fixture": "delivery_note_id",
    "fields": [
        ("internalComment", STR),
    ],
}

CHECKS = build_document_checks(CONFIG)
