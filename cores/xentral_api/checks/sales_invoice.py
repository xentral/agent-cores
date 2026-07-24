"""xentral_api · Sales Invoice — document read/write verification checks."""

from ._builders import STR, build_document_checks

CONFIG = {
    "slug": "sales_invoice",
    "entity": "SalesInvoice",
    "fixture": "invoice_id",
    "fields": [
        ("internalComment", STR),
        ("customerOrderNumber", STR),
    ],
}

CHECKS = build_document_checks(CONFIG)
