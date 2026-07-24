"""xentral_api · Sales Credit Note — document read/write verification checks.

No fixture id — the check discovers the first credit note on the tenant.
"""

from ._builders import STR, build_document_checks

CONFIG = {
    "slug": "sales_credit_note",
    "entity": "SalesCreditNote",
    "fixture": None,
    "fields": [
        ("internalComment", STR),
        ("customerOrderNumber", STR),
        ("deviatingDebtorAccountNumber", STR),
    ],
}

CHECKS = build_document_checks(CONFIG)
