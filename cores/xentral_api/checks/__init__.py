"""xentral_api verification checks — one module per entity, aggregated here.

Co-located with this core's entity definitions: ``cores/xentral_api/emulated/``
holds *what* each entity is, ``cores/xentral_api/checks/`` holds *what is proven*
about it (list/filter/sort/read + reversible writes), so the two stay 1:1 and
side by side. The shared runner harness stays in ``tests/tool_suite`` and is
imported by ``_builders``; ``python -m tests.tool_suite`` picks these up.

Each entity file (``sales_order``, ``sales_invoice``, …) declares its CONFIG and
builds its own ``CHECKS`` from the shared builders in ``_builders``. This module
just concatenates them, so adding a new entity = drop a file and add it below.
"""

from . import delivery_note, product, sales_credit_note, sales_invoice, sales_order

CHECKS = [
    *sales_order.CHECKS,
    *sales_invoice.CHECKS,
    *sales_credit_note.CHECKS,
    *delivery_note.CHECKS,
    *product.CHECKS,
]
