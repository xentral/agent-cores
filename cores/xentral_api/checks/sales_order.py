"""xentral_api · Sales Order — document read/write verification checks.

Add fields to exercise by appending to CONFIG["fields"] (STR = text field,
BOOL = boolean flag) — only writable fields; read-only ones would no-op. Then
re-run: `python -m tests.tool_suite --tool doc:sales_order.fields --writes
--save-verified --fixtures tests/tool_suite/fixtures.mvp.json`.
"""

from ._builders import BOOL, STR, build_document_checks, lifecycle_check

CONFIG = {
    "slug": "sales_order",
    "entity": "SalesOrder",
    "fixture": "sales_order_id",
    # Cover the entity's FULL writable surface: the field roundtrip unions the
    # curated list below with every writable scalar (string/boolean, non
    # read-only) field derived from the schema. So the write ticks reflect what
    # is actually writable, not a hand-picked sample.
    "derive_fields": True,
    "fields": [
        ("internalComment", STR),
        ("customerOrderNumber", STR),
        ("fastLane", BOOL),
        ("autoDispatch", BOOL),
        ("manualPaymentApproval", BOOL),
        ("manualShippingCostApproval", BOOL),
        ("disableTrackingEmail", BOOL),
        ("disableCancellationEmail", BOOL),
    ],
}

# Standard set + the destructive release→cancel lifecycle (only with --destructive).
CHECKS = [*build_document_checks(CONFIG), lifecycle_check(CONFIG)]
